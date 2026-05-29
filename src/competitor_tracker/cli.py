"""Command-line entrypoint for the new competitor tracker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

from .article_context import ArticleContextExtractor
from .analyzer import (
    CompetitorAlertAnalyzer,
    CompetitorAnalyzer,
    resolve_final_publication_date,
)
from .config import TrackerConfig, TrackerRuntimeConfig
from .digest import DigestBuilder
from .environment import build_env_preflight_report, get_env_value
from .geo_policy import GeoPolicy
from .models import CompetitorDigest, RunSummary
from .normalization import (
    deduplicate_raw_articles,
    deduplicate_raw_articles_with_metrics,
    normalize_title,
    normalize_url,
    parse_published_at,
)
from .notion_sync import CompetitorNotionMirrorSync
from .providers import (
    Provider,
    ProviderError,
    ProviderRequest,
    RegionalRssProvider,
    build_providers,
)
from .storage import JsonFileStorage, SQLiteTrackerStorage
from .telegram_sender import TelegramSender

COMMAND_NAMES = {"run", "dry-run", "send-digest", "sync-notion", "backfill", "test-provider", "qa-feeds", "preflight"}
MAX_ARTICLE_AGE_DAYS = 7
HIGH_SIGNAL_SCORE_THRESHOLD = 7
logger = logging.getLogger(__name__)


def _today() -> date:
    """Return the current local date for freshness filtering."""
    return date.today()


def _utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp in ISO 8601 format."""

    return datetime.now(timezone.utc).isoformat()


def _newsapi_disabled_diagnostic(
    *,
    queries: Sequence[str],
    reason: str,
) -> dict[str, object]:
    return {
        "provider": "newsapi",
        "status": "skipped",
        "queries": [
            {
                "provider": "newsapi",
                "query": query,
                "request_url": "",
                "http_status": None,
                "exception": reason,
                "items_found": 0,
                "items_after_filter": 0,
                "status": "skipped",
            }
            for query in queries
        ],
        "items_found": 0,
        "items_after_filter": 0,
        "items_after_global_dedup": 0,
    }


def _query_history_sort_key(
    query_spec: tuple[str, str, str, str],
    *,
    precision_by_query: dict[str, float],
) -> float:
    query, _competitor, _region, _topic_name = query_spec
    return -precision_by_query.get(query, 0.0)


def _limit_newsapi_request(
    request: ProviderRequest,
    *,
    max_queries: int,
) -> tuple[ProviderRequest, Optional[str]]:
    if max_queries <= 0:
        return replace(request, queries=[]), (
            "NewsAPI skipped for full-run because COMPETITOR_TRACKER_NEWSAPI_MAX_QUERIES_PER_RUN=0."
        )
    if len(request.queries) <= max_queries:
        return request, None
    limited_queries = list(request.queries[:max_queries])
    skipped_count = len(request.queries) - len(limited_queries)
    limited_hints = {
        query: request.query_competitor_hints[query]
        for query in limited_queries
        if query in request.query_competitor_hints
    }
    return (
        replace(
            request,
            queries=limited_queries,
            query_competitor_hints=limited_hints,
        ),
        (
            f"NewsAPI query set truncated from {len(request.queries)} to {len(limited_queries)} "
            f"for this run; skipped {skipped_count} queries."
        ),
    )


def _limit_provider_request(
    request: ProviderRequest,
    *,
    provider_name: str,
    max_queries: int,
) -> tuple[ProviderRequest, Optional[str]]:
    if max_queries <= 0:
        return replace(request, queries=[]), (
            f"{provider_name} skipped for full-run because its max queries per run is 0."
        )
    if len(request.queries) <= max_queries:
        return request, None
    limited_queries = list(request.queries[:max_queries])
    skipped_count = len(request.queries) - len(limited_queries)
    limited_hints = {
        query: request.query_competitor_hints[query]
        for query in limited_queries
        if query in request.query_competitor_hints
    }
    return (
        replace(
            request,
            queries=limited_queries,
            query_competitor_hints=limited_hints,
        ),
        (
            f"{provider_name} query set truncated from {len(request.queries)} to {len(limited_queries)} "
            f"for this run; skipped {skipped_count} queries."
        ),
    )


def _build_gdelt_request(
    *,
    request: ProviderRequest,
    query_specs: Sequence[tuple[str, str, str, str]],
    config: TrackerConfig,
) -> ProviderRequest:
    simplified_queries: list[str] = []
    simplified_hints: dict[str, tuple[str, ...]] = {}
    seen_queries: set[str] = set()
    for _query, competitor, region, topic_name in query_specs:
        region_config = config.regions[region]
        topic_keywords = config.topic_groups.get(topic_name, ())
        primary_keyword = str(topic_keywords[0] if topic_keywords else topic_name.replace("_", " ")).strip()
        primary_geo = str(region_config.geo_terms[0] if region_config.geo_terms else region_config.label).strip()
        simplified_query = " ".join(
            part
            for part in (
                f'"{competitor}"',
                primary_keyword,
                primary_geo,
            )
            if part
        )
        normalized_query = " ".join(simplified_query.split())
        if not normalized_query or normalized_query in seen_queries:
            continue
        seen_queries.add(normalized_query)
        simplified_queries.append(normalized_query)
        simplified_hints[normalized_query] = (competitor,)
    if not simplified_queries:
        return request
    return replace(
        request,
        queries=simplified_queries,
        query_competitor_hints=simplified_hints,
    )


def _build_focused_api_request(
    *,
    request: ProviderRequest,
    query_specs: Sequence[tuple[str, str, str, str]],
    config: TrackerConfig,
) -> ProviderRequest:
    focused_queries: list[str] = []
    focused_hints: dict[str, tuple[str, ...]] = {}
    seen_queries: set[str] = set()
    for _query, competitor, region, topic_name in query_specs:
        region_config = config.regions[region]
        focused_query = " ".join(
            part
            for part in (
                f'"{competitor}"',
                topic_name.replace("_", " "),
                region_config.label,
            )
            if part
        )
        normalized_query = " ".join(focused_query.split())
        if not normalized_query or normalized_query in seen_queries:
            continue
        seen_queries.add(normalized_query)
        focused_queries.append(normalized_query)
        focused_hints[normalized_query] = (competitor,)
    if not focused_queries:
        return request
    return replace(
        request,
        queries=focused_queries,
        query_competitor_hints=focused_hints,
    )


