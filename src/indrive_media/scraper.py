import csv
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from requests import HTTPError

from .analyzer import MentionAnalyzer
from .title_matching import (
    canonical_title_tokens,
    is_semantic_title_duplicate,
    is_title_contained_duplicate,
    normalize_title,
)


load_dotenv()

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


def _log_provider_failure(provider: str, query: str, exc: Exception) -> None:
    status_code = None
    reason = str(exc)
    if isinstance(exc, HTTPError) and exc.response is not None:
        status_code = exc.response.status_code
        reason = exc.response.text[:300] or exc.response.reason

    logger.warning(
        "News provider failed; provider=%s query=%r status=%s error=%s detail=%s",
        provider,
        query,
        status_code or "n/a",
        exc.__class__.__name__,
        reason,
    )


DEFAULT_QUERIES = [
    "indrive",
    "indriver",
    "inDrive taxi ride-hailing",
    "inDrive delivery courier freight",
    "inDrive driver passenger fare pricing safety",
    "inDrive regulation license strike protest",
]


@dataclass
class Mention:
    title: str
    url: str
    source: str
    published_at: str
    snippet: str
    collected_at: str
    query: str
    provider: str
    analysis: Dict


class InDriveMentionScraper:
    def __init__(
        self,
        days: int = 30,
        output_dir: str = "output",
        min_score: int = 6,
        use_llm: bool = True,
    ):
        self.days = days
        self.min_score = min_score
        self.output_dir = Path(output_dir)
        self.news_api_key = os.getenv("NEWS_API_KEY")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            }
        )
        self.analyzer = MentionAnalyzer(use_llm=use_llm)
        self.provider_stats: Dict[str, Dict[str, object]] = {}

    def run(self, queries: Optional[List[str]] = None) -> List[Dict]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        queries = queries or DEFAULT_QUERIES

        raw_articles: List[Dict] = []
        gdelt_seen = set()
        for query in queries:
            raw_articles.extend(self._safe_provider_fetch("newsapi", self.search_newsapi, query))
            time.sleep(1)  # Rate limit: delay between API calls
            gdelt_query = "indriver" if "indriver" in query.casefold() else "indrive"
            if gdelt_query not in gdelt_seen:
                raw_articles.extend(self._safe_provider_fetch("gdelt", self.search_gdelt, query))
                gdelt_seen.add(gdelt_query)
                time.sleep(1)  # Rate limit: delay between API calls
            raw_articles.extend(self._safe_provider_fetch("google_news_rss", self.search_google_news, query))
            time.sleep(1)  # Rate limit: delay between API calls

        logger.info("Collected %s raw articles before deduplication", len(raw_articles))
        raw_articles = self._deduplicate(raw_articles)
        logger.info("Kept %s unique articles", len(raw_articles))

        mentions = []
        audit_items = []
        for article in raw_articles:
            prefilter_analysis = self.analyzer.heuristic_analysis(
                article.get("title", ""),
                article.get("snippet", ""),
                article.get("url", ""),
            )

            if int(prefilter_analysis.get("relevance_score", 0)) < self.min_score:
                audit_items.append({**article, "analysis": prefilter_analysis})
                continue

            analysis = self.analyzer.analyze_article(
                article.get("title", ""),
                article.get("snippet", ""),
                article.get("url", ""),
            )
            audited = {**article, "analysis": analysis}
            audit_items.append(audited)
            if int(analysis.get("relevance_score", 0)) >= self.min_score:
                mentions.append(Mention(analysis=analysis, **article))

        mentions.sort(
            key=lambda item: (
                int(item.analysis.get("relevance_score", 0)),
                item.published_at or "",
            ),
            reverse=True,
        )

        result = [asdict(item) for item in mentions]
        self.export(result, audit_items, self._build_run_summary(queries, result, audit_items))
        logger.info("Saved %s relevant mentions", len(result))
        return result

    def _safe_provider_fetch(self, provider_name: str, provider_call, query: str) -> List[Dict]:
        stats = self.provider_stats.setdefault(
            provider_name,
            {"attempts": 0, "successes": 0, "failures": 0, "articles": 0, "last_error": ""},
        )
        stats["attempts"] = int(stats["attempts"]) + 1
        try:
            articles = provider_call(query)
            stats["successes"] = int(stats["successes"]) + 1
            stats["articles"] = int(stats["articles"]) + len(articles)
            return articles
        except ProviderError as exc:
            stats["failures"] = int(stats["failures"]) + 1
            stats["last_error"] = str(exc)
            return []

    def _build_run_summary(
        self,
        queries: List[str],
        mentions: List[Dict],
        audit_items: List[Dict],
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": self.days,
            "min_score": self.min_score,
            "queries": queries,
            "audited_articles": len(audit_items),
            "relevant_mentions": len(mentions),
            "provider_stats": self.provider_stats,
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def search_newsapi(self, query: str) -> List[Dict]:
        if not self.news_api_key:
            return []

        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 50,
            "apiKey": self.news_api_key,
        }
        try:
            response = self.session.get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
            return [
                self._article(
                    title=item.get("title"),
                    url=item.get("url"),
                    source=(item.get("source") or {}).get("name") or "NewsAPI",
                    published_at=item.get("publishedAt"),
                    snippet=item.get("description") or item.get("content") or "",
                    query=query,
                    provider="newsapi",
                )
                for item in data.get("articles", [])
                if item.get("url")
            ]
        except Exception as exc:
            _log_provider_failure("newsapi", query, exc)
            raise ProviderError(f"Failed to fetch from newsapi for query '{query}'") from exc

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def search_gdelt(self, query: str) -> List[Dict]:
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        gdelt_query = "indriver" if "indriver" in query.casefold() else "indrive"
        params = {
            "query": gdelt_query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 75,
            "sort": "datedesc",
            "timespan": f"{self.days}d",
        }
        try:
            response = self.session.get(url, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()
            return [
                self._article(
                    title=item.get("title"),
                    url=item.get("url"),
                    source=item.get("domain") or self._domain(item.get("url")),
                    published_at=item.get("seendate"),
                    snippet=item.get("snippet") or "",
                    query=query,
                    provider="gdelt",
                )
                for item in data.get("articles", [])
                if item.get("url")
            ]
        except Exception as exc:
            _log_provider_failure("gdelt", gdelt_query, exc)
            raise ProviderError(f"Failed to fetch from gdelt for query '{query}'") from exc

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def search_google_news(self, query: str) -> List[Dict]:
        rss_url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query)}+when:{self.days}d&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = self.session.get(rss_url, timeout=20)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item")
            articles = []
            for item in items[:75]:
                title = self._xml_text(item, "title")
                link = self._xml_text(item, "link")
                source = self._xml_text(item, "source") or self._domain(link)
                articles.append(
                    self._article(
                        title=title,
                        url=link,
                        source=source,
                        published_at=self._xml_text(item, "pubDate"),
                        snippet=self._html_to_text(self._xml_text(item, "description")),
                        query=query,
                        provider="google_news_rss",
                    )
                )
            return [item for item in articles if item["url"]]
        except Exception as exc:
            _log_provider_failure("google_news_rss", query, exc)
            raise ProviderError(f"Failed to fetch from google_news_rss for query '{query}'") from exc

    def export(
        self,
        mentions: List[Dict],
        audit_items: Optional[List[Dict]] = None,
        run_summary: Optional[Dict[str, object]] = None,
    ) -> None:
        json_path = self.output_dir / "indrive_mentions.json"
        audit_path = self.output_dir / "indrive_mentions_audit.json"
        csv_path = self.output_dir / "indrive_mentions.csv"
        md_path = self.output_dir / "indrive_pm_report.md"
        summary_path = self.output_dir / "indrive_run_summary.json"

        json_path.write_text(json.dumps(mentions, ensure_ascii=False, indent=2), encoding="utf-8")
        audit_path.write_text(
            json.dumps(audit_items or mentions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary_path.write_text(
            json.dumps(run_summary or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "number",
                    "published_at",
                    "source",
                    "title",
                    "url",
                    "provider",
                    "query",
                    "relevance_score",
                    "sentiment",
                    "main_topic",
                    "summary",
                    "article_essence_ru",
                    "mention_context_ru",
                    "pm_insight",
                    "pm_importance_ru",
                    "category_ru",
                ],
            )
            writer.writeheader()
            for index, item in enumerate(mentions, start=1):
                analysis = item.get("analysis", {})
                writer.writerow(
                    {
                        "number": index,
                        "published_at": item.get("published_at", ""),
                        "source": item.get("source", ""),
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "provider": item.get("provider", ""),
                        "query": item.get("query", ""),
                        "relevance_score": analysis.get("relevance_score", ""),
                        "sentiment": analysis.get("sentiment", ""),
                        "main_topic": analysis.get("main_topic", ""),
                        "summary": analysis.get("summary", ""),
                        "article_essence_ru": analysis.get("article_essence_ru", ""),
                        "mention_context_ru": analysis.get("mention_context_ru", ""),
                        "pm_insight": analysis.get("pm_insight", ""),
                        "pm_importance_ru": analysis.get("pm_importance_ru", ""),
                        "category_ru": analysis.get("category_ru", ""),
                    }
                )

        md_path.write_text(self._markdown_report(mentions, run_summary or {}), encoding="utf-8")

    def _markdown_report(self, mentions: List[Dict], run_summary: Optional[Dict[str, object]] = None) -> str:
        provider_stats = ((run_summary or {}).get("provider_stats") or {})
        lines = [
            "# inDrive media mentions report",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Relevant mentions: {len(mentions)}",
            f"Minimum relevance score: {self.min_score}",
            "",
            "## Pipeline health",
            "",
        ]
        if provider_stats:
            for provider_name, stats in provider_stats.items():
                line = (
                    f"- {provider_name}: attempts={stats.get('attempts', 0)}, "
                    f"successes={stats.get('successes', 0)}, "
                    f"failures={stats.get('failures', 0)}, "
                    f"articles={stats.get('articles', 0)}"
                )
                if stats.get("last_error"):
                    line += f", last_error={stats.get('last_error')}"
                lines.append(line)
            lines.append("")

        lines.extend([
            "## Product manager view",
            "",
        ])
        if not mentions:
            lines.append("No relevant mentions found for the selected window.")
            return "\n".join(lines) + "\n"

        for index, item in enumerate(mentions, start=1):
            analysis = item.get("analysis", {})
            lines.extend(
                [
                    f"### {index}. {item.get('title') or 'Untitled'}",
                    "",
                    f"- Source: {item.get('source', '')}",
                    f"- Published: {item.get('published_at', '')}",
                    f"- Relevance: {analysis.get('relevance_score', '')}/10",
                    f"- Topic: {analysis.get('main_topic', '')}",
                    f"- Category: {analysis.get('category_ru', '')}",
                    f"- Sentiment: {analysis.get('sentiment', '')}",
                    f"- Article essence: {analysis.get('article_essence_ru') or analysis.get('summary', '')}",
                    f"- Mention context: {analysis.get('mention_context_ru', '')}",
                    f"- Why it matters for PM: {analysis.get('pm_importance_ru') or analysis.get('pm_insight', '')}",
                    f"- URL: {item.get('url', '')}",
                    "",
                ]
            )
        return "\n".join(lines)

    def _article(
        self,
        title: Optional[str],
        url: Optional[str],
        source: Optional[str],
        published_at: Optional[str],
        snippet: Optional[str],
        query: str,
        provider: str,
    ) -> Dict:
        return {
            "title": self._clean(title),
            "url": url or "",
            "source": self._clean(source) or self._domain(url),
            "published_at": published_at or "",
            "snippet": self._clean(snippet),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "provider": provider,
        }

    @staticmethod
    def _deduplicate(articles: Iterable[Dict]) -> List[Dict]:
        seen_urls = set()
        seen_titles: List[str] = []
        unique = []
        for article in articles:
            url_key = (article.get("url") or "").strip().casefold()
            title_key = normalize_title(article.get("title", ""))

            if url_key and url_key in seen_urls:
                continue
            if title_key and any(
                title_key == existing
                or is_title_contained_duplicate(title_key, existing)
                or is_semantic_title_duplicate(title_key, existing)
                or SequenceMatcher(None, title_key, existing).ratio() >= 0.72
                for existing in seen_titles
            ):
                continue

            if url_key:
                seen_urls.add(url_key)
            if title_key:
                seen_titles.append(title_key)
            unique.append(article)
        return unique

    @staticmethod
    def _normalize_title(title: str) -> str:
        return normalize_title(title)

    @staticmethod
    def _is_title_contained_duplicate(left: str, right: str) -> bool:
        return is_title_contained_duplicate(left, right)

    @staticmethod
    def _is_semantic_title_duplicate(left: str, right: str) -> bool:
        return is_semantic_title_duplicate(left, right)

    @staticmethod
    def _canonical_title_tokens(title: str) -> set[str]:
        return canonical_title_tokens(title)

    @staticmethod
    def _clean(value: Optional[str]) -> str:
        if not value:
            return ""
        return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ").split())

    @staticmethod
    def _html_to_text(value: str) -> str:
        return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())

    @staticmethod
    def _xml_text(node, tag: str) -> str:
        found = node.find(tag)
        return found.text.strip() if found is not None and found.text else ""

    @staticmethod
    def _domain(url: Optional[str]) -> str:
        if not url:
            return ""
        return urlparse(url).netloc.replace("www.", "")
