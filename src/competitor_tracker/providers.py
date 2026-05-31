"""Provider adapters for competitor tracker raw article collection."""

from __future__ import annotations

import logging
import os
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .environment import get_env_value
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
GDELT_MIN_REQUEST_INTERVAL_SECONDS = 5.0
GDELT_COOLDOWN_SECONDS = 60
NEWSAPI_PAGE_SIZE = 50
NEWSAPI_DAILY_REQUEST_LIMIT = 90
NEWSAPI_CACHE_TTL_SECONDS = 600
NEWSAPI_COOLDOWN_SECONDS = 900
GUARDIAN_PAGE_SIZE = 20
GUARDIAN_DAILY_REQUEST_LIMIT = 450
GUARDIAN_CACHE_TTL_SECONDS = 900
GUARDIAN_COOLDOWN_SECONDS = 900
SHORT_COMPETITOR_CONTEXT_TERMS = (
    "app",
    "apps",
    "ride",
    "rides",
    "rider",
    "riders",
    "taxi",
    "driver",
    "drivers",
    "mobility",
    "transport",
)


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
        metadata: Optional[dict[str, str]] = None,
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
            metadata=metadata or {},
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
    def _response_text(response: Optional[requests.Response]) -> str:
        if response is None:
            return ""
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text
        content = getattr(response, "content", b"")
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _response_content_type(response: Optional[requests.Response]) -> str:
        if response is None:
            return ""
        headers = getattr(response, "headers", {}) or {}
        if isinstance(headers, dict):
            value = headers.get("Content-Type") or headers.get("content-type") or ""
            return str(value).strip().lower()
        return ""

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


