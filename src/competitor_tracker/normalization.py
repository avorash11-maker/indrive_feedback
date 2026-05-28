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

SOURCE_TIER_SCORE = {
    "tier2_direct": 2,
    "tier1_aggregator": 1,
}

PROVIDER_QUALITY_SCORE = {
    "regional_rss": 4,
    "guardian": 3,
    "gdelt": 2,
    "google_news_rss": 1,
    "newsapi": 1,
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


def _article_quality(article: RawArticle) -> tuple[int, int, int, int]:
    tier_score = SOURCE_TIER_SCORE.get(
        str(article.metadata.get("source_tier") or "").strip(),
        0,
    )
    provider_score = PROVIDER_QUALITY_SCORE.get(article.provider, 0)
    snippet_score = 1 if clean_text(article.snippet) else 0
    published_score = 1 if parse_published_at(article.published_at or "") else 0
    return (tier_score, provider_score, snippet_score, published_score)


def deduplicate_raw_articles_with_metrics(
    articles: Iterable[RawArticle],
) -> tuple[List[RawArticle], dict[str, object]]:
    """Deduplicate raw articles and report source-tier replacement metrics."""
    seen_urls: dict[str, int] = {}
    seen_titles: List[str] = []
    unique: List[RawArticle] = []
    source_tier_wins_by_provider: dict[str, int] = {}
    source_tier_wins_by_tier: dict[str, int] = {}
    direct_source_wins_over_aggregators = 0

    for article in articles:
        normalized = normalize_raw_article(article)
        url_key = normalized.url
        title_key = normalize_title(normalized.title)
        duplicate_index = seen_urls.get(url_key, -1) if url_key else -1
        if duplicate_index < 0 and title_key:
            for index, existing in enumerate(seen_titles):
                if (
                    title_key == existing
                    or is_title_contained_duplicate(title_key, existing)
                    or is_semantic_title_duplicate(title_key, existing)
                    or SequenceMatcher(None, title_key, existing).ratio() >= 0.72
                ):
                    duplicate_index = index
                    break

        if duplicate_index < 0:
            if url_key:
                seen_urls[url_key] = len(unique)
            seen_titles.append(title_key)
            unique.append(normalized)
            continue

        existing_article = unique[duplicate_index]
        if _article_quality(normalized) <= _article_quality(existing_article):
            continue
        old_tier = str(existing_article.metadata.get("source_tier") or "").strip()
        new_tier = str(normalized.metadata.get("source_tier") or "").strip()
        source_tier_wins_by_provider[normalized.provider] = (
            source_tier_wins_by_provider.get(normalized.provider, 0) + 1
        )
        if new_tier:
            source_tier_wins_by_tier[new_tier] = source_tier_wins_by_tier.get(new_tier, 0) + 1
        if old_tier == "tier1_aggregator" and new_tier == "tier2_direct":
            direct_source_wins_over_aggregators += 1
        existing_url_key = existing_article.url
        if existing_url_key:
            seen_urls[existing_url_key] = duplicate_index
        if url_key:
            seen_urls[url_key] = duplicate_index
        seen_titles[duplicate_index] = title_key
        unique[duplicate_index] = normalized

    return unique, {
        "source_tier_wins_by_provider": source_tier_wins_by_provider,
        "source_tier_wins_by_tier": source_tier_wins_by_tier,
        "direct_source_wins_over_aggregators": direct_source_wins_over_aggregators,
    }


def deduplicate_raw_articles(articles: Iterable[RawArticle]) -> List[RawArticle]:
    """Deduplicate raw articles by normalized URL/title while preferring stronger sources."""
    unique, _ = deduplicate_raw_articles_with_metrics(articles)
    return unique