def _extract_provider_metrics(
    provider_diagnostics: dict[str, dict[str, object]],
) -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for provider_name, diagnostics in provider_diagnostics.items():
        query_rows = diagnostics.get("queries")
        if not isinstance(query_rows, list):
            query_rows = []
        cache_hits = sum(1 for item in query_rows if isinstance(item, dict) and item.get("status") == "cached")
        skipped_items = sum(1 for item in query_rows if isinstance(item, dict) and item.get("status") == "skipped")
        budget_hits = sum(1 for item in query_rows if isinstance(item, dict) and item.get("budget_hit"))
        cooldown_hits = sum(1 for item in query_rows if isinstance(item, dict) and item.get("cooldown_hit"))
        metrics[provider_name] = {
            "cache_hits": cache_hits,
            "skipped_items": skipped_items,
            "budget_hits": budget_hits,
            "cooldown_hits": cooldown_hits,
            "source_tier_wins": int(diagnostics.get("source_tier_wins") or 0),
            "items_after_global_dedup": int(diagnostics.get("items_after_global_dedup") or 0),
        }
        if "feeds_skipped" in diagnostics:
            metrics[provider_name]["feeds_skipped"] = int(diagnostics.get("feeds_skipped") or 0)
    return metrics


def _build_feed_metric_rows(
    *,
    provider_diagnostics: dict[str, dict[str, object]],
    raw_articles: Sequence,
    analysis,
    digest,
    measured_at: str,
) -> list[dict[str, object]]:
    diagnostics = provider_diagnostics.get("regional_rss")
    if not isinstance(diagnostics, dict):
        return []
    query_rows = diagnostics.get("queries")
    if not isinstance(query_rows, list):
        return []

    def _feed_key_from_metadata(metadata: dict[str, object]) -> tuple[str, str, str]:
        return (
            str(metadata.get("query_owner_region") or ""),
            str(metadata.get("direct_feed_name") or ""),
            str(metadata.get("direct_feed_url") or ""),
        )

    raw_counts: dict[tuple[str, str, str], int] = {}
    raw_urls_to_feed_key: dict[str, tuple[str, str, str]] = {}
    for article in raw_articles:
        metadata = dict(getattr(article, "metadata", {}) or {})
        feed_key = _feed_key_from_metadata(metadata)
        if not feed_key[1]:
            continue
        raw_counts[feed_key] = raw_counts.get(feed_key, 0) + 1
        raw_urls_to_feed_key[str(getattr(article, "url", "") or "")] = feed_key

    candidate_counts: dict[tuple[str, str, str], int] = {}
    for candidate in getattr(analysis, "candidates", []):
        metadata = dict(getattr(candidate.raw_article, "metadata", {}) or {})
        feed_key = _feed_key_from_metadata(metadata)
        if not feed_key[1]:
            continue
        candidate_counts[feed_key] = candidate_counts.get(feed_key, 0) + 1

    alert_counts: dict[tuple[str, str, str], int] = {}
    for alert in getattr(digest, "alerts", ()):
        metadata = dict(getattr(alert.candidate.raw_article, "metadata", {}) or {})
        feed_key = _feed_key_from_metadata(metadata)
        if not feed_key[1]:
            continue
        alert_counts[feed_key] = alert_counts.get(feed_key, 0) + 1

    dropped_counts: dict[tuple[str, str, str], int] = {}
    for dropped in getattr(analysis, "dropped_articles", []):
        feed_key = raw_urls_to_feed_key.get(str(getattr(dropped, "url", "") or ""))
        if feed_key is None:
            continue
        dropped_counts[feed_key] = dropped_counts.get(feed_key, 0) + 1

    rows: list[dict[str, object]] = []
    for item in query_rows:
        if not isinstance(item, dict):
            continue
        feed_name = str(item.get("feed_name") or "").strip()
        feed_url = str(item.get("feed_url") or "").strip()
        region = str(item.get("feed_region") or "").strip()
        if not feed_name:
            continue
        feed_key = (region, feed_name, feed_url)
        provider_matches = int(item.get("items_after_filter") or 0)
        candidates_kept = candidate_counts.get(feed_key, 0)
        prefilter_passed = candidates_kept
        alerts_created = alert_counts.get(feed_key, 0)
        items_found = int(item.get("items_found") or 0)
        noise_ratio = (
            round(1.0 - (candidates_kept / provider_matches), 4)
            if provider_matches > 0
            else (1.0 if items_found > 0 else 0.0)
        )
        rows.append(
            {
                "measured_at": measured_at,
                "provider": "regional_rss",
                "region": region,
                "feed_name": feed_name,
                "feed_url": feed_url,
                "items_found": items_found,
                "provider_matches": provider_matches,
                "raw_articles_after_global_dedup": raw_counts.get(feed_key, 0),
                "prefilter_passed": prefilter_passed,
                "candidates_kept": candidates_kept,
                "alerts_created": alerts_created,
                "dropped_prefilter": dropped_counts.get(feed_key, 0),
                "noise_ratio": noise_ratio,
                "recommendation": SQLiteTrackerStorage.recommend_feed_action(
                    items_found=items_found,
                    provider_matches=provider_matches,
                    prefilter_passed=prefilter_passed,
                    alerts_created=alerts_created,
                ),
            }
        )
    return rows


def _has_fresh_ingest(
    *,
    raw_articles: Sequence[object],
    fetched_articles_count: int,
) -> bool:
    """Return True only when the current run produced fresh raw articles."""

    return fetched_articles_count > 0 and bool(raw_articles)


def run_feed_qa(
    *,
    days: int = 30,
    min_items_found: int = 5,
    limit: int = 20,
) -> dict[str, object]:
    runtime = TrackerRuntimeConfig.from_env()
    storage = SQLiteTrackerStorage(runtime.database_path)
    report = storage.get_feed_health_report(
        days=days,
        min_items_found=min_items_found,
        limit=limit,
    )
    report["database_path"] = str(runtime.database_path)
    return report


def run_preflight(
    *,
    mode: str,
    require_openai: bool = False,
) -> dict[str, object]:
    """Check local env readiness for a selected operating mode without external side effects."""

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "local-dry-run":
        return {
            "mode": normalized_mode,
            **build_env_preflight_report(
                telegram_delivery=False,
                notion_sync=False,
                require_openai=require_openai,
            ),
        }
    if normalized_mode == "telegram-delivery":
        return {
            "mode": normalized_mode,
            **build_env_preflight_report(
                telegram_delivery=True,
                notion_sync=False,
                require_openai=require_openai,
            ),
        }
    if normalized_mode == "notion-sync":
        return {
            "mode": normalized_mode,
            **build_env_preflight_report(
                telegram_delivery=False,
                notion_sync=True,
                require_openai=require_openai,
            ),
        }
    if normalized_mode == "github-actions-production":
        return {
            "mode": normalized_mode,
            **build_env_preflight_report(
                telegram_delivery=True,
                notion_sync=False,
                require_openai=require_openai,
            ),
        }
    raise ValueError(
        "Unsupported preflight mode. Use one of: "
        "local-dry-run, telegram-delivery, notion-sync, github-actions-production."
    )


