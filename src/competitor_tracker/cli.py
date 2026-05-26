"""Command-line entrypoint for the new competitor tracker."""

from __future__ import annotations

import argparse
import logging
import os
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
from .geo_policy import GeoPolicy
from .models import CompetitorDigest, RunSummary
from .normalization import (
    deduplicate_raw_articles,
    normalize_title,
    normalize_url,
    parse_published_at,
)
from .notion_sync import CompetitorNotionMirrorSync
from .providers import Provider, ProviderError, ProviderRequest, build_providers
from .storage import JsonFileStorage, SQLiteTrackerStorage
from .telegram_sender import TelegramSender

COMMAND_NAMES = {"run", "dry-run", "send-digest", "sync-notion", "backfill"}
MAX_ARTICLE_AGE_DAYS = 7
HIGH_SIGNAL_SCORE_THRESHOLD = 7


def _today() -> date:
    """Return the current local date for freshness filtering."""
    return date.today()


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
    query_specs = []
    seen_queries = set()
    for region in regions:
        for query, competitor in config.query_specs_for_region(region):
            if query in seen_queries:
                continue
            seen_queries.add(query)
            query_specs.append((query, competitor, region))
    queries = [query for query, _, _ in query_specs]
    request = ProviderRequest(
        competitors=tuple(competitors),
        days=query_days,
        queries=queries,
        regions=tuple(regions),
        query_competitor_hints={
            query: (competitor,) for query, competitor, _ in query_specs
        },
    )
    query_owner_by_query = {
        query: {"query_owner_competitor": competitor, "query_owner_region": region}
        for query, competitor, region in query_specs
    }
    active_providers = list(providers) if providers is not None else build_providers(
        config.enabled_providers
    )

    raw_articles = []
    provider_errors: dict[str, str] = {}
    for provider in active_providers:
        try:
            fetched = provider.fetch(request)
        except ProviderError as exc:
            provider_errors[provider.name] = str(exc)
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

    raw_articles, provider_names, provider_errors = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=selected_regions,
        competitors=selected_competitors,
        days=days,
    )

    analyzer = CompetitorAnalyzer(min_score=min_score, config=config)
    analysis = analyzer.prefilter_raw_articles(raw_articles, regions=selected_regions)
    delivery_channel = (
        "telegram" if telegram_mode in {"dry", "send"} else "daily_digest"
    )
    delivery_destination = (
        os.getenv("TELEGRAM_CHAT_ID", "") if telegram_mode in {"dry", "send"} else ""
    )
    candidate_pool = list(analysis.candidates)

    if telegram_mode in {"dry", "send"}:
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

    regional_candidates = _deduplicate_region_candidates(
        candidate_pool,
        selected_regions=selected_regions,
        config=config,
    )

    all_digest_alerts = []
    all_alert_schemas = []
    all_article_contexts = []
    expired_alerts = []
    digest_generated_at = datetime.now(timezone.utc).isoformat()
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
        drop_reasons={
            reason: sum(1 for item in analysis.dropped_articles if item.reason == reason)
            for reason in sorted({item.reason for item in analysis.dropped_articles})
        },
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
            telegram_top_n=effective_telegram_top_n,
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
        "dropped_articles_path": dropped_articles_path,
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
