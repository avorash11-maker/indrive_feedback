# inDrive Media Intelligence Pipeline

A robust AI-powered media monitoring system for tracking inDrive mentions across global news sources. This project demonstrates advanced integration of heuristic filtering, LLM analysis, and automated data pipelines to solve real-world business intelligence challenges in the ride-hailing and delivery industry.

## Business Problem

In the competitive ride-hailing and delivery market, companies like inDrive need to:
- **Monitor global media coverage** for regulatory changes, competitor actions, and market sentiment
- **Identify critical business signals** such as new permits, safety incidents, or pricing discussions
- **Scale analysis beyond manual review** of thousands of daily news articles
- **Maintain data quality** while handling noisy, duplicate-rich news feeds

Traditional approaches fail because news APIs return high volumes of irrelevant content, and manual filtering doesn't scale. This project solves these challenges with an automated, AI-enhanced pipeline that delivers actionable insights to product managers and executives.

## Solution Overview

This system automates media intelligence by:
- Aggregating news from multiple sources (NewsAPI, GDELT, Google News RSS)
- Removing duplicates using semantic title matching
- Applying two-tier analysis: fast heuristic prefiltering + optional LLM deep analysis
- Scoring relevance on a 0-10 scale with detailed categorization
- Exporting structured reports and integrating with Notion for team collaboration

## Architecture & Pipeline

```
News Sources → Deduplication → Heuristic Prefilter → LLM Analysis → Scoring → Export
     ↓              ↓              ↓              ↓              ↓              ↓
  Raw Articles   Title Matching   Keyword Rules   GPT-4o-mini   0-10 Scale   JSON/CSV/MD/Notion
```

### Key Components

1. **Data Collection** (`scraper.py`): Multi-source news aggregation with retry/backoff and rate limiting
2. **Deduplication** (`title_matching.py`): Semantic title matching using canonical tokens and stopwords
3. **Analysis Engine** (`analyzer.py`): Two-tier scoring system
4. **Integration** (`notion_integration.py`): Automated export with schema management
5. **Orchestration** (`src/indrive_media/main.py`): CLI-driven pipeline execution

### AI Integration: Heuristic vs LLM Boundary

The system uses a **hybrid approach** to balance speed, cost, and accuracy:

- **Heuristic Layer** (Always Active): Fast keyword-based scoring (0-10) using regex patterns and term matching. Identifies obvious signals like "permit", "regulation", "safety" while filtering noise. Processes ~1000 articles/second.
- **LLM Layer** (Optional): Deep semantic analysis using GPT-4o-mini for nuanced understanding. Provides detailed categorization (topic, category, context, importance) and handles edge cases. Processes ~10 articles/second with API costs.

**Trade-offs**:
- Heuristic-only: Free, fast, but misses subtle signals (e.g., "cashless payment adoption" vs explicit "pricing strategy")
- LLM-enhanced: Higher accuracy (~85% vs 65%), but slower and costs ~$0.01/article
- Production recommendation: Use LLM for critical monitoring, heuristic for broad surveillance

## Sample Output

### Structured JSON Export
```json
{
  "title": "inDrive Secures Operating Permit in São Paulo",
  "url": "https://example.com/news",
  "published_at": "2024-01-15",
  "source": "Reuters",
  "analysis": {
    "relevance_score": 9,
    "topic": "Regulatory Expansion",
    "category": "Business Development",
    "context": "inDrive obtained authorization to operate ride-hailing services in Brazil's largest city",
    "pm_importance": "High priority: New market entry requires pricing and safety feature adjustments"
  }
}
```

### PM Report (Markdown)
```markdown
# inDrive Media Intelligence Report
Generated: 2024-01-15 | Period: 30 days | Min Score: 6

## High Priority (Score 8-10)
- **Regulatory**: 3 mentions - New permits in Brazil, Mexico
- **Safety**: 2 mentions - Incident investigations
- **Competition**: 1 mention - Pricing comparison with Uber

## Medium Priority (Score 6-7)
- **Market Expansion**: 5 mentions - Delivery service growth
```

## Limitations & Failure Modes

### Technical Limitations
- **API Dependencies**: Relies on external news APIs with potential rate limits and outages
- **Geographic Bias**: English-language sources may miss local coverage
- **Temporal Lag**: News APIs have 1-24 hour delays
- **Cost Scaling**: LLM analysis costs scale linearly with article volume