def _ensure_requested_delivery_readiness(
    *,
    telegram_mode: Optional[str],
    notion_mode: Optional[str],
    require_openai: bool = False,
) -> None:
    """Fail fast only for explicitly requested real delivery modes."""

    report = build_env_preflight_report(
        telegram_delivery=telegram_mode == "send",
        notion_sync=notion_mode == "sync",
        require_openai=require_openai,
    )
    if report.get("ok"):
        return
    missing_rows = report.get("required_missing", [])
    parts = []
    for item in missing_rows:
        if not isinstance(item, dict):
            continue
        parts.append(
            f"{item.get('expected_names', '')} required for {item.get('required_for', 'selected mode')}"
        )
    joined = "; ".join(part for part in parts if part)
    raise ValueError(f"Environment is not ready for the selected mode: {joined}")


def _configure_runtime_providers(
    providers: Sequence[Provider],
    *,
    config: TrackerConfig,
) -> list[Provider]:
    configured: list[Provider] = []
    for provider in providers:
        if isinstance(provider, RegionalRssProvider):
            provider.configure(
                feeds_by_region=config.regional_rss_feeds or {},
                competitor_aliases=config.competitor_aliases or {},
            )
        configured.append(provider)
    return configured


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Attach shared tracker options to a subcommand parser."""
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window for competitor signals.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=5,
        help="Minimum score required to keep a mention in the digest.",
    )
    parser.add_argument(
        "--competitor",
        action="append",
        dest="competitors",
        help="Competitor to track. Can be passed multiple times.",
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Region key from config. Can be passed multiple times.",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Save CSV artifacts for manual QA alongside JSON/Markdown outputs.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create parser for the new tracker namespace without affecting legacy CLI."""
    parser = argparse.ArgumentParser(
        description="CLI for the new competitor tracker."
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Run the competitor tracker pipeline locally.",
    )
    add_common_args(run_parser)
    run_parser.add_argument(
        "--to-telegram",
        action="store_true",
        help="Send the ranked digest to Telegram.",
    )
    run_parser.add_argument(
        "--telegram-dry-run",
        action="store_true",
        help="Render and log Telegram delivery without sending any API request.",
    )
    run_parser.add_argument(
        "--to-notion",
        action="store_true",
        help="Mirror final competitor alerts to Notion when env is configured.",
    )
    run_parser.add_argument(
        "--notion-dry-run",
        action="store_true",
        help="Plan Notion mirror actions without writing anything.",
    )

    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help="Run the pipeline and preview Telegram/Notion delivery in dry mode.",
    )
    add_common_args(dry_run_parser)

    send_digest_parser = subparsers.add_parser(
        "send-digest",
        help="Run the pipeline and deliver the digest to Telegram.",
    )
    add_common_args(send_digest_parser)
    send_digest_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="delivery_dry_run",
        help="Log Telegram delivery without calling the API.",
    )

    sync_notion_parser = subparsers.add_parser(
        "sync-notion",
        help="Run the pipeline and mirror final alerts to Notion.",
    )
    add_common_args(sync_notion_parser)
    sync_notion_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="delivery_dry_run",
        help="Preview Notion sync actions without writing any pages.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Run historical collection without external delivery.",
    )
    add_common_args(backfill_parser)

    test_provider_parser = subparsers.add_parser(
        "test-provider",
        help="Run a direct provider diagnostics check for one or more queries.",
    )
    test_provider_parser.add_argument(
        "--provider",
        required=True,
        help="Provider name to test, for example google_news_rss or gdelt.",
    )
    test_provider_parser.add_argument(
        "--query",
        action="append",
        required=True,
        dest="queries",
        help="Raw query to test. Can be passed multiple times.",
    )
    test_provider_parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window passed to the provider request.",
    )
    test_provider_parser.add_argument(
        "--competitor",
        action="append",
        dest="competitors",
        help="Optional competitor hints attached to the provider request.",
    )

    qa_feeds_parser = subparsers.add_parser(
        "qa-feeds",
        help="Summarize stored RSS feed quality metrics and cleanup recommendations.",
    )
    qa_feeds_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback window for feed QA snapshots.",
    )
    qa_feeds_parser.add_argument(
        "--min-feed-items",
        type=int,
        default=5,
        dest="min_feed_items",
        help="Minimum aggregated feed items required before a feed appears in the report.",
    )
    qa_feeds_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of feeds to return in the QA report.",
    )

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check local env readiness for a selected mode without sending anything.",
    )
    preflight_parser.add_argument(
        "--mode",
        choices=(
            "local-dry-run",
            "telegram-delivery",
            "notion-sync",
            "github-actions-production",
        ),
        default="local-dry-run",
        help="Readiness mode to validate.",
    )
    preflight_parser.add_argument(
        "--require-openai",
        action="store_true",
        help="Treat OPENAI_API_KEY as required for this readiness check.",
    )

    return parser