class JsonBudgetCacheMixin:
    """Small reusable file-backed cache/budget helpers for capped providers."""

    cache_path: Path
    budget_path: Path
    cache_ttl_seconds: int
    daily_request_limit: int
    cooldown_seconds: int
    name: str

    def _read_json_file(self, path: Path) -> dict[str, object]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable JSON state file: %s", path)
            return {}

    def _write_json_file(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _load_cached_query(
        self,
        *,
        query: str,
        competitor_hints: Sequence[str],
        request_url: str,
    ) -> Optional[tuple[List[RawArticle], dict[str, object]]]:
        if self.cache_ttl_seconds <= 0:
            return None
        payload = self._read_json_file(self.cache_path)
        cache = payload.get("queries")
        if not isinstance(cache, dict):
            return None
        entry = cache.get(query)
        if not isinstance(entry, dict):
            return None
        expires_at_raw = str(entry.get("expires_at") or "").strip()
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= self._utc_now():
            cache.pop(query, None)
            self._write_json_file(self.cache_path, payload)
            return None
        articles_payload = entry.get("articles")
        if not isinstance(articles_payload, list):
            return None
        articles = [
            RawArticle(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                provider=self.name,
                source=str(item.get("source") or ""),
                published_at=item.get("published_at"),
                snippet=str(item.get("snippet") or ""),
                query=query,
                region=item.get("region"),
                language=item.get("language"),
                competitor_hints=tuple(item.get("competitor_hints") or competitor_hints),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in articles_payload
            if isinstance(item, dict)
        ]
        stored_diagnostic = entry.get("diagnostic")
        http_status = None
        if isinstance(stored_diagnostic, dict):
            http_status = stored_diagnostic.get("http_status")
        diagnostic = BaseHttpProvider._query_diagnostic(
            provider=self.name,
            query=query,
            request_url=request_url,
            http_status=http_status,
            items_found=len(articles),
            items_after_filter=len(articles),
            status="cached",
        )
        diagnostic["cached"] = True
        return articles, diagnostic

    def _store_cached_query(
        self,
        *,
        query: str,
        articles: Sequence[RawArticle],
        diagnostic: dict[str, object],
    ) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        payload = self._read_json_file(self.cache_path)
        cache = payload.get("queries")
        if not isinstance(cache, dict):
            cache = {}
        cache[query] = {
            "expires_at": (
                self._utc_now() + timedelta(seconds=self.cache_ttl_seconds)
            ).isoformat(),
            "articles": [
                {
                    "title": article.title,
                    "url": article.url,
                    "source": article.source,
                    "published_at": article.published_at,
                    "snippet": article.snippet,
                    "region": article.region,
                    "language": article.language,
                    "competitor_hints": list(article.competitor_hints),
                    "metadata": dict(article.metadata),
                }
                for article in articles
            ],
            "diagnostic": {
                "request_url": diagnostic.get("request_url", ""),
                "http_status": diagnostic.get("http_status"),
            },
        }
        payload["queries"] = cache
        self._write_json_file(self.cache_path, payload)

    def _reserve_request_budget(
        self,
        *,
        query: str,
        request_url: str,
        cooldown_message: str,
    ) -> None:
        payload = self._read_json_file(self.budget_path)
        today = self._utc_now().date().isoformat()
        if payload.get("day") != today:
            payload = {"day": today, "count": 0, "cooldown_until": ""}
        cooldown_until_raw = str(payload.get("cooldown_until") or "").strip()
        if cooldown_until_raw:
            try:
                cooldown_until = datetime.fromisoformat(cooldown_until_raw)
            except ValueError:
                cooldown_until = self._utc_now()
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
            if cooldown_until > self._utc_now():
                diagnostic = BaseHttpProvider._query_diagnostic(
                    provider=self.name,
                    query=query,
                    request_url=request_url,
                    exception=cooldown_message,
                    status="error",
                )
                diagnostic["cooldown_hit"] = True
                raise ProviderError(
                    f"Failed to fetch from {self.name}: cooldown is active after rate limiting",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
        count = int(payload.get("count") or 0)
        if count >= self.daily_request_limit:
            diagnostic = BaseHttpProvider._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                exception=(
                    f"{self.name} daily request limit reached ({self.daily_request_limit})."
                ),
                status="error",
            )
            diagnostic["budget_hit"] = True
            raise ProviderError(
                f"Failed to fetch from {self.name}: daily request limit reached ({self.daily_request_limit})",
                diagnostics={
                    "provider": self.name,
                    "status": "error",
                    "queries": [diagnostic],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                },
            )
        payload["count"] = count + 1
        self._write_json_file(self.budget_path, payload)

    def _activate_cooldown(self) -> None:
        payload = self._read_json_file(self.budget_path)
        if payload.get("day") != self._utc_now().date().isoformat():
            payload["day"] = self._utc_now().date().isoformat()
            payload["count"] = int(payload.get("count") or 0)
        payload["cooldown_until"] = (
            self._utc_now() + timedelta(seconds=self.cooldown_seconds)
        ).isoformat()
        self._write_json_file(self.budget_path, payload)


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


class GuardianProvider(JsonBudgetCacheMixin, BaseHttpProvider):
    """Guardian Content API provider for non-commercial portfolio usage."""

    name = "guardian"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        api_key: Optional[str] = None,
    ) -> None:
        super().__init__(session=session)
        self.api_key = (
            get_env_value("GUARDIAN_API_KEY")
            if api_key is None
            else api_key.strip()
        )
        output_dir = Path(
            os.getenv("COMPETITOR_TRACKER_OUTPUT_DIR", "output/competitor_tracker")
        )
        self.cache_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_GUARDIAN_CACHE_PATH",
                str(output_dir / "guardian_cache.json"),
            )
        )
        self.budget_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_GUARDIAN_BUDGET_PATH",
                str(output_dir / "guardian_budget.json"),
            )
        )
        self.daily_request_limit = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_GUARDIAN_DAILY_REQUEST_LIMIT",
                    str(GUARDIAN_DAILY_REQUEST_LIMIT),
                )
            ),
        )
        self.cache_ttl_seconds = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_GUARDIAN_CACHE_TTL_SECONDS",
                    str(GUARDIAN_CACHE_TTL_SECONDS),
                )
            ),
        )
        self.cooldown_seconds = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_GUARDIAN_COOLDOWN_SECONDS",
                    str(GUARDIAN_COOLDOWN_SECONDS),
                )
            ),
        )

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, _ = self.fetch_with_diagnostics(request)
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        if not self.api_key:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query="guardian-content-api",
                request_url="https://content.guardianapis.com/search",
                exception="GUARDIAN_API_KEY is missing; Guardian provider skipped.",
                status="skipped",
            )
            return [], {
                "provider": self.name,
                "status": "skipped",
                "queries": [diagnostic],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

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
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _fetch_query(
        self,
        *,
        query: str,
        days: int,
        competitor_hints: Sequence[str],
    ) -> tuple[List[RawArticle], dict[str, object]]:
        url = "https://content.guardianapis.com/search"
        params = {
            "q": query,
            "api-key": self.api_key,
            "page-size": GUARDIAN_PAGE_SIZE,
            "order-by": "newest",
            "show-fields": "trailText,bodyText",
            "from-date": (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat(),
        }
        request_url = self._build_request_url(
            url=url,
            params=params,
            redacted_params=("api-key",),
        )
        cached = self._load_cached_query(
            query=query,
            competitor_hints=competitor_hints,
            request_url=request_url,
        )
        if cached is not None:
            return cached
        self._reserve_request_budget(
            query=query,
            request_url=request_url,
            cooldown_message="Guardian cooldown is active after a rate limit response.",
        )
        response = None
        try:
            response = self.session.get(url, params=params, timeout=20)
            if self._http_status(response) == 429:
                diagnostic = self._query_diagnostic(
                    provider=self.name,
                    query=query,
                    request_url=request_url,
                    http_status=429,
                    exception="Guardian rate limit hit; cooldown activated.",
                    status="error",
                )
                diagnostic["cooldown_hit"] = True
                self._activate_cooldown()
                raise ProviderError(
                    f"Failed to fetch from guardian for query '{query}': rate limit hit [429]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            response.raise_for_status()
            data = response.json()
            response_payload = data.get("response") or {}
            found_items = list(response_payload.get("results", []))
            articles = [
                self._article(
                    title=item.get("webTitle"),
                    url=item.get("webUrl"),
                    source="The Guardian",
                    published_at=item.get("webPublicationDate"),
                    snippet=(item.get("fields") or {}).get("trailText")
                    or (item.get("fields") or {}).get("bodyText")
                    or "",
                    query=query,
                    provider=self.name,
                    competitor_hints=competitor_hints,
                    metadata={
                        "source_tier": "tier2_direct",
                    },
                )
                for item in found_items
                if item.get("webUrl")
            ]
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                items_found=len(found_items),
                items_after_filter=len(articles),
            )
            self._store_cached_query(
                query=query,
                articles=articles,
                diagnostic=diagnostic,
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


class RegionalRssProvider(BaseHttpProvider):
    """Curated regional RSS provider with direct-feed source matching."""

    name = "regional_rss"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        feeds_by_region: Optional[dict[str, Sequence[object]]] = None,
        competitor_aliases: Optional[dict[str, Sequence[str]]] = None,
    ) -> None:
        super().__init__(session=session)
        self.feeds_by_region = dict(feeds_by_region or {})
        self.competitor_aliases = {
            str(key): tuple(value)
            for key, value in (competitor_aliases or {}).items()
        }

    def configure(
        self,
        *,
        feeds_by_region: dict[str, Sequence[object]],
        competitor_aliases: dict[str, Sequence[str]],
    ) -> None:
        self.feeds_by_region = dict(feeds_by_region)
        self.competitor_aliases = {
            str(key): tuple(value)
            for key, value in competitor_aliases.items()
        }

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, _ = self.fetch_with_diagnostics(request)
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        feed_specs = list(self._iter_feed_specs(request.regions))
        if not feed_specs:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query="regional_rss",
                request_url="",
                exception="No curated RSS feeds configured for requested regions.",
                status="skipped",
            )
            return [], {
                "provider": self.name,
                "status": "skipped",
                "queries": [diagnostic],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
                "feeds_skipped": 0,
            }

        articles: List[RawArticle] = []
        query_diagnostics: list[dict[str, object]] = []
        feeds_skipped = 0
        for region_key, feed in feed_specs:
            fetched, diagnostic = self._fetch_feed(
                region_key=region_key,
                feed=feed,
                competitors=request.competitors,
            )
            articles.extend(fetched)
            if int(diagnostic.get("items_after_filter") or 0) == 0:
                feeds_skipped += 1
            query_diagnostics.append(diagnostic)
        return articles, {
            "provider": self.name,
            "status": "ok",
            "queries": query_diagnostics,
            "items_found": sum(int(item["items_found"]) for item in query_diagnostics),
            "items_after_filter": sum(int(item["items_after_filter"]) for item in query_diagnostics),
            "items_after_global_dedup": 0,
            "feeds_skipped": feeds_skipped,
        }

    def _iter_feed_specs(self, regions: Sequence[str]) -> Iterable[tuple[str, object]]:
        seen = set()
        for region in regions:
            for feed in self.feeds_by_region.get(region, ()):
                feed_key = (region, getattr(feed, "url", ""))
                if feed_key in seen:
                    continue
                seen.add(feed_key)
                yield region, feed

    def _fetch_feed(
        self,
        *,
        region_key: str,
        feed: object,
        competitors: Sequence[str],
    ) -> tuple[List[RawArticle], dict[str, object]]:
        feed_name = str(getattr(feed, "name", "") or region_key)
        feed_url = str(getattr(feed, "url", "") or "")
        feed_language = str(getattr(feed, "language", "") or "")
        response = None
        try:
            response = self.session.get(feed_url, timeout=20)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item")
            found_items = len(items)
            articles: List[RawArticle] = []
            for item in items[:GOOGLE_NEWS_MAX_ITEMS]:
                title = self._xml_text(item, "title")
                link = self._xml_text(item, "link")
                snippet = self._html_to_text(self._xml_text(item, "description"))
                source = self._xml_text(item, "source") or feed_name or extract_domain(link)
                matched_competitor = self._match_competitor(
                    title=title,
                    snippet=snippet,
                    source=source,
                    competitors=competitors,
                )
                if not matched_competitor:
                    continue
                articles.append(
                    self._article(
                        title=title,
                        url=link,
                        source=source,
                        published_at=self._xml_text(item, "pubDate"),
                        snippet=snippet,
                        query=f"regional_rss::{region_key}::{matched_competitor}",
                        provider=self.name,
                        competitor_hints=(matched_competitor,),
                        metadata={
                            "query_owner_competitor": matched_competitor,
                            "query_owner_region": region_key,
                            "source_tier": "tier2_direct",
                            "direct_feed_name": feed_name,
                            "direct_feed_url": feed_url,
                        },
                    )
                )
                if feed_language:
                    articles[-1] = RawArticle(
                        title=articles[-1].title,
                        url=articles[-1].url,
                        provider=articles[-1].provider,
                        source=articles[-1].source,
                        published_at=articles[-1].published_at,
                        snippet=articles[-1].snippet,
                        query=articles[-1].query,
                        region=region_key,
                        language=feed_language,
                        competitor_hints=articles[-1].competitor_hints,
                        metadata=articles[-1].metadata,
                    )
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=f"{region_key}:{feed_name}",
                request_url=feed_url,
                http_status=self._http_status(response),
                items_found=found_items,
                items_after_filter=len(articles),
            )
            diagnostic["feed_name"] = feed_name
            diagnostic["feed_url"] = feed_url
            diagnostic["feed_region"] = region_key
            return articles, diagnostic
        except Exception as exc:
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=f"{region_key}:{feed_name}",
                request_url=feed_url,
                http_status=self._http_status(response),
                exception=str(exc),
                status="error",
            )
            diagnostic["feed_name"] = feed_name
            diagnostic["feed_url"] = feed_url
            diagnostic["feed_region"] = region_key
            self._raise_provider_error(
                provider=self.name,
                query=f"{region_key}:{feed_name}",
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

    def _match_competitor(
        self,
        *,
        title: str,
        snippet: str,
        source: str,
        competitors: Sequence[str],
    ) -> str:
        text_blob = " ".join(
            value.casefold()
            for value in (title, snippet, source)
            if value
        )
        for competitor in competitors:
            aliases = self.competitor_aliases.get(competitor, ())
            for candidate in (competitor, *aliases):
                normalized = " ".join(str(candidate).casefold().split())
                if self._matches_competitor_token(text_blob, normalized):
                    return competitor
        return ""

    def _matches_competitor_token(self, text_blob: str, candidate: str) -> bool:
        if not candidate:
            return False
        if self._is_context_required_token(candidate):
            return self._matches_contextual_short_token(text_blob, candidate)
        return bool(self._compile_competitor_pattern(candidate).search(text_blob))

    def _is_context_required_token(self, candidate: str) -> bool:
        compact = re.sub(r"[\W_]+", "", candidate)
        if not compact:
            return False
        return compact.isdigit() or len(compact) <= 2

    def _matches_contextual_short_token(self, text_blob: str, candidate: str) -> bool:
        token_pattern = self._token_pattern(candidate)
        context_pattern = "|".join(re.escape(term) for term in SHORT_COMPETITOR_CONTEXT_TERMS)
        pattern = re.compile(
            rf"(?:{token_pattern}(?!\s*[%$])(?:\W+\w+){{0,3}}\W+\b(?:{context_pattern})\b)"
            rf"|(?:\b(?:{context_pattern})\b(?:\W+\w+){{0,3}}\W+{token_pattern}(?![%$]))",
            re.IGNORECASE,
        )
        return bool(pattern.search(text_blob))

    def _compile_competitor_pattern(self, candidate: str) -> re.Pattern[str]:
        return re.compile(self._token_pattern(candidate), re.IGNORECASE)

    def _token_pattern(self, candidate: str) -> str:
        parts = [re.escape(part) for part in candidate.split() if part]
        if not parts:
            return r"$^"
        joined = r"(?:\W|_)".join(parts) if len(parts) > 1 else parts[0]
        return rf"(?<!\w){joined}(?!\w)"


class NewsApiProvider(JsonBudgetCacheMixin, BaseHttpProvider):
    """NewsAPI provider with conservative page sizing to avoid quota spikes."""

    name = "newsapi"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        api_key: Optional[str] = None,
    ) -> None:
        super().__init__(session=session)
        self.api_key = (
            get_env_value("NEWS_API_KEY")
            if api_key is None
            else api_key.strip()
        )
        output_dir = Path(
            os.getenv("COMPETITOR_TRACKER_OUTPUT_DIR", "output/competitor_tracker")
        )
        self.cache_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",
                str(output_dir / "newsapi_cache.json"),
            )
        )
        self.budget_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_NEWSAPI_BUDGET_PATH",
                str(output_dir / "newsapi_budget.json"),
            )
        )
        self.daily_request_limit = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_NEWSAPI_DAILY_REQUEST_LIMIT",
                    str(NEWSAPI_DAILY_REQUEST_LIMIT),
                )
            ),
        )
        self.cache_ttl_seconds = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_NEWSAPI_CACHE_TTL_SECONDS",
                    str(NEWSAPI_CACHE_TTL_SECONDS),
                )
            ),
        )
        self.cooldown_seconds = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_NEWSAPI_COOLDOWN_SECONDS",
                    str(NEWSAPI_COOLDOWN_SECONDS),
                )
            ),
        )

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
        cached = self._load_cached_query(
            query=query,
            competitor_hints=competitor_hints,
            request_url=request_url,
        )
        if cached is not None:
            return cached
        self._reserve_request_budget(
            query=query,
            request_url=request_url,
            cooldown_message="NewsAPI cooldown is active after a rate limit response.",
        )
        response = None
        try:
            response = self.session.get(url, params=params, timeout=20)
            if self._http_status(response) == 429:
                diagnostic = self._query_diagnostic(
                    provider=self.name,
                    query=query,
                    request_url=request_url,
                    http_status=429,
                    exception="NewsAPI rate limit hit; cooldown activated.",
                    status="error",
                )
                diagnostic["cooldown_hit"] = True
                self._activate_cooldown()
                raise ProviderError(
                    f"Failed to fetch from newsapi for query '{query}': rate limit hit [rateLimited]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
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
                if code in {"rateLimited", "apiKeyExhausted"}:
                    diagnostic["cooldown_hit"] = True
                    self._activate_cooldown()
                if code == "apiKeyExhausted":
                    diagnostic["budget_hit"] = True
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
                    metadata={"source_tier": "tier1_aggregator"},
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
            self._store_cached_query(
                query=query,
                articles=articles,
                diagnostic=diagnostic,
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

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _read_json_file(self, path: Path) -> dict[str, object]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable JSON state file: %s", path)
            return {}

    def _write_json_file(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)


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
                        metadata={"source_tier": "tier1_aggregator"},
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

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        super().__init__(session=session)
        output_dir = Path(
            os.getenv("COMPETITOR_TRACKER_OUTPUT_DIR", "output/competitor_tracker")
        )
        self.rate_limit_state_path = Path(
            os.getenv(
                "COMPETITOR_TRACKER_GDELT_RATE_LIMIT_STATE_PATH",
                str(output_dir / "gdelt_rate_limit_state.json"),
            )
        )
        self.min_request_interval_seconds = max(
            0.0,
            float(
                os.getenv(
                    "COMPETITOR_TRACKER_GDELT_MIN_REQUEST_INTERVAL_SECONDS",
                    str(GDELT_MIN_REQUEST_INTERVAL_SECONDS),
                )
            ),
        )
        self.cooldown_seconds = max(
            0,
            int(
                os.getenv(
                    "COMPETITOR_TRACKER_GDELT_COOLDOWN_SECONDS",
                    str(GDELT_COOLDOWN_SECONDS),
                )
            ),
        )

    def fetch(self, request: ProviderRequest) -> List[RawArticle]:
        articles, diagnostics = self.fetch_with_diagnostics(request)
        if not articles and str(diagnostics.get("status") or "").strip().lower() in {
            "error",
            "skipped",
        }:
            query_rows = diagnostics.get("queries")
            first_query = ""
            first_exception = "GDELT provider failed."
            if isinstance(query_rows, list):
                for item in query_rows:
                    if not isinstance(item, dict):
                        continue
                    first_query = str(item.get("query") or "").strip()
                    http_status = item.get("http_status")
                    item_exception = str(item.get("exception") or "").strip()
                    if http_status == 429:
                        first_exception = "rate limit hit [429]"
                    elif item_exception:
                        first_exception = item_exception
                    if first_exception:
                        break
            raise ProviderError(
                f"Failed to fetch from gdelt for query '{first_query}': {first_exception}",
                diagnostics=diagnostics,
            )
        return articles

    def fetch_with_diagnostics(
        self,
        request: ProviderRequest,
    ) -> tuple[List[RawArticle], dict[str, object]]:
        articles: List[RawArticle] = []
        query_diagnostics: list[dict[str, object]] = []
        rate_limited = False
        for query in request.queries:
            if rate_limited:
                query_diagnostics.append(
                    self._cooldown_skip_diagnostic(
                        query=query,
                        request_url=self._build_request_url(
                            url="https://api.gdeltproject.org/api/v2/doc/doc",
                            params={
                                "query": query,
                                "mode": "artlist",
                                "format": "json",
                                "maxrecords": GDELT_MAX_RECORDS,
                                "sort": "datedesc",
                                "timespan": f"{request.days}d",
                            },
                        ),
                        exception="GDELT cooldown is active after a prior 429 response in this run.",
                    )
                )
                continue
            fetched, diagnostic = self._fetch_query(
                query=query,
                days=request.days,
                competitor_hints=request.competitor_hints_for_query(query),
            )
            articles.extend(fetched)
            query_diagnostics.append(diagnostic)
            if diagnostic.get("cooldown_hit"):
                rate_limited = True
        successful_rows = [
            item for item in query_diagnostics if str(item.get("status") or "").strip().lower() == "ok"
        ]
        error_rows = [
            item for item in query_diagnostics if str(item.get("status") or "").strip().lower() == "error"
        ]
        skipped_rows = [
            item for item in query_diagnostics if str(item.get("status") or "").strip().lower() == "skipped"
        ]
        status = "ok"
        if error_rows and successful_rows:
            status = "partial_error"
        elif error_rows:
            status = "error"
        elif skipped_rows and not successful_rows:
            status = "skipped"
        return articles, {
            "provider": self.name,
            "status": status,
            "queries": query_diagnostics,
            "items_found": sum(int(item["items_found"]) for item in query_diagnostics),
            "items_after_filter": sum(int(item["items_after_filter"]) for item in query_diagnostics),
            "items_after_global_dedup": 0,
        }

    def _fetch_query(
        self,
        *,
        query: str,
        days: int,
        competitor_hints: Sequence[str],
    ) -> tuple[List[RawArticle], dict[str, object]]:
        rate_limit_wait_seconds = 0.0

        def _build_gdelt_diagnostic(
            *,
            exception: str = "",
            status: str = "ok",
            items_found: int = 0,
            items_after_filter: int = 0,
            response_body_kind: str = "",
            response_parse_stage: str = "",
        ) -> dict[str, object]:
            raw_body = self._response_text(response)
            normalized_body = raw_body.strip()
            diagnostic = self._query_diagnostic(
                provider=self.name,
                query=query,
                request_url=request_url,
                http_status=self._http_status(response),
                exception=exception,
                status=status,
                items_found=items_found,
                items_after_filter=items_after_filter,
            )
            diagnostic["response_content_type"] = self._response_content_type(response)
            diagnostic["response_body_length"] = len(raw_body.encode("utf-8", errors="replace"))
            diagnostic["response_body_empty"] = not bool(normalized_body)
            diagnostic["response_body_kind"] = response_body_kind
            diagnostic["response_preview"] = clean_text(normalized_body[:240])
            diagnostic["response_parse_stage"] = response_parse_stage
            diagnostic["rate_limit_wait_seconds"] = round(rate_limit_wait_seconds, 3)
            return diagnostic

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
            cooldown_skip = self._active_cooldown_skip_diagnostic(
                query=query,
                request_url=request_url,
            )
            if cooldown_skip is not None:
                return [], cooldown_skip
            rate_limit_wait_seconds = self._wait_for_rate_limit_window()
            response = self.session.get(url, params=params, timeout=25)
            if self._http_status(response) == 429:
                diagnostic = _build_gdelt_diagnostic(
                    exception="GDELT rate limit hit; enforced 5-second pacing remains active.",
                    status="error",
                    response_body_kind="unknown",
                    response_parse_stage="http",
                )
                diagnostic["cooldown_hit"] = True
                self._activate_cooldown()
                return [], diagnostic
            response.raise_for_status()
            raw_text = self._response_text(response)
            normalized_text = raw_text.strip()
            content_type = self._response_content_type(response)
            if not normalized_text:
                diagnostic = _build_gdelt_diagnostic(
                    exception="empty response body",
                    status="error",
                    response_body_kind="empty",
                    response_parse_stage="body_validation",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': empty response body [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            if content_type and "json" not in content_type and normalized_text.startswith("<"):
                diagnostic = _build_gdelt_diagnostic(
                    exception="non-JSON response body",
                    status="error",
                    response_body_kind="html",
                    response_parse_stage="body_validation",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': non-JSON response body [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            if normalized_text.startswith("<"):
                diagnostic = _build_gdelt_diagnostic(
                    exception="non-JSON response body",
                    status="error",
                    response_body_kind="html",
                    response_parse_stage="body_validation",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': non-JSON response body [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            if not normalized_text.startswith(("{", "[")):
                diagnostic = _build_gdelt_diagnostic(
                    exception="non-JSON response body",
                    status="error",
                    response_body_kind="text",
                    response_parse_stage="body_validation",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': non-JSON response body [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                )
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                diagnostic = _build_gdelt_diagnostic(
                    exception="invalid JSON response",
                    status="error",
                    response_body_kind="invalid_json",
                    response_parse_stage="json_decode",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': invalid JSON response [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                ) from exc
            except ValueError as exc:
                diagnostic = _build_gdelt_diagnostic(
                    exception="invalid JSON response",
                    status="error",
                    response_body_kind="invalid_json",
                    response_parse_stage="json_decode",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': invalid JSON response [{self._http_status(response)}]",
                    diagnostics={
                        "provider": self.name,
                        "status": "error",
                        "queries": [diagnostic],
                        "items_found": 0,
                        "items_after_filter": 0,
                        "items_after_global_dedup": 0,
                    },
                ) from exc
            if not isinstance(data, dict) or not isinstance(data.get("articles", []), list):
                diagnostic = _build_gdelt_diagnostic(
                    exception="unexpected JSON structure",
                    status="error",
                    response_body_kind="json",
                    response_parse_stage="schema_validation",
                )
                raise ProviderError(
                    f"Failed to fetch from gdelt for query '{query}': unexpected JSON structure [{self._http_status(response)}]",
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
                    source=item.get("domain") or extract_domain(item.get("url")),
                    published_at=item.get("seendate"),
                    snippet=item.get("snippet") or "",
                    query=query,
                    provider=self.name,
                    competitor_hints=competitor_hints,
                    metadata={"source_tier": "tier1_aggregator"},
                )
                for item in found_items
                if item.get("url")
            ]
            diagnostic = _build_gdelt_diagnostic(
                items_found=len(found_items),
                items_after_filter=len(articles),
                response_body_kind="json",
                response_parse_stage="schema_validation",
            )
            return articles, diagnostic
        except ProviderError as exc:
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            query_rows = diagnostics.get("queries")
            if isinstance(query_rows, list) and query_rows:
                query_diagnostic = query_rows[0]
                if isinstance(query_diagnostic, dict):
                    query_diagnostic.setdefault("rate_limit_wait_seconds", round(rate_limit_wait_seconds, 3))
                    return [], query_diagnostic
            raise
        except Exception as exc:
            diagnostic = _build_gdelt_diagnostic(
                exception=str(exc),
                status="error",
                response_body_kind="unknown",
                response_parse_stage="http",
            )
            return [], diagnostic

    def _wait_for_rate_limit_window(self) -> float:
        if self.min_request_interval_seconds <= 0:
            return 0.0
        payload = self._read_rate_limit_state()
        last_request_at_raw = str(payload.get("last_request_at") or "").strip()
        waited_seconds = 0.0
        if last_request_at_raw:
            try:
                last_request_at = datetime.fromisoformat(last_request_at_raw)
            except ValueError:
                last_request_at = self._utc_now() - timedelta(seconds=self.min_request_interval_seconds)
            if last_request_at.tzinfo is None:
                last_request_at = last_request_at.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(
                (self._utc_now() - last_request_at.astimezone(timezone.utc)).total_seconds(),
                0.0,
            )
            if elapsed_seconds < self.min_request_interval_seconds:
                waited_seconds = self.min_request_interval_seconds - elapsed_seconds
                time.sleep(waited_seconds)
        self._write_rate_limit_state({"last_request_at": self._utc_now().isoformat()})
        return waited_seconds

    def _read_rate_limit_state(self) -> dict[str, object]:
        try:
            return json.loads(self.rate_limit_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable GDELT rate limit state file: %s", self.rate_limit_state_path)
            return {}

    def _write_rate_limit_state(self, payload: dict[str, object]) -> None:
        self.rate_limit_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.rate_limit_state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _activate_cooldown(self) -> None:
        payload = self._read_rate_limit_state()
        payload["last_request_at"] = self._utc_now().isoformat()
        if self.cooldown_seconds > 0:
            payload["cooldown_until"] = (
                self._utc_now() + timedelta(seconds=self.cooldown_seconds)
            ).isoformat()
        self._write_rate_limit_state(payload)

    def _active_cooldown_skip_diagnostic(
        self,
        *,
        query: str,
        request_url: str,
    ) -> dict[str, object] | None:
        payload = self._read_rate_limit_state()
        cooldown_until_raw = str(payload.get("cooldown_until") or "").strip()
        if not cooldown_until_raw:
            return None
        try:
            cooldown_until = datetime.fromisoformat(cooldown_until_raw)
        except ValueError:
            return None
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        if cooldown_until <= self._utc_now():
            return None
        return self._cooldown_skip_diagnostic(
            query=query,
            request_url=request_url,
            exception="GDELT cooldown is active after a prior 429 response.",
        )

    def _cooldown_skip_diagnostic(
        self,
        *,
        query: str,
        request_url: str,
        exception: str,
    ) -> dict[str, object]:
        diagnostic = self._query_diagnostic(
            provider=self.name,
            query=query,
            request_url=request_url,
            exception=exception,
            status="skipped",
        )
        diagnostic["cooldown_hit"] = True
        diagnostic["rate_limit_wait_seconds"] = 0.0
        diagnostic["response_content_type"] = ""
        diagnostic["response_body_length"] = 0
        diagnostic["response_body_empty"] = True
        diagnostic["response_body_kind"] = "cooldown"
        diagnostic["response_preview"] = ""
        diagnostic["response_parse_stage"] = "cooldown_guard"
        return diagnostic

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)


def supported_provider_names() -> tuple[str, ...]:
    """Return the canonical provider names supported by the codebase."""
    return ("newsapi", "gdelt", "google_news_rss", "guardian", "regional_rss")


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
        "guardian": GuardianProvider,
        "regional_rss": RegionalRssProvider,
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
