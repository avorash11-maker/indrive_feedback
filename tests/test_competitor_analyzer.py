from types import SimpleNamespace

from competitor_tracker.analyzer import CompetitorAlertAnalyzer, CompetitorAnalyzer
from competitor_tracker.config import TrackerConfig
from competitor_tracker.models import ArticleContext, CandidateArticle, RawArticle


def build_config() -> TrackerConfig:
    return TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil"],
                    "language_hints": ["es", "pt", "en"],
                },
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand", "Vietnam"],
                    "language_hints": ["id", "th", "vi", "en"],
                },
            },
            "competitors_by_region": {
                "latam": ["Uber", "DiDi"],
                "sea": ["Grab", "Gojek"],
            },
            "topic_groups": {
                "pricing": ["price", "pricing", "fare", "commission", "discount"],
                "regulation": ["regulation", "permit", "license", "ban", "compliance"],
                "safety": ["safety", "incident", "security", "insurance"],
                "product_launch": ["launch", "rollout", "expansion", "partnership"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["gdelt", "google_news_rss"],
        }
    )


def test_prefilter_matches_topic_region_and_language_hint():
    analyzer = CompetitorAnalyzer(min_score=5, config=build_config())
    raw_article = RawArticle(
        title="Grab launches new airport pricing in Thailand",
        url="https://example.com/grab-thailand",
        provider="google_news_rss",
        snippet="The launch includes discount fare options for Bangkok riders.",
        query='"Grab" pricing Southeast Asia th',
        competitor_hints=("Grab",),
    )

    result = analyzer.prefilter_raw_articles([raw_article], regions=("sea",))

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.competitor == "Grab"
    assert candidate.topic_group == "pricing"
    assert candidate.region == "sea"
    assert candidate.country_hint == "Thailand"
    assert candidate.language_hint == "th"
    assert "pricing" in candidate.matched_keywords
    assert candidate.score >= 8


def test_prefilter_priority_candidate_scoring_for_regulation_signal():
    analyzer = CompetitorAnalyzer(min_score=6, config=build_config())
    raw_article = RawArticle(
        title="Uber faces permit ban in Mexico after pricing dispute",
        url="https://example.com/uber-mexico-ban",
        provider="gdelt",
        snippet="Mexico regulators review Uber license status and fare commission rules.",
        query='"Uber" regulation Latin America es',
        competitor_hints=("Uber",),
    )

    result = analyzer.prefilter_raw_articles([raw_article], regions=("latam",))

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.topic_group == "regulation"
    assert candidate.region == "latam"
    assert candidate.country_hint == "Mexico"
    assert candidate.language_hint == "es"
    assert candidate.score == 10
    assert "priority_signal" in candidate.reasons


def test_prefilter_drops_articles_without_competitor_or_topic_match():
    analyzer = CompetitorAnalyzer(min_score=5, config=build_config())
    raw_article = RawArticle(
        title="City mobility forum discusses transport trends",
        url="https://example.com/forum",
        provider="gdelt",
        snippet="General urban transport discussion without competitor signal.",
        query='mobility forum',
    )

    result = analyzer.prefilter_raw_articles([raw_article], regions=("latam",))

    assert result.candidates == []
    assert result.dropped_count == 1


def test_competitor_alert_analyzer_returns_fallback_schema_without_llm():
    analyzer = CompetitorAlertAnalyzer(use_llm=False)
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab expands driver fuel support program in the Philippines",
            url="https://example.com/grab-ph",
            provider="google_news_rss",
            snippet="Grab promotes driver support and subsidies in the Philippines.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=7,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        matched_keywords=("support", "subsidies"),
        reasons=("priority_signal", "region_match:sea"),
    )

    alert = analyzer.analyze_candidate(candidate)

    assert alert["competitor"] == "Grab / Move It"
    assert alert["region"] == "sea"
    assert alert["country"] == "Philippines"
    assert alert["topic"] == "marketing + policy narrative"
    assert alert["priority"] == "MEDIUM"
    assert 0.0 <= alert["confidence"] <= 1.0
    assert alert["what_happened"]
    assert alert["why_it_matters"]
    assert alert["potential_impact"]
    assert alert["recommended_action"]


