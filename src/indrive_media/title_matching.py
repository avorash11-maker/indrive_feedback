import re


TITLE_DEDUP_STOPWORDS = {
    "a",
    "against",
    "and",
    "announces",
    "announce",
    "in",
    "introduces",
    "launch",
    "launches",
    "legally",
    "new",
    "news",
    "of",
    "on",
    "operate",
    "operating",
    "permit",
    "permits",
    "powers",
    "rollout",
    "secures",
    "the",
    "to",
    "with",
}


def normalize_title(title: str) -> str:
    value = str(title or "").casefold()
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"\bwef\b", "world economic forum", value)
    value = re.sub(r"\s*\|\s*[^|]{2,80}$", "", value)
    value = re.sub(r"\s+-\s+[^-]{2,80}$", "", value)
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"\b(\d+)\s*\.\s*(\d+)x\b", r"\1.\2x", value)
    value = re.sub(r"[^a-z0-9#%.]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def is_title_contained_duplicate(left: str, right: str) -> bool:
    left_tokens = left.split()
    right_tokens = right.split()
    if min(len(left_tokens), len(right_tokens)) < 5:
        return False
    shorter = " ".join(left_tokens if len(left_tokens) <= len(right_tokens) else right_tokens)
    longer = " ".join(right_tokens if len(left_tokens) <= len(right_tokens) else left_tokens)
    return shorter in longer


def is_semantic_title_duplicate(left: str, right: str) -> bool:
    left_tokens = canonical_title_tokens(left)
    right_tokens = canonical_title_tokens(right)
    if "indrive" not in left_tokens or "indrive" not in right_tokens:
        return False
    if min(len(left_tokens), len(right_tokens)) < 3:
        return False

    overlap = left_tokens & right_tokens
    shorter_ratio = len(overlap) / min(len(left_tokens), len(right_tokens))
    union_ratio = len(overlap) / len(left_tokens | right_tokens)
    return (len(overlap) >= 3 and shorter_ratio >= 0.85) or (
        len(overlap) >= 4 and (shorter_ratio >= 0.6 or union_ratio >= 0.45)
    )


def canonical_title_tokens(title: str) -> set[str]:
    tokens = set()
    for token in title.split():
        token = token.strip()
        if len(token) < 3 or token in TITLE_DEDUP_STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.add(token)
    return tokens
