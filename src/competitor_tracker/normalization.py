"""Independent normalization and deduplication helpers for competitor tracker."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import RawArticle


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

DROP_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def clean_text(value: Optional[str]) -> str:
    """Collapse whitespace for provider strings without external deps."""
    return " ".join(str(value or "").split())


def normalize_source(source: Optional[str], url: Optional[str] = None) -> str:
    """Normalize source name or fall back to URL domain."""
    cleaned = clean_text(source)
    if cleaned:
        return cleaned
    return extract_domain(url)


def extract_domain(url: Optional[str]) -> str:
    """Return normalized domain name for a URL."""
    if not url:
        return ""
    return urlparse(url).netloc.casefold().replace("www.", "")


def normalize_url(url: Optional[str]) -> str:
    """Normalize URL for seen-checks and deduplication."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").casefold()
    netloc = parsed.netloc.casefold().replace("www.", "")
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in DROP_QUERY_PARAMS
    ]
    filtered_query.sort()
    return urlunparse(
        (scheme, netloc, path, "", urlencode(filtered_query, doseq=True), "")
    )


def normalize_title(title: str) -> str:
    """Normalize title text for fuzzy and semantic deduplication."""
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


def canonical_title_tokens(title: str) -> set[str]:
    """Build canonical token set for semantic dedupe."""
    tokens = set()
    for token in title.split():
        token = token.strip()
        if len(token) < 3 or token in TITLE_DEDUP_STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.add(token)
    return tokens


def is_title_contained_duplicate(left: str, right: str) -> bool:
    """Check whether one title is mostly contained inside the other."""
    left_tokens = left.split()
    right_tokens = right.split()
    if min(len(left_tokens), len(right_tokens)) < 5:
        return False
    shorter = " ".join(left_tokens if len(left_tokens) <= len(right_tokens) else right_tokens)
    longer = " ".join(right_tokens if len(left_tokens) <= len(right_tokens) else left_tokens)
    return shorter in longer


def is_semantic_title_duplicate(left: str, right: str) -> bool:
    """Heuristic semantic duplicate check without legacy imports."""
    left_tokens = canonical_title_tokens(left)
    right_tokens = canonical_title_tokens(right)
    if min(len(left_tokens), len(right_tokens)) < 3:
        return False

    overlap = left_tokens & right_tokens
    shorter_ratio = len(overlap) / min(len(left_tokens), len(right_tokens))
    union_ratio = len(overlap) / len(left_tokens | right_tokens)
    return (len(overlap) >= 3 and shorter_ratio >= 0.85) or (
        len(overlap) >= 4 and (shorter_ratio >= 0.6 or union_ratio >= 0.45)
    )


def parse_published_at(value: str) -> Optional[str]:
    """Parse provider date into stable ISO date string."""
    if not value:
        return None
    try:
        if "," in value and "GMT" in value:
            return parsedate_to_datetime(value).date().isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def normalize_raw_article(article: RawArticle) -> RawArticle:
    """Return a normalized copy of a raw article."""
    return replace(
        article,
        title=clean_text(article.title),
        url=normalize_url(article.url),
        source=normalize_source(article.source, article.url),
        snippet=clean_text(article.snippet),
        query=clean_text(article.query),
        published_at=parse_published_at(article.published_at or "") or article.published_at,
    )


def deduplicate_raw_articles(articles: Iterable[RawArticle]) -> List[RawArticle]:
    """Deduplicate raw articles by normalized URL and normalized title."""
    seen_urls = set()
    seen_titles: List[str] = []
    unique: List[RawArticle] = []

    for article in articles:
        normalized = normalize_raw_article(article)
        url_key = normalized.url
        title_key = normalize_title(normalized.title)

        if url_key and url_key in seen_urls:
            continue
        if title_key and any(
            title_key == existing
            or is_title_contained_duplicate(title_key, existing)
            or is_semantic_title_duplicate(title_key, existing)
            or SequenceMatcher(None, title_key, existing).ratio() >= 0.72
            for existing in seen_titles
        ):
            continue

        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.append(title_key)
        unique.append(normalized)

    return unique
