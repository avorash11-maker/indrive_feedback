# Competitor Tracker MVP

Config-driven competitor intelligence pipeline for daily market monitoring. The system collects low-cost public signals, removes duplicates, applies a cheap rule-based prefilter before any LLM step, ranks the strongest candidates, and delivers a compact digest to Telegram with an optional Notion mirror.

## Product Split

- `competitor_tracker` is the new MVP product branch in this repository.
- `legacy pipeline` (`indrive_media`) stays in place and is not the source of truth for the new competitor tracker.

## What This MVP Does

The current MVP is optimized for a practical daily monitoring loop:

- `SQLite` is the operational source of truth for raw articles, candidates, alerts, runs, and delivery history.
- Regions, competitors, topic groups, keyword templates, provider list, and digest limit are driven by config.
- `competitors_by_region` is the strict source-of-truth matrix for both query generation and downstream validation of `competitor + region` pairs.
- News collection uses low-cost public sources: `Google News RSS` and `GDELT`.
- A rule-based prefilter detects competitor, topic, region, country/language hints, and baseline score before any LLM call.
- `competitor` and `region` are treated as pipeline-detected signals during alert enrichment; the LLM should preserve them unless the article contains explicit evidence that the pipeline signal is wrong.
- In practice, final identity resolution is conservative:
  - `candidate.competitor` always wins over a conflicting LLM competitor
  - `candidate.region` wins when the pipeline already detected a region
  - if the pipeline region is missing, the system may still restore a region from validated country evidence and config, but not from a free LLM guess alone
- Digest ranking prioritizes `priority -> freshness -> confidence/score`.
- Duplicate suppression works both within one run and across history.
- Publication date is resolved into one canonical field before final digest ranking:
  - the internal source of truth is `resolved_publication_date`
  - provider / normalized `published_at`, HTML extraction, and optional LLM inference all feed the same resolver
  - final priority is `llm > html_scraped > provider > undated_fallback`
  - undated items keep a technical sentinel internally, but user-facing `published_date` stays empty for those cases
- Pre-ranking date enrichment is no longer limited to fully undated articles:
  - missing provider dates can trigger early HTML extraction
  - clearly suspicious provider dates such as stale or future dates can also trigger early HTML extraction before ranking
  - this prevents a good HTML date from being ignored during digest freshness sorting
- Post-ranking LLM enrichment is region-scoped, not global:
  - the pipeline builds a local digest for each selected macro-region first
  - `llm_top_n` is then applied inside each region independently
  - this prevents one region from consuming the entire LLM slice for the whole run
- Final digest delivery applies a `7-day` freshness gate:
  - clearly stale alerts are excluded from the main digest and Telegram
  - stale-but-important newly detected alerts can still pass through a high-signal override
  - undated alerts are treated as suspicious by default and are filtered out unless explicitly allowed
  - archived stale articles are kept with `is_expired = True` for QA and debugging
- Telegram delivery uses a bounded carry-over queue:
  - `delivered` alerts are not retried
  - relevant alerts that miss the Telegram top cap become `deferred`
  - deferred alerts can compete again on the next Telegram run for up to `48 hours`
  - stale deferred alerts become `expired`
- Final alerts can be:
  - previewed locally as `JSON`, `Markdown`, optional `CSV`
  - sent to `Telegram`
  - mirrored to `Notion` as a showcase/archive layer

## Why This Architecture

The design goal is not “use AI everywhere”, but “use AI only where it helps”.

- `SQLite first`: the system keeps local operational history and does not depend on Notion to run.
- `Cheap before smart`: rule-based filtering reduces unnecessary LLM usage and lowers daily cost.
- `Config over code`: new regions, competitors, and topic groups can be adjusted without rewriting pipeline logic.
- `Truth before rewrite`: the LLM is not allowed to freely reinterpret `competitor` and `region`; config and pipeline detections stay primary unless the article clearly disproves them.
- `Digest, not firehose`: suppression and ranking keep the daily output readable for humans.
- `Carry-over, not loss`: relevant alerts that miss today's Telegram window can re-enter the next run, but only for a limited time.
- `Dates with guardrails`: the pipeline resolves one canonical publication date before ranking, stale gating, Telegram, Notion, and CSV export, and very old articles are archived unless they qualify as strong newly detected signals.
- `Optional integrations`: Telegram and Notion are delivery layers, not core state.

