"""Provider adapters for competitor tracker raw article collection."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import RawArticle
from .normalization import clean_text, extract_domain, normalize_source


logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
GOOGLE_NEWS_MAX_ITEMS = 75
GDELT_MAX_RECORDS = 75
NEWSAPI_PAGE_SIZE = 50


class ProviderError(Exception):
    """Raised when a raw article provider fails."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Optional[dict[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


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

    @staticmethod
    def _sanitize_url(
        url: str,
        *,
        redacted_params: Sequence[str] = (),
    ) -> str:
        parts = urlsplit(url)
        query_params = parse_qsl(parts.query, keep_blank_values=True)
        sanitized_query = urlencode(
            [
                (key, "***" if key in set(redacted_params) else value)
                for key, value in query_params
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, sanitized_query, parts.fragment))

    @classmethod
    def _build_request_url(
        cls,
        *,
        url: str,
        params: Optional[dict[str, object]] = None,
        redacted_params: Sequence[str] = (),
    ) -> str:
        prepared = requests.Request("GET", url, params=params).prepare()
        return cls._sanitize_url(prepared.url, redacted_params=redacted_params)

    @staticmethod
    def _http_status(response: Optional[requests.Response]) -> Optional[int]:
        return getattr(response, "status_code", None)

    @staticmethod
    def _query_diagnostic(
        *,
        provider: str,
        query: str,
        request_url: str,
        items_found: int = 0,
        items_after_filter: int = 0,
        http_status: Optional[int] = None,
        exception: str = "",
        status: str = "ok",
    ) -> dict[str, object]:
        return {
            "provider": provider,
            "query": query,
            "request_url": request_url,
            "http_status": http_status,
            "exception": clean_text(exception),
            "items_found": int(items_found),
            "items_after_filter": int(items_after_filter),
            "status": status,
        }

    @staticmethod
    def _raise_provider_error(
        *,
        provider: str,
        query: str,
        exc: Exception,
        diagnostics: Optional[dict[str, object]] = None,
    ) -> None:
        raise ProviderError(
            f"Failed to fetch from {provider} for query '{query}': {clean_text(str(exc)) or exc.__class__.__name__}",
            diagnostics=diagnostics,
        ) from exc


class UnsupportedProvider:
    """Placeholder that makes unknown configured providers visible at runtime."""

    def __init__(self, provider_name: str) -> None:
        self.name = provider_name

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        raise ProviderError(
            f"Provider '{self.name}' is enabled in config but is not supported by competitor_tracker"
        )

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        diagnostic = {
            "provider": self.name,
            "status": "error",
            "queries": [
                {
                    "provider": self.name,
                    "query": query,
                    "request_url": "",
                    "http_status": None,
                    "exception": (
                        f"Provider '{self.name}' is enabled in config but is not supported by competitor_tracker"
                    ),
                    "items_found": 0,
                    "items_after_filter": 0,
                    "status": "error",
                }
                for query in request.queries
            ],
            "items_found": 0,
            "items_after_filter": 0,
            "items_after_global_dedup": 0,
        }
        raise ProviderError(
            f"Provider '{self.name}' is enabled in config but is not supported by competitor_tracker",
            diagnostics=diagnostic,
        )


class NewsApiProvider(BaseHttpProvider):
    """NewsAPI provider with conservative page sizing to avoid quota spikes."""

    name = "newsapi"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        api_key: Optional[str] = None,
    ) -> None:
        super().__init__(session=session)
        self.api_key = api_key or os.getenv("NEWS_API_KEY", "").strip()

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, _ = self.fetch_with_diagnostics(request)
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        articles: List[RawArticle] = []
        query_diagnostics: list[dict[str, object]] = []
        for query in request.queries:
            fetched, diagnostic = self._fetch_query(
                query=query,
                competitor_hints=request.competitor_hints_for_query(query),
            )
            articles.extend(fetched)
            query_diagnostics.append(diagnostic)
        return articles, {
            "provider": self.name,
            "status": "ok",
            "queries": query_diagnostics,
            "items_found": sum(int(item["items_found"]) for item in query_diagnostics),
            "items_after_filter": sum(int(item["items_after_filter"]) for item in query_diagnostics),
            "items_after_global_dedup": 0,
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _fetch_query(
        self,
        *,
        query: str,
        competitor_hints: Sequence[str],
    ) -> tuple[List[RawArticle], dict[str, object]]:
        if not self.api_key:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url="https://newsapi.org/v2/everything",
                exception="NEWS_API_KEY is missing for enabled provider 'newsapi'",
                status="error",
            )
            raise ProviderError(
                "Failed to fetch from newsapi: NEWS_API_KEY is missing for enabled provider 'newsapi'",
                diagnostics={
                    "provider": self.name,
                    "status": "error",
                    "queries": [diagnostic],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                },
            )

        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": NEWSAPI_PAGE_SIZE,
            "apiKey": self.api_key,
        }
        request_url = self._build_request_url(
            url=url,
            params=params,
            redacted_params=("apiKey",),
        )
        response = None
        try:
            response = self.session.get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "error":
                message = clean_text(data.get("message") or "unknown error")
                code = clean_text(data.get("code") or "")
                details = f" [{code}]" if code else ""
                diagnostic = self._query_diagnostic(
                    provider=self.name,
                    query=query,
                    request_url=request_url,
                    http_status=self._http_status(response),
                    exception=f"{message}{details}",
                    status="error",
                )
                raise ProviderError(
                    f"Failed to fetch from newsapi for query '{query}': {message}{details}",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            found_items = list(data.get("articles", []))
            articles = [
                self._article(
                    title=item.get("title"),
                    url=item.get("url"),
                    source=(item.get("source") or {}).get("name") or "NewsAPI",
                    published_at=item.get("publishedAt"),
                    snippet=item.get("description") or item.get("content") or "",
                    query=query,
                    provider=self.name,
                    competitor_hints=competitor_hints,
                )
                for item in found_items
                if item.get("url")
            ]
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                items_found=len(found_items),
                items_after_filter=len(articles),
            )
            return articles, diagnostic
        except ProviderError:
            raise
        except Exception as exc:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                exception=str(exc),
                status="error",
            )
            self._raise_provider_error(
                provider=self.name,
                query=query,
                exc=exc,
                diagnostics={
                    "provider": self.name,
                    "status": "error",
                    "queries": [diagnostic],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                },
            )


