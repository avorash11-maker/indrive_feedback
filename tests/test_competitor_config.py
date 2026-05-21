import json

import pytest

from competitor_tracker.config import TrackerConfig


def test_load_default_competitor_tracker_config():
    config = TrackerConfig.load_default()

    assert "latam" in config.regions
    assert "africa" in config.regions
    assert "mea" in config.regions
    assert "cis_central_asia" in config.regions
    assert config.regions["latam"].label == "Latin America"
    assert config.competitors_by_region["latam"] == ("Uber", "DiDi", "Cabify", "99")
    assert config.competitors_by_region["sea"] == ("Grab", "Gojek", "Maxim", "Bolt")
    assert config.competitors_by_region["africa"] == ("Bolt", "Uber", "Careem", "Yassir", "Heetch")
    assert config.competitors_by_region["mea"] == ("Bolt", "Uber", "Careem", "Yassir", "Heetch")
    assert config.competitors_by_region["cis_central_asia"] == ("Yandex Go", "Bolt", "Maxim")
    assert "market_expansion" in config.topic_groups
    assert "campaign_launches" in config.topic_groups
    assert "pricing_promo" in config.topic_groups
    assert "industry_context" in config.topic_groups
    assert "strategic_operations" in config.topic_groups
    assert "performance_growth" in config.topic_groups
    assert "product_features_innovation" in config.topic_groups
    assert "launching in" in config.topic_groups["market_expansion"]
    assert "brand ambassador" in config.topic_groups["campaign_launches"]
    assert "promo code" in config.topic_groups["pricing_promo"]
    assert "ride-hailing" in config.topic_groups["industry_context"]
    assert "bidding model" in config.topic_groups["product_features_innovation"]
    assert config.daily_digest_limit == 12
    assert config.enabled_providers == ("newsapi", "gdelt", "google_news_rss")
    assert config.competitors_for_region("latam") == ("Uber", "DiDi", "Cabify", "99")
    assert config.competitors_for_region("sea") == ("Grab", "Gojek", "Maxim", "Bolt")
    assert config.competitors_for_region("africa") == ("Bolt", "Uber", "Careem", "Yassir", "Heetch")
    assert config.competitors_for_region("mea") == ("Bolt", "Uber", "Careem", "Yassir", "Heetch")
    assert config.competitors_for_region("cis_central_asia") == ("Yandex Go", "Bolt", "Maxim")
    assert config.region_for_competitor("99") == ("latam",)
    assert config.region_for_competitor("Grab") == ("sea",)
    assert config.region_for_competitor("Careem") == ("africa", "mea")
    assert config.region_for_competitor("Yandex Go") == ("cis_central_asia",)
    assert config.is_competitor_allowed_in_region("Grab", "sea") is True
    assert config.is_competitor_allowed_in_region("Grab", "latam") is False
    assert config.is_competitor_allowed_in_region("99", "latam") is True
    assert config.is_competitor_allowed_in_region("99", "sea") is False


def test_competitor_truth_layer_helpers_reject_unknown_or_empty_values():
    config = TrackerConfig.load_default()

    with pytest.raises(ValueError, match="Unknown region 'unknown_region'"):
        config.competitors_for_region("unknown_region")

    assert config.region_for_competitor("") == ()
    assert config.region_for_competitor("Unknown Brand") == ()
    assert config.is_competitor_allowed_in_region("", "sea") is False
    assert config.is_competitor_allowed_in_region("Grab", "unknown_region") is False


def test_from_dict_rejects_unknown_region_reference():
    payload = {
        "regions": {
            "latam": {
                "label": "Latin America",
                "geo_terms": ["Mexico"],
                "language_hints": ["es"],
            }
        },
        "competitors_by_region": {"sea": ["Grab"]},
        "topic_groups": {"pricing": ["price"]},
        "keyword_templates": ["{competitor} {topic_name} {region_label}"],
        "daily_digest_limit": 5,
        "enabled_providers": ["newsapi"],
    }

    with pytest.raises(ValueError, match="unknown region 'sea'"):
        TrackerConfig.from_dict(payload)


def test_load_custom_config_and_expand_queries(tmp_path):
    config_path = tmp_path / "competitor_config.json"
    payload = {
        "regions": {
            "latam": {
                "label": "Latin America",
                "geo_terms": ["Mexico", "Brazil"],
                "language_hints": ["es", "pt"],
            }
        },
        "competitors_by_region": {"latam": ["Uber", "DiDi"]},
        "topic_groups": {
            "pricing": ["price", "fare"],
            "regulation": ["regulation", "permit"],
        },
        "keyword_templates": [
            "\"{competitor}\" {topic_name} {region_label}",
            "\"{competitor}\" {topic_keywords} {geo_terms}",
        ],
        "daily_digest_limit": 7,
        "enabled_providers": ["newsapi", "gdelt"],
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = TrackerConfig.load(config_path)
    queries = config.queries_for_region("latam")

    assert len(queries) == 8
    assert "\"Uber\" pricing Latin America" in queries
    assert "\"DiDi\" regulation OR permit Mexico OR Brazil" in queries


def test_queries_for_region_rejects_unknown_topic_group():
    config = TrackerConfig.load_default()

    with pytest.raises(ValueError, match="Unknown topic groups: impossible_topic"):
        config.queries_for_region("latam", topic_groups=["impossible_topic"])