## Current Stack

- Python
- `SQLite` for operational storage
- `Google News RSS` and `GDELT` for ingestion
- optional `OpenAI` step for richer alert narratives
- `Telegram` for delivery
- optional `Notion` mirror for archive / portfolio presentation
- `pytest` for unit and integration-style coverage

## Core Flow

```text
Config
  -> Query Expansion
  -> Providers (Google News RSS, GDELT)
  -> Normalization + Raw Deduplication
  -> Rule-Based Prefilter
  -> SQLite History Checks
  -> Pre-Ranking Date Resolution
  -> Regional Ranking + Suppression
  -> Regional LLM / Fallback Split
  -> Final Alert Formatting
  -> Telegram / Notion / Local Artifacts
```

Detailed architecture: [docs/architecture.md](/C:/Users/shar0/Desktop/indrive_feedback/docs/architecture.md)

## Repository Highlights

- [src/competitor_tracker/config.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/config.py)
  Config loader, validation, and query expansion.
- [src/competitor_tracker/providers.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/providers.py)
  Low-cost provider adapters for `Google News RSS` and `GDELT`.
- [src/competitor_tracker/analyzer.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/analyzer.py)
  Rule-based prefilter plus alert analyzer.
- [src/competitor_tracker/digest.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/digest.py)
  Ranking and suppression logic.
- [src/competitor_tracker/storage.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/storage.py)
  `SQLite` operational storage plus local artifact persistence.
- [src/competitor_tracker/telegram_sender.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/telegram_sender.py)
  Telegram delivery with dry-run and delivery logging.
- [src/competitor_tracker/notion_sync.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/notion_sync.py)
  Optional Notion mirror.
- [src/competitor_tracker/cli.py](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/cli.py)
  Separate CLI for the new system.

## Quick Start