def collect_raw_articles(
    *,
    config: TrackerConfig,
    runtime: TrackerRuntimeConfig,
    regions: Sequence[str],
    competitors: Sequence[str],
    days: Optional[int] = None,
    providers: Optional[Sequence[Provider]] = None,
) -> tuple[list, tuple[str, ...], dict[str, str], int, dict[str, dict[str, object]]]:
    """Generate queries, fetch raw articles, deduplicate, and store in SQLite."""
    query_days = days if days is not None else runtime.lookback_days
    sqlite_storage = SQLiteTrackerStorage(runtime.database_path)
    query_specs = []
    seen_queries = set()
    for region in regions:
        for query, competitor, topic_name in config.prioritized_query_specs_for_region(region):
            if query in seen_queries:
                continue
            seen_queries.add(query)
            query_specs.append((query, competitor, region, topic_name))
    precision_by_query = sqlite_storage.get_query_precision_by_text(
        [query for query, _, _, _ in query_specs],
        half_life_days=runtime.historical_precision_half_life_days,
    )
    query_specs = sorted(
        query_specs,
        key=lambda item: _query_history_sort_key(
            item,
            precision_by_query=precision_by_query,
        ),
    )
    queries = [query for query, _, _, _ in query_specs]
    request = ProviderRequest(
        competitors=tuple(competitors),
        days=query_days,
        queries=queries,
        regions=tuple(regions),
        query_competitor_hints={
            query: (competitor,) for query, competitor, _, _ in query_specs
        },
    )
    query_owner_by_query = {
        query: {
            "query_owner_competitor": competitor,
            "query_owner_region": region,
            "query_owner_topic_group": topic_name,
        }
        for query, competitor, region, topic_name in query_specs
    }
    active_providers = _configure_runtime_providers(
        list(providers) if providers is not None else build_providers(config.enabled_providers),
        config=config,
    )

    raw_articles = []
    provider_errors: dict[str, str] = {}
    provider_diagnostics: dict[str, dict[str, object]] = {}
    for provider in active_providers:
        provider_request = request
        skipped_reason = None
        if provider.name == "gdelt":
            provider_request = _build_gdelt_request(
                request=request,
                query_specs=query_specs,
                config=config,
            )
        elif provider.name == "google_news_rss":
            provider_request = _build_gdelt_request(
                request=request,
                query_specs=query_specs,
                config=config,
            )
        elif provider.name == "guardian":
            provider_request = _build_focused_api_request(
                request=request,
                query_specs=query_specs,
                config=config,
            )
            provider_request, skipped_reason = _limit_provider_request(
                provider_request,
                provider_name="Guardian",
                max_queries=runtime.guardian_max_queries_per_run,
            )
            if not provider_request.queries:
                provider_errors[provider.name] = skipped_reason or "Guardian skipped for this run."
                provider_diagnostics[provider.name] = {
                    "provider": provider.name,
                    "status": "skipped",
                    "queries": [
                        {
                            "provider": provider.name,
                            "query": query,
                            "request_url": "",
                            "http_status": None,
                            "exception": provider_errors[provider.name],
                            "items_found": 0,
                            "items_after_filter": 0,
                            "status": "skipped",
                        }
                        for query in request.queries
                    ],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                }
                continue
        elif provider.name == "newsapi":
            provider_request = _build_focused_api_request(
                request=request,
                query_specs=query_specs,
                config=config,
            )
            if not runtime.enable_newsapi_full_run:
                skipped_reason = (
                    "NewsAPI is disabled for full pipeline runs by default. "
                    "Use COMPETITOR_TRACKER_ENABLE_NEWSAPI_FULL_RUN=true to opt in."
                )
                provider_errors[provider.name] = skipped_reason
                provider_diagnostics[provider.name] = _newsapi_disabled_diagnostic(
                    queries=request.queries,
                    reason=skipped_reason,
                )
                continue
            provider_request, skipped_reason = _limit_newsapi_request(
                provider_request,
                max_queries=runtime.newsapi_max_queries_per_run,
            )
            if not provider_request.queries:
                provider_errors[provider.name] = skipped_reason or "NewsAPI skipped for this run."
                provider_diagnostics[provider.name] = _newsapi_disabled_diagnostic(
                    queries=request.queries,
                    reason=provider_errors[provider.name],
                )
                continue
        try:
            if hasattr(provider, "fetch_with_diagnostics"):
                fetched, diagnostics = provider.fetch_with_diagnostics(provider_request)
            else:
                fetched = provider.fetch(provider_request)
                diagnostics = {
                    "provider": provider.name,
                    "status": "ok",
                    "queries": [
                        {
                            "provider": provider.name,
                            "query": query,
                            "request_url": "",
                            "http_status": None,
                            "exception": "",
                            "items_found": 0,
                            "items_after_filter": 0,
                            "status": "ok",
                        }
                        for query in provider_request.queries
                    ],
                    "items_found": len(fetched),
                    "items_after_filter": len(fetched),
                    "items_after_global_dedup": 0,
                }
            if skipped_reason:
                diagnostics["warning"] = skipped_reason
            provider_diagnostics[provider.name] = diagnostics
        except ProviderError as exc:
            provider_errors[provider.name] = str(exc)
            provider_diagnostics[provider.name] = (
                exc.diagnostics
                if getattr(exc, "diagnostics", None)
                else {
                    "provider": provider.name,
                    "status": "error",
                    "queries": [
                        {
                            "provider": provider.name,
                            "query": query,
                            "request_url": "",
                            "http_status": None,
                            "exception": str(exc),
                            "items_found": 0,
                            "items_after_filter": 0,
                            "status": "error",
                        }
                        for query in provider_request.queries
                    ],
                    "items_found": 0,
                    "items_after_filter": 0,
                    "items_after_global_dedup": 0,
                }
            )
            continue
        raw_articles.extend(
            replace(
                article,
                metadata={
                    **query_owner_by_query.get(article.query, {}),
                    **article.metadata,
                },
            )
            for article in fetched
        )

    fetched_articles_count = len(raw_articles)
    raw_articles, dedup_metrics = deduplicate_raw_articles_with_metrics(raw_articles)
    deduped_provider_counts: dict[str, int] = {}
    for article in raw_articles:
        deduped_provider_counts[article.provider] = deduped_provider_counts.get(article.provider, 0) + 1
    for provider_name, diagnostics in provider_diagnostics.items():
        diagnostics["items_after_global_dedup"] = deduped_provider_counts.get(provider_name, 0)
        diagnostics["source_tier_wins"] = (
            dedup_metrics.get("source_tier_wins_by_provider", {}).get(provider_name, 0)
            if isinstance(dedup_metrics.get("source_tier_wins_by_provider"), dict)
            else 0
        )
    provider_diagnostics["global_dedup"] = {
        "provider": "global_dedup",
        "status": "ok",
        "queries": [],
        "items_found": fetched_articles_count,
        "items_after_filter": len(raw_articles),
        "items_after_global_dedup": len(raw_articles),
        "source_tier_wins": int(dedup_metrics.get("direct_source_wins_over_aggregators") or 0),
    }
    sqlite_storage.insert_raw_articles(raw_articles)
    return (
        raw_articles,
        tuple(provider.name for provider in active_providers),
        provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    )


def test_provider(
    *,
    provider_name: str,
    queries: Sequence[str],
    days: int,
    competitors: Optional[Sequence[str]] = None,
) -> dict[str, object]:
    """Run one provider directly and return structured diagnostics."""

    providers = build_providers([provider_name])
    if provider_name == "regional_rss":
        runtime = TrackerRuntimeConfig.from_env()
        config = TrackerConfig.load(runtime.config_path)
        providers = _configure_runtime_providers(providers, config=config)
    provider = providers[0]
    request = ProviderRequest(
        competitors=tuple(competitors or ()),
        days=days,
        queries=list(queries),
        query_competitor_hints={
            query: tuple(competitors or ())
            for query in queries
        },
    )
    try:
        if hasattr(provider, "fetch_with_diagnostics"):
            articles, diagnostics = provider.fetch_with_diagnostics(request)
        else:
            articles = provider.fetch(request)
            diagnostics = {
                "provider": provider.name,
                "status": "ok",
                "queries": [
                    {
                        "provider": provider.name,
                        "query": query,
                        "request_url": "",
                        "http_status": None,
                        "exception": "",
                        "items_found": 0,
                        "items_after_filter": 0,
                        "status": "ok",
                    }
                    for query in queries
                ],
                "items_found": len(articles),
                "items_after_filter": len(articles),
                "items_after_global_dedup": len(articles),
            }
        diagnostics["items_after_global_dedup"] = len(deduplicate_raw_articles(articles))
        provider_status = str(diagnostics.get("status") or "").strip().lower()
        skipped = provider_status == "skipped"
        query_rows = diagnostics.get("queries")
        if not isinstance(query_rows, list):
            query_rows = []
        skip_reason = ""
        if skipped:
            for item in query_rows:
                if not isinstance(item, dict):
                    continue
                skip_reason = str(item.get("exception") or "").strip()
                if skip_reason:
                    break
        return {
            "ok": not skipped,
            "skipped": skipped,
            "provider": provider.name,
            "query_count": len(queries),
            "raw_articles_fetched": len(articles),
            "raw_articles_after_global_dedup": diagnostics["items_after_global_dedup"],
            "diagnostics": diagnostics,
            "sample_urls": [article.url for article in articles[:5]],
            **({"warning": skip_reason} if skipped and skip_reason else {}),
        }
    except ProviderError as exc:
        diagnostics = (
            exc.diagnostics
            if getattr(exc, "diagnostics", None)
            else {
                "provider": provider.name,
                "status": "error",
                "queries": [
                    {
                        "provider": provider.name,
                        "query": query,
                        "request_url": "",
                        "http_status": None,
                        "exception": str(exc),
                        "items_found": 0,
                        "items_after_filter": 0,
                        "status": "error",
                    }
                    for query in queries
                ],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }
        )
        return {
            "ok": False,
            "provider": provider.name,
            "query_count": len(queries),
            "raw_articles_fetched": 0,
            "raw_articles_after_global_dedup": 0,
            "error": str(exc),
            "diagnostics": diagnostics,
            "sample_urls": [],
        }


