"""Digest generation helpers for competitor tracker outputs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from .models import AlertSchema, CandidateArticle, CompetitorDigest
from .normalization import (
    is_semantic_title_duplicate,
    is_title_contained_duplicate,
    normalize_title,
    parse_published_at,
)
from .storage import SQLiteTrackerStorage


class DigestBuilder:
    """Builds a compact digest from analyzed competitor mentions."""

    PRIORITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    DEFERRED_MAX_AGE_DAYS = 2

    def build(
        self,
        competitors: List[str],
        candidates: Iterable[CandidateArticle],
        *,
        regions: List[str] | None = None,
        digest_limit: int = 10,
        storage: Optional[SQLiteTrackerStorage] = None,
        delivery_channel: str = "daily_digest",
        delivery_destination: str = "",
        include_deferred: bool = False,
        ranking_alert_schemas_builder: Optional[
            Callable[[Sequence], Sequence[AlertSchema]]
        ] = None,
    ) -> CompetitorDigest:
        candidate_list = list(candidates)
        if storage is not None and include_deferred:
            candidate_list = self._merge_deferred_candidates(
                candidate_list,
                storage=storage,
                delivery_channel=delivery_channel,
                delivery_destination=delivery_destination,
            )
        alerts = [candidate.to_alert() for candidate in candidate_list]
        ranking_alert_schemas = (
            list(ranking_alert_schemas_builder(alerts))
            if ranking_alert_schemas_builder is not None
            else None
        )
        alerts = self._rank_alerts(alerts, alert_schemas=ranking_alert_schemas)
        if storage is not None:
            alerts = self._suppress_history(
                alerts,
                storage=storage,
                delivery_channel=delivery_channel,
                delivery_destination=delivery_destination,
            )
        alerts = alerts[: max(1, digest_limit)]
        alerts_tuple = tuple(alerts)
        highlights = tuple(alert.headline for alert in alerts[:5])
        return CompetitorDigest(
            generated_at=datetime.now(timezone.utc).isoformat(),
            competitors=tuple(competitors),
            alerts=alerts_tuple,
            highlights=highlights,
            regions=tuple(regions or ()),
        )

    def _rank_alerts(
        self,
        alerts: Sequence,
        *,
        alert_schemas: Optional[Sequence[AlertSchema]] = None,
    ) -> list:
        alert_schema_pairs = (
            list(zip(alerts, alert_schemas))
            if alert_schemas is not None and len(alert_schemas) == len(alerts)
            else [(alert, None) for alert in alerts]
        )
        ranked_pairs = sorted(
            alert_schema_pairs,
            key=lambda pair: (
                self.PRIORITY_ORDER.get(pair[0].priority.upper(), 0),
                self._freshness_sort_key(
                    pair[1] if pair[1] is not None else pair[0].candidate.published_date
                ),
                self._deferred_sort_key(pair[0]),
                pair[0].confidence,
                pair[0].score,
            ),
            reverse=True,
        )
        return [alert for alert, _ in ranked_pairs]

    def _merge_deferred_candidates(
        self,
        candidates: Sequence[CandidateArticle],
        *,
        storage: SQLiteTrackerStorage,
        delivery_channel: str,
        delivery_destination: str,
    ) -> list[CandidateArticle]:
        storage.expire_stale_deferred(
            channel=delivery_channel,
            destination=delivery_destination,
            max_age_days=self.DEFERRED_MAX_AGE_DAYS,
        )
        deferred_candidates = storage.get_deferred_candidates(
            channel=delivery_channel,
            destination=delivery_destination,
            max_age_days=self.DEFERRED_MAX_AGE_DAYS,
            limit=100,
        )
        return [*list(candidates), *deferred_candidates]

    def _suppress_history(
        self,
        alerts: Sequence,
        *,
        storage: SQLiteTrackerStorage,
        delivery_channel: str,
        delivery_destination: str,
    ) -> list:
        history = storage.get_recent_alert_history(limit=200)
        kept = []
        for alert in alerts:
            if storage.has_sent_alert(
                alert.digest_key,
                delivery_channel,
                delivery_destination,
            ):
                continue
            if self._is_similar_to_history(alert, history):
                continue
            if self._is_similar_to_kept(alert, kept):
                continue
            kept.append(alert)
        return kept

    def _is_similar_to_history(self, alert, history: Sequence[dict]) -> bool:
        title_key = normalize_title(alert.candidate.title)
        deferred_digest_key = alert.candidate.raw_article.metadata.get("deferred_digest_key")
        for item in history:
            if deferred_digest_key and item.get("digest_key") == deferred_digest_key:
                continue
            if item.get("competitor") != alert.competitor:
                continue
            if item.get("topic_group") != alert.topic_group:
                continue
            history_country = item.get("country_hint") or ""
            alert_country = alert.candidate.country_hint or ""
            history_region = item.get("region") or ""
            alert_region = alert.candidate.region or ""
            if alert_country and history_country and alert_country != history_country:
                continue
            if not alert_country and alert_region and history_region and alert_region != history_region:
                continue

            history_title = normalize_title(item.get("article_title") or item.get("headline") or "")
            if self._is_similar_title(title_key, history_title):
                return True
        return False

    def _is_similar_to_kept(self, alert, kept: Sequence) -> bool:
        title_key = normalize_title(alert.candidate.title)
        for existing in kept:
            if existing.competitor != alert.competitor:
                continue
            if existing.topic_group != alert.topic_group:
                continue
            if self._is_similar_title(title_key, normalize_title(existing.candidate.title)):
                return True
        return False

    @staticmethod
    def _is_similar_title(left: str, right: str) -> bool:
        if not left or not right:
            return False
        return (
            left == right
            or is_title_contained_duplicate(left, right)
            or is_semantic_title_duplicate(left, right)
            or SequenceMatcher(None, left, right).ratio() >= 0.72
        )

    @staticmethod
    def _freshness_sort_key(value: Mapping[str, Any] | Optional[str]) -> date:
        if isinstance(value, Mapping):
            resolved_value = value.get("resolved_publication_date")
            if isinstance(resolved_value, datetime):
                return resolved_value.date()
            if isinstance(resolved_value, date):
                return resolved_value
            return date.min
        normalized = parse_published_at(value or "")
        return date.fromisoformat(normalized) if normalized else date.min

    @staticmethod
    def _deferred_sort_key(alert) -> int:
        return 0 if alert.candidate.raw_article.metadata.get("deferred_digest_key") else 1