Install the project in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev]
```

Run all tests:

```powershell
.\.venv\Scripts\python -m pytest
```

Run the new tracker locally:

```powershell
.\.venv\Scripts\python -m competitor_tracker run --days 7 --export-csv
```

Or use the packaged entrypoint:

```powershell
.\.venv\Scripts\competitor-tracker run --days 7 --export-csv
```

## CLI Commands

The new CLI supports:

- `run`
- `dry-run`
- `send-digest`
- `sync-notion`
- `backfill`
- `test-provider`
- `qa-feeds`

Examples:

```powershell
.\.venv\Scripts\competitor-tracker dry-run --days 7 --export-csv
.\.venv\Scripts\competitor-tracker send-digest --dry-run --region sea
.\.venv\Scripts\competitor-tracker sync-notion --dry-run --region sea
.\.venv\Scripts\competitor-tracker backfill --days 30 --region sea --export-csv
.\.venv\Scripts\competitor-tracker test-provider --provider google_news_rss --query "Grab launch Philippines"
.\.venv\Scripts\competitor-tracker qa-feeds --days 30 --min-feed-items 5 --limit 15
```

`test-provider` is the fastest way to separate `no data` from `provider/network/parser` failures during live ingestion checks. It prints structured JSON diagnostics with:

- provider name
- query
- request URL with secrets redacted
- HTTP status when available
- exception text
- items found before provider-side filtering
- items kept after provider-side filtering
- items left after global deduplication

`qa-feeds` reads stored feed-level QA snapshots from SQLite and highlights:

- how many items each curated RSS feed published
- how many survived provider-side competitor matching
- how many survived the tracker prefilter
- how many became final alerts
- which feeds currently have the highest noise ratio
- which feeds should be reviewed or removed from the whitelist

For quick local automation you can also run [qa_competitor_feeds.py](/abs/path/C:/Users/shar0/Desktop/indrive_feedback/scripts/qa_competitor_feeds.py).

## Configuration

The tracker uses one config file as source of truth:

- [default_config.json](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/default_config.json)

It defines:

- `regions`
- `competitors_by_region`
- `topic_groups`
- `keyword_templates`
- `ignored_geo_terms`
- `daily_digest_limit`
- `enabled_providers`

Important config contract:

- `competitors_by_region` is not only a query-expansion helper.
- It is the validation matrix for allowed `competitor + region` combinations in the new tracker flow.
- If a detected or LLM-returned pair conflicts with this matrix, the pipeline should reject or normalize it instead of trusting free-form model output.
- Regions can also define `country_validation_terms` as a broader vocabulary for safe `country` validation in final alert schemas.
- This allows the tracker to accept valid country values that are broader than the query-oriented `geo_terms`, without weakening the fallback rules.
- `ignored_geo_terms` defines global non-target geo markers such as `USA` or `Europe`. If article text clearly matches one of these markers and does not also contain explicit target-region confirmation, the prefilter drops the article before it can reach the digest.
- `run_summary.json` now includes aggregated `drop_reasons` so geo-policy rejections and other prefilter losses are auditable after each run.
- `run_summary.json` also includes `provider_diagnostics`, so a `raw_articles=0` run can now be explained as `no provider data`, `network error`, `HTTP 403/429`, `feed/parser breakage`, or `deduped away`.
- `dropped_articles.json` stores article-level rejection records with structured reasons and details for manual QA of the prefilter.
- For competitors that are valid in multiple regions, region is not inferred from competitor alone. The tracker uses detected geo/country hints first; if those hints are missing or ambiguous, it preserves the pipeline-detected region when available or leaves region empty rather than trusting an LLM guess.
- Final outward-facing alert schemas map internal `africa` and `mea` region keys to the shared business label `Africa & MEA`, so Telegram and Notion stay aligned to one macro-region.
- Final alert schemas can include internal provenance flags such as `competitor_source`, `region_source`, `country_source`, `published_date_source`, and `geo_validation_fallback` for QA and downstream validation.
- Final alert schemas also include `resolved_publication_date` and `resolved_publication_date_source` as the canonical date pair used by ranking and freshness logic.
- `region_source` may also expose `geo_country_override` when the pipeline had no trusted region, but config-backed country validation was sufficient to restore one safely.
- `country` validation understands configured country vocabulary plus common aliases and ISO-style country codes.
- Local review artifacts such as markdown preview and review CSV should expose geo/date validation outcomes for manual QA, while the Telegram card should stay concise and human-readable.

The default MVP config is now aligned to these monitoring themes:

- `market_expansion`
  keywords like `launching in`, `new city`, `entering market`, `expansion`
- `campaign_launches`
  keywords like `campaign`, `partnership`, `brand ambassador`, `new feature`
- `pricing_promo`
  keywords like `discount`, `promo code`, `price cut`, `subscription`
- `industry_context`
  terms like `ride-hailing`, `e-hailing`, `on-demand mobility`, `ride-sharing`, `taxi app`, `VTC`, `MaaS`
- `strategic_operations`
  terms like `market entry`, `launching operations`, `license obtained`, `regulatory approval`, `strategic partnership`, `driver recruitment campaign`
- `performance_growth`
  terms like `first ride free`, `discounted rides`, `referral bonus`, `loyalty program`, `low commission for drivers`, `bonus for new drivers`
- `product_features_innovation`
  terms like `intercity`, `delivery`, `courier service`, `freight`, `fixed price`, `bidding model`, `safety features`

Runtime settings come from env:

```env
COMPETITOR_TRACKER_CONFIG_PATH=
COMPETITOR_TRACKER_OUTPUT_DIR=
COMPETITOR_TRACKER_DB_PATH=
COMPETITOR_TRACKER_LOOKBACK_DAYS=
COMPETITOR_TRACKER_MIN_SCORE=
COMPETITOR_TRACKER_USE_LLM_ALERTS=
COMPETITOR_TRACKER_LLM_TOP_N=
COMPETITOR_TRACKER_TELEGRAM_TOP_N=
COMPETITOR_TRACKER_ARTICLE_CONTEXT_MAX_CHARS=
COMPETITOR_TRACKER_ENABLE_NEWSAPI_FULL_RUN=false
COMPETITOR_TRACKER_NEWSAPI_MAX_QUERIES_PER_RUN=25
COMPETITOR_TRACKER_NEWSAPI_DAILY_REQUEST_LIMIT=90
COMPETITOR_TRACKER_NEWSAPI_CACHE_TTL_SECONDS=600
COMPETITOR_TRACKER_NEWSAPI_COOLDOWN_SECONDS=900
COMPETITOR_TRACKER_GUARDIAN_MAX_QUERIES_PER_RUN=40
COMPETITOR_TRACKER_GUARDIAN_DAILY_REQUEST_LIMIT=450
COMPETITOR_TRACKER_GUARDIAN_CACHE_TTL_SECONDS=900
COMPETITOR_TRACKER_GUARDIAN_COOLDOWN_SECONDS=900
COMPETITOR_TRACKER_HISTORICAL_PRECISION_HALF_LIFE_DAYS=30
GUARDIAN_API_KEY=

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