def resolve_targets(
    config: TrackerConfig,
    requested_regions: Optional[Sequence[str]],
    requested_competitors: Optional[Sequence[str]],
) -> tuple[list[str], list[str]]:
    """Resolve regions and competitor names from config plus CLI filters."""
    selected_regions = list(requested_regions or config.regions.keys())
    unknown_regions = sorted(set(selected_regions) - set(config.regions))
    if unknown_regions:
        missing_list = ", ".join(unknown_regions)
        raise ValueError(f"Unknown regions: {missing_list}")

    if requested_competitors:
        return selected_regions, list(requested_competitors)

    competitors: list[str] = []
    seen = set()
    for region in selected_regions:
        for competitor in config.competitors_by_region[region]:
            if competitor in seen:
                continue
            seen.add(competitor)
            competitors.append(competitor)
    return selected_regions, competitors


def _resolve_runtime_alert_settings(
    *,
    use_llm_alerts: Optional[bool] = None,
    llm_top_n: Optional[int] = None,
    telegram_top_n: Optional[int] = None,
    article_context_max_chars: Optional[int] = None,
) -> tuple[bool, int, int, int]:
    runtime = TrackerRuntimeConfig.from_env()
    return (
        runtime.use_llm_alerts if use_llm_alerts is None else use_llm_alerts,
        max(0, runtime.llm_top_n if llm_top_n is None else llm_top_n),
        max(0, runtime.telegram_top_n if telegram_top_n is None else telegram_top_n),
        max(
            0,
            runtime.article_context_max_chars
            if article_context_max_chars is None
            else article_context_max_chars,
        ),
    )


def build_delivery_alert_schemas(
    digest_alerts,
    *,
    config: Optional[TrackerConfig] = None,
    use_llm_alerts: Optional[bool] = None,
    llm_top_n: Optional[int] = None,
    article_context_max_chars: Optional[int] = None,
    prefetched_contexts: Optional[dict[str, object]] = None,
):
    """Enrich only the strongest post-ranking alerts with LLM output."""
    def make_alert_analyzer(*, use_llm: bool):
        try:
            return CompetitorAlertAnalyzer(use_llm=use_llm, config=config)
        except TypeError:
            return CompetitorAlertAnalyzer(use_llm=use_llm)

    fallback_analyzer = make_alert_analyzer(use_llm=False)
    if not digest_alerts:
        return [], []

    (
        effective_use_llm_alerts,
        effective_llm_top_n,
        _,
        effective_article_context_max_chars,
    ) = _resolve_runtime_alert_settings(
        use_llm_alerts=use_llm_alerts,
        llm_top_n=llm_top_n,
        article_context_max_chars=article_context_max_chars,
    )
    llm_limit = max(0, effective_llm_top_n) if effective_use_llm_alerts else 0
    llm_analyzer = (
        make_alert_analyzer(use_llm=True) if llm_limit > 0 else fallback_analyzer
    )
    fallback_context_extractor = ArticleContextExtractor(
        max_chars=effective_article_context_max_chars
    )
    context_extractor = (
        fallback_context_extractor if llm_analyzer.use_llm else None
    )

    alert_schemas = []
    article_contexts = []
    reusable_contexts = prefetched_contexts or {}
    for index, alert in enumerate(digest_alerts):
        article_context = reusable_contexts.get(alert.candidate.url)
        analyzer = llm_analyzer if index < llm_limit else fallback_analyzer
        if (
            article_context is None
            and context_extractor is not None
            and index < llm_limit
        ):
            article_context = context_extractor.extract(alert.candidate)
            reusable_contexts[alert.candidate.url] = article_context
        article_contexts.append(
            article_context
            or fallback_context_extractor.build_fallback_context(alert.candidate)
        )
        alert_schemas.append(
            analyzer.analyze_candidate(
                alert.candidate,
                article_context=article_context,
            )
        )
    return alert_schemas, article_contexts


def _candidate_needs_preranking_context(
    candidate,
    *,
    today: Optional[date] = None,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
) -> bool:
    provider_date = parse_published_at(candidate.published_date or "")
    if provider_date is None:
        return True

    effective_today = today or _today()
    provider_day = date.fromisoformat(provider_date)
    if provider_day > effective_today:
        return True
    return provider_day < (effective_today - timedelta(days=max_age_days))


def build_preranking_alert_schemas(
    alerts,
    *,
    config: Optional[TrackerConfig] = None,
    today: Optional[date] = None,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
    article_context_max_chars: Optional[int] = None,
):
    """Resolve canonical publication dates before digest ranking."""

    (_, _, _, effective_article_context_max_chars) = _resolve_runtime_alert_settings(
        article_context_max_chars=article_context_max_chars,
    )
    context_extractor = ArticleContextExtractor(
        max_chars=effective_article_context_max_chars
    )
    alert_schemas = []
    prefetched_contexts = {}
    analyzer = None
    effective_today = today or _today()
    for alert in alerts:
        provider_date = parse_published_at(alert.candidate.published_date or "")
        needs_context = _candidate_needs_preranking_context(
            alert.candidate,
            today=effective_today,
            max_age_days=max_age_days,
        )
        if provider_date is not None and not needs_context:
            resolved_date = date.fromisoformat(provider_date)
            alert_schemas.append(
                {
                    "published_date": resolved_date.isoformat(),
                    "published_date_source": "provider",
                    "resolved_publication_date": resolved_date,
                    "resolved_publication_date_source": "provider",
                }
            )
            continue
        article_context = None
        if needs_context:
            article_context = context_extractor.extract(alert.candidate)
            prefetched_contexts[alert.candidate.url] = article_context
        if analyzer is None:
            try:
                analyzer = CompetitorAlertAnalyzer(use_llm=False, config=config)
            except TypeError:
                analyzer = CompetitorAlertAnalyzer(use_llm=False)
        alert_schemas.append(
            analyzer.analyze_candidate(
                alert.candidate,
                article_context=article_context,
            )
        )
    return alert_schemas, prefetched_contexts


