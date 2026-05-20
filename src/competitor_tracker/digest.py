"""Digest generation helpers for competitor tracker outputs."""

from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence

from .models import CandidateArticle, CompetitorDigest
from .normalization import (
    is_semantic_title_duplicate,
    is_title_contained_duplicate,
    normalize_title,
)
from .storage import SQLiteTrackerStorage


class DigestBuilder:
    """Builds a compact digest from analyzed competitor mentions."""

    PRIORITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

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
    ) -> CompetitorDigest:
        candidate_list = list(candidates)
        alerts = [candidate.to_alert() for candidate in candidate_list]
        alerts = self._rank_alerts(alerts)
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

    def _rank_alerts(self, alerts: Sequence) -> list:
        return sorted(
            alerts,
            key=lambda alert: (
                self.PRIORITY_ORDER.get(alert.priority.upper(), 0),
                self._freshness_sort_key(alert.candidate.published_date),
                alert.confidence,
                alert.score,
            ),
            reverse=True,
        )

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
        for item in history:
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
    def _freshness_sort_key(value: Optional[str]) -> str:
        return value or ""