NOTION_TOKEN=
NOTION_DATABASE_ID=
COMPETITOR_TRACKER_NOTION_DATABASE_ID=
```

NewsAPI is now treated as an opt-in provider for full pipeline runs. By default, the repository config uses `gdelt`, `google_news_rss`, and a curated `regional_rss` tier, while `newsapi` remains available for `test-provider` and manual diagnostics. If you explicitly enable NewsAPI for a full run, the tracker applies a local query cap, daily request budget, TTL cache, and cooldown after `429 rateLimited` responses.

`competitor_tracker` now loads `.env` automatically, so the CLI reads keys from one place without manual shell export. Canonical names are:

- `OPENAI_API_KEY`
- `NEWS_API_KEY`
- `GUARDIAN_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `NOTION_TOKEN`
- `COMPETITOR_TRACKER_NOTION_DATABASE_ID`

`NOTION_DATABASE_ID` is still accepted only as a legacy fallback.

`guardian` is enabled in the default provider stack as an additional tier-2 direct source whenever `GUARDIAN_API_KEY` is configured. Full runs apply the same safety pattern there too: per-run query caps, daily request budget, local TTL cache, and cooldown after `429` responses.

`regional_rss` now ships with a wider curated whitelist by zone:

- `LATAM`: EL PAIS America, Mexico News Daily, Buenos Aires Times, MercoPress
- `Africa`: AllAfrica Kenya, Nigeria, and South Africa slices
- `MEA`: AGBI, Doha News
- `SEA`: CNA Asia
- `CIS / Central Asia`: Civil Georgia, Astana Times, The Times of Central Asia

Query generation now follows `topic_priority_groups`, and equal-priority queries are further reordered by historical precision from SQLite so providers spend caps on the queries that have produced the best candidate and alert yield in past runs. That score now uses time decay plus a confidence factor on recent evidence volume, so a query that was great months ago does not permanently outrank fresher winners.

Important:

- `Telegram` is optional.
- `Notion` is optional.
- Without `Notion` env, the pipeline should skip mirror sync with a warning, not fail.
- `SQLite` remains the main operational store either way.

## Output Artifacts

Each run can generate a review-friendly set of artifacts in `output/competitor_tracker/`:

- `run_summary.json`
- `candidates.json`
- `dropped_articles.json`
- `digest.json`
- `digest_preview.md`
- `candidates_review.csv` when `--export-csv` is enabled
- `tracker.db`

Important ingestion diagnostics detail:

- `provider_errors` is the compact high-level failure map.
- `provider_diagnostics` is the structured per-provider payload with query-level request and count details.
- `provider_metrics` is the compact per-provider KPI block with values such as `cache_hits`, `skipped_items`, `budget_hits`, `cooldown_hits`, `feeds_skipped`, `items_after_global_dedup`, and `source_tier_wins`.
- `rss_feed_metrics` in SQLite stores per-run feed QA snapshots for curated RSS sources, including `items_found`, `provider_matches`, `prefilter_passed`, `candidates_kept`, `alerts_created`, `noise_ratio`, and auto-generated cleanup recommendations.
- If `test-provider` fails for several providers and queries with connection or DNS-style errors, that points to the environment or network path, not necessarily to tracker code.