def select_telegram_delivery_payload(
    alerts,
    alert_schemas,
    article_contexts,
    *,
    telegram_top_n: Optional[int] = None,
):
    """Send only the configured top slice to Telegram delivery."""
    (_, _, effective_telegram_top_n, _) = _resolve_runtime_alert_settings(
        telegram_top_n=telegram_top_n,
    )
    delivery_limit = min(len(alerts), max(0, effective_telegram_top_n))
    return (
        list(alerts[:delivery_limit]),
        list(alert_schemas[:delivery_limit]),
        list(article_contexts[:delivery_limit]),
    )


def _resolve_alert_publication_date(alert, alert_schema) -> Optional[date]:
    resolved_value = alert_schema.get("resolved_publication_date")
    resolved_source = str(
        alert_schema.get("resolved_publication_date_source") or ""
    ).strip().lower()
    if isinstance(resolved_value, datetime):
        return None if resolved_source == "undated_fallback" else resolved_value.date()
    if isinstance(resolved_value, date):
        return None if resolved_source == "undated_fallback" else resolved_value

    resolved_date, resolved_date_source = resolve_final_publication_date(
        alert_schema,
        alert.candidate.raw_article,
    )
    if resolved_date_source != "undated_fallback":
        alert_schema["resolved_publication_date"] = resolved_date
        alert_schema["resolved_publication_date_source"] = resolved_date_source
        return resolved_date

    raw_date = parse_published_at(alert.candidate.raw_article.published_at or "")
    if raw_date:
        fallback_date = date.fromisoformat(raw_date)
        alert_schema["resolved_publication_date"] = fallback_date
        alert_schema["resolved_publication_date_source"] = "provider"
        return fallback_date
    return None


def _is_alert_expired(
    alert,
    alert_schema,
    *,
    today: Optional[date] = None,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
    average_score: float = 0.0,
) -> bool:
    effective_today = today or _today()
    published_date = _resolve_alert_publication_date(alert, alert_schema)
    if published_date is None:
        return not bool(alert.candidate.raw_article.metadata.get("allow_undated"))
    if published_date >= (effective_today - timedelta(days=max_age_days)):
        return False
    if _has_fresh_high_signal_override(alert, average_score=average_score):
        return False
    return True


def _has_fresh_high_signal_override(
    alert,
    *,
    average_score: float,
    score_threshold: int = HIGH_SIGNAL_SCORE_THRESHOLD,
) -> bool:
    if alert.candidate.raw_article.metadata.get("deferred_digest_key"):
        return False
    return alert.score >= score_threshold and alert.score > average_score


def _apply_candidate_expired_flag(candidate, *, is_expired: bool) -> None:
    candidate.raw_article.metadata["is_expired"] = is_expired


def filter_expired_digest_items(
    digest: CompetitorDigest,
    alert_schemas,
    article_contexts,
    *,
    today: Optional[date] = None,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
):
    effective_today = today or _today()
    filtered_alerts = []
    filtered_schemas = []
    filtered_contexts = []
    expired_alerts = []
    average_score = (
        sum(alert.score for alert in digest.alerts) / len(digest.alerts)
        if digest.alerts
        else 0.0
    )

    for alert, alert_schema, article_context in zip(
        digest.alerts, alert_schemas, article_contexts
    ):
        is_expired = _is_alert_expired(
            alert,
            alert_schema,
            today=effective_today,
            max_age_days=max_age_days,
            average_score=average_score,
        )
        _apply_candidate_expired_flag(alert.candidate, is_expired=is_expired)
        if is_expired:
            expired_alerts.append(alert)
            continue
        filtered_alerts.append(alert)
        filtered_schemas.append(alert_schema)
        filtered_contexts.append(article_context)

    filtered_digest = CompetitorDigest(
        generated_at=digest.generated_at,
        competitors=digest.competitors,
        alerts=tuple(filtered_alerts),
        highlights=digest.highlights,
        regions=digest.regions,
    )
    return filtered_digest, filtered_schemas, filtered_contexts, expired_alerts


def _candidate_region_key(
    candidate,
    *,
    selected_regions: Optional[Sequence[str]] = None,
) -> str:
    resolved_region = (candidate.region or candidate.raw_article.region or "").strip().lower()
    if resolved_region:
        return resolved_region
    if selected_regions and len(selected_regions) == 1:
        return selected_regions[0].strip().lower()
    return ""


def _candidate_dedup_key(candidate) -> str:
    url_key = normalize_url(candidate.url)
    if url_key:
        return f"url:{url_key}"
    return (
        f"text:{normalize_title(candidate.title)}::"
        f"{normalize_title(candidate.raw_article.snippet or '')}"
    )


def _query_owner_region(candidate) -> str:
    return str(candidate.raw_article.metadata.get("query_owner_region") or "").strip().lower()


def _geo_region_strength(candidate, region: str, config: TrackerConfig) -> int:
    region_config = config.regions.get(region)
    if region_config is None:
        return 0
    geo_text_blob = " ".join(
        value.casefold()
        for value in (
            candidate.raw_article.title,
            candidate.raw_article.snippet,
            candidate.raw_article.source,
        )
        if value
    )
    if not geo_text_blob:
        return 0
    matched_markers = set()
    for marker in (
        region_config.label,
        *region_config.geo_terms,
        *region_config.country_validation_terms,
    ):
        if GeoPolicy.contains_geo_term(geo_text_blob, marker):
            matched_markers.add(marker.casefold())
    return len(matched_markers)


def _assign_candidate_region(
    candidate,
    *,
    selected_regions: Sequence[str],
    config: TrackerConfig,
) -> str:
    best_region = ""
    best_score = 0
    tied_regions = []
    for region in selected_regions:
        score = _geo_region_strength(candidate, region, config)
        if score > best_score:
            best_region = region
            best_score = score
            tied_regions = [region]
        elif score > 0 and score == best_score:
            tied_regions.append(region)
    if best_score > 0 and len(tied_regions) == 1:
        return best_region

    owner_region = _query_owner_region(candidate)
    if owner_region in selected_regions:
        return owner_region
    return _candidate_region_key(candidate, selected_regions=selected_regions)


def _deduplicate_region_candidates(
    candidates,
    *,
    selected_regions: Sequence[str],
    config: TrackerConfig,
):
    assigned = {}
    for candidate in candidates:
        candidate_region = _assign_candidate_region(
            candidate,
            selected_regions=selected_regions,
            config=config,
        )
        if candidate_region not in selected_regions:
            continue
        assigned_candidate = (
            candidate
            if candidate.region == candidate_region
            else replace(candidate, region=candidate_region)
        )
        dedup_key = _candidate_dedup_key(candidate)
        selection_rank = (
            _geo_region_strength(candidate, candidate_region, config),
            1 if candidate.country_hint else 0,
            candidate.score,
            -selected_regions.index(candidate_region),
        )
        existing = assigned.get(dedup_key)
        if existing is None or selection_rank > existing[2]:
            assigned[dedup_key] = (candidate_region, assigned_candidate, selection_rank)

    regional_candidates = {region: [] for region in selected_regions}
    for region, candidate, _ in assigned.values():
        regional_candidates[region].append(candidate)
    return regional_candidates


