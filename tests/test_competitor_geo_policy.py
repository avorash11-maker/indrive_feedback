import pytest

from competitor_tracker.config import TrackerConfig
from competitor_tracker.geo_policy import GeoPolicy


def build_config() -> TrackerConfig:
    return TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand", "Vietnam"],
                    "country_validation_terms": ["Indonesia", "Thailand", "Vietnam", "Singapore", "Malaysia"],
                    "language_hints": ["en", "id", "th"],
                },
                "mea": {
                    "label": "Middle East",
                    "geo_terms": ["Jordan", "Saudi Arabia", "UAE"],
                    "country_validation_terms": ["Jordan", "Saudi Arabia", "UAE", "United Arab Emirates"],
                    "language_hints": ["ar", "en"],
                },
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil"],
                    "country_validation_terms": ["Mexico", "Brazil", "Argentina"],
                    "language_hints": ["es", "pt", "en"],
                },
                "cis_central_asia": {
                    "label": "CIS / Central Asia",
                    "geo_terms": ["Georgia", "Kazakhstan"],
                    "country_validation_terms": ["Georgia", "Kazakhstan", "Russia"],
                    "language_hints": ["ru", "en"],
                },
            },
            "competitors_by_region": {
                "sea": ["Grab", "Gojek"],
                "mea": ["Careem"],
                "latam": ["Uber", "DiDi"],
                "cis_central_asia": ["Yandex Go"],
            },
            "topic_groups": {
                "pricing": ["price", "pricing", "fare", "discount"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA", "United States", "North America", "Europe", "UK"],
            "daily_digest_limit": 10,
            "enabled_providers": ["gdelt", "google_news_rss"],
        }
    )


@pytest.mark.parametrize(
    ("text_blob", "geo_term", "expected"),
    [
        ("grab launches in uk", "UK", True),
        ("truck demand is rising", "UK", False),
        ("operations expand in   united   states", "United States", True),
        ("join us in singapore", "USA", False),
        ("new service in jordanian market", "Jordan", False),
    ],
)
def test_geo_policy_contains_geo_term_uses_word_boundaries(text_blob, geo_term, expected):
    policy = GeoPolicy(build_config())

    assert policy.contains_geo_term(text_blob, geo_term) is expected


def test_geo_policy_ignored_geo_decision_returns_details_without_target_confirmation():
    policy = GeoPolicy(build_config())

    decision = policy.ignored_geo_decision(
        geo_text_blob="grab pilots discount rides across the usa and united states market",
        selected_regions=("sea",),
    )

    assert decision == {
        "ignored_geo_terms": "USA | United States",
        "selected_regions": "sea",
    }


def test_geo_policy_ignored_geo_decision_allows_mixed_geo_when_target_is_present():
    policy = GeoPolicy(build_config())

    decision = policy.ignored_geo_decision(
        geo_text_blob="grab launches singapore airport campaign for travelers returning from europe",
        selected_regions=("sea",),
    )

    assert decision is None


def test_geo_policy_region_markers_include_article_region_and_detected_geo_terms():
    policy = GeoPolicy(build_config())

    markers = policy.region_markers(
        article_region="sea",
        geo_text_blob="uber expands discount service in mexico",
        selected_regions=("sea", "latam"),
    )

    assert markers == ("sea", "latam")


@pytest.mark.parametrize(
    ("article_region", "geo_text_blob", "regions", "expected"),
    [
        (None, "careem expands service in jordan", ("mea",), ("mea", "Jordan")),
        (None, "yandex go launches in georgia", ("cis_central_asia",), ("cis_central_asia", "Georgia")),
        ("sea", "grab rolls out airport promo", ("sea",), ("sea", None)),
    ],
)
def test_geo_policy_detect_region_handles_target_edge_cases(article_region, geo_text_blob, regions, expected):
    policy = GeoPolicy(build_config())

    assert policy.detect_region(
        article_region=article_region,
        geo_text_blob=geo_text_blob,
        regions=regions,
    ) == expected


def test_geo_policy_target_geo_terms_include_region_labels_and_validation_terms():
    policy = GeoPolicy(build_config())

    target_terms = policy.target_geo_terms(("sea", "mea"))

    assert "Southeast Asia" in target_terms
    assert "Singapore" in target_terms
    assert "Middle East" in target_terms
    assert "Jordan" in target_terms
