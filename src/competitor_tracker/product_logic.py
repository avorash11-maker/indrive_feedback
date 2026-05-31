"""Canonical product logic contract for the competitor tracker MVP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


REGION_COMPETITORS: dict[str, tuple[str, ...]] = {
    "latam": ("Uber", "DiDi", "Cabify", "99"),
    "sea": ("Grab", "Gojek", "Maxim", "Bolt"),
    "africa": ("Bolt", "Uber", "Careem", "Yassir", "Heetch"),
    "mea": ("Bolt", "Uber", "Careem", "Yassir", "Heetch"),
    "cis_central_asia": ("Yandex Go", "Bolt", "Maxim"),
}

MACRO_REGIONS: dict[str, tuple[str, ...]] = {
    "LATAM": ("latam",),
    "SEA": ("sea",),
    "Africa & MEA": ("africa", "mea"),
    "CIS / Central Asia": ("cis_central_asia",),
}

TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "market_expansion": (
        "launching in",
        "new city",
        "entering market",
        "expansion",
    ),
    "campaign_launches": (
        "campaign",
        "partnership",
        "brand ambassador",
        "new feature",
    ),
    "pricing_promo": (
        "discount",
        "promo code",
        "price cut",
        "subscription",
    ),
    "core_industry_terms": (
        "ride-hailing",
        "e-hailing",
        "on-demand mobility",
        "ride-sharing",
        "taxi app",
        "vtc",
        "maas",
    ),
    "strategic_operations": (
        "market entry",
        "launching operations",
        "license obtained",
        "regulatory approval",
        "strategic partnership",
        "driver recruitment campaign",
    ),
    "performance_growth": (
        "first ride free",
        "discounted rides",
        "referral bonus",
        "loyalty program",
        "low commission for drivers",
        "bonus for new drivers",
    ),
    "product_features_innovation": (
        "intercity",
        "delivery",
        "courier service",
        "freight",
        "fixed price",
        "bidding model",
        "safety features",
    ),
}

TOPIC_PRIORITY_GROUPS: tuple[str, ...] = (
    "market_expansion",
    "campaign_launches",
    "pricing_promo",
    "strategic_operations",
    "performance_growth",
    "product_features_innovation",
    "core_industry_terms",
)

LEGACY_TOPIC_ALIASES: dict[str, str] = {
    "market_entry": "market_expansion",
    "pricing": "pricing_promo",
    "industry_context": "core_industry_terms",
    "product_launch": "product_features_innovation",
}

BUSINESS_REGION_LABELS: dict[str, str] = {
    "latam": "LATAM",
    "sea": "SEA",
    "africa": "Africa & MEA",
    "mea": "Africa & MEA",
    "cis_central_asia": "CIS / Central Asia",
}

DIGEST_ALWAYS_RELEVANT_TOPICS: frozenset[str] = frozenset(
    {
        "market_expansion",
        "campaign_launches",
        "pricing_promo",
        "strategic_operations",
        "performance_growth",
    }
)

DIGEST_CONDITIONAL_TOPIC_RULES: dict[str, str] = {
    "product_features_innovation": "requires explicit product or service innovation terms",
    "core_industry_terms": "requires explicit competitor action terms, not generic industry noise",
}

DIGEST_COMPETITOR_ACTION_TERMS: tuple[str, ...] = (
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


@dataclass(frozen=True, slots=True)
class VisualAssetsPolicy:
    """Explicit MVP boundary for visual asset monitoring."""

    enabled_by_default: bool
    in_scope: bool
    summary: str


VISUAL_ASSETS_POLICY = VisualAssetsPolicy(
    enabled_by_default=False,
    in_scope=False,
    summary=(
        "Visual assets monitoring is out of scope for the current MVP. "
        "Logos, banners, direct social media monitoring, vision analysis, and social scraping "
        "must stay disabled by default."
    ),
)


def normalize_topic_group_name(topic_group: str) -> str:
    """Map legacy topic names to canonical product-contract names."""

    normalized = str(topic_group or "").strip()
    if not normalized:
        return ""
    return LEGACY_TOPIC_ALIASES.get(normalized, normalized)


def presentable_region_name(region_key: str) -> str:
    """Map internal region keys to outward business labels."""

    normalized = str(region_key or "").strip()
    return BUSINESS_REGION_LABELS.get(normalized, normalized)


def presentable_topic_name(topic_group: str) -> str:
    """Map topic keys to outward canonical labels."""

    normalized = normalize_topic_group_name(topic_group)
    return normalized.replace("_", " ")


def allowed_competitor_region_pairs(
    competitors_by_region: Mapping[str, Sequence[str]] | None = None,
) -> frozenset[tuple[str, str]]:
    """Return the strict allowed competitor-region matrix."""

    source = competitors_by_region or REGION_COMPETITORS
    return frozenset(
        (region, competitor)
        for region, competitors in source.items()
        for competitor in competitors
    )


def validate_default_product_config(payload: Mapping[str, object]) -> None:
    """Ensure the repository default config matches the frozen product contract."""

    configured_competitors = payload.get("competitors_by_region")
    configured_topics = payload.get("topic_groups")
    configured_priorities = payload.get("topic_priority_groups")

    if configured_competitors != {key: list(value) for key, value in REGION_COMPETITORS.items()}:
        raise ValueError(
            "default_config.json competitors_by_region does not match the product logic contract"
        )
    if configured_topics != {key: list(value) for key, value in TOPIC_GROUPS.items()}:
        raise ValueError(
            "default_config.json topic_groups does not match the product logic contract"
        )
    if configured_priorities != list(TOPIC_PRIORITY_GROUPS):
        raise ValueError(
            "default_config.json topic_priority_groups does not match the product logic contract"
        )