def run_pipeline(
    *,
    days: int,
    min_score: int,
    competitors: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    telegram_mode: Optional[str] = None,
    notion_mode: Optional[str] = None,
    export_csv: bool = False,
    use_llm_alerts: Optional[bool] = None,
    llm_top_n: Optional[int] = None,
    telegram_top_n: Optional[int] = None,
    article_context_max_chars: Optional[int] = None,
) -> dict[str, object]:
    """Execute the competitor tracker pipeline for one CLI command."""
    run_started_at = _utc_now_iso()
    runtime = TrackerRuntimeConfig.from_env()
    config = TrackerConfig.load(runtime.config_path)
    (
        effective_use_llm_alerts,
        effective_llm_top_n,
        effective_telegram_top_n,
        effective_article_context_max_chars,
    ) = _resolve_runtime_alert_settings(
        use_llm_alerts=use_llm_alerts,
        llm_top_n=llm_top_n,
        telegram_top_n=telegram_top_n,
        article_context_max_chars=article_context_max_chars,
    )
    selected_regions, selected_competitors = resolve_targets(config, regions, competitors)
    sqlite_storage = SQLiteTrackerStorage(runtime.database_path)

    collect_result = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=selected_regions,
        competitors=selected_competitors,
        days=days,
    )
    provider_diagnostics: dict[str, dict[str, object]] = {}
    if len(collect_result) == 5:
        (
            raw_articles,
            provider_names,
            provider_errors,
            fetched_articles_count,
            provider_diagnostics,
        ) = collect_result
    elif len(collect_result) == 4:
        raw_articles, provider_names, provider_errors, fetched_articles_count = collect_result
    else:
        raw_articles, provider_names, provider_errors = collect_result
        fetched_articles_count = len(raw_articles)

    analyzer = CompetitorAnalyzer(min_score=min_score, config=config)
    analysis = analyzer.prefilter_raw_articles(raw_articles, regions=selected_regions)
    has_fresh_ingest = _has_fresh_ingest(
        raw_articles=raw_articles,
        fetched_articles_count=fetched_articles_count,
    )
    delivery_channel = (
        "telegram" if telegram_mode in {"dry", "send"} else "daily_digest"
    )
    delivery_destination = (
        get_env_value("TELEGRAM_CHAT_ID") if telegram_mode in {"dry", "send"} else ""
    )
    candidate_pool = list(analysis.candidates)

    if telegram_mode in {"dry", "send"} and has_fresh_ingest:
        sqlite_storage.expire_stale_deferred(
            channel=delivery_channel,
            destination=delivery_destination,
            max_age_days=DigestBuilder.DEFERRED_MAX_AGE_DAYS,
        )
        deferred_candidates = sqlite_storage.get_deferred_candidates(
            channel=delivery_channel,
            destination=delivery_destination,
            max_age_days=DigestBuilder.DEFERRED_MAX_AGE_DAYS,
            limit=100,
        )
        candidate_pool.extend(deferred_candidates)
    elif telegram_mode in {"dry", "send"}:
        provider_diagnostics["pipeline"] = {
            "provider": "pipeline",
            "status": "skipped",
            "queries": [],
            "items_found": 0,
            "items_after_filter": 0,
            "items_after_global_dedup": 0,
            "warning": (
                "No fresh ingest available for this run; deferred backlog was not used to build "
                "a fresh daily digest."
            ),
        }

    regional_candidates = _deduplicate_region_candidates(
        candidate_pool,
        selected_regions=selected_regions,
        config=config,
    )

    all_digest_alerts = []
    all_alert_schemas = []
    all_article_contexts = []
    expired_alerts = []
    digest_generated_at = _utc_now_iso()
    for region in selected_regions:
        prefetched_contexts: dict[str, object] = {}

        def ranking_alert_schemas_builder(alerts):
            nonlocal prefetched_contexts
            ranking_alert_schemas, prefetched_contexts = build_preranking_alert_schemas(
                alerts,
                config=config,
                article_context_max_chars=effective_article_context_max_chars,
            )
            return ranking_alert_schemas

        regional_digest = DigestBuilder().build(
            competitors=selected_competitors,
            candidates=regional_candidates[region],
            regions=[region],
            digest_limit=config.daily_digest_limit,
            storage=sqlite_storage,
            delivery_channel=delivery_channel,
            delivery_destination=delivery_destination,
            include_deferred=False,
            apply_marketing_filters=True,
            marketing_config=config,
            ranking_alert_schemas_builder=ranking_alert_schemas_builder,
        )
        regional_alert_schemas, regional_article_contexts = build_delivery_alert_schemas(
            regional_digest.alerts,
            config=config,
            use_llm_alerts=effective_use_llm_alerts,
            llm_top_n=effective_llm_top_n,
            article_context_max_chars=effective_article_context_max_chars,
            prefetched_contexts=prefetched_contexts,
        )
        (
            regional_digest,
            regional_alert_schemas,
            regional_article_contexts,
            regional_expired_alerts,
        ) = filter_expired_digest_items(
            regional_digest,
            regional_alert_schemas,
            regional_article_contexts,
        )
        all_digest_alerts.extend(regional_digest.alerts)
        all_alert_schemas.extend(regional_alert_schemas)
        all_article_contexts.extend(regional_article_contexts)
        expired_alerts.extend(regional_expired_alerts)

    digest = CompetitorDigest(
        generated_at=digest_generated_at,
        competitors=tuple(selected_competitors),
        alerts=tuple(all_digest_alerts),
        highlights=tuple(alert.headline for alert in all_digest_alerts[:5]),
        regions=tuple(selected_regions),
    )
    alert_schemas = all_alert_schemas
    article_contexts = all_article_contexts
    for candidate in analysis.candidates:
        candidate.raw_article.metadata.setdefault("is_expired", False)
        sqlite_storage.merge_raw_article_metadata(
            url=candidate.url,
            metadata_updates={"is_expired": bool(candidate.raw_article.metadata.get("is_expired"))},
        )
    sqlite_storage.insert_alerts(digest.alerts)

    storage = JsonFileStorage(runtime.output_dir)
    candidates_path = storage.save_candidates(analysis.candidates)
    dropped_articles_path = storage.save_dropped_articles(analysis.dropped_articles)
    digest_path = storage.save_digest(digest)
    preview_path = storage.save_markdown_preview(
        digest.alerts,
        alert_schemas,
        generated_at=digest.generated_at,
    )
    candidates_csv_path = (
        storage.save_candidates_csv(analysis.candidates, alert_schemas=alert_schemas)
        if export_csv
        else None
    )

    telegram_result = None
    notion_result = None
    telegram_alerts = []
    if telegram_mode in {"dry", "send"} and has_fresh_ingest:
        telegram_alerts, telegram_schemas, _ = select_telegram_delivery_payload(
            digest.alerts,
            alert_schemas,
            article_contexts,
            telegram_top_n=effective_telegram_top_n,
        )
        sender = TelegramSender(
            storage=sqlite_storage,
            dry_run=telegram_mode == "dry",
        )
        telegram_result = sender.send_daily_digest(
            telegram_schemas,
            alerts=telegram_alerts,
            source_urls=[alert.candidate.url for alert in telegram_alerts],
            generated_at=digest.generated_at,
        )
        if telegram_mode == "send":
            deferred_alerts = list(digest.alerts[len(telegram_alerts) :])
            for alert in deferred_alerts:
                sqlite_storage.mark_deferred(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    destination=get_env_value("TELEGRAM_CHAT_ID"),
                    metadata={
                        "mode": "daily_digest",
                        "generated_at": digest.generated_at,
                    },
                )
    elif telegram_mode in {"dry", "send"}:
        telegram_result = {
            "ok": True,
            "skipped": True,
            "reason": "no_fresh_ingest",
            "message_id": None,
        }

    if notion_mode in {"dry", "sync"}:
        notion_sync = CompetitorNotionMirrorSync()
        notion_result = notion_sync.sync_alerts(
            list(zip(digest.alerts, alert_schemas)),
            dry_run=notion_mode == "dry",
        )

    run_status = "success_with_provider_errors" if provider_errors else "success"
    if not has_fresh_ingest:
        run_status = f"{run_status}_no_fresh_ingest"
    run_finished_at = _utc_now_iso()
    provider_metrics = _extract_provider_metrics(provider_diagnostics)
    feed_metric_rows = _build_feed_metric_rows(
        provider_diagnostics=provider_diagnostics,
        raw_articles=raw_articles,
        analysis=analysis,
        digest=digest,
        measured_at=run_finished_at,
    )
    run_summary = RunSummary(
        started_at=run_started_at,
        finished_at=run_finished_at,
        regions=tuple(selected_regions),
        providers=provider_names,
        queries_generated=len(config.queries_for_regions(selected_regions)),
        raw_articles_collected=len(raw_articles),
        candidates_kept=len(analysis.candidates),
        alerts_created=len(digest.alerts),
        daily_digest_limit=config.daily_digest_limit,
        raw_articles_fetched=fetched_articles_count,
        raw_articles_deduplicated=len(raw_articles),
        articles_filtered_out=analysis.dropped_count,
        alerts_sent=len(telegram_alerts) if telegram_mode == "send" and telegram_result else 0,
        status=run_status,
        drop_reasons={
            reason: sum(1 for item in analysis.dropped_articles if item.reason == reason)
            for reason in sorted({item.reason for item in analysis.dropped_articles})
        },
        provider_errors=provider_errors,
        provider_diagnostics=provider_diagnostics,
        provider_metrics=provider_metrics,
    )
    summary_path = storage.save_run_summary(run_summary)
    try:
        run_id = sqlite_storage.insert_run(run_summary)
        sqlite_storage.insert_feed_metric_rows(
            run_id=run_id,
            rows=feed_metric_rows,
        )
    except sqlite3.Error as exc:
        logger.warning(
            "Failed to persist run summary to SQLite runs table. db=%s error=%s",
            runtime.database_path,
            exc,
        )

    return {
        "analysis": analysis,
        "digest": digest,
        "alert_schemas": alert_schemas,
        "article_contexts": article_contexts,
        "expired_alerts_count": len(expired_alerts),
        "query_count": len(config.queries_for_regions(selected_regions)),
        "candidates_path": candidates_path,
        "dropped_articles_path": dropped_articles_path,
        "digest_path": digest_path,
        "preview_path": preview_path,
        "candidates_csv_path": candidates_csv_path,
        "summary_path": summary_path,
        "runtime": runtime,
        "raw_articles_count": len(raw_articles),
        "provider_diagnostics": provider_diagnostics,
        "provider_metrics": provider_metrics,
        "feed_metrics": feed_metric_rows,
        "telegram_result": telegram_result,
        "notion_result": notion_result,
    }


