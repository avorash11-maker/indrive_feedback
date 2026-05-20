import json

import pytest

from competitor_tracker.config import TrackerConfig


def test_load_default_competitor_tracker_config():
    config = TrackerConfig.load_default()

    assert "latam" in config.regions
    assert config.regions["latam"].label == "Latin America"
    assert "Uber" in config.competitors_by_region["latam"]
    assert "pricing" in config.topic_groups
    assert config.daily_digest_limit == 12
    assert config.enabled_providers == ("newsapi", "gdelt", "google_news_rss")


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
