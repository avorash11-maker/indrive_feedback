"""Configuration loading and query expansion for competitor tracker."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")


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
class TrackerConfig:
    """Loaded configuration and source of truth for competitor tracking."""

    regions: Dict[str, RegionConfig]
    competitors_by_region: Dict[str, Tuple[str, ...]]
    topic_groups: Dict[str, Tuple[str, ...]]
    keyword_templates: Tuple[str, ...]
    daily_digest_limit: int
    enabled_providers: Tuple[str, ...]
    ignored_geo_terms: Tuple[str, ...] = ()

    @classmethod
    def load_default(cls) -> "TrackerConfig":
        """Load the repository default config."""
        return cls.load(DEFAULT_CONFIG_PATH)

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
        return cls(
            regions=regions,
            competitors_by_region=competitors_by_region,
            topic_groups=topic_groups,
            keyword_templates=_dedupe_keep_order(payload["keyword_templates"]),
            daily_digest_limit=payload["daily_digest_limit"],
            enabled_providers=_dedupe_keep_order(payload["enabled_providers"]),
            ignored_geo_terms=_dedupe_keep_order(payload.get("ignored_geo_terms", ())),
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
        return cls(
            output_dir=output_dir,
            database_path=database_path,
            lookback_days=lookback_days,
            min_score=min_score,
            config_path=config_path,
        )
