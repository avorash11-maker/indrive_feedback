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
                    "country_validation_terms": ["Mexico", "Brazil", "Argentina"],
                    "language_hints": ["es", "pt", "en"],
                },
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand", "Vietnam"],
                    "country_validation_terms": ["Indonesia", "Thailand", "Vietnam", "Philippines", "Singapore", "Malaysia"],
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


def build_africa_mea_shared_config() -> TrackerConfig:
    return TrackerConfig.from_dict(
        {
            "regions": {
                "africa": {
                    "label": "Africa",
                    "geo_terms": ["South Africa", "Kenya", "Nigeria", "Egypt"],
                    "country_validation_terms": ["South Africa", "ZA", "Kenya", "KE", "Nigeria", "NG", "Egypt", "EG"],
                    "language_hints": ["en", "fr"],
                },
                "mea": {
                    "label": "Middle East",
                    "geo_terms": ["Saudi Arabia", "UAE", "Qatar", "Jordan"],
                    "country_validation_terms": ["Saudi Arabia", "SA", "KSA", "United Arab Emirates", "UAE", "AE", "Qatar", "QA", "Jordan", "JO", "Egypt", "EG"],
                    "language_hints": ["ar", "en"],
                },
            },
            "competitors_by_region": {
                "africa": ["Bolt", "Uber", "Careem", "Yassir", "Heetch"],
                "mea": ["Bolt", "Uber", "Careem", "Yassir", "Heetch"],
            },
            "topic_groups": {
                "campaign_launches": ["campaign", "partnership", "driver"],
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


def test_prefilter_drops_articles_with_competitor_region_matrix_mismatch():
    analyzer = CompetitorAnalyzer(min_score=5, config=build_config())
    raw_article = RawArticle(
        title="Grab launches new pricing campaign in Mexico",
        url="https://example.com/grab-mexico",
        provider="google_news_rss",
        snippet="Grab promotes discount fare options in Mexico.",
        query='"Grab" pricing Latin America es',
        competitor_hints=("Grab",),
    )

    result = analyzer.prefilter_raw_articles([raw_article], regions=("latam", "sea"))

    assert result.candidates == []
    assert result.dropped_count == 1


def test_prefilter_keeps_detected_region_without_fabricating_country_hint():
    analyzer = CompetitorAnalyzer(min_score=5, config=build_africa_mea_shared_config())
    raw_article = RawArticle(
        title="Careem launches new driver campaign across the region",
        url="https://example.com/careem-region-campaign",
        provider="google_news_rss",
        snippet="Careem announced a new driver campaign with regional messaging.",
        query='"Careem" campaign Middle East',
        region="mea",
        competitor_hints=("Careem",),
    )

    result = analyzer.prefilter_raw_articles([raw_article], regions=("mea",))

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.region == "mea"
    assert candidate.country_hint is None


def test_competitor_alert_analyzer_returns_fallback_schema_without_llm():
    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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
    assert "Treat `competitor` and `region` from candidate metadata as pre-detected pipeline signals" in captured["messages"][0]["content"]
    assert "Do not override the provided `competitor` or `region` unless the article contains explicit evidence" in captured["messages"][0]["content"]
    assert "If the signal is ambiguous, mixed, or weak, preserve the provided pipeline `competitor` and `region`." in captured["messages"][0]["content"]
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
    assert "treat candidate `competitor` and `region` as pipeline-detected inputs that should stay unchanged by default" in captured["messages"][1]["content"]
    assert "change `competitor` or `region` only if the article explicitly proves the pipeline signal is wrong" in captured["messages"][1]["content"]
    assert "if the article is ambiguous, preserve the provided pipeline `competitor` and `region`" in captured["messages"][1]["content"]
    assert '"why_it_matters" should explain the strategic meaning, not just restate the article' in captured["messages"][1]["content"]
    assert '"recommended_action" should give specific next moves for inDrive' in captured["messages"][1]["content"]


def test_competitor_alert_analyzer_skips_llm_when_article_body_is_unavailable():
    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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
    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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


def test_competitor_alert_analyzer_rejects_llm_competitor_region_mismatch():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Uber",
                          "region": "latam",
                          "country": "Mexico",
                          "topic": "Marketing + Policy Narrative",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-19",
                          "published_date_source": "llm",
                          "what_happened": "Uber launched a new campaign.",
                          "why_it_matters": "It may shift market perception.",
                          "potential_impact": "Potential trust and growth impact.",
                          "recommended_action": "Respond with local messaging.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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

    assert alert["competitor"] == "Grab / Move It"
    assert alert["region"] == "sea"
    assert alert["country"] == "Philippines"
    assert alert["competitor_source"] == "pipeline"
    assert alert["region_source"] == "pipeline"
    assert alert["country_source"] == "pipeline"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_falls_back_to_candidate_country_hint_on_llm_conflict():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "sea",
                          "country": "Mexico",
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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

    assert alert["country"] == "Philippines"
    assert alert["country_source"] == "pipeline"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_clears_unreliable_llm_country_without_candidate_hint():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "sea",
                          "country": "Mexico",
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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
            snippet="Driver support programs gain public attention in the region.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint=None,
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

    assert alert["country"] == ""
    assert alert["country_source"] == "empty"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_keeps_valid_llm_country_from_validation_vocabulary():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "sea",
                          "country": "Malaysia",
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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
            snippet="Driver support programs gain public attention in the region.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint=None,
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

    assert alert["country"] == "Malaysia"
    assert alert["country_source"] == "llm"
    assert alert["geo_validation_fallback"] is False


def test_competitor_alert_analyzer_understands_iso_country_codes():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Grab / Move It",
                          "region": "sea",
                          "country": "MY",
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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
            snippet="Driver support programs gain public attention in the region.",
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint=None,
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

    assert alert["country"] == "Malaysia"
    assert alert["country_source"] == "llm"


def test_competitor_alert_analyzer_understands_target_region_country_aliases():
    alias_cases = (
        ("BR", "latam", "Brazil"),
        ("brasil", "latam", "Brazil"),
        ("KSA", "mea", "Saudi Arabia"),
        ("Russian Federation", "cis_central_asia", "Russia"),
    )

    for raw_country, region, expected_country in alias_cases:
        config = TrackerConfig.from_dict(
            {
                "regions": {
                    "latam": {
                        "label": "Latin America",
                        "geo_terms": ["Mexico", "Brazil"],
                        "country_validation_terms": ["Brazil", "BR", "Mexico", "MX"],
                        "language_hints": ["es", "pt", "en"],
                    },
                    "mea": {
                        "label": "Middle East",
                        "geo_terms": ["Saudi Arabia", "UAE"],
                        "country_validation_terms": ["Saudi Arabia", "SA", "KSA", "United Arab Emirates", "AE", "UAE"],
                        "language_hints": ["ar", "en"],
                    },
                    "cis_central_asia": {
                        "label": "CIS / Central Asia",
                        "geo_terms": ["Kazakhstan", "Georgia"],
                        "country_validation_terms": ["Russia", "RU", "Russian Federation", "Kazakhstan", "KZ"],
                        "language_hints": ["ru", "en"],
                    },
                },
                "competitors_by_region": {
                    "latam": ["Uber"],
                    "mea": ["Careem"],
                    "cis_central_asia": ["Yandex Go"],
                },
                "topic_groups": {
                    "pricing": ["price", "pricing"],
                },
                "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
                "daily_digest_limit": 10,
                "enabled_providers": ["gdelt", "google_news_rss"],
            }
        )

        def fake_create(**kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=f"""
                            {{
                              "competitor": "Test Competitor",
                              "region": "{region}",
                              "country": "{raw_country}",
                              "topic": "Pricing",
                              "priority": "MEDIUM",
                              "published_date": "2026-05-19",
                              "published_date_source": "llm",
                              "what_happened": "Pricing signal detected.",
                              "why_it_matters": "Pricing may affect market positioning.",
                              "potential_impact": "Potential trust and growth impact.",
                              "recommended_action": "Review local pricing response.",
                              "confidence": 0.86
                            }}
                            """
                        )
                    )
                ]
            )

        analyzer = CompetitorAlertAnalyzer(use_llm=False, config=config)
        analyzer.use_llm = True
        analyzer.model = "gpt-4o-mini"
        analyzer.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )
        candidate = CandidateArticle(
            raw_article=RawArticle(
                title="Test pricing signal",
                url="https://example.com/test-country-alias",
                provider="google_news_rss",
                snippet="Regional pricing signal.",
            ),
            competitor="Test Competitor",
            topic_group="pricing",
            score=8,
            region=region,
            country_hint=None,
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

        assert alert["country"] == expected_country
        assert alert["country_source"] == "llm"


def test_competitor_alert_analyzer_falls_back_when_llm_changes_competitor_within_same_region():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Gojek",
                          "region": "sea",
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

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_config())
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

    assert alert["competitor"] == "Grab / Move It"
    assert alert["competitor_source"] == "pipeline"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_clears_ambiguous_shared_region_without_geo_proof():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Careem",
                          "region": "mea",
                          "country": "",
                          "topic": "Campaign launches",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-19",
                          "published_date_source": "llm",
                          "what_happened": "Careem launched a broad campaign.",
                          "why_it_matters": "It may affect local perception.",
                          "potential_impact": "Potential trust and growth impact.",
                          "recommended_action": "Validate geo specifics first.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_africa_mea_shared_config())
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Careem launches broad regional campaign",
            url="https://example.com/careem-regional-campaign",
            provider="google_news_rss",
            snippet="Careem expands a broad regional campaign without naming a country.",
        ),
        competitor="Careem",
        topic_group="campaign_launches",
        score=8,
        region=None,
        country_hint=None,
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

    assert alert["region"] == ""
    assert alert["region_source"] == "empty"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_resolves_shared_region_from_unique_country_hint():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Careem",
                          "region": "africa",
                          "country": "KSA",
                          "topic": "Campaign launches",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-19",
                          "published_date_source": "llm",
                          "what_happened": "Careem launched a broad campaign.",
                          "why_it_matters": "It may affect local perception.",
                          "potential_impact": "Potential trust and growth impact.",
                          "recommended_action": "Validate geo specifics first.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_africa_mea_shared_config())
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Careem launches broad regional campaign",
            url="https://example.com/careem-regional-campaign-ksa",
            provider="google_news_rss",
            snippet="Careem expands a broad regional campaign.",
        ),
        competitor="Careem",
        topic_group="campaign_launches",
        score=8,
        region=None,
        country_hint=None,
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

    assert alert["region"] == "mea"
    assert alert["region_source"] == "geo_country_override"
    assert alert["country"] == "Saudi Arabia"
    assert alert["country_source"] == "llm"
    assert alert["geo_validation_fallback"] is True