class GoogleNewsRssProvider(BaseHttpProvider):
    """Low-cost Google News RSS provider."""

    name = "google_news_rss"

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, _ = self.fetch_with_diagnostics(request)
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        articles: List[RawArticle] = []
        query_diagnostics: list[dict[str, object]] = []
        for query in request.queries:
            fetched, diagnostic = self._fetch_query(
                query=query,
                days=request.days,
                competitor_hints=request.competitor_hints_for_query(query),
            )
            articles.extend(fetched)
            query_diagnostics.append(diagnostic)
        return articles, {
            "provider": self.name,
            "status": "ok",
            "queries": query_diagnostics,
            "items_found": sum(int(item["items_found"]) for item in query_diagnostics),
            "items_after_filter": sum(int(item["items_after_filter"]) for item in query_diagnostics),
            "items_after_global_dedup": 0,
        }

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
    ) -> tuple[List[RawArticle], dict[str, object]]:
        rss_url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query)}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
        )
        response = None
        try:
            response = self.session.get(rss_url, timeout=20)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item")
            articles: List[RawArticle] = []
            for item in items[:GOOGLE_NEWS_MAX_ITEMS]:
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
            filtered_articles = [article for article in articles if article.url]
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=rss_url,
                http_status=self._http_status(response),
                items_found=len(items),
                items_after_filter=len(filtered_articles),
            )
            return filtered_articles, diagnostic
        except Exception as exc:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=rss_url,
                http_status=self._http_status(response),
                exception=str(exc),
                status="error",
            )
            self._raise_provider_error(
                provider=self.name,
                query=query,
                exc=exc,
                diagnostics={
                    "provider": self.name,
                    "status": "error",
                    "queries": [diagnostic],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                },
            )


class GdeltProvider(BaseHttpProvider):
    """Low-cost GDELT provider."""

    name = "gdelt"

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, _ = self.fetch_with_diagnostics(request)
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        articles: List[RawArticle] = []
        query_diagnostics: list[dict[str, object]] = []
        for query in request.queries:
            fetched, diagnostic = self._fetch_query(
                query=query,
                days=request.days,
                competitor_hints=request.competitor_hints_for_query(query),
            )
            articles.extend(fetched)
            query_diagnostics.append(diagnostic)
        return articles, {
            "provider": self.name,
            "status": "ok",
            "queries": query_diagnostics,
            "items_found": sum(int(item["items_found"]) for item in query_diagnostics),
            "items_after_filter": sum(int(item["items_after_filter"]) for item in query_diagnostics),
            "items_after_global_dedup": 0,
        }

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
    ) -> tuple[List[RawArticle], dict[str, object]]:
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": GDELT_MAX_RECORDS,
            "sort": "datedesc",
            "timespan": f"{days}d",
        }
        request_url = self._build_request_url(url=url, params=params)
        response = None
        try:
            response = self.session.get(url, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()
            found_items = list(data.get("articles", []))
            articles = [
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
                for item in found_items
                if item.get("url")
            ]
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                items_found=len(found_items),
                items_after_filter=len(articles),
            )
            return articles, diagnostic
        except Exception as exc:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                exception=str(exc),
                status="error",
            )
            self._raise_provider_error(
                provider=self.name,
                query=query,
                exc=exc,
                diagnostics={
                    "provider": self.name,
                    "status": "error",
                    "queries": [diagnostic],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                },
            )


def supported_provider_names() -> tuple[str, ...]:
    """Return the canonical provider names supported by the codebase."""
    return ("newsapi", "gdelt", "google_news_rss")


def build_providers(
    enabled_provider_names: Sequence[str],
    *,
    session: Optional[requests.Session] = None,
) -> List[Provider]:
    """Build provider adapters for configured provider names."""
    provider_map = {
        "newsapi": NewsApiProvider,
        "google_news_rss": GoogleNewsRssProvider,
        "gdelt": GdeltProvider,
    }
    providers: List[Provider] = []
    for provider_name in enabled_provider_names:
        normalized_name = str(provider_name).strip().lower()
        provider_class = provider_map.get(normalized_name)
        if provider_class is None:
            logger.warning(
                "Unknown provider '%s' is enabled in config. It will be reported in provider_errors.",
                provider_name,
            )
            providers.append(UnsupportedProvider(provider_name))
            continue
        providers.append(provider_class(session=session))
    return providers
