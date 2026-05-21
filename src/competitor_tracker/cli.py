"""Command-line entrypoint for the new competitor tracker."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

from .article_context import ArticleContextExtractor
from .analyzer import CompetitorAlertAnalyzer, CompetitorAnalyzer
from .config import TrackerConfig, TrackerRuntimeConfig
from .digest import DigestBuilder
from .models import CompetitorDigest, RunSummary
from .normalization import deduplicate_raw_articles, parse_published_at
from .notion_sync import CompetitorNotionMirrorSync
from .providers import Provider, ProviderError, ProviderRequest, build_providers
from .storage import JsonFileStorage, SQLiteTrackerStorage
from .telegram_sender import TelegramSender

COMMAND_NAMES = {"run", "dry-run", "send-digest", "sync-notion", "backfill"}
POST_RANKING_LLM_TOP_N = 15
MAX_ARTICLE_AGE_DAYS = 7
REFERENCE_TODAY = date.fromisoformat("2026-05-21")
HIGH_SIGNAL_SCORE_THRESHOLD = 7


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

    return parser


def collect_raw_articles(
    *,
    config: TrackerConfig,
    runtime: TrackerRuntimeConfig,
    regions: Sequence[str],
    competitors: Sequence[str],
    days: Optional[int] = None,
    providers: Optional[Sequence[Provider]] = None,
) -> tuple[list, tuple[str, ...], dict[str, str]]:
    """Generate queries, fetch raw articles, deduplicate, and store in SQLite."""
    query_days = days if days is not None else runtime.lookback_days
    queries = config.queries_for_regions(regions)
    request = ProviderRequest(
        competitors=tuple(competitors),
        days=query_days,
        queries=queries,
        regions=tuple(regions),
    )
    active_providers = list(providers) if providers is not None else build_providers(
        config.enabled_providers
    )

    raw_articles = []
    provider_errors: dict[str, str] = {}
    for provider in active_providers:
        try:
            raw_articles.extend(provider.fetch(request))
        except ProviderError as exc:
            provider_errors[provider.name] = str(exc)

    raw_articles = deduplicate_raw_articles(raw_articles)
    sqlite_storage = SQLiteTrackerStorage(runtime.database_path)
    sqlite_storage.insert_raw_articles(raw_articles)
    return raw_articles, tuple(provider.name for provider in active_providers), provider_errors


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


def build_delivery_alert_schemas(
    digest_alerts,
    *,
    llm_top_n: int = POST_RANKING_LLM_TOP_N,
):
    """Enrich only the strongest post-ranking alerts with LLM output."""
    fallback_analyzer = CompetitorAlertAnalyzer(use_llm=False)
    if not digest_alerts:
        return [], []

    llm_limit = max(0, llm_top_n)
    llm_analyzer = (
        CompetitorAlertAnalyzer(use_llm=True) if llm_limit > 0 else fallback_analyzer
    )
    fallback_context_extractor = ArticleContextExtractor()
    context_extractor = (
        fallback_context_extractor if llm_analyzer.use_llm else None
    )

    alert_schemas = []
    article_contexts = []
    for index, alert in enumerate(digest_alerts):
        article_context = None
        analyzer = llm_analyzer if index < llm_limit else fallback_analyzer
        if context_extractor is not None and index < llm_limit:
            article_context = context_extractor.extract(alert.candidate)
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


def select_telegram_delivery_payload(
    alerts,
    alert_schemas,
    article_contexts,
    *,
    llm_top_n: int = POST_RANKING_LLM_TOP_N,
):
    """Send only the post-ranking LLM-targeted top slice to Telegram."""
    delivery_limit = min(len(alerts), max(0, llm_top_n))
    return (
        list(alerts[:delivery_limit]),
        list(alert_schemas[:delivery_limit]),
        list(article_contexts[:delivery_limit]),
    )


def _resolve_alert_publication_date(alert, alert_schema) -> Optional[date]:
    raw_date = parse_published_at(alert.candidate.published_date or "")
    if raw_date:
        return date.fromisoformat(raw_date)
    schema_date = parse_published_at(str(alert_schema.get("published_date") or ""))
    if schema_date:
        return date.fromisoformat(schema_date)
    return None


def _is_alert_expired(
    alert,
    alert_schema,
    *,
    today: date = REFERENCE_TODAY,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
    average_score: float = 0.0,
) -> bool:
    published_date = _resolve_alert_publication_date(alert, alert_schema)
    if published_date is None:
        return False
    if published_date >= (today - timedelta(days=max_age_days)):
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
    today: date = REFERENCE_TODAY,
    max_age_days: int = MAX_ARTICLE_AGE_DAYS,
):
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
            today=today,
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


def run_pipeline(
    *,
    days: int,
    min_score: int,
    competitors: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    telegram_mode: Optional[str] = None,
    notion_mode: Optional[str] = None,
    export_csv: bool = False,
) -> dict[str, object]:
    """Execute the competitor tracker pipeline for one CLI command."""
    runtime = TrackerRuntimeConfig.from_env()
    config = TrackerConfig.load(runtime.config_path)
    selected_regions, selected_competitors = resolve_targets(config, regions, competitors)
    sqlite_storage = SQLiteTrackerStorage(runtime.database_path)

    raw_articles, provider_names, provider_errors = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=selected_regions,
        competitors=selected_competitors,
        days=days,
    )

    analyzer = CompetitorAnalyzer(min_score=min_score, config=config)
    analysis = analyzer.prefilter_raw_articles(raw_articles, regions=selected_regions)
    digest = DigestBuilder().build(
        competitors=selected_competitors,
        candidates=analysis.candidates,
        regions=selected_regions,
        digest_limit=config.daily_digest_limit,
        storage=sqlite_storage,
        delivery_channel="telegram" if telegram_mode in {"dry", "send"} else "daily_digest",
        delivery_destination=os.getenv("TELEGRAM_CHAT_ID", "")
        if telegram_mode in {"dry", "send"}
        else "",
        include_deferred=telegram_mode in {"dry", "send"},
    )
    alert_schemas, article_contexts = build_delivery_alert_schemas(digest.alerts)
    digest, alert_schemas, article_contexts, expired_alerts = filter_expired_digest_items(
        digest,
        alert_schemas,
        article_contexts,
    )
    for candidate in analysis.candidates:
        candidate.raw_article.metadata.setdefault("is_expired", False)
        sqlite_storage.merge_raw_article_metadata(
            url=candidate.url,
            metadata_updates={"is_expired": bool(candidate.raw_article.metadata.get("is_expired"))},
        )
    sqlite_storage.insert_alerts(digest.alerts)

    storage = JsonFileStorage(runtime.output_dir)
    candidates_path = storage.save_candidates(analysis.candidates)
    digest_path = storage.save_digest(digest)
    preview_path = storage.save_markdown_preview(
        digest.alerts,
        alert_schemas,
        generated_at=digest.generated_at,
    )
    candidates_csv_path = storage.save_candidates_csv(analysis.candidates) if export_csv else None
    run_summary = RunSummary(
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        regions=tuple(selected_regions),
        providers=provider_names,
        queries_generated=len(config.queries_for_regions(selected_regions)),
        raw_articles_collected=len(raw_articles),
        candidates_kept=len(analysis.candidates),
        alerts_created=len(digest.alerts),
        daily_digest_limit=config.daily_digest_limit,
        provider_errors=provider_errors,
    )
    summary_path = storage.save_run_summary(run_summary)

    telegram_result = None
    notion_result = None
    if telegram_mode in {"dry", "send"}:
        telegram_alerts, telegram_schemas, _ = select_telegram_delivery_payload(
            digest.alerts,
            alert_schemas,
            article_contexts,
        )
        if telegram_mode == "send":
            for alert in digest.alerts:
                sqlite_storage.mark_deferred(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    destination=os.getenv("TELEGRAM_CHAT_ID", ""),
                    metadata={
                        "mode": "daily_digest",
                        "generated_at": digest.generated_at,
                    },
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

    if notion_mode in {"dry", "sync"}:
        notion_sync = CompetitorNotionMirrorSync()
        notion_result = notion_sync.sync_alerts(
            list(zip(digest.alerts, alert_schemas)),
            dry_run=notion_mode == "dry",
        )

    return {
        "analysis": analysis,
        "digest": digest,
        "alert_schemas": alert_schemas,
        "article_contexts": article_contexts,
        "expired_alerts_count": len(expired_alerts),
        "query_count": len(config.queries_for_regions(selected_regions)),
        "candidates_path": candidates_path,
        "digest_path": digest_path,
        "preview_path": preview_path,
        "candidates_csv_path": candidates_csv_path,
        "summary_path": summary_path,
        "runtime": runtime,
        "raw_articles_count": len(raw_articles),
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
