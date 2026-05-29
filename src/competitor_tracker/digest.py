"""Digest generation helpers for competitor tracker outputs."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from .config import TrackerConfig
from .geo_policy import GeoPolicy
from .models import AlertSchema, CandidateArticle, CompetitorDigest
from .normalization import (
    extract_domain,
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
    MARKETING_DIGEST_TOPIC_ALLOWLIST = {
        "market_expansion",
        "market_entry",
        "campaign_launches",
        "pricing_promo",
        "pricing",
        "strategic_operations",
        "performance_growth",
    }
    PRODUCT_FEATURES_MARKETING_TERMS = (
        "launch",
        "new feature",
        "subscription",
        "driver",
        "rider",
        "courier",
        "delivery",
        "intercity",
        "safety",
        "freight",
        "fixed price",
        "bidding model",
    )
    INDUSTRY_CONTEXT_ACTION_TERMS = (
        "launch",
        "launches",
        "launched",
        "expands",
        "expansion",
        "enters",
        "entering market",
        "market entry",
        "campaign",
        "partnership",
        "partnered",
        "partners with",
        "promo",
        "discount",
        "subscription",
        "price cut",
        "regulatory approval",
        "license obtained",
        "driver recruitment",
        "new feature",
        "rolls out",
        "rolled out",
        "pilot",
        "pilots",
    )
    MARKETING_SOURCE_DENYLIST = {
        "openpr",
        "openpr.com",
        "the points guy",
        "thepointsguy.com",
    }
    MARKETING_NOISE_PATTERNS = (
        "forecast",
        "market outlook",
        "market size",
        "market report",
        "industry analysis",
        "research report",
        "seo services",
        "guest post",
        "sponsored content",
        "points and miles",
        "credit card rewards",
    )
    CITY_TO_COUNTRY_HINTS = {
        "almaty": "Kazakhstan",
        "bandung": "Indonesia",
        "baguio": "Philippines",
        "bangkok": "Thailand",
        "bogota": "Colombia",
        "brasilia": "Brazil",
        "cebu": "Philippines",
        "chiang mai": "Thailand",
        "curitiba": "Brazil",
        "davao": "Philippines",
        "doha": "Qatar",
        "guadalajara": "Mexico",
        "iloilo": "Philippines",
        "jakarta": "Indonesia",
        "lima": "Peru",
        "manila": "Philippines",
        "makassar": "Indonesia",
        "medellin": "Colombia",
        "mexico city": "Mexico",
        "monterrey": "Mexico",
        "pattaya": "Thailand",
        "phuket": "Thailand",
        "recife": "Brazil",
        "sao paulo": "Brazil",
        "semarang": "Indonesia",
        "singapore": "Singapore",
        "solo": "Indonesia",
        "surabaya": "Indonesia",
        "tashkent": "Uzbekistan",
        "yerevan": "Armenia",
        "yogyakarta": "Indonesia",
    }
    FOREIGN_GEO_TERMS = {
        "india",
        "indian",
        "usa",
        "united states",
        "uk",
        "united kingdom",
        "europe",
        "canada",
        "australia",
        "china",
    }

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
        apply_marketing_filters: bool = False,
        marketing_config: Optional[TrackerConfig] = None,
        ranking_alert_schemas_builder: Optional[
            Callable[[Sequence], Sequence[AlertSchema]]
        ] = None,
    ) -> CompetitorDigest:
        candidate_list = list(candidates)
        if apply_marketing_filters:
            candidate_list = self._filter_marketing_digest_candidates(
                candidate_list,
                regions=regions or (),
                config=marketing_config,
            )
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

    def _filter_marketing_digest_candidates(
        self,
        candidates: Sequence[CandidateArticle],
        *,
        regions: Sequence[str],
        config: Optional[TrackerConfig],
    ) -> list[CandidateArticle]:
        return [
            candidate
            for candidate in candidates
            if self._is_marketing_digest_candidate(
                candidate,
                regions=regions,
                config=config,
            )
        ]

    def _is_marketing_digest_candidate(
        self,
        candidate: CandidateArticle,
        *,
        regions: Sequence[str],
        config: Optional[TrackerConfig],
    ) -> bool:
        topic_group = str(candidate.topic_group or "").strip()
        if not self._has_marketing_geo_confirmation(
            candidate,
            regions=regions,
            config=config,
        ):
            return False
        if not self._passes_marketing_source_quality(candidate):
            return False
        if not self._passes_secondary_source_quality(candidate):
            return False
        if topic_group in self.MARKETING_DIGEST_TOPIC_ALLOWLIST:
            return True
        if topic_group in {"product_features_innovation", "product_launch"}:
            return self._product_features_candidate_is_marketing_relevant(candidate)
        if topic_group == "industry_context":
            return self._industry_context_candidate_has_competitor_action(candidate)
        return False

    def _product_features_candidate_is_marketing_relevant(self, candidate: CandidateArticle) -> bool:
        text_blob = self._candidate_text_blob(candidate)
        return any(term in text_blob for term in self.PRODUCT_FEATURES_MARKETING_TERMS)

    def _industry_context_candidate_has_competitor_action(self, candidate: CandidateArticle) -> bool:
        text_blob = self._candidate_text_blob(candidate)
        return any(term in text_blob for term in self.INDUSTRY_CONTEXT_ACTION_TERMS)

    @staticmethod
    def _candidate_text_blob(candidate: CandidateArticle) -> str:
        return " ".join(
            part.casefold()
            for part in (
                candidate.title,
                candidate.summary,
                candidate.raw_article.title,
                candidate.raw_article.snippet,
            )
            if part
        )

    @staticmethod
    def _candidate_geo_blob(candidate: CandidateArticle) -> str:
        return " ".join(
            part.casefold()
            for part in (
                candidate.title,
                candidate.raw_article.snippet,
                candidate.raw_article.source,
            )
            if part
        )

    def _has_marketing_geo_confirmation(
        self,
        candidate: CandidateArticle,
        *,
        regions: Sequence[str],
        config: Optional[TrackerConfig],
    ) -> bool:
        if not regions or config is None:
            return True

        selected_regions = tuple(region for region in regions if region in config.regions)
        if not selected_regions:
            return True

        country_hint = str(candidate.country_hint or "").strip()
        if country_hint:
            return any(
                country_hint in config.regions[region].country_validation_terms
                for region in selected_regions
            )

        geo_blob = self._candidate_geo_blob(candidate)
        if not geo_blob:
            return False

        inferred_country = self._infer_country_from_city_terms(geo_blob)
        if inferred_country:
            if any(
                inferred_country in config.regions[region].country_validation_terms
                for region in selected_regions
            ):
                return True
            if candidate.region in selected_regions:
                return not self._contains_explicit_foreign_geo(
                    geo_blob,
                    selected_regions=selected_regions,
                    config=config,
                )
            return False

        geo_policy = GeoPolicy(config)
        has_target_confirmation = any(
            geo_policy.contains_geo_term(geo_blob, geo_term)
            for geo_term in geo_policy.target_geo_terms(selected_regions)
        )
        if has_target_confirmation:
            return True

        if candidate.region in selected_regions:
            return not self._contains_explicit_foreign_geo(
                geo_blob,
                selected_regions=selected_regions,
                config=config,
            )

        return False

    def _passes_marketing_source_quality(self, candidate: CandidateArticle) -> bool:
        domain = extract_domain(candidate.url)
        source = str(candidate.raw_article.source or "").casefold().strip()
        source_tokens = {source, domain}
        if source_tokens & self.MARKETING_SOURCE_DENYLIST:
            return False

        text_blob = self._candidate_text_blob(candidate)
        if any(pattern in text_blob for pattern in self.MARKETING_NOISE_PATTERNS):
            source_tier = str(candidate.raw_article.metadata.get("source_tier") or "").strip()
            if source_tier != "tier2_direct":
                return False

        if re.search(r"\bforecast\s+20\d{2}\b", text_blob) is not None:
            return False

        return True

    def _passes_secondary_source_quality(self, candidate: CandidateArticle) -> bool:
        source_tier = str(candidate.raw_article.metadata.get("source_tier") or "").strip()
        is_secondary_source = (
            candidate.provider == "google_news_rss" or source_tier == "tier1_aggregator"
        )
        if not is_secondary_source:
            return True

        text_blob = self._candidate_text_blob(candidate)
        has_action_signal = any(term in text_blob for term in self.INDUSTRY_CONTEXT_ACTION_TERMS)
        if not has_action_signal:
            return False

        if candidate.score < 7:
            return False

        has_explicit_geo = bool(candidate.country_hint) or bool(self._infer_country_from_city_terms(text_blob))
        if not has_explicit_geo and candidate.region is None:
            return False

        return True

    def _infer_country_from_city_terms(self, geo_blob: str) -> str:
        for city_term, country_name in self.CITY_TO_COUNTRY_HINTS.items():
            if GeoPolicy.contains_geo_term(geo_blob, city_term):
                return country_name
        return ""

    def _contains_explicit_foreign_geo(
        self,
        geo_blob: str,
        *,
        selected_regions: Sequence[str],
        config: TrackerConfig,
    ) -> bool:
        allowed_geo_terms = {
            geo_term.casefold()
            for geo_term in GeoPolicy(config).target_geo_terms(selected_regions)
        }
        for term in self.FOREIGN_GEO_TERMS:
            if term in allowed_geo_terms:
                continue
            if GeoPolicy.contains_geo_term(geo_blob, term):
                return True
        for region_key, region_config in config.regions.items():
            if region_key in selected_regions:
                continue
            for geo_term in (*region_config.geo_terms, *region_config.country_validation_terms):
                if GeoPolicy.contains_geo_term(geo_blob, geo_term):
                    return True
        return False

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
        history = storage.get_recent_alert_history(
            channel=delivery_channel,
            destination=delivery_destination,
            limit=200,
        )
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
