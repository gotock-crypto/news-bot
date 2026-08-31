import threading
import time
import requests
import uuid


class GroqCooldown(RuntimeError):
    pass


_LOCK = threading.RLock()
_LAST_REQUEST_AT = 0.0
_GROQ_COOLDOWN_UNTIL = 0.0
_GIGACHAT_TOKEN = None
_GIGACHAT_TOKEN_UNTIL = 0.0

def groq_available():
    """True if Groq can be called without hitting our local cooldown."""
    with _LOCK:
        return time.monotonic() >= _GROQ_COOLDOWN_UNTIL


def _gigachat_token(key, timeout):
    global _GIGACHAT_TOKEN
    global _GIGACHAT_TOKEN_UNTIL

    now = time.monotonic()

    with _LOCK:
        if _GIGACHAT_TOKEN and now < _GIGACHAT_TOKEN_UNTIL:
            return _GIGACHAT_TOKEN

    print("GIGACHAT: OAuth request start", flush=True)

    response = requests.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {key}",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        timeout=(5, 10),
        verify=False,
    )

    print(
        f"GIGACHAT: OAuth response HTTP {response.status_code}",
        flush=True,
    )

    response.raise_for_status()

    data = response.json()
    token = data.get("access_token")

    if not token:
        raise RuntimeError("GigaChat OAuth returned no access_token")

    expires_in = float(data.get("expires_in", 1800))

    with _LOCK:
        _GIGACHAT_TOKEN = token
        _GIGACHAT_TOKEN_UNTIL = (
            time.monotonic() + max(60.0, expires_in - 60.0)
        )

    print("GIGACHAT: OAuth token cached", flush=True)

    return token


def _retry_after(response, default):
    try:
        value = float(response.headers.get("Retry-After", default))
        return max(1.0, value)
    except (TypeError, ValueError):
        return float(default)


def request(
    provider,
    key,
    model,
    messages,
    max_tokens,
    temperature,
    timeout,
    min_interval,
    groq_cooldown,
    extra=None,
):
    global _LAST_REQUEST_AT
    global _GROQ_COOLDOWN_UNTIL

    now = time.monotonic()

    with _LOCK:
        now = time.monotonic()

        if provider == "groq" and now < _GROQ_COOLDOWN_UNTIL:
            remaining = _GROQ_COOLDOWN_UNTIL - now
            raise GroqCooldown(
                f"Groq cooldown active for {remaining:.1f}s"
            )

        wait = float(min_interval) - (now - _LAST_REQUEST_AT)

        if wait > 0:
            time.sleep(wait)

        _LAST_REQUEST_AT = time.monotonic()

        verify = True

        if provider == "groq":
            url = "https://api.groq.com/openai/v1/chat/completions"
            authorization = f"Bearer {key}"
        elif provider == "mistral":
            url = "https://api.mistral.ai/v1/chat/completions"
            authorization = f"Bearer {key}"
        elif provider == "gigachat":
            url = "https://api.giga.chat/v1/chat/completions"
            token = _gigachat_token(key, timeout)
            authorization = f"Bearer {token}"
            verify = False
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if extra:
            payload.update(extra)

        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
                verify=verify,
            )
        except requests.RequestException:
            raise

        if response.status_code == 429:
            if provider == "groq":
                cooldown = _retry_after(
                    response,
                    groq_cooldown,
                )

                _GROQ_COOLDOWN_UNTIL = (
                    time.monotonic() + cooldown
                )

                raise GroqCooldown(
                    f"Groq HTTP 429; cooldown={cooldown:.1f}s"
                )

            response.raise_for_status()

        response.raise_for_status()

        data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Invalid {provider} response: {data}"
            ) from exc

        if not content:
            raise RuntimeError(
                f"{provider} returned empty content"
            )

        return content
