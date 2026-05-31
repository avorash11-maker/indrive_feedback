"""Storage abstractions for competitor tracker artifacts."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from math import pow
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from .formatter import format_daily_digest_markdown
from .models import Alert, CandidateArticle, CompetitorDigest, DeliveryRecord, DroppedArticle, RawArticle, RunSummary
from .product_logic import normalize_topic_group_name, presentable_region_name


class StorageBackend(Protocol):
    """Interface for persistence implementations."""

    def save_candidates(self, candidates: Sequence[CandidateArticle]) -> Path:
        """Persist normalized candidates and return the output path."""

    def save_dropped_articles(self, dropped_articles: Sequence[DroppedArticle]) -> Path:
        """Persist rejected raw-article audit records and return the output path."""

    def save_digest(self, digest: CompetitorDigest) -> Path:
        """Persist digest artifact and return the output path."""

    def save_run_summary(self, summary: RunSummary) -> Path:
        """Persist execution summary and return the output path."""

    def save_markdown_preview(
        self,
        alerts: Sequence[Alert],
        alert_schemas: Sequence[dict[str, Any]],
        *,
        generated_at: str,
        title: str = "Competitor Tracker Digest Preview",
    ) -> Path:
        """Persist a human-readable markdown preview and return the output path."""

    def save_candidates_csv(self, candidates: Sequence[CandidateArticle]) -> Path:
        """Persist a CSV export for manual QA and return the output path."""


class JsonFileStorage:
    """Small JSON-backed storage for the scaffolded tracker."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_candidates(self, candidates: Sequence[CandidateArticle]) -> Path:
        output_path = self.base_dir / "candidates.json"
        payload = [self._serialize_candidate(candidate) for candidate in candidates]
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_path

    def save_dropped_articles(self, dropped_articles: Sequence[DroppedArticle]) -> Path:
        output_path = self.base_dir / "dropped_articles.json"
        payload = [asdict(item) for item in dropped_articles]
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_path

    def save_digest(self, digest: CompetitorDigest) -> Path:
        output_path = self.base_dir / "digest.json"
        payload = asdict(digest)
        payload["regions"] = list(
            dict.fromkeys(presentable_region_name(region) for region in digest.regions)
        )
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_path

    def save_run_summary(self, summary: RunSummary) -> Path:
        output_path = self.base_dir / "run_summary.json"
        output_path.write_text(
            json.dumps(asdict(summary), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_path

    def save_markdown_preview(
        self,
        alerts: Sequence[Alert],
        alert_schemas: Sequence[dict[str, Any]],
        *,
        generated_at: str,
        title: str = "Competitor Tracker Digest Preview",
    ) -> Path:
        output_path = self.base_dir / "digest_preview.md"
        source_urls = [alert.candidate.url for alert in alerts]
        output_path.write_text(
            format_daily_digest_markdown(
                alert_schemas,
                source_urls=source_urls,
                generated_at=generated_at,
                title=title,
            ),
            encoding="utf-8",
        )
        return output_path

    def save_candidates_csv(
        self,
        candidates: Sequence[CandidateArticle],
        alert_schemas: Optional[Sequence[dict[str, Any]]] = None,
    ) -> Path:
        output_path = self.base_dir / "candidates_review.csv"
        with output_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "competitor",
                    "topic_group",
                    "score",
                    "is_expired",
                    "region",
                    "country_hint",
                    "language_hint",
                    "final_competitor",
                    "final_region",
                    "final_country",
                    "published_date",
                    "published_date_source",
                    "resolved_publication_date",
                    "resolved_publication_date_source",
                    "competitor_source",
                    "region_source",
                    "country_source",
                    "geo_validation_fallback",
                    "news_gatekeeper_accept",
                    "news_gatekeeper_canonical_topic",
                    "news_gatekeeper_relevance_reason",
                    "news_gatekeeper_priority_hint",
                    "news_gatekeeper_rejection_reason",
                    "product_take",
                    "product_risk",
                    "product_follow_up",
                    "product_strategist_invoked",
                    "product_strategist_trigger",
                    "title",
                    "source",
                    "published_at",
                    "url",
                    "matched_keywords",
                    "reasons",
                    "summary",
                ],
            )
            writer.writeheader()
            for index, candidate in enumerate(candidates):
                alert_schema = (
                    alert_schemas[index]
                    if alert_schemas is not None and index < len(alert_schemas)
                    else {}
                )
                writer.writerow(
                    {
                        "competitor": candidate.competitor,
                        "topic_group": candidate.topic_group,
                        "score": candidate.score,
                        "is_expired": bool(candidate.raw_article.metadata.get("is_expired")),
                        "region": candidate.region or "",
                        "country_hint": candidate.country_hint or "",
                        "language_hint": candidate.language_hint or "",
                        "final_competitor": alert_schema.get("competitor", ""),
                        "final_region": alert_schema.get("region", ""),
                        "final_country": alert_schema.get("country", ""),
                        "published_date": alert_schema.get("published_date", ""),
                        "published_date_source": alert_schema.get("published_date_source", ""),
                        "resolved_publication_date": alert_schema.get(
                            "resolved_publication_date", ""
                        ),
                        "resolved_publication_date_source": alert_schema.get(
                            "resolved_publication_date_source", ""
                        ),
                        "competitor_source": alert_schema.get("competitor_source", ""),
                        "region_source": alert_schema.get("region_source", ""),
                        "country_source": alert_schema.get("country_source", ""),
                        "geo_validation_fallback": alert_schema.get("geo_validation_fallback", ""),
                        "news_gatekeeper_accept": alert_schema.get("news_gatekeeper_accept", ""),
                        "news_gatekeeper_canonical_topic": alert_schema.get(
                            "news_gatekeeper_canonical_topic", ""
                        ),
                        "news_gatekeeper_relevance_reason": alert_schema.get(
                            "news_gatekeeper_relevance_reason", ""
                        ),
                        "news_gatekeeper_priority_hint": alert_schema.get(
                            "news_gatekeeper_priority_hint", ""
                        ),
                        "news_gatekeeper_rejection_reason": alert_schema.get(
                            "news_gatekeeper_rejection_reason", ""
                        ),
                        "product_take": alert_schema.get("product_take", ""),
                        "product_risk": alert_schema.get("product_risk", ""),
                        "product_follow_up": alert_schema.get("product_follow_up", ""),
                        "product_strategist_invoked": alert_schema.get(
                            "product_strategist_invoked", ""
                        ),
                        "product_strategist_trigger": alert_schema.get(
                            "product_strategist_trigger", ""
                        ),
                        "title": candidate.title,
                        "source": candidate.raw_article.source,
                        "published_at": candidate.raw_article.published_at or "",
                        "url": candidate.url,
                        "matched_keywords": " | ".join(candidate.matched_keywords),
                        "reasons": " | ".join(candidate.reasons),
                        "summary": candidate.summary,
                    }
                )
        return output_path

    @staticmethod
    def _serialize_candidate(candidate: CandidateArticle) -> dict[str, Any]:
        payload = asdict(candidate)
        payload.update(
            {
                "title": candidate.title,
                "url": candidate.url,
                "provider": candidate.provider,
                "source": candidate.raw_article.source,
                "published_at": candidate.raw_article.published_at,
                "query": candidate.raw_article.query,
            }
        )
        return payload


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SQLiteTrackerStorage:
    """SQLite-backed operational storage for competitor tracker history."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS articles_raw (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        provider TEXT NOT NULL,
        source TEXT NOT NULL,
        published_at TEXT,
        snippet TEXT NOT NULL,
        query_text TEXT NOT NULL,
        region TEXT,
        language TEXT,
        competitor_hints_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS article_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_article_id INTEGER NOT NULL,
        competitor TEXT NOT NULL,
        topic_group TEXT NOT NULL,
        score INTEGER NOT NULL,
        matched_keywords_json TEXT NOT NULL,
        summary TEXT NOT NULL,
        region TEXT,
        country_hint TEXT,
        language_hint TEXT,
        reasons_json TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(raw_article_id, competitor, topic_group),
        FOREIGN KEY(raw_article_id) REFERENCES articles_raw(id)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        digest_key TEXT NOT NULL UNIQUE,
        candidate_id INTEGER NOT NULL,
        headline TEXT NOT NULL,
        competitor TEXT NOT NULL,
        topic_group TEXT NOT NULL,
        severity TEXT NOT NULL,
        priority TEXT NOT NULL,
        confidence REAL NOT NULL,
        score INTEGER NOT NULL,
        reason TEXT NOT NULL,
        delivery_channels_json TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(candidate_id) REFERENCES article_candidates(id)
    );

    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        regions_json TEXT NOT NULL,
        providers_json TEXT NOT NULL,
        queries_generated INTEGER NOT NULL,
        raw_articles_collected INTEGER NOT NULL,
        candidates_kept INTEGER NOT NULL,
        alerts_created INTEGER NOT NULL,
        daily_digest_limit INTEGER NOT NULL,
        raw_articles_fetched INTEGER NOT NULL DEFAULT 0,
        raw_articles_deduplicated INTEGER NOT NULL DEFAULT 0,
        articles_filtered_out INTEGER NOT NULL DEFAULT 0,
        alerts_sent INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success',
        drop_reasons_json TEXT NOT NULL DEFAULT '{}',
        provider_errors_json TEXT NOT NULL,
        provider_diagnostics_json TEXT NOT NULL DEFAULT '{}',
        provider_metrics_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS delivery_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_key TEXT NOT NULL,
        channel TEXT NOT NULL,
        status TEXT NOT NULL,
        delivered_at TEXT,
        destination TEXT NOT NULL,
        external_id TEXT NOT NULL,
        error_message TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(alert_key, channel, destination)
    );

    CREATE TABLE IF NOT EXISTS rss_feed_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        measured_at TEXT NOT NULL,
        provider TEXT NOT NULL,
        region TEXT NOT NULL,
        feed_name TEXT NOT NULL,
        feed_url TEXT NOT NULL,
        items_found INTEGER NOT NULL DEFAULT 0,
        provider_matches INTEGER NOT NULL DEFAULT 0,
        raw_articles_after_global_dedup INTEGER NOT NULL DEFAULT 0,
        prefilter_passed INTEGER NOT NULL DEFAULT 0,
        candidates_kept INTEGER NOT NULL DEFAULT 0,
        alerts_created INTEGER NOT NULL DEFAULT 0,
        dropped_prefilter INTEGER NOT NULL DEFAULT 0,
        noise_ratio REAL NOT NULL DEFAULT 0,
        recommendation TEXT NOT NULL DEFAULT 'keep',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(run_id) REFERENCES runs(id)
    );
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(self.SCHEMA)
            self._migrate_runs_table(connection)
            self._migrate_rss_feed_metrics_table(connection)

    @staticmethod
    def _migrate_runs_table(connection: sqlite3.Connection) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        required_columns = {
            "raw_articles_fetched": "INTEGER NOT NULL DEFAULT 0",
            "raw_articles_deduplicated": "INTEGER NOT NULL DEFAULT 0",
            "articles_filtered_out": "INTEGER NOT NULL DEFAULT 0",
            "alerts_sent": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'success'",
            "drop_reasons_json": "TEXT NOT NULL DEFAULT '{}'",
            "provider_diagnostics_json": "TEXT NOT NULL DEFAULT '{}'",
            "provider_metrics_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                f"ALTER TABLE runs ADD COLUMN {column_name} {column_sql}"
            )

    @staticmethod
    def _migrate_rss_feed_metrics_table(connection: sqlite3.Connection) -> None:
        existing_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "rss_feed_metrics" not in existing_tables:
            return
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(rss_feed_metrics)").fetchall()
        }
        required_columns = {
            "run_id": "INTEGER",
            "measured_at": "TEXT NOT NULL DEFAULT ''",
            "provider": "TEXT NOT NULL DEFAULT ''",
            "region": "TEXT NOT NULL DEFAULT ''",
            "feed_name": "TEXT NOT NULL DEFAULT ''",
            "feed_url": "TEXT NOT NULL DEFAULT ''",
            "items_found": "INTEGER NOT NULL DEFAULT 0",
            "provider_matches": "INTEGER NOT NULL DEFAULT 0",
            "raw_articles_after_global_dedup": "INTEGER NOT NULL DEFAULT 0",
            "prefilter_passed": "INTEGER NOT NULL DEFAULT 0",
            "candidates_kept": "INTEGER NOT NULL DEFAULT 0",
            "alerts_created": "INTEGER NOT NULL DEFAULT 0",
            "dropped_prefilter": "INTEGER NOT NULL DEFAULT 0",
            "noise_ratio": "REAL NOT NULL DEFAULT 0",
            "recommendation": "TEXT NOT NULL DEFAULT 'keep'",
        }
        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                f"ALTER TABLE rss_feed_metrics ADD COLUMN {column_name} {column_sql}"
            )

    def has_seen_article(self, url: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM articles_raw WHERE url = ? LIMIT 1",
                (url,),
            ).fetchone()
        return row is not None

    def has_sent_alert(
        self,
        alert_key: str,
        channel: str,
        destination: str = "",
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM delivery_log
                WHERE alert_key = ?
                  AND channel = ?
                  AND destination = ?
                  AND status = 'delivered'
                LIMIT 1
                """,
                (alert_key, channel, destination),
            ).fetchone()
        return row is not None

    def insert_raw_article(self, article: RawArticle) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO articles_raw (
                    url, title, provider, source, published_at, snippet, query_text,
                    region, language, competitor_hints_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.url,
                    article.title,
                    article.provider,
                    article.source,
                    article.published_at,
                    article.snippet,
                    article.query,
                    article.region,
                    article.language,
                    _json_dumps(list(article.competitor_hints)),
                    _json_dumps(article.metadata),
                ),
            )
            row = connection.execute(
                "SELECT id FROM articles_raw WHERE url = ?",
                (article.url,),
            ).fetchone()
        return int(row["id"])

    def insert_raw_articles(self, articles: Sequence[RawArticle]) -> list[int]:
        return [self.insert_raw_article(article) for article in articles]

    def merge_raw_article_metadata(
        self,
        *,
        url: str,
        metadata_updates: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM articles_raw WHERE url = ?",
                (url,),
            ).fetchone()
            if row is None:
                return
            current_metadata = json.loads(row["metadata_json"] or "{}")
            current_metadata.update(metadata_updates)
            connection.execute(
                "UPDATE articles_raw SET metadata_json = ? WHERE url = ?",
                (_json_dumps(current_metadata), url),
            )

    def insert_candidate(self, candidate: CandidateArticle) -> int:
        raw_article_id = self.insert_raw_article(candidate.raw_article)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO article_candidates (
                    raw_article_id, competitor, topic_group, score, matched_keywords_json,
                    summary, region, country_hint, language_hint, reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_article_id,
                    candidate.competitor,
                    candidate.topic_group,
                    candidate.score,
                    _json_dumps(list(candidate.matched_keywords)),
                    candidate.summary,
                    candidate.region,
                    candidate.country_hint,
                    candidate.language_hint,
                    _json_dumps(list(candidate.reasons)),
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM article_candidates
                WHERE raw_article_id = ? AND competitor = ? AND topic_group = ?
                """,
                (raw_article_id, candidate.competitor, candidate.topic_group),
            ).fetchone()
        return int(row["id"])

    def insert_alert(self, alert: Alert) -> int:
        candidate_id = self.insert_candidate(alert.candidate)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO alerts (
                    digest_key, candidate_id, headline, competitor, topic_group,
                    severity, priority, confidence, score, reason, delivery_channels_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.digest_key,
                    candidate_id,
                    alert.headline,
                    alert.competitor,
                    alert.topic_group,
                    alert.severity,
                    alert.priority,
                    alert.confidence,
                    alert.score,
                    alert.reason,
                    _json_dumps(list(alert.delivery_channels)),
                ),
            )
            row = connection.execute(
                "SELECT id FROM alerts WHERE digest_key = ?",
                (alert.digest_key,),
            ).fetchone()
        return int(row["id"])

    def insert_alerts(self, alerts: Sequence[Alert]) -> list[int]:
        return [self.insert_alert(alert) for alert in alerts]

    def insert_run(self, summary: RunSummary) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    started_at, finished_at, regions_json, providers_json,
                    queries_generated, raw_articles_collected, candidates_kept,
                    alerts_created, daily_digest_limit, raw_articles_fetched,
                    raw_articles_deduplicated, articles_filtered_out, alerts_sent,
                    status, drop_reasons_json, provider_errors_json, provider_diagnostics_json,
                    provider_metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.started_at,
                    summary.finished_at,
                    _json_dumps(list(summary.regions)),
                    _json_dumps(list(summary.providers)),
                    summary.queries_generated,
                    summary.raw_articles_collected,
                    summary.candidates_kept,
                    summary.alerts_created,
                    summary.daily_digest_limit,
                    summary.raw_articles_fetched,
                    summary.raw_articles_deduplicated,
                    summary.articles_filtered_out,
                    summary.alerts_sent,
                    summary.status,
                    _json_dumps(summary.drop_reasons),
                    _json_dumps(summary.provider_errors),
                    _json_dumps(summary.provider_diagnostics),
                    _json_dumps(summary.provider_metrics),
                ),
            )
        return int(cursor.lastrowid)

    @staticmethod
    def recommend_feed_action(
        *,
        items_found: int,
        provider_matches: int,
        prefilter_passed: int,
        alerts_created: int,
    ) -> str:
        if items_found >= 15 and provider_matches == 0:
            return "consider_removal_broad_feed"
        if provider_matches >= 8 and alerts_created == 0 and prefilter_passed <= 1:
            return "consider_removal_high_noise"
        if provider_matches >= 5 and prefilter_passed <= 1:
            return "review_low_signal"
        if provider_matches >= 5 and alerts_created == 0:
            return "review_low_alert_yield"
        return "keep"

    def insert_feed_metric_rows(
        self,
        *,
        run_id: Optional[int],
        rows: Sequence[dict[str, Any]],
    ) -> list[int]:
        inserted_ids: list[int] = []
        if not rows:
            return inserted_ids
        with self._connect() as connection:
            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO rss_feed_metrics (
                        run_id,
                        measured_at,
                        provider,
                        region,
                        feed_name,
                        feed_url,
                        items_found,
                        provider_matches,
                        raw_articles_after_global_dedup,
                        prefilter_passed,
                        candidates_kept,
                        alerts_created,
                        dropped_prefilter,
                        noise_ratio,
                        recommendation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(row.get("measured_at") or ""),
                        str(row.get("provider") or ""),
                        str(row.get("region") or ""),
                        str(row.get("feed_name") or ""),
                        str(row.get("feed_url") or ""),
                        int(row.get("items_found") or 0),
                        int(row.get("provider_matches") or 0),
                        int(row.get("raw_articles_after_global_dedup") or 0),
                        int(row.get("prefilter_passed") or 0),
                        int(row.get("candidates_kept") or 0),
                        int(row.get("alerts_created") or 0),
                        int(row.get("dropped_prefilter") or 0),
                        float(row.get("noise_ratio") or 0.0),
                        str(row.get("recommendation") or "keep"),
                    ),
                )
                inserted_ids.append(int(cursor.lastrowid))
        return inserted_ids

    def get_feed_health_report(
        self,
        *,
        days: int = 30,
        min_items_found: int = 5,
        limit: int = 20,
    ) -> dict[str, Any]:
        lookback_days = max(1, int(days))
        row_limit = max(1, int(limit))
        min_found = max(0, int(min_items_found))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    provider,
                    region,
                    feed_name,
                    feed_url,
                    COUNT(*) AS snapshots,
                    SUM(items_found) AS items_found,
                    SUM(provider_matches) AS provider_matches,
                    SUM(raw_articles_after_global_dedup) AS raw_articles_after_global_dedup,
                    SUM(prefilter_passed) AS prefilter_passed,
                    SUM(candidates_kept) AS candidates_kept,
                    SUM(alerts_created) AS alerts_created,
                    SUM(dropped_prefilter) AS dropped_prefilter
                FROM rss_feed_metrics
                WHERE measured_at >= datetime('now', ?)
                GROUP BY provider, region, feed_name, feed_url
                HAVING SUM(items_found) >= ?
                ORDER BY
                    CASE
                        WHEN SUM(provider_matches) > 0
                            THEN 1.0 - (CAST(SUM(candidates_kept) AS REAL) / SUM(provider_matches))
                        ELSE CASE WHEN SUM(items_found) > 0 THEN 1.0 ELSE 0.0 END
                    END DESC,
                    SUM(items_found) DESC,
                    feed_name ASC
                LIMIT ?
                """,
                (f"-{lookback_days} days", min_found, row_limit),
            ).fetchall()
        feeds: list[dict[str, Any]] = []
        recommendations: list[dict[str, Any]] = []
        for row in rows:
            items_found = int(row["items_found"] or 0)
            provider_matches = int(row["provider_matches"] or 0)
            candidates_kept = int(row["candidates_kept"] or 0)
            prefilter_passed = int(row["prefilter_passed"] or 0)
            alerts_created = int(row["alerts_created"] or 0)
            noise_ratio = (
                round(1.0 - (candidates_kept / provider_matches), 4)
                if provider_matches > 0
                else (1.0 if items_found > 0 else 0.0)
            )
            recommendation = self.recommend_feed_action(
                items_found=items_found,
                provider_matches=provider_matches,
                prefilter_passed=prefilter_passed,
                alerts_created=alerts_created,
            )
            feed_row = {
                "provider": str(row["provider"] or ""),
                "region": str(row["region"] or ""),
                "feed_name": str(row["feed_name"] or ""),
                "feed_url": str(row["feed_url"] or ""),
                "snapshots": int(row["snapshots"] or 0),
                "items_found": items_found,
                "provider_matches": provider_matches,
                "raw_articles_after_global_dedup": int(row["raw_articles_after_global_dedup"] or 0),
                "prefilter_passed": prefilter_passed,
                "candidates_kept": candidates_kept,
                "alerts_created": alerts_created,
                "dropped_prefilter": int(row["dropped_prefilter"] or 0),
                "noise_ratio": noise_ratio,
                "recommendation": recommendation,
            }
            feeds.append(feed_row)
            if recommendation != "keep":
                recommendations.append(
                    {
                        "feed_name": feed_row["feed_name"],
                        "feed_url": feed_row["feed_url"],
                        "region": feed_row["region"],
                        "recommendation": recommendation,
                        "reason": (
                            f"items_found={items_found} provider_matches={provider_matches} "
                            f"candidates_kept={candidates_kept} alerts_created={alerts_created} "
                            f"noise_ratio={noise_ratio}"
                        ),
                    }
                )
        highest_noise_feed = feeds[0] if feeds else None
        return {
            "days": lookback_days,
            "min_items_found": min_found,
            "feed_count": len(feeds),
            "highest_noise_feed": highest_noise_feed,
            "recommendations": recommendations,
            "feeds": feeds,
        }

    def get_query_precision_by_text(
        self,
        queries: Sequence[str],
        *,
        half_life_days: float = 30.0,
        now: Optional[datetime] = None,
    ) -> dict[str, float]:
        normalized_queries = [str(query).strip() for query in queries if str(query).strip()]
        if not normalized_queries:
            return {}
        placeholders = ", ".join("?" for _ in normalized_queries)
        effective_half_life_days = max(float(half_life_days), 0.0001)
        reference_now = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    r.query_text,
                    r.created_at,
                    CASE WHEN COUNT(DISTINCT c.id) > 0 THEN 1 ELSE 0 END AS has_candidate,
                    CASE WHEN COUNT(DISTINCT a.id) > 0 THEN 1 ELSE 0 END AS has_alert
                FROM articles_raw r
                LEFT JOIN article_candidates c
                    ON c.raw_article_id = r.id
                LEFT JOIN alerts a
                    ON a.candidate_id = c.id
                WHERE r.query_text IN ({placeholders})
                GROUP BY r.id, r.query_text, r.created_at
                """,
                tuple(normalized_queries),
            ).fetchall()
        totals: dict[str, dict[str, float]] = {}
        for row in rows:
            created_at_raw = str(row["created_at"] or "").strip()
            created_at = reference_now
            if created_at_raw:
                normalized = created_at_raw.replace("Z", "+00:00")
                try:
                    created_at = datetime.fromisoformat(normalized)
                except ValueError:
                    created_at = reference_now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = max((reference_now - created_at.astimezone(timezone.utc)).total_seconds() / 86400.0, 0.0)
            weight = pow(0.5, age_days / effective_half_life_days)
            bucket = totals.setdefault(
                str(row["query_text"]),
                {
                    "raw_weight": 0.0,
                    "candidate_weight": 0.0,
                    "alert_weight": 0.0,
                },
            )
            bucket["raw_weight"] += weight
            if int(row["has_candidate"] or 0):
                bucket["candidate_weight"] += weight
            if int(row["has_alert"] or 0):
                bucket["alert_weight"] += weight
        scores: dict[str, float] = {}
        for query_text, bucket in totals.items():
            raw_weight = max(bucket["raw_weight"], 0.0001)
            candidate_ratio = bucket["candidate_weight"] / raw_weight
            alert_ratio = bucket["alert_weight"] / raw_weight
            precision = (candidate_ratio * 0.4) + (alert_ratio * 0.6)
            confidence = raw_weight / (raw_weight + 2.0)
            scores[query_text] = round(precision * confidence, 4)
        return scores

    def log_delivery(self, record: DeliveryRecord) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO delivery_log (
                    alert_key, channel, status, delivered_at, destination,
                    external_id, error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.alert_key,
                    record.channel,
                    record.status,
                    record.delivered_at,
                    record.destination,
                    record.external_id,
                    record.error_message,
                    _json_dumps(record.metadata),
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM delivery_log
                WHERE alert_key = ? AND channel = ? AND destination = ?
                """,
                (record.alert_key, record.channel, record.destination),
            ).fetchone()
        return int(row["id"])

    def mark_delivered(
        self,
        *,
        alert_key: str,
        channel: str,
        delivered_at: str,
        destination: str = "",
        external_id: str = "",
        metadata: Optional[dict[str, str]] = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_log (
                    alert_key, channel, status, delivered_at, destination,
                    external_id, error_message, metadata_json
                ) VALUES (?, ?, 'delivered', ?, ?, ?, '', ?)
                ON CONFLICT(alert_key, channel, destination) DO UPDATE SET
                    status = 'delivered',
                    delivered_at = excluded.delivered_at,
                    external_id = excluded.external_id,
                    error_message = '',
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    alert_key,
                    channel,
                    delivered_at,
                    destination,
                    external_id,
                    _json_dumps(metadata or {}),
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM delivery_log
                WHERE alert_key = ? AND channel = ? AND destination = ?
                """,
                (alert_key, channel, destination),
            ).fetchone()
        return int(row["id"])

    def mark_deferred(
        self,
        *,
        alert_key: str,
        channel: str,
        destination: str = "",
        metadata: Optional[dict[str, str]] = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_log (
                    alert_key, channel, status, delivered_at, destination,
                    external_id, error_message, metadata_json
                ) VALUES (?, ?, 'deferred', NULL, ?, '', '', ?)
                ON CONFLICT(alert_key, channel, destination) DO UPDATE SET
                    status = CASE
                        WHEN delivery_log.status = 'delivered' THEN delivery_log.status
                        ELSE 'deferred'
                    END,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    alert_key,
                    channel,
                    destination,
                    _json_dumps(metadata or {}),
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM delivery_log
                WHERE alert_key = ? AND channel = ? AND destination = ?
                """,
                (alert_key, channel, destination),
            ).fetchone()
        return int(row["id"])

    def expire_stale_deferred(
        self,
        *,
        channel: str,
        destination: str = "",
        max_age_days: int = 2,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_log
                SET status = 'expired',
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel = ?
                  AND destination = ?
                  AND status = 'deferred'
                  AND alert_key IN (
                      SELECT digest_key
                      FROM alerts
                      WHERE created_at < datetime('now', ?)
                  )
                """,
                (channel, destination, f"-{max_age_days} days"),
            )
        return int(cursor.rowcount or 0)

    def get_recent_alert_history(
        self,
        *,
        channel: str,
        destination: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    a.digest_key,
                    a.headline,
                    a.competitor,
                    a.topic_group,
                    a.priority,
                    a.confidence,
                    a.score,
                    c.region,
                    c.country_hint,
                    r.title AS article_title,
                    r.published_at,
                    a.created_at
                FROM alerts a
                JOIN article_candidates c ON c.id = a.candidate_id
                JOIN articles_raw r ON r.id = c.raw_article_id
                WHERE EXISTS (
                    SELECT 1
                    FROM delivery_log d
                    WHERE d.alert_key = a.digest_key
                      AND d.channel = ?
                      AND d.destination = ?
                      AND d.status = 'delivered'
                )
                ORDER BY COALESCE(r.published_at, a.created_at) DESC, a.id DESC
                LIMIT ?
                """,
                (channel, destination, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_deferred_candidates(
        self,
        *,
        channel: str,
        destination: str = "",
        max_age_days: int = 2,
        limit: int = 100,
    ) -> list[CandidateArticle]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    a.digest_key,
                    a.priority,
                    a.confidence,
                    a.score AS alert_score,
                    c.competitor,
                    c.topic_group,
                    c.score AS candidate_score,
                    c.matched_keywords_json,
                    c.summary,
                    c.region,
                    c.country_hint,
                    c.language_hint,
                    c.reasons_json,
                    r.title,
                    r.url,
                    r.provider,
                    r.source,
                    r.published_at,
                    r.snippet,
                    r.query_text,
                    r.language,
                    r.competitor_hints_json,
                    r.metadata_json
                FROM alerts a
                JOIN article_candidates c ON c.id = a.candidate_id
                JOIN articles_raw r ON r.id = c.raw_article_id
                JOIN delivery_log d
                    ON d.alert_key = a.digest_key
                   AND d.channel = ?
                   AND d.destination = ?
                WHERE d.status = 'deferred'
                  AND a.created_at >= datetime('now', ?)
                ORDER BY COALESCE(r.published_at, a.created_at) DESC, a.id DESC
                LIMIT ?
                """,
                (channel, destination, f"-{max_age_days} days", limit),
            ).fetchall()

        deferred_candidates: list[CandidateArticle] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata["deferred_digest_key"] = row["digest_key"]
            raw_article = RawArticle(
                title=row["title"],
                url=row["url"],
                provider=row["provider"],
                source=row["source"],
                published_at=row["published_at"],
                snippet=row["snippet"],
                query=row["query_text"],
                region=row["region"],
                language=row["language"],
                competitor_hints=tuple(json.loads(row["competitor_hints_json"] or "[]")),
                metadata=metadata,
            )
            deferred_candidates.append(
                CandidateArticle(
                    raw_article=raw_article,
                    competitor=row["competitor"],
                    topic_group=normalize_topic_group_name(row["topic_group"]),
                    score=int(row["candidate_score"]),
                    matched_keywords=tuple(json.loads(row["matched_keywords_json"] or "[]")),
                    summary=row["summary"],
                    region=row["region"],
                    country_hint=row["country_hint"],
                    language_hint=row["language_hint"],
                    reasons=tuple(json.loads(row["reasons_json"] or "[]")),
                )
            )
        return deferred_candidates