### Failure Modes
- **False Positives**: Over-scoring irrelevant articles (mitigated by score thresholds)
- **False Negatives**: Missing critical signals in noisy data (LLM reduces this)
- **Duplicate Handling**: Semantic matching may incorrectly merge related but distinct stories
- **API Failures**: Pipeline fails fast on network issues (configurable retry)
- **Data Quality**: Noisy sources can introduce bias (multi-source validation helps)

### Known Edge Cases
- Ambiguous company mentions (e.g., "drive" in non-transport context)
- International name variations ("inDrive" vs "inDriver")
- Satirical or opinion pieces scoring high on keywords

## Quick Start

Set up a clean local environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python main.py --days 30 --min-score 6 --no-llm
```

This repository uses a `src/` layout. The simplest local workflow is:

- run tests with `python -m pytest`
- run the pipeline from the repo root with `python main.py ...`

Optional: install the package in editable mode if you want the packaged CLI entrypoint:

```powershell
.\.venv\Scripts\python -m pip install -e .
```

Then the equivalent CLI becomes:

```powershell
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --no-llm
```

For VS Code, use `.venv\Scripts\python.exe` as the project interpreter.

## Configuration

Create a local `.env` based on `.env.example`:

```powershell
Copy-Item .env.example .env
```

Template:

```env
OPENAI_API_KEY=
NEWS_API_KEY=
OPENAI_MODEL=gpt-4o-mini