def test_competitor_alert_analyzer_uses_openai_response_shape():
    captured = {}

    def fake_create(**kwargs):
        captured["model"] = kwargs["model"]
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "Southeast Asia",
                          "country": "Philippines",
                          "topic": "Marketing + Policy Narrative",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-19",
                          "published_date_source": "llm",
                          "what_happened": "Platforms are promoting driver support programs as part of public messaging.",
                          "why_it_matters": "Driver support is becoming part of brand communication, not just operations.",
                          "potential_impact": "Improved driver perception and stronger trust narrative.",
                          "recommended_action": "Highlight driver benefits in campaigns and test driver care messaging.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False)
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=fake_create
            )
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab offers support programs to drivers as fuel prices soar",
            url="https://example.com/grab-fuel",
            provider="google_news_rss",
            snippet="Driver support programs gain public attention in the Philippines.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
    )

    alert = analyzer.analyze_candidate(candidate)

    assert alert["competitor"] == "Grab / Move It"
    assert alert["country"] == "Philippines"
    assert alert["priority"] == "MEDIUM"
    assert alert["confidence"] == 0.86
    assert alert["published_date"] == "2026-05-19"
    assert alert["published_date_source"] == "llm"
    assert "driver support" in alert["what_happened"].lower()
    assert "senior international marketing strategist for inDrive" in captured["messages"][0]["content"]
    assert "Think like a senior operator responsible for competitor response" in captured["messages"][0]["content"]
    assert "Do not invent facts, metrics, partnerships, timelines, internal intent, or campaign performance." in captured["messages"][0]["content"]
    assert "If the article metadata field `published_at` is None or missing" in captured["messages"][0]["content"]
    assert 'Recommended actions must be applicable to inDrive, not generic advice for "a company".' in captured["messages"][0]["content"]
    assert '"published_date": "YYYY-MM-DD"' in captured["messages"][0]["content"]
    assert '"published_date_source": "metadata|llm|unknown"' in captured["messages"][0]["content"]
    assert '"recommended_action": "string"' in captured["messages"][0]["content"]
    assert "Today's date for reference: 2026-05-21." in captured["messages"][1]["content"]
    assert "Article published_at metadata:" in captured["messages"][1]["content"]
    assert "Write the alert for the inDrive Marcom / growth team." in captured["messages"][1]["content"]
    assert "competitor strategy" in captured["messages"][1]["content"]
    assert "what inDrive can do better or differently" in captured["messages"][1]["content"]
    assert '"why_it_matters" should explain the strategic meaning, not just restate the article' in captured["messages"][1]["content"]
    assert '"recommended_action" should give specific next moves for inDrive' in captured["messages"][1]["content"]


def test_competitor_alert_analyzer_skips_llm_when_article_body_is_unavailable():
    analyzer = CompetitorAlertAnalyzer(use_llm=False)
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"

    def fail_if_called(**kwargs):
        raise AssertionError("OpenAI client should not be called without article body")

    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fail_if_called)
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab offers support programs to drivers as fuel prices soar",
            url="https://example.com/grab-fuel",
            provider="google_news_rss",
            snippet="Driver support programs gain public attention in the Philippines.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
    )

    alert = analyzer.analyze_candidate(
        candidate,
        article_context=ArticleContext(
            title=candidate.title,
            snippet=candidate.raw_article.snippet,
            source_url=candidate.url,
            article_body="",
        ),
    )

    assert alert["competitor"] == "Grab / Move It"
    assert alert["country"] == "Philippines"
    assert alert["published_date"] == ""
    assert alert["published_date_source"] == "unknown"
    assert alert["why_it_matters"] == analyzer.INSUFFICIENT_SOURCE_DATA_MESSAGE
    assert alert["recommended_action"] == analyzer.INSUFFICIENT_SOURCE_DATA_MESSAGE
    assert alert["confidence"] == 0.0


def test_competitor_alert_analyzer_marks_metadata_date_source_for_fallback():
    analyzer = CompetitorAlertAnalyzer(use_llm=False)
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab expands driver fuel support program in the Philippines",
            url="https://example.com/grab-ph",
            provider="google_news_rss",
            snippet="Grab promotes driver support and subsidies in the Philippines.",
            published_at="2026-05-20T09:00:00Z",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=7,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        matched_keywords=("support", "subsidies"),
        reasons=("priority_signal", "region_match:sea"),
    )

    alert = analyzer.analyze_candidate(candidate)

    assert alert["published_date"] == "2026-05-20"
    assert alert["published_date_source"] == "metadata"


def test_competitor_alert_analyzer_prefers_metadata_date_over_conflicting_llm_date():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "sea",
                          "country": "Philippines",
                          "topic": "Marketing + Policy Narrative",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-18",
                          "published_date_source": "llm",
                          "what_happened": "Platforms are promoting driver support programs as part of public messaging.",
                          "why_it_matters": "Driver support is becoming part of brand communication, not just operations.",
                          "potential_impact": "Improved driver perception and stronger trust narrative.",
                          "recommended_action": "Highlight driver benefits in campaigns and test driver care messaging.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False)
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab offers support programs to drivers as fuel prices soar",
            url="https://example.com/grab-fuel",
            provider="google_news_rss",
            snippet="Driver support programs gain public attention in the Philippines.",
            published_at="2026-05-20T09:00:00Z",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
    )

    alert = analyzer.analyze_candidate(
        candidate,
        article_context=ArticleContext(
            title=candidate.title,
            snippet=candidate.raw_article.snippet,
            source_url=candidate.url,
            article_body="A full article body is available for analysis.",
            published_at="2026-05-20",
        ),
    )

    assert alert["published_date"] == "2026-05-20"
    assert alert["published_date_source"] == "metadata"
