import logging

log = logging.getLogger(__name__)
import threading
import time
import requests

GIGACHAT_CA_BUNDLE = "/opt/news-bot-v1/runtime/certs/gigachat-ca-bundle.pem"
import uuid


class GroqCooldown(RuntimeError):
    pass


_LOCK = threading.RLock()
_LAST_REQUEST_AT = 0.0
_GROQ_COOLDOWN_UNTIL = {}
_PROVIDER_COOLDOWN_UNTIL = {}
_PROVIDER_TRANSIENT_FAILURES = {}
_CREDENTIAL_COOLDOWN_UNTIL = {}
_GIGACHAT_TOKEN = None
_GIGACHAT_TOKEN_UNTIL = 0.0

TRANSIENT_PROVIDER_COOLDOWN = 60.0
TRANSIENT_PROVIDER_FAILURE_THRESHOLD = 3
TRANSIENT_CREDENTIAL_COOLDOWN = 30.0
AUTH_CREDENTIAL_COOLDOWN = 300.0

def groq_available(key=None):
    """True if the given Groq credential can be called without local cooldown."""
    with _LOCK:
        if not key:
            return True
        return (
            time.monotonic() >= _GROQ_COOLDOWN_UNTIL.get(key, 0.0)
            and time.monotonic() >= _CREDENTIAL_COOLDOWN_UNTIL.get(("groq", key), 0.0)
            and time.monotonic() >= _PROVIDER_COOLDOWN_UNTIL.get("groq", 0.0)
        )


def provider_available(provider):
    with _LOCK:
        return time.monotonic() >= _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0)


def credential_available(provider, key):
    with _LOCK:
        return time.monotonic() >= _CREDENTIAL_COOLDOWN_UNTIL.get(
            (provider, key), 0.0
        )


def mark_provider_cooldown(provider, seconds):
    with _LOCK:
        _PROVIDER_COOLDOWN_UNTIL[provider] = max(
            _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0),
            time.monotonic() + max(1.0, float(seconds)),
        )


def mark_provider_transient_failure(
    provider,
    threshold=TRANSIENT_PROVIDER_FAILURE_THRESHOLD,
):
    with _LOCK:
        failures = _PROVIDER_TRANSIENT_FAILURES.get(provider, 0) + 1
        _PROVIDER_TRANSIENT_FAILURES[provider] = failures

        if failures >= threshold:
            _PROVIDER_COOLDOWN_UNTIL[provider] = max(
                _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0),
                time.monotonic() + TRANSIENT_PROVIDER_COOLDOWN,
            )
            return True

        return False


def mark_credential_cooldown(provider, key, seconds):
    with _LOCK:
        _CREDENTIAL_COOLDOWN_UNTIL[(provider, key)] = max(
            _CREDENTIAL_COOLDOWN_UNTIL.get((provider, key), 0.0),
            time.monotonic() + max(1.0, float(seconds)),
        )


def clear_provider_cooldown(provider):
    with _LOCK:
        _PROVIDER_COOLDOWN_UNTIL.pop(provider, None)
        _PROVIDER_TRANSIENT_FAILURES.pop(provider, None)


def clear_credential_cooldown(provider, key):
    with _LOCK:
        _CREDENTIAL_COOLDOWN_UNTIL.pop((provider, key), None)


def _gigachat_token(key, timeout):
    global _GIGACHAT_TOKEN
    global _GIGACHAT_TOKEN_UNTIL

    now = time.monotonic()

    with _LOCK:
        if _GIGACHAT_TOKEN and now < _GIGACHAT_TOKEN_UNTIL:
            return _GIGACHAT_TOKEN



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
        verify=GIGACHAT_CA_BUNDLE,
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



    return token


def _retry_after(response, default):
    try:
        value = float(response.headers.get("Retry-After", default))
        return max(1.0, value)
    except (TypeError, ValueError):
        return float(default)