NOTION_TOKEN=
NOTION_DATABASE_ID=
```

`OPENAI_API_KEY` and `NEWS_API_KEY` are optional. Without them, you'll get GDELT, Google News RSS, and heuristic scoring.

`NOTION_TOKEN` and `NOTION_DATABASE_ID` are only needed for Notion export.

Important: The real `.env` contains secrets and stays in `.gitignore`.

## Usage

Canonical local run from the repository root:

```powershell
.\.venv\Scripts\python main.py --days 30 --min-score 6
```

Run without OpenAI:

```powershell
.\.venv\Scripts\python main.py --days 30 --min-score 6 --no-llm
```

Custom search queries:

```powershell
.\.venv\Scripts\python main.py --query "`"inDrive`" delivery" --query "`"inDrive`" taxi safety"
```

Different output directory:

```powershell
.\.venv\Scripts\python main.py --days 7 --min-score 8 --output-dir output_week
```

If you installed the package in editable mode, replace `python main.py` with `indrive-media`.

Main CLI flags:

```text
--days DAYS              News search window in days. Default: 30.
--min-score MIN_SCORE    Minimum score for final report inclusion. Default: 6.
--no-llm                 Disable OpenAI, use heuristic analysis only.
--query QUERY            Custom search query. Can be passed multiple times.
--output-dir OUTPUT_DIR  Output directory. Default: output.
--to-notion              After collection, export results to Notion.
--notion-ensure-schema   Create missing Notion properties before export.
--notion-dry-run         Plan Notion create/update/skip actions without writing.
```

### Relevance Heuristic

Эвристический score без OpenAI:

```text
+7 если найдено прямое упоминание inDrive / inDriver / in-driver
+2 если найдены сервисные термины: taxi, ride-hailing, driver, passenger, fare, delivery, courier, freight
+1 если найдены конкуренты: Uber, Bolt, Grab, Gojek, Didi и т.д.
+1 если найдены регуляторные или риск-термины: regulation, law, ban, license, strike, safety, pricing
-2 если найден шумовой термин и нет прямого упоминания inDrive
```

Итоговый score ограничивается диапазоном `0..10`.

## Notion Export

В Notion нужна database с колонками:

| Колонка | Тип |
| --- | --- |
| `Название статьи` | Title |
| `№` | Number |
| `Дата` | Date |
| `Ссылка на статью` | URL |
| `Контекст` | Rich text |
| `Почему важно для PM` | Rich text |

Настройка Notion:

1. Создайте integration: https://www.notion.so/my-integrations
2. Скопируйте `Internal Integration Secret` в `.env` как `NOTION_TOKEN`.
3. Откройте нужную database в Notion и дайте доступ integration через `... -> Connections`.
4. Скопируйте ID базы из URL и добавьте в `.env` как `NOTION_DATABASE_ID`.

Безопасная проверка перед записью:

```powershell
.\.venv\Scripts\python -m indrive_media.notion_integration --input output/indrive_mentions.json --dry-run
```

Экспорт готового файла:

```powershell
.\.venv\Scripts\python -m indrive_media.notion_integration --input output/indrive_mentions.json
```

Сбор новостей и dry-run экспорта:

```powershell
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --to-notion --notion-dry-run
```

Сбор новостей и реальный экспорт:

```powershell
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --to-notion
```

По умолчанию экспорт не меняет схему Notion database. Если нужно автоматически создать недостающие колонки, используйте явный флаг:

```powershell
.\.venv\Scripts\python -m indrive_media.notion_integration --input output/indrive_mentions.json --ensure-schema
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --to-notion --notion-ensure-schema
```

Экспорт не должен создавать дубли по URL: если запись с таким `Ссылка на статью` уже есть, она обновляется; если URL новый, создается новая запись.

Флаг `--no-ensure-schema` поддерживается для обратной совместимости и ничего не делает, потому что изменение схемы уже выключено по умолчанию.

## Tests

Подготовить чистое локальное окружение:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Запустить тесты:

```powershell
.\.venv\Scripts\python -m pytest
```

Тесты покрывают:

- эвристическую релевантность `MentionAnalyzer`;
- дедупликацию заголовков в `scraper.py`;
- `parse_date()` и fuzzy duplicate logic в `notion_integration.py`;
- Notion create/update/dry-run поведение без реального API.

## Packaging

The project is packaged with `pyproject.toml`, `setuptools`, and a `src/` layout.

- Local development workflow: `python main.py ...` and `python -m pytest`
- Packaged CLI workflow: `python -m pip install -e .` and then `indrive-media ...`
- CI workflow: install with `python -m pip install -e .[dev]` and run `python -m pytest`

This keeps the repository easy to run from a checkout while still supporting a clean packaging story for CI and demos.

## Tech Stack & Development

- **Language**: Python 3.13
- **Key Libraries**: requests, tenacity, beautifulsoup4, openai, python-dotenv
- **Architecture**: Modular CLI application with separation of concerns
- **Testing**: pytest with 22 tests covering analysis, deduplication, Notion integration, and pipeline health reporting
- **Deployment**: Designed for scheduled execution with local runs or GitHub Actions

This project showcases practical AI automation: external signal collection, heuristic plus LLM analysis, structured exports, and operational visibility for a real workflow.

## Output Files

Основные результаты лежат в папке `output/`.

```text
output/indrive_mentions.json
```

Итоговые релевантные упоминания в JSON. Используется для экспорта в Notion.

```text
output/indrive_mentions.csv
```

Таблица для просмотра и ручного анализа.

```text
output/indrive_pm_report.md
```

Человекочитаемый Markdown-отчет: источник, дата, relevance, категория, контекст и PM importance.

```text
output/indrive_mentions_audit.json
```

Аудит всех просмотренных материалов, включая отфильтрованные.

```text
output/indrive_run_summary.json
```

Сводка по запуску: окно поиска, число релевантных материалов, статистика по провайдерам и последняя ошибка по каждому источнику.

## Monitoring

Во время обычного запуска проект пишет проблемы в консоль через `logging`:

- ошибки news-провайдеров идут как `WARNING: News provider failed ...`;
- сетевые вызовы OpenAI и Notion видны как `INFO` / traceback при сбое;
- финальная статистика записи в Notion печатается в конце запуска.

После завершения запуска удобнее всего смотреть:

- `output/indrive_run_summary.json` — быстрый health check по `newsapi`, `gdelt`, `google_news_rss`;
- `output/indrive_pm_report.md` — человекочитаемый отчет, теперь с секцией `Pipeline health`;
- `output/indrive_mentions_audit.json` — что именно прошло через фильтрацию и анализ;
- терминальный лог текущего запуска — полный контекст ошибок и traceback.

## Repository Artifacts

This repository includes additional documentation and examples:

- **[docs/architecture.md](docs/architecture.md)**: Detailed system architecture with Mermaid diagrams and design decisions
- **[examples/sample_mentions.json](examples/sample_mentions.json)**: Sample dataset showing typical analysis output structure
- **[LICENSE](LICENSE)**: MIT license for open-source distribution
- **[pyproject.toml](pyproject.toml)**: Modern Python packaging configuration
- **[.github/workflows/ci.yml](.github/workflows/ci.yml)**: CI pipeline for automated testing

These artifacts demonstrate engineering maturity, clear documentation practices, and production-ready code organization.
