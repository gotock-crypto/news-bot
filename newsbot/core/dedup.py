import re
from difflib import SequenceMatcher

STOP = set(
    """
    и в во на по из за от до с со а но что как это уже еще или для при
    не ни был была были быть есть мы вы они он она оно их его ее где когда
    кто почему чтобы о об же бы ли то так этот эта эти тот та те
    """
    .split()
)


def tokens(text):
    return {
        x
        for x in re.findall(
            r"[а-яa-z0-9]{3,}",
            (text or "").lower().replace("ё", "е"),
            flags=re.I,
        )
        if x not in STOP
    }


def numbers(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def normalize(text):
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.U)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a, b):
    """Combined lexical score for Russian news deduplication."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0

    ta, tb = tokens(na), tokens(nb)
    if not ta or not tb:
        return SequenceMatcher(None, na, nb).ratio()

    inter = ta & tb
    union = ta | tb
    jaccard = len(inter) / max(1, len(union))

    seq = SequenceMatcher(None, na, nb).ratio()

    shorter = min(len(ta), len(tb))
    overlap = len(inter) / max(1, shorter)

    shared_numbers = numbers(na) & numbers(nb)
    number_bonus = min(0.10, 0.025 * len(shared_numbers))

    # Strongly shared distinctive facts help catch reports from different
    # sources with different wording, while the score remains conservative.
    score = (
        0.45 * jaccard
        + 0.30 * seq
        + 0.25 * overlap
        + number_bonus
    )
    return min(1.0, score)


def find_match(text, candidates, threshold=0.72):
    best = None
    for row in candidates:
        score = similarity(text, row["text"] or "")
        if score >= threshold and (
            best is None or score > best[0]
        ):
            best = (score, row)
    return best
