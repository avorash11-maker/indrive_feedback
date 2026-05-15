from indrive_media.analyzer import MentionAnalyzer
from types import SimpleNamespace


def test_heuristic_scores_direct_indrive_service_and_regulation_signal():
    analyzer = MentionAnalyzer(use_llm=False)

    result = analyzer.heuristic_analysis(
        title="inDrive faces new taxi safety regulation",
        text="Drivers and passengers discuss fare pricing and safety rules.",
        url="https://example.com/indrive-safety",
    )

    assert result["mentions_indrive"] is True
    assert result["relevance_score"] == 10
    assert result["is_business_critical"] is True
    assert "indrive" in result["matched_terms"]["company"]
    assert result["matched_terms"]["service"]
    assert result["matched_terms"]["regulation"]


def test_heuristic_scores_market_context_without_direct_indrive_lower():
    analyzer = MentionAnalyzer(use_llm=False)

    result = analyzer.heuristic_analysis(
        title="Uber and Bolt drivers protest new taxi commission rules",
        text="Ride-hailing drivers discuss fares and regulation.",
    )

    assert result["mentions_indrive"] is False
    assert result["relevance_score"] == 4
    assert result["is_business_critical"] is False


def test_heuristic_penalizes_noise_without_company_mention():
    analyzer = MentionAnalyzer(use_llm=False)

    result = analyzer.heuristic_analysis(
        title="Celebrity football recipe goes viral",
        text="A stock market and crypto story unrelated to mobility.",
    )

    assert result["mentions_indrive"] is False
    assert result["relevance_score"] == 0


def test_heuristic_does_not_match_indriver_inside_unrelated_slug_word():
    analyzer = MentionAnalyzer(use_llm=False)

    result = analyzer.heuristic_analysis(
        title="Murder and robbery charges laid against two",
        text="Local crime report unrelated to ride-hailing.",
        url="https://jamaica-gleaner.com/article/news/20260409/update-murder-and-robbery-charges-laid-against-two-death-indriverutech",
    )

    assert result["mentions_indrive"] is False
    assert result["matched_terms"]["company"] == []
    assert result["relevance_score"] < 6


def test_analyze_article_returns_heuristic_result_when_llm_disabled():
    analyzer = MentionAnalyzer(use_llm=False)

    result = analyzer.analyze_article(
        title="inDrive expands taxi service",
        text="Drivers and passengers discuss fares.",
        url="https://example.com/indrive-expands",
    )

    assert result["mentions_indrive"] is True
    assert result["matched_terms"]["company"] == ["indrive"]
    assert result["relevance_score"] == 9


def test_analyze_article_uses_openai_chat_completions_response_shape():
    analyzer = MentionAnalyzer(use_llm=False)
    analyzer.use_llm = True
    analyzer.model = "gpt-4o-mini"
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="""
                                {
                                  "sentiment": "negative",
                                  "main_topic": "Новый регуляторный риск",
                                  "category_ru": "Регулирование",
                                  "relevance_score": 8,
                                  "is_business_critical": true,
                                  "article_essence_ru": "Регулятор обсуждает новые правила для сервиса.",
                                  "mention_context_ru": "inDrive прямо упомянут в новости о рынке.",
                                  "pm_importance_ru": "Нужно проверить влияние на onboarding и compliance.",
                                  "summary": "Новые правила для inDrive.",
                                  "pm_insight": "Проверить локальные ограничения."
                                }
                                """
                            )
                        )
                    ]
                )
            )
        )
    )

    result = analyzer.analyze_article(
        title="inDrive faces new regulation",
        text="Taxi market rule changes affect drivers.",
        url="https://example.com/regulation",
    )

    assert result["sentiment"] == "negative"
    assert result["category_ru"] == "Регулирование"
    assert result["relevance_score"] == 8
    assert result["mentions_indrive"] is True
    assert "indrive" in result["matched_terms"]["company"]