The markdown preview intentionally uses readable Russian section labels where helpful for manual review:

- `Что произошло`
- `Почему это важно`
- `Потенциальное влияние`
- `Что делать`

This makes it easier to validate false positives and signal quality over a 1-2 week test period without querying SQLite by hand.

Important QA detail:

- `candidates_review.csv` now includes `is_expired`
- `digest_preview.md` and `candidates_review.csv` also include geo/date validation outcomes such as `competitor_source`, `region_source`, `country_source`, `published_date_source`, and `geo_validation_fallback`
- `candidates_review.csv` also stores `resolved_publication_date` and `resolved_publication_date_source` for technical QA and debugging
- old articles that miss the `7-day` freshness gate stay in the archive for review
- late-discovered but high-signal articles can still surface in the digest when they clearly outrank the average signal level

## Delivery Layers

### Telegram

Telegram is the main delivery channel for the MVP.

- supports `dry-run`
- logs delivery history into `SQLite`
- enables suppression of already sent alerts
- uses its own `telegram_top_n` delivery slice instead of reusing the LLM cap
- excludes articles older than `7 days` from the main digest by default
- allows a high-signal override for stale but newly detected important articles
- keeps a Telegram-specific `deferred` queue for relevant alerts that miss the top slice
- retries deferred alerts for up to `48 hours`
- marks stale deferred alerts as `expired`
- gives fresh new alerts a slight ranking advantage over yesterday's carry-over alerts

### Notion

Notion is an optional mirror, not the database of record.

- final alerts can be archived to a separate competitor tracker database
- Notion reads already computed `resolved_publication_date` from the final schema and only falls back to the resolver if that field is unexpectedly missing
- dry-run is supported
- if env is missing, sync is skipped safely

## Reliability

The new branch is covered by:

- unit tests for config, models, normalization, storage, providers, ranking, formatter, Telegram, and Notion
- integration-style tests for:
  - full dry-run with mocked providers
  - duplicate suppression across runs
  - already-sent suppression
  - digest top-cap behavior
  - provider partial failure
  - optional Notion behavior

Run them with:

```powershell
.\.venv\Scripts\python -m pytest
```

## GitHub Actions

The legacy CI workflow stays untouched.

The new tracker has its own workflow:

- [competitor-tracker.yml](/C:/Users/shar0/Desktop/indrive_feedback/.github/workflows/competitor-tracker.yml)

It supports:

- daily schedule
- manual dispatch
- scheduled Telegram delivery by default
- manual dry-run mode
- secrets for Telegram / Notion / OpenAI
- artifact upload from `output/competitor_tracker/`

Required GitHub Secrets for scheduled production delivery:

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional GitHub Secrets:

- `NOTION_TOKEN`
- `COMPETITOR_TRACKER_NOTION_DATABASE_ID`
- `NOTION_DATABASE_ID`

Behavior:

- scheduled runs send the digest to `Telegram` by default
- manual `workflow_dispatch` keeps `dry_run` support for safe testing
- real Telegram delivery fails fast with a clear error if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is missing
- scheduled Notion sync turns on only when both `NOTION_TOKEN` and `COMPETITOR_TRACKER_NOTION_DATABASE_ID` are present
- missing Notion secrets do not block Telegram delivery

## Legacy Pipeline

The repository still contains the original `indrive_media` pipeline and its CLI:

- [src/indrive_media/main.py](/C:/Users/shar0/Desktop/indrive_feedback/src/indrive_media/main.py)
- [main.py](/C:/Users/shar0/Desktop/indrive_feedback/main.py)

That legacy flow remains available and should not be assumed to behave like the new competitor tracker MVP.

## Portfolio Framing

This repository can now be shown as a portfolio case for:

- productized competitor monitoring
- low-cost AI pipeline design
- config-driven monitoring systems
- digest-first alerting architecture
- safe migration from legacy pipeline to new product branch

## License

MIT — see [LICENSE](/C:/Users/shar0/Desktop/indrive_feedback/LICENSE)
