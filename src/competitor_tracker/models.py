"""Typed domain models for competitor tracker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple


def _normalize_tags(values: List[str] | Tuple[str, ...]) -> Tuple[str, ...]:
    seen = set()
    ordered = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class Competitor:
    """Tracked competitor entity."""

    name: str
    markets: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RawArticle:
    """Provider-level article before scoring and filtering."""

    title: str
    url: str
    provider: str
    source: str = ""
    published_at: Optional[str] = None
    snippet: str = ""
    query: str = ""
    region: Optional[str] = None
    language: Optional[str] = None
    competitor_hints: Tuple[str, ...] = ()
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArticleContext:
    """Expanded article context prepared for post-ranking LLM enrichment."""

    title: str
    snippet: str
    source_url: str
    article_body: str = ""


@dataclass(frozen=True, slots=True)
class CandidateArticle:
    """Scored article kept for downstream alerting or digesting."""

    raw_article: RawArticle
    competitor: str
    topic_group: str
    score: int
    matched_keywords: Tuple[str, ...] = ()
    summary: str = ""
    region: Optional[str] = None
    country_hint: Optional[str] = None
    language_hint: Optional[str] = None
    reasons: Tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return self.raw_article.title

    @property
    def url(self) -> str:
        return self.raw_article.url

    @property
    def provider(self) -> str:
        return self.raw_article.provider

    def to_alert(
        self,
        *,
        severity: str = "medium",
        priority: Optional[str] = None,
        confidence: Optional[float] = None,
        digest_key: Optional[str] = None,
        channels: Tuple[str, ...] = (),
    ) -> "Alert":
        """Promote a candidate into an alert object."""
        normalized_key = digest_key or f"{self.competitor.lower()}::{self.topic_group}::{self.url}"
        reason = self.summary or "; ".join(self.reasons) or self.title
        headline = f"{self.competitor}: {self.title}"
        normalized_priority = (priority or self._priority_from_score(self.score)).upper()
        normalized_confidence = confidence if confidence is not None else self._confidence_from_score(self.score)
        return Alert(
            digest_key=normalized_key,
            headline=headline,
            competitor=self.competitor,
            topic_group=self.topic_group,
            severity=severity,
            priority=normalized_priority,
            confidence=max(0.0, min(1.0, normalized_confidence)),
            score=self.score,
            reason=reason,
            candidate=self,
            delivery_channels=_normalize_tags(channels),
        )

    @property
    def published_date(self) -> Optional[str]:
        return self.raw_article.published_at

    @staticmethod
    def _priority_from_score(score: int) -> str:
        if score >= 9:
            return "HIGH"
        if score >= 6:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _confidence_from_score(score: int) -> float:
        return max(0.35, min(0.95, score / 10))


@dataclass(frozen=True, slots=True)
class Alert:
    """Digest-ready alert entity derived from a candidate article."""

    digest_key: str
    headline: str
    competitor: str
    topic_group: str
    severity: str
    priority: str
    confidence: float
    score: int
    reason: str
    candidate: CandidateArticle
    delivery_channels: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Structured summary of one tracker execution."""

    started_at: str
    finished_at: Optional[str]
    regions: Tuple[str, ...]
    providers: Tuple[str, ...]
    queries_generated: int
    raw_articles_collected: int
    candidates_kept: int
    alerts_created: int
    daily_digest_limit: int
    provider_errors: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """Future-facing delivery result for an alert notification."""

    alert_key: str
    channel: str
    status: str
    delivered_at: Optional[str] = None
    destination: str = ""
    external_id: str = ""
    error_message: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompetitorDigest:
    """Digest artifact produced after analysis."""

    generated_at: str
    competitors: Tuple[str, ...]
    alerts: Tuple[Alert, ...] = ()
    highlights: Tuple[str, ...] = ()
    regions: Tuple[str, ...] = ()