def test_competitor_alert_analyzer_keeps_region_empty_for_ambiguous_shared_country():
    def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="""
                        {
                          "competitor": "Careem",
                          "region": "mea",
                          "country": "Egypt",
                          "topic": "Campaign launches",
                          "priority": "MEDIUM",
                          "published_date": "2026-05-19",
                          "published_date_source": "llm",
                          "what_happened": "Careem launched a broad campaign.",
                          "why_it_matters": "It may affect local perception.",
                          "potential_impact": "Potential trust and growth impact.",
                          "recommended_action": "Validate geo specifics first.",
                          "confidence": 0.86
                        }
                        """
                    )
                )
            ]
        )

    analyzer = CompetitorAlertAnalyzer(use_llm=False, config=build_africa_mea_shared_config())
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Careem launches broad regional campaign",
            url="https://example.com/careem-regional-campaign-egypt",
            provider="google_news_rss",
            snippet="Careem expands a broad regional campaign.",
        ),
        competitor="Careem",
        topic_group="campaign_launches",
        score=8,
        region=None,
        country_hint=None,
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

    assert alert["region"] == ""
    assert alert["region_source"] == "empty"
    assert alert["country"] == ""
    assert alert["country_source"] == "empty"
    assert alert["geo_validation_fallback"] is True
