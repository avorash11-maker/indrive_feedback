"""Configuration loading and query expansion for competitor tracker."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .product_logic import (
    VISUAL_ASSETS_POLICY,
    allowed_competitor_region_pairs,
    normalize_topic_group_name,
    presentable_region_name,
    validate_default_product_config,
)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _dedupe_keep_order(values: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class RegionConfig:
    """Region metadata used for query generation."""

    label: str
    geo_terms: Tuple[str, ...]
    country_validation_terms: Tuple[str, ...]
    language_hints: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RssFeedConfig:
    """Curated direct RSS feed used by the regional RSS provider."""

    name: str
    url: str
    language: str = ""


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Loaded configuration and source of truth for competitor tracking."""

    regions: Dict[str, RegionConfig]
    competitors_by_region: Dict[str, Tuple[str, ...]]
    topic_groups: Dict[str, Tuple[str, ...]]
    keyword_templates: Tuple[str, ...]
    daily_digest_limit: int
    enabled_providers: Tuple[str, ...]
    ignored_geo_terms: Tuple[str, ...] = ()
    topic_priority_groups: Tuple[str, ...] = ()
    competitor_aliases: Dict[str, Tuple[str, ...]] = None
    regional_rss_feeds: Dict[str, Tuple[RssFeedConfig, ...]] = None

    @classmethod
    def load_default(cls) -> "TrackerConfig":
        """Load the repository default config."""
        payload = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        validate_default_product_config(payload)
        return cls.from_dict(payload)

    @classmethod
    def load(cls, path: Path | str) -> "TrackerConfig":
        """Load config from a JSON file and validate it."""
        config_path = Path(path)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict) -> "TrackerConfig":
        """Create a validated config object from a Python dictionary."""
        cls._validate_payload(payload)

        regions = {
            region_key: RegionConfig(
                label=region_data["label"].strip(),
                geo_terms=_dedupe_keep_order(region_data["geo_terms"]),
                country_validation_terms=_dedupe_keep_order(
                    region_data.get("country_validation_terms", region_data["geo_terms"])
                ),
                language_hints=_dedupe_keep_order(region_data["language_hints"]),
            )
            for region_key, region_data in payload["regions"].items()
        }
        competitors_by_region = {
            region_key: _dedupe_keep_order(competitors)
            for region_key, competitors in payload["competitors_by_region"].items()
        }
        topic_groups = {
            topic_key: _dedupe_keep_order(keywords)
            for topic_key, keywords in payload["topic_groups"].items()
        }
        competitor_aliases = {
            competitor.strip(): _dedupe_keep_order(aliases)
            for competitor, aliases in payload.get("competitor_aliases", {}).items()
            if competitor.strip()
        }
        regional_rss_feeds = {
            region_key: tuple(
                RssFeedConfig(
                    name=str(feed["name"]).strip(),
                    url=str(feed["url"]).strip(),
                    language=str(feed.get("language") or "").strip(),
                )
                for feed in feeds
                if str(feed.get("name") or "").strip() and str(feed.get("url") or "").strip()
            )
            for region_key, feeds in payload.get("regional_rss_feeds", {}).items()
        }
        return cls(
            regions=regions,
            competitors_by_region=competitors_by_region,
            topic_groups=topic_groups,
            keyword_templates=_dedupe_keep_order(payload["keyword_templates"]),
            daily_digest_limit=payload["daily_digest_limit"],
            enabled_providers=_dedupe_keep_order(payload["enabled_providers"]),
            ignored_geo_terms=_dedupe_keep_order(payload.get("ignored_geo_terms", ())),
            topic_priority_groups=_dedupe_keep_order(
                payload.get("topic_priority_groups", ())
            ),
            competitor_aliases=competitor_aliases,
            regional_rss_feeds=regional_rss_feeds,
        )

    @staticmethod
    def _validate_payload(payload: dict) -> None:
        required_keys = {
            "regions",
            "competitors_by_region",
            "topic_groups",
            "keyword_templates",
            "daily_digest_limit",
            "enabled_providers",
        }
        missing = required_keys - set(payload)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Missing config keys: {missing_list}")

        if not isinstance(payload["regions"], dict) or not payload["regions"]:
            raise ValueError("'regions' must be a non-empty object")
        if not isinstance(payload["competitors_by_region"], dict) or not payload["competitors_by_region"]:
            raise ValueError("'competitors_by_region' must be a non-empty object")
        if not isinstance(payload["topic_groups"], dict) or not payload["topic_groups"]:
            raise ValueError("'topic_groups' must be a non-empty object")
        if not isinstance(payload["keyword_templates"], list) or not payload["keyword_templates"]:
            raise ValueError("'keyword_templates' must be a non-empty list")
        if not isinstance(payload["enabled_providers"], list) or not payload["enabled_providers"]:
            raise ValueError("'enabled_providers' must be a non-empty list")
        if not isinstance(payload["daily_digest_limit"], int) or payload["daily_digest_limit"] <= 0:
            raise ValueError("'daily_digest_limit' must be a positive integer")
        if "ignored_geo_terms" in payload and not isinstance(payload["ignored_geo_terms"], list):
            raise ValueError("'ignored_geo_terms' must be a list when provided")
        if "topic_priority_groups" in payload and not isinstance(payload["topic_priority_groups"], list):
            raise ValueError("'topic_priority_groups' must be a list when provided")
        if "competitor_aliases" in payload and not isinstance(payload["competitor_aliases"], dict):
            raise ValueError("'competitor_aliases' must be an object when provided")
        if "regional_rss_feeds" in payload and not isinstance(payload["regional_rss_feeds"], dict):
            raise ValueError("'regional_rss_feeds' must be an object when provided")

        region_keys = set(payload["regions"])
        for region_key, region_data in payload["regions"].items():
            if not isinstance(region_data, dict):
                raise ValueError(f"Region '{region_key}' must be an object")
            label = region_data.get("label")
            geo_terms = region_data.get("geo_terms")
            country_validation_terms = region_data.get("country_validation_terms")
            language_hints = region_data.get("language_hints")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"Region '{region_key}' must have a non-empty label")
            if not isinstance(geo_terms, list) or not any(term.strip() for term in geo_terms):
                raise ValueError(f"Region '{region_key}' must define non-empty geo_terms")
            if country_validation_terms is not None and (
                not isinstance(country_validation_terms, list)
                or not any(term.strip() for term in country_validation_terms)
            ):
                raise ValueError(
                    f"Region '{region_key}' must define non-empty country_validation_terms when provided"
                )
            if not isinstance(language_hints, list) or not any(
                hint.strip() for hint in language_hints
            ):
                raise ValueError(
                    f"Region '{region_key}' must define non-empty language_hints"
                )

        for region_key, competitors in payload["competitors_by_region"].items():
            if region_key not in region_keys:
                raise ValueError(
                    f"competitors_by_region references unknown region '{region_key}'"
                )
            if not isinstance(competitors, list) or not any(
                competitor.strip() for competitor in competitors
            ):
                raise ValueError(
                    f"Region '{region_key}' must define at least one competitor"
                )

        for topic_key, keywords in payload["topic_groups"].items():
            if not isinstance(keywords, list) or not any(keyword.strip() for keyword in keywords):
                raise ValueError(
                    f"Topic group '{topic_key}' must define at least one keyword"
                )
        if "topic_priority_groups" in payload:
            unknown_priority_topics = set(payload["topic_priority_groups"]) - set(payload["topic_groups"])
            if unknown_priority_topics:
                missing_list = ", ".join(sorted(unknown_priority_topics))
                raise ValueError(
                    f"topic_priority_groups references unknown topics: {missing_list}"
                )
        if "competitor_aliases" in payload:
            known_competitors = {
                competitor.strip()
                for competitors in payload["competitors_by_region"].values()
                for competitor in competitors
                if competitor.strip()
            }
            for competitor, aliases in payload["competitor_aliases"].items():
                if competitor not in known_competitors:
                    raise ValueError(
                        f"competitor_aliases references unknown competitor '{competitor}'"
                    )
                if not isinstance(aliases, list):
                    raise ValueError(
                        f"competitor_aliases for '{competitor}' must be a list"
                    )
        if "regional_rss_feeds" in payload:
            for region_key, feeds in payload["regional_rss_feeds"].items():
                if region_key not in region_keys:
                    raise ValueError(
                        f"regional_rss_feeds references unknown region '{region_key}'"
                    )
                if not isinstance(feeds, list):
                    raise ValueError(
                        f"regional_rss_feeds for '{region_key}' must be a list"
                    )
                for feed in feeds:
                    if not isinstance(feed, dict):
                        raise ValueError(
                            f"regional_rss_feeds entry for '{region_key}' must be an object"
                        )
                    if not isinstance(feed.get("name"), str) or not feed["name"].strip():
                        raise ValueError(
                            f"regional_rss_feeds entry for '{region_key}' must have a non-empty name"
                        )
                    if not isinstance(feed.get("url"), str) or not feed["url"].strip():
                        raise ValueError(
                            f"regional_rss_feeds entry for '{region_key}' must have a non-empty url"
                        )

        required_template_fields = {
            "{competitor}",
            "{topic_name}",
            "{topic_keywords}",
            "{region_label}",
            "{geo_terms}",
            "{language_hints}",
        }
        for template in payload["keyword_templates"]:
            if not isinstance(template, str) or not template.strip():
                raise ValueError("Each keyword template must be a non-empty string")
            if "{competitor}" not in template:
                raise ValueError("Each keyword template must include {competitor}")
            if not any(field in template for field in required_template_fields - {"{competitor}"}):
                raise ValueError(
                    "Each keyword template must include at least one expansion field"
                )

    def all_competitors(self) -> Tuple[str, ...]:
        """Return the deduplicated competitor universe across all regions."""
        return _dedupe_keep_order(
            competitor
            for competitors in self.competitors_by_region.values()
            for competitor in competitors
        )

    def competitors_for_region(self, region: str) -> Tuple[str, ...]:
        """Return the configured competitor set for one region."""
        if region not in self.competitors_by_region:
            raise ValueError(f"Unknown region '{region}'")
        return self.competitors_by_region[region]

    def region_for_competitor(self, competitor: str) -> Tuple[str, ...]:
        """Return every configured region where this competitor is allowed."""
        normalized = competitor.strip()
        if not normalized:
            return ()
        return tuple(
            region
            for region, competitors in self.competitors_by_region.items()
            if normalized in competitors
        )

    def is_competitor_allowed_in_region(self, competitor: str, region: str) -> bool:
        """Check whether a competitor is valid for a region according to config."""
        if region not in self.competitors_by_region:
            return False
        normalized = competitor.strip()
        if not normalized:
            return False
        return normalized in self.competitors_by_region[region]

    def allowed_competitor_region_pairs(self) -> frozenset[tuple[str, str]]:
        """Return the strict allowed competitor-region matrix for this config."""

        return allowed_competitor_region_pairs(self.competitors_by_region)

    @staticmethod
    def canonical_topic_group_name(topic_group: str) -> str:
        """Return the canonical product-contract topic group name."""

        return normalize_topic_group_name(topic_group)

    @staticmethod
    def business_region_name(region: str) -> str:
        """Return the outward-facing region label used by delivery layers."""

        return presentable_region_name(region)

    @property
    def visual_assets_enabled_by_default(self) -> bool:
        """Expose the frozen MVP visual-assets scope boundary."""

        return VISUAL_ASSETS_POLICY.enabled_by_default

    def queries_for_region(
        self,
        region: str,
        *,
        topic_groups: Optional[Sequence[str]] = None,
        competitors: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Expand search queries for a single region."""
        if region not in self.regions:
            raise ValueError(f"Unknown region '{region}'")

        region_config = self.regions[region]
        selected_competitors = _dedupe_keep_order(
            competitors or self.competitors_by_region[region]
        )
        selected_topic_groups = _dedupe_keep_order(topic_groups or self.topic_groups.keys())

        unknown_topics = set(selected_topic_groups) - set(self.topic_groups)
        if unknown_topics:
            missing_list = ", ".join(sorted(unknown_topics))
            raise ValueError(f"Unknown topic groups: {missing_list}")

        queries: List[str] = []
        for competitor in selected_competitors:
            for topic_name in selected_topic_groups:
                keyword_list = self.topic_groups[topic_name]
                expanded = self._expand_template_values(
                    competitor=competitor,
                    topic_name=topic_name,
                    topic_keywords=keyword_list,
                    region_config=region_config,
                )
                for template in self.keyword_templates:
                    query = template.format(**expanded).strip()
                    if query:
                        queries.append(" ".join(query.split()))
        return list(_dedupe_keep_order(queries))

    def prioritized_topic_groups(
        self,
        topic_groups: Optional[Sequence[str]] = None,
    ) -> Tuple[str, ...]:
        """Return topic groups ordered by business priority first, then the remainder."""
        selected_topic_groups = _dedupe_keep_order(topic_groups or self.topic_groups.keys())
        prioritized = [
            topic_name
            for topic_name in self.topic_priority_groups
            if topic_name in selected_topic_groups
        ]
        remaining = [
            topic_name
            for topic_name in selected_topic_groups
            if topic_name not in set(prioritized)
        ]
        return tuple([*prioritized, *remaining])

    def query_specs_for_region(
        self,
        region: str,
        *,
        topic_groups: Optional[Sequence[str]] = None,
        competitors: Optional[Sequence[str]] = None,
    ) -> List[tuple[str, str]]:
        """Expand query strings and preserve their owning competitor."""
        if region not in self.regions:
            raise ValueError(f"Unknown region '{region}'")

        region_config = self.regions[region]
        selected_competitors = _dedupe_keep_order(
            competitors or self.competitors_by_region[region]
        )
        selected_topic_groups = _dedupe_keep_order(topic_groups or self.topic_groups.keys())

        unknown_topics = set(selected_topic_groups) - set(self.topic_groups)
        if unknown_topics:
            missing_list = ", ".join(sorted(unknown_topics))
            raise ValueError(f"Unknown topic groups: {missing_list}")

        query_specs: List[tuple[str, str]] = []
        seen_queries = set()
        for competitor in selected_competitors:
            for topic_name in selected_topic_groups:
                keyword_list = self.topic_groups[topic_name]
                expanded = self._expand_template_values(
                    competitor=competitor,
                    topic_name=topic_name,
                    topic_keywords=keyword_list,
                    region_config=region_config,
                )
                for template in self.keyword_templates:
                    query = template.format(**expanded).strip()
                    normalized_query = " ".join(query.split())
                    if not normalized_query or normalized_query in seen_queries:
                        continue
                    seen_queries.add(normalized_query)
                    query_specs.append((normalized_query, competitor))
        return query_specs

    def prioritized_query_specs_for_region(
        self,
        region: str,
        *,
        topic_groups: Optional[Sequence[str]] = None,
        competitors: Optional[Sequence[str]] = None,
    ) -> List[tuple[str, str, str]]:
        """Expand query strings in priority order and preserve topic ownership."""
        if region not in self.regions:
            raise ValueError(f"Unknown region '{region}'")

        region_config = self.regions[region]
        selected_competitors = _dedupe_keep_order(
            competitors or self.competitors_by_region[region]
        )
        selected_topic_groups = self.prioritized_topic_groups(topic_groups)

        unknown_topics = set(selected_topic_groups) - set(self.topic_groups)
        if unknown_topics:
            missing_list = ", ".join(sorted(unknown_topics))
            raise ValueError(f"Unknown topic groups: {missing_list}")

        query_specs: List[tuple[str, str, str]] = []
        seen_queries = set()
        for topic_name in selected_topic_groups:
            keyword_list = self.topic_groups[topic_name]
            for competitor in selected_competitors:
                expanded = self._expand_template_values(
                    competitor=competitor,
                    topic_name=topic_name,
                    topic_keywords=keyword_list,
                    region_config=region_config,
                )
                for template in self.keyword_templates:
                    query = template.format(**expanded).strip()
                    normalized_query = " ".join(query.split())
                    if not normalized_query or normalized_query in seen_queries:
                        continue
                    seen_queries.add(normalized_query)
                    query_specs.append((normalized_query, competitor, topic_name))
        return query_specs

    def queries_for_regions(
        self,
        regions: Optional[Sequence[str]] = None,
        *,
        topic_groups: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Expand search queries across multiple regions."""
        selected_regions = _dedupe_keep_order(regions or self.regions.keys())
        queries: List[str] = []
        for region in selected_regions:
            queries.extend(self.queries_for_region(region, topic_groups=topic_groups))
        return list(_dedupe_keep_order(queries))

    def query_specs_for_regions(
        self,
        regions: Optional[Sequence[str]] = None,
        *,
        topic_groups: Optional[Sequence[str]] = None,
    ) -> List[tuple[str, str]]:
        """Expand query strings across regions and preserve owning competitor."""
        selected_regions = _dedupe_keep_order(regions or self.regions.keys())
        query_specs: List[tuple[str, str]] = []
        seen_queries = set()
        for region in selected_regions:
            for query, competitor in self.query_specs_for_region(
                region,
                topic_groups=topic_groups,
            ):
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                query_specs.append((query, competitor))
        return query_specs

    def competitor_aliases_for(self, competitor: str) -> Tuple[str, ...]:
        """Return configured aliases for a competitor."""
        if not competitor:
            return ()
        alias_map = self.competitor_aliases or {}
        return alias_map.get(competitor.strip(), ())

    def regional_rss_feeds_for_region(self, region: str) -> Tuple[RssFeedConfig, ...]:
        """Return curated RSS feeds for one region."""
        if region not in self.regions:
            raise ValueError(f"Unknown region '{region}'")
        feed_map = self.regional_rss_feeds or {}
        return feed_map.get(region, ())

    @staticmethod
    def _expand_template_values(
        *,
        competitor: str,
        topic_name: str,
        topic_keywords: Sequence[str],
        region_config: RegionConfig,
    ) -> dict:
        return {
            "competitor": competitor,
            "topic_name": topic_name.replace("_", " "),
            "topic_keywords": " OR ".join(topic_keywords),
            "region_label": region_config.label,
            "geo_terms": " OR ".join(region_config.geo_terms),
            "language_hints": " OR ".join(region_config.language_hints),
        }


@dataclass(frozen=True, slots=True)
class TrackerRuntimeConfig:
    """Operational settings kept separate from domain config."""

    output_dir: Path = Path("output") / "competitor_tracker"
    database_path: Path = Path("output") / "competitor_tracker" / "tracker.db"
    lookback_days: int = 7
    min_score: int = 5
    config_path: Path = DEFAULT_CONFIG_PATH
    use_llm_alerts: bool = False
    llm_top_n: int = 15
    telegram_top_n: int = 15
    article_context_max_chars: int = 8000
    enable_newsapi_full_run: bool = False
    gdelt_max_queries_per_run: int = 10
    newsapi_max_queries_per_run: int = 25
    guardian_max_queries_per_run: int = 40
    historical_precision_half_life_days: int = 30

    @classmethod
    def from_env(cls) -> "TrackerRuntimeConfig":
        """Build runtime settings from environment variables."""
        output_dir = Path(
            os.getenv("COMPETITOR_TRACKER_OUTPUT_DIR", "output/competitor_tracker")
        )
        database_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_DB_PATH",
                "output/competitor_tracker/tracker.db",
            )
        )
        lookback_days = int(os.getenv("COMPETITOR_TRACKER_LOOKBACK_DAYS", "7"))
        min_score = int(os.getenv("COMPETITOR_TRACKER_MIN_SCORE", "5"))
        config_path = Path(
            os.getenv("COMPETITOR_TRACKER_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))
        )
        use_llm_alerts = _env_flag("COMPETITOR_TRACKER_USE_LLM_ALERTS", False)
        llm_top_n = max(0, int(os.getenv("COMPETITOR_TRACKER_LLM_TOP_N", "15")))
        telegram_top_n = max(
            0,
            int(os.getenv("COMPETITOR_TRACKER_TELEGRAM_TOP_N", "15")),
        )
        article_context_max_chars = max(
            0,
            int(os.getenv("COMPETITOR_TRACKER_ARTICLE_CONTEXT_MAX_CHARS", "8000")),
        )
        enable_newsapi_full_run = _env_flag(
            "COMPETITOR_TRACKER_ENABLE_NEWSAPI_FULL_RUN",
            False,
        )
        gdelt_max_queries_per_run = max(
            0,
            int(os.getenv("COMPETITOR_TRACKER_GDELT_MAX_QUERIES_PER_RUN", "10")),
        )
        newsapi_max_queries_per_run = max(
            0,
            int(os.getenv("COMPETITOR_TRACKER_NEWSAPI_MAX_QUERIES_PER_RUN", "25")),
        )
        guardian_max_queries_per_run = max(
            0,
            int(os.getenv("COMPETITOR_TRACKER_GUARDIAN_MAX_QUERIES_PER_RUN", "40")),
        )
        historical_precision_half_life_days = max(
            1,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_HISTORICAL_PRECISION_HALF_LIFE_DAYS",
                    "30",
                )
            ),
        )
        return cls(
            output_dir=output_dir,
            database_path=database_path,
            lookback_days=lookback_days,
            min_score=min_score,
            config_path=config_path,
            use_llm_alerts=use_llm_alerts,
            llm_top_n=llm_top_n,
            telegram_top_n=telegram_top_n,
            article_context_max_chars=article_context_max_chars,
            enable_newsapi_full_run=enable_newsapi_full_run,
            gdelt_max_queries_per_run=gdelt_max_queries_per_run,
            newsapi_max_queries_per_run=newsapi_max_queries_per_run,
            guardian_max_queries_per_run=guardian_max_queries_per_run,
            historical_precision_half_life_days=historical_precision_half_life_days,
        )