def _log_usage(provider, model, data, duration_ms):
    if provider == "gemini":
        usage = data.get("usageMetadata") or {}
        input_tokens = usage.get("promptTokenCount")
        output_tokens = usage.get("candidatesTokenCount")
        total_tokens = usage.get("totalTokenCount")
    else:
        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

    log.info(
        "LLM_USAGE provider=%s model=%s input_tokens=%s "
        "output_tokens=%s total_tokens=%s duration_ms=%d",
        provider,
        model,
        input_tokens if input_tokens is not None else "?",
        output_tokens if output_tokens is not None else "?",
        total_tokens if total_tokens is not None else "?",
        duration_ms,
    )


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

    now = time.monotonic()

    with _LOCK:
        now = time.monotonic()

        provider_until = _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0)
        if now < provider_until:
            remaining = provider_until - now
            raise RuntimeError(
                f"{provider} provider cooldown active for {remaining:.1f}s"
            )

        credential_until = _CREDENTIAL_COOLDOWN_UNTIL.get((provider, key), 0.0)
        if now < credential_until:
            remaining = credential_until - now
            raise RuntimeError(
                f"{provider} credential cooldown active for {remaining:.1f}s"
            )

        if provider == "groq":
            cooldown_until = _GROQ_COOLDOWN_UNTIL.get(key, 0.0)
            if now < cooldown_until:
                remaining = cooldown_until - now
                raise GroqCooldown(
                    f"Groq credential cooldown active for {remaining:.1f}s"
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
        verify = GIGACHAT_CA_BUNDLE
    elif provider == "gemini":
        # Gemini API key is passed as a query parameter, so never expose
        # the request URL through an exception or log message.
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        authorization = None
        verify = True
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    if provider == "gemini":
        contents = []
        system_instruction = None

        for message in messages:
            role = message.get("role")
            content = message.get("content", "")

            if role == "system":
                system_instruction = {
                    "parts": [{"text": str(content)}]
                }
            elif role == "assistant":
                contents.append({
                    "role": "model",
                    "parts": [{"text": str(content)}],
                })
            else:
                contents.append({
                    "role": "user",
                    "parts": [{"text": str(content)}],
                })

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
                "thinkingConfig": {
                    "thinkingLevel": "minimal",
                },
            },
        }

        if system_instruction:
            payload["systemInstruction"] = system_instruction

    else:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if extra:
            payload.update(extra)

    try:
        headers = {
            "Content-Type": "application/json",
        }

        if authorization:
            headers["Authorization"] = authorization

        if provider == "gemini":
            gemini_retries = 0
            gemini_retry_delays = (1.0, 2.0)

            while True:
                started_at = time.monotonic()
                try:
                    response = requests.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                        verify=verify,
                    )
                except requests.RequestException as exc:
                    raise RuntimeError(
                        f"Gemini request failed: {exc.__class__.__name__}"
                    ) from exc

                duration_ms = int((time.monotonic() - started_at) * 1000)

                if response.status_code not in (429, 503):
                    break

                if gemini_retries >= len(gemini_retry_delays):
                    raise RuntimeError(
                        f"Gemini HTTP {response.status_code}; retries exhausted"
                    )

                delay = min(
                    _retry_after(response, gemini_retry_delays[gemini_retries]),
                    5.0,
                )

                log.warning(
                    "Gemini temporary HTTP %s; retry=%s delay=%.1fs",
                    response.status_code,
                    gemini_retries + 1,
                    delay,
                )

                time.sleep(delay)
                gemini_retries += 1
        else:
            started_at = time.monotonic()
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
                verify=verify,
            )
            duration_ms = int((time.monotonic() - started_at) * 1000)
    except requests.RequestException:
        raise

    if response.status_code < 400:
        data = response.json()
        _log_usage(provider, model, data, duration_ms)

    if response.status_code == 429:
        if provider == "groq":
            cooldown = _retry_after(
                response,
                groq_cooldown,
            )

            log.warning(
                "Groq rate limit: retry_after=%s "
                "limit_requests=%s remaining_requests=%s reset_requests=%s "
                "limit_tokens=%s remaining_tokens=%s reset_tokens=%s "
                "body=%s",
                response.headers.get("Retry-After"),
                response.headers.get("x-ratelimit-limit-requests"),
                response.headers.get("x-ratelimit-remaining-requests"),
                response.headers.get("x-ratelimit-reset-requests"),
                response.headers.get("x-ratelimit-limit-tokens"),
                response.headers.get("x-ratelimit-remaining-tokens"),
                response.headers.get("x-ratelimit-reset-tokens"),
                response.text[:1000],
            )

            with _LOCK:
                _GROQ_COOLDOWN_UNTIL[key] = (
                    time.monotonic() + cooldown
                )

            mark_credential_cooldown("groq", key, cooldown)

            raise GroqCooldown(
                f"Groq HTTP 429; cooldown={cooldown:.1f}s"
            )

        if provider == "gemini":
            if response.status_code >= 500:
                mark_credential_cooldown(
                    "gemini", key, TRANSIENT_CREDENTIAL_COOLDOWN
                )
            raise RuntimeError(f"Gemini HTTP {response.status_code}")

        if response.status_code >= 500:
            mark_credential_cooldown(
                provider, key, TRANSIENT_CREDENTIAL_COOLDOWN
            )

        response.raise_for_status()

    if provider == "gemini" and response.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {response.status_code}")

    response.raise_for_status()

    if "data" not in locals():
        data = response.json()

    if provider == "gemini":
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and part.get("text")
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Invalid {provider} response: {data}"
            ) from exc
    else:
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
