from competitor_tracker import cli
from competitor_tracker.article_context import ArticleContextExtractor
from competitor_tracker.models import CandidateArticle, RawArticle


def build_candidate(*, title="Grab expands in Manila", url="https://example.com/article") -> CandidateArticle:
    raw_article = RawArticle(
        title=title,
        url=url,
        provider="mock_provider",
        source="Example News",
        published_at="2026-05-20T09:00:00Z",
        snippet="Grab launches a new city campaign with driver messaging.",
        query='"Grab" market entry Southeast Asia',
        region="sea",
        language="en",
        competitor_hints=("Grab",),
    )
    return CandidateArticle(
        raw_article=raw_article,
        competitor="Grab",
        topic_group="market_expansion",
        score=8,
        matched_keywords=("launch", "new city"),
        summary="Grab / market expansion / Philippines",
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        reasons=("competitor_mentioned", "topic_match:market_expansion"),
    )


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("bad response")


class FakeSession:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.headers = {}
        self._response = response

    def get(self, url: str, timeout: int):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_article_context_extractor_returns_cleaned_article_body():
    html = """
    <html>
      <body>
        <article>
          <p>Grab launched a driver support campaign in Manila with fuel subsidies and bonuses.</p>
          <p>The company framed the move as part of its public driver-care narrative.</p>
        </article>
      </body>
    </html>
    """
    extractor = ArticleContextExtractor(session=FakeSession(FakeResponse(html)))

    context = extractor.extract(build_candidate())

    assert context.title == "Grab expands in Manila"
    assert context.source_url == "https://example.com/article"
    assert "driver support campaign in Manila" in context.article_body
    assert "public driver-care narrative" in context.article_body


def test_article_context_extractor_falls_back_without_crashing():
    extractor = ArticleContextExtractor(session=FakeSession(RuntimeError("network down")))

    context = extractor.extract(build_candidate())

    assert context.title == "Grab expands in Manila"
    assert context.snippet == "Grab launches a new city campaign with driver messaging."
    assert context.source_url == "https://example.com/article"
    assert context.article_body == ""


def test_build_delivery_alert_schemas_only_extracts_context_for_llm_top_n(monkeypatch):
    alerts = [build_candidate(title=f"Grab signal {index}", url=f"https://example.com/{index}").to_alert() for index in range(4)]
    extractor_calls = []

    class FakeExtractor:
        def extract(self, candidate):
            extractor_calls.append(candidate.title)
            return type(
                "Context",
                (),
                {
                    "title": candidate.title,
                    "snippet": candidate.raw_article.snippet,
                    "source_url": candidate.url,
                    "article_body": f"body for {candidate.title}",
                },
            )()

        def build_fallback_context(self, candidate):
            return type(
                "Context",
                (),
                {
                    "title": candidate.title,
                    "snippet": candidate.raw_article.snippet,
                    "source_url": candidate.url,
                    "article_body": "",
                },
            )()

    class FakeAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": article_context.article_body if article_context else "fallback",
                "why_it_matters": "ok",
                "potential_impact": "ok",
                "recommended_action": "ok",
                "confidence": 0.7,
            }

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeExtractor)
    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAnalyzer)

    alert_schemas, contexts = cli.build_delivery_alert_schemas(alerts, llm_top_n=2)

    assert extractor_calls == ["Grab signal 0", "Grab signal 1"]
    assert len(contexts) == 4
    assert contexts[0].article_body == "body for Grab signal 0"
    assert contexts[1].article_body == "body for Grab signal 1"
    assert contexts[2].article_body == ""
    assert contexts[3].article_body == ""
    assert alert_schemas[0]["what_happened"] == "body for Grab signal 0"
    assert alert_schemas[2]["what_happened"] == "fallback"