def summarize_result(result: dict[str, object]) -> str:
    """Render a compact CLI summary after one command finishes."""
    analysis = result["analysis"]
    runtime = result["runtime"]
    telegram_result = result["telegram_result"]
    notion_result = result["notion_result"]
    summary = (
        "Competitor tracker completed. "
        f"kept={len(analysis.candidates)} dropped={analysis.dropped_count} "
        f"queries={result['query_count']} candidates={result['candidates_path']} "
        f"raw_articles={result['raw_articles_count']} sqlite={runtime.database_path} "
        f"digest={result['digest_path']} preview={result['preview_path']} "
        f"summary={result['summary_path']}"
    )
    if result["candidates_csv_path"] is not None:
        summary += f" csv={result['candidates_csv_path']}"
    if telegram_result is not None:
        summary += f" telegram={telegram_result}"
    if notion_result is not None:
        summary += f" notion={notion_result}"
    return summary


def normalize_argv(argv: Optional[Sequence[str]]) -> list[str]:
    """Keep backward compatibility by treating bare flags as `run`."""
    normalized = list(argv) if argv is not None else sys.argv[1:]
    if not normalized:
        return ["run"]
    if normalized[0] in COMMAND_NAMES or normalized[0] in {"-h", "--help"}:
        return normalized
    return ["run", *normalized]


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the competitor tracker CLI without touching the legacy CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(normalize_argv(argv))
    command = args.command or "run"

    telegram_mode = None
    notion_mode = None
    if command == "run":
        if args.to_telegram or args.telegram_dry_run:
            telegram_mode = "dry" if args.telegram_dry_run else "send"
        if args.to_notion or args.notion_dry_run:
            notion_mode = "dry" if args.notion_dry_run else "sync"
    elif command == "dry-run":
        telegram_mode = "dry"
        notion_mode = "dry"
    elif command == "send-digest":
        telegram_mode = "dry" if args.delivery_dry_run else "send"
    elif command == "sync-notion":
        notion_mode = "dry" if args.delivery_dry_run else "sync"
    elif command == "backfill":
        telegram_mode = None
        notion_mode = None
    elif command == "test-provider":
        result = test_provider(
            provider_name=args.provider,
            queries=args.queries,
            days=args.days,
            competitors=args.competitors,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    elif command == "qa-feeds":
        result = run_feed_qa(
            days=args.days,
            min_items_found=args.min_feed_items,
            limit=args.limit,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    elif command == "preflight":
        result = run_preflight(
            mode=args.mode,
            require_openai=args.require_openai,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)
        return

    _ensure_requested_delivery_readiness(
        telegram_mode=telegram_mode,
        notion_mode=notion_mode,
        require_openai=False,
    )

    result = run_pipeline(
        days=args.days,
        min_score=args.min_score,
        competitors=args.competitors,
        regions=args.regions,
        telegram_mode=telegram_mode,
        notion_mode=notion_mode,
        export_csv=args.export_csv,
    )
    print(summarize_result(result))


if __name__ == "__main__":
    main()
