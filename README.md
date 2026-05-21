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
- Digest ranking prioritizes `priority -> freshness -> confidence/score`.
- Duplicate suppression works both within one run and across history.
- Publication date is resolved in layers:
  - provider / normalized `published_at`
  - HTML extraction via `htmldate`
  - optional LLM fallback only when metadata date is missing
  - final priority is `metadata > llm > unknown`
- Final digest delivery applies a `7-day` freshness gate:
  - clearly stale alerts are excluded from the main digest and Telegram
  - stale-but-important newly detected alerts can still pass through a high-signal override
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
- `Dates with guardrails`: metadata dates win, LLM dates are fallback-only, and very old articles are archived unless they qualify as strong newly detected signals.
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
  -> Ranking + Suppression
  -> Alert Formatting
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

Examples:

```powershell
.\.venv\Scripts\competitor-tracker dry-run --days 7 --export-csv
.\.venv\Scripts\competitor-tracker send-digest --dry-run --region sea
.\.venv\Scripts\competitor-tracker sync-notion --dry-run --region sea
.\.venv\Scripts\competitor-tracker backfill --days 30 --region sea --export-csv
```

## Configuration

The tracker uses one config file as source of truth:

- [default_config.json](/C:/Users/shar0/Desktop/indrive_feedback/src/competitor_tracker/default_config.json)

It defines:

- `regions`
- `competitors_by_region`
- `topic_groups`
- `keyword_templates`
- `daily_digest_limit`
- `enabled_providers`

Important config contract:

- `competitors_by_region` is not only a query-expansion helper.
- It is the validation matrix for allowed `competitor + region` combinations in the new tracker flow.
- If a detected or LLM-returned pair conflicts with this matrix, the pipeline should reject or normalize it instead of trusting free-form model output.

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

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

NOTION_TOKEN=
NOTION_DATABASE_ID=
COMPETITOR_TRACKER_NOTION_DATABASE_ID=
```

Important:

- `Telegram` is optional.
- `Notion` is optional.
- Without `Notion` env, the pipeline should skip mirror sync with a warning, not fail.
- `SQLite` remains the main operational store either way.

## Output Artifacts

Each run can generate a review-friendly set of artifacts in `output/competitor_tracker/`:

- `run_summary.json`
- `candidates.json`
- `digest.json`
- `digest_preview.md`
- `candidates_review.csv` when `--export-csv` is enabled
- `tracker.db`

The markdown preview intentionally uses readable Russian section labels where helpful for manual review:

- `Что произошло`
- `Почему это важно`
- `Потенциальное влияние`
- `Что делать`

This makes it easier to validate false positives and signal quality over a 1-2 week test period without querying SQLite by hand.

Important QA detail:

- `candidates_review.csv` now includes `is_expired`
- old articles that miss the `7-day` freshness gate stay in the archive for review
- late-discovered but high-signal articles can still surface in the digest when they clearly outrank the average signal level

## Delivery Layers

### Telegram

Telegram is the main delivery channel for the MVP.

- supports `dry-run`
- logs delivery history into `SQLite`
- enables suppression of already sent alerts
- excludes articles older than `7 days` from the main digest by default
- allows a high-signal override for stale but newly detected important articles
- keeps a Telegram-specific `deferred` queue for relevant alerts that miss the top slice
- retries deferred alerts for up to `48 hours`
- marks stale deferred alerts as `expired`
- gives fresh new alerts a slight ranking advantage over yesterday's carry-over alerts

### Notion

Notion is an optional mirror, not the database of record.

- final alerts can be archived to a separate competitor tracker database
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
- dry-run mode
- secrets for Telegram / Notion / OpenAI
- artifact upload from `output/competitor_tracker/`

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
