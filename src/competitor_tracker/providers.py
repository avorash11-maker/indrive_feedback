"""Provider adapters for competitor tracker raw article collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import RawArticle
from .normalization import clean_text, extract_domain, normalize_source


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


class ProviderError(Exception):
    """Raised when a raw article provider fails."""


@dataclass(slots=True)
class ProviderRequest:
    """Input contract passed to providers."""

    competitors: Sequence[str]
    days: int
    queries: List[str] = field(default_factory=list)
    regions: Sequence[str] = field(default_factory=tuple)
    query_competitor_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def competitor_hints_for_query(self, query: str) -> tuple[str, ...]:
        hints = self.query_competitor_hints.get(query)
        if hints is not None:
            return hints
        return tuple(self.competitors)


class Provider(Protocol):
    """Contract for any competitor signal provider."""

    name: str

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        """Return raw articles for the given request."""


class BaseHttpProvider:
    """Shared HTTP setup for provider adapters."""

    name = "base"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    @staticmethod
    def _article(
        *,
        title: Optional[str],
        url: Optional[str],
        source: Optional[str],
        published_at: Optional[str],
        snippet: Optional[str],
        query: str,
        provider: str,
        competitor_hints: Sequence[str],
    ) -> RawArticle:
        return RawArticle(
            title=clean_text(title),
            url=str(url or ""),
            provider=provider,
            source=normalize_source(source, url),
            published_at=published_at or "",
            snippet=clean_text(snippet),
            query=clean_text(query),
            competitor_hints=tuple(competitor_hints),
        )

    @staticmethod
    def _html_to_text(value: str) -> str:
        return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())

    @staticmethod
    def _xml_text(node: ElementTree.Element, tag: str) -> str:
        found = node.find(tag)
        return found.text.strip() if found is not None and found.text else ""


class GoogleNewsRssProvider(BaseHttpProvider):
    """Low-cost Google News RSS provider."""

    name = "google_news_rss"

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles: List[RawArticle] = []
        for query in request.queries:
            articles.extend(
                self._fetch_query(
                    query=query,
                    days=request.days,
                    competitor_hints=request.competitor_hints_for_query(query),
                )
            )
        return articles

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _fetch_query(
        self,
        *,
        query: str,
        days: int,
        competitor_hints: Sequence[str],
    ) -> List[RawArticle]:
        rss_url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query)}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = self.session.get(rss_url, timeout=20)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item")
            articles: List[RawArticle] = []
            for item in items[:75]:
                title = self._xml_text(item, "title")
                link = self._xml_text(item, "link")
                source = self._xml_text(item, "source") or extract_domain(link)
                articles.append(
                    self._article(
                        title=title,
                        url=link,
                        source=source,
                        published_at=self._xml_text(item, "pubDate"),
                        snippet=self._html_to_text(self._xml_text(item, "description")),
                        query=query,
                        provider=self.name,
                        competitor_hints=competitor_hints,
                    )
                )
            return [article for article in articles if article.url]
        except Exception as exc:
            raise ProviderError(
                f"Failed to fetch from {self.name} for query '{query}'"
            ) from exc


class GdeltProvider(BaseHttpProvider):
    """Low-cost GDELT provider."""

    name = "gdelt"

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles: List[RawArticle] = []
        for query in request.queries:
            articles.extend(
                self._fetch_query(
                    query=query,
                    days=request.days,
                    competitor_hints=request.competitor_hints_for_query(query),
                )
            )
        return articles

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _fetch_query(
        self,
        *,
        query: str,
        days: int,
        competitor_hints: Sequence[str],
    ) -> List[RawArticle]:
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 75,
            "sort": "datedesc",
            "timespan": f"{days}d",
        }
        try:
            response = self.session.get(url, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()
            return [
                self._article(
                    title=item.get("title"),
                    url=item.get("url"),
                    source=item.get("domain") or extract_domain(item.get("url")),
                    published_at=item.get("seendate"),
                    snippet=item.get("snippet") or "",
                    query=query,
                    provider=self.name,
                    competitor_hints=competitor_hints,
                )
                for item in data.get("articles", [])
                if item.get("url")
            ]
        except Exception as exc:
            raise ProviderError(
                f"Failed to fetch from {self.name} for query '{query}'"
            ) from exc


def build_providers(
    enabled_provider_names: Sequence[str],
    *,
    session: Optional[requests.Session] = None,
) -> List[Provider]:
    """Build provider adapters for configured provider names."""
    provider_map = {
        "google_news_rss": GoogleNewsRssProvider,
        "gdelt": GdeltProvider,
    }
    providers: List[Provider] = []
    for provider_name in enabled_provider_names:
        provider_class = provider_map.get(provider_name)
        if provider_class is None:
            continue
        providers.append(provider_class(session=session))
    return providers
