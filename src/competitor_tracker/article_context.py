"""Safe article-context extraction for post-ranking LLM enrichment."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .models import ArticleContext, CandidateArticle
from .normalization import clean_text


logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


class ArticleContextExtractor:
    """Fetch and clean extra article text without breaking the pipeline."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: int = 12,
        max_chars: int = 8000,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout_seconds = timeout_seconds
        self.max_chars = max_chars

    def build_fallback_context(self, candidate: CandidateArticle) -> ArticleContext:
        """Return minimal article context from already-known fields."""
        return ArticleContext(
            title=clean_text(candidate.title),
            snippet=clean_text(candidate.raw_article.snippet),
            source_url=candidate.url,
            article_body="",
        )

    def extract(self, candidate: CandidateArticle) -> ArticleContext:
        """Return best-effort context for one candidate without raising."""
        fallback = self.build_fallback_context(candidate)
        if not self._is_fetchable_url(candidate.url):
            return fallback

        try:
            response = self.session.get(candidate.url, timeout=self.timeout_seconds)
            response.raise_for_status()
            article_body = self._extract_page_text(response.text)
            if not article_body:
                return fallback
            return ArticleContext(
                title=fallback.title,
                snippet=fallback.snippet,
                source_url=fallback.source_url,
                article_body=article_body[: self.max_chars],
            )
        except Exception as exc:
            logger.info(
                "Article context extraction failed; using title/snippet/url fallback. url=%r error=%s",
                candidate.url,
                exc,
            )
            return fallback

    @staticmethod
    def _is_fetchable_url(url: str) -> bool:
        parsed = urlparse(url or "")
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _extract_page_text(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for selector in ("script", "style", "noscript", "svg", "iframe"):
            for node in soup.select(selector):
                node.decompose()

        candidate_blocks = []
        article_node = soup.find("article")
        if article_node is not None:
            candidate_blocks.append(article_node)
        main_node = soup.find("main")
        if main_node is not None:
            candidate_blocks.append(main_node)
        body_node = soup.body
        if body_node is not None:
            candidate_blocks.append(body_node)

        for block in candidate_blocks:
            paragraphs = [
                clean_text(node.get_text(" ", strip=True))
                for node in block.find_all(["p", "li"])
            ]
            paragraphs = [value for value in paragraphs if len(value) >= 40]
            if paragraphs:
                return "\n".join(paragraphs)

        fallback_text = clean_text(soup.get_text(" ", strip=True))
        return fallback_text if len(fallback_text) >= 80 else ""
