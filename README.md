# inDrive Media Intelligence

AI-assisted pipeline for media monitoring: collects news about inDrive, removes duplicates, scores relevance, enriches high-signal mentions, and exports results into analyst-friendly reports and Notion.

## Product Branches

- `legacy pipeline`: текущий production-like поток для `indrive_media`, который собирает упоминания про inDrive и остаётся источником правды в этом репозитории.
- `new competitor tracker`: новая продуктовая ветка, которая начинается рядом с legacy-контуром, но не должна менять текущее поведение `indrive_media` до отдельного рефакторинга и явного переключения.

## Зачем этот проект

Командам продукта и operations недостаточно просто “собирать новости”. Нужен поток сигналов, который:

- находит упоминания inDrive в разных источниках;
- отсекает шум и дубли;
- выделяет действительно важные сигналы по рынку, безопасности, регулированию и конкурентам;
- превращает сырые статьи в артефакты, с которыми уже можно работать дальше.

Этот проект показывает, как такую задачу можно автоматизировать с помощью Python, эвристик, LLM-анализа и интеграции с Notion.

## Что делает pipeline

1. Собирает новости из `NewsAPI`, `GDELT` и `Google News RSS`.
2. Нормализует статьи и удаляет дубли по URL и похожим заголовкам.
3. Применяет быстрый эвристический prefilter.
4. При наличии `OPENAI_API_KEY` добавляет LLM-анализ для high-signal материалов.
5. Сохраняет результаты в `JSON`, `CSV`, `Markdown`.
6. По флагу экспортирует итоговые записи в Notion.

## Архитектура

```text
News Sources -> Deduplication -> Heuristic Prefilter -> LLM Analysis -> Scoring -> Export
```

Ключевые модули:

- `src/indrive_media/scraper.py` — сбор данных из внешних источников
- `src/indrive_media/title_matching.py` — логика дедупликации
- `src/indrive_media/analyzer.py` — эвристический и LLM-анализ
- `src/indrive_media/notion_integration.py` — экспорт в Notion
- `src/indrive_media/main.py` — orchestration и CLI

Подробная схема: [docs/architecture.md](/C:/Users/shar0/Desktop/indrive_feedback/docs/architecture.md)

Важно: пока CLI `python main.py` и `indrive-media` относятся только к legacy pipeline. Конкурентный трекер здесь пока обозначен как отдельное направление развития, а не как замена текущего контура.

## Почему это хороший AI automation case

Этот репозиторий показывает не только “вызов модели”, а полный рабочий контур:

- интеграцию с внешними источниками данных;
- дешёвый rule-based prefilter перед LLM;
- понятную границу между heuristic и LLM слоями;
- safe workflow для Notion через `dry-run`;
- тестируемость и воспроизводимый CLI-процесс.

## Quick Start

Создай чистое окружение и установи зависимости:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Проверь, что тесты проходят:

```powershell
.\.venv\Scripts\python -m pytest
```

Запусти pipeline локально:

```powershell
.\.venv\Scripts\python main.py --days 30 --min-score 6 --no-llm
```

Если хочешь использовать packaged CLI:

```powershell
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --no-llm
```

## Конфигурация

Создай `.env` из шаблона:

```powershell
Copy-Item .env.example .env
```

```env
OPENAI_API_KEY=
NEWS_API_KEY=
OPENAI_MODEL=gpt-4o-mini

NOTION_TOKEN=
NOTION_DATABASE_ID=
```

Что важно:

- `OPENAI_API_KEY` опционален; без него проект работает только на эвристиках
- `NEWS_API_KEY` опционален; без него останутся `GDELT` и `Google News RSS`
- `NOTION_TOKEN` и `NOTION_DATABASE_ID` нужны только для экспорта в Notion

## Основные команды

Базовый запуск:

```powershell
.\.venv\Scripts\python main.py --days 30 --min-score 6
```

Без LLM:

```powershell
.\.venv\Scripts\python main.py --days 30 --min-score 6 --no-llm
```

С кастомными запросами:

```powershell
.\.venv\Scripts\python main.py --query "`"inDrive`" delivery" --query "`"inDrive`" taxi safety"
```

С другим output directory:

```powershell
.\.venv\Scripts\python main.py --days 7 --min-score 8 --output-dir output_week
```

CLI flags:

```text
--days DAYS
--min-score MIN_SCORE
--no-llm
--query QUERY
--output-dir OUTPUT_DIR
--to-notion
--notion-ensure-schema
--notion-dry-run
```

## Эвристика релевантности

Базовый score строится по простым сигналам:

```text
+7  прямое упоминание inDrive / inDriver / in-driver
+2  сервисные термины: taxi, ride-hailing, driver, passenger, fare, delivery, courier, freight
+1  конкуренты: Uber, Bolt, Grab, Gojek, Didi и т.д.
+1  регуляторные и риск-термины: regulation, law, ban, license, strike, safety, pricing
-2  шумовой термин без прямого упоминания inDrive
```

Итоговый `relevance_score` ограничен диапазоном `0..10`.

## Notion Export

Проект умеет создавать и обновлять записи в Notion-базе с полями:

- `Название статьи`
- `№`
- `Дата`
- `Ссылка на статью`
- `Контекст`
- `Почему важно для PM`

Проверка без записи:

```powershell
.\.venv\Scripts\python -m indrive_media.notion_integration --input output/indrive_mentions.json --dry-run
```

Реальный экспорт:

```powershell
.\.venv\Scripts\indrive-media --days 30 --min-score 6 --to-notion
```

Экспорт не должен плодить дубли: если URL уже есть в базе, запись обновляется.

## Выходные файлы

После запуска проект создаёт артефакты в `output/`:

- `output/indrive_mentions.json` — итоговые релевантные материалы
- `output/indrive_mentions.csv` — плоская таблица для просмотра
- `output/indrive_pm_report.md` — человекочитаемый отчёт
- `output/indrive_mentions_audit.json` — аудит всех просмотренных материалов
- `output/indrive_run_summary.json` — сводка по запуску и health check провайдеров

Пример результата: [examples/sample_mentions.json](/C:/Users/shar0/Desktop/indrive_feedback/examples/sample_mentions.json)

## Тесты

В проекте есть `pytest`-покрытие для ключевых сценариев:

- heuristic scoring
- title deduplication
- Notion dry-run / create / update behavior
- pipeline health reporting

Запуск:

```powershell
.\.venv\Scripts\python -m pytest
```

## Ограничения

- проект зависит от внешних news APIs и их доступности;
- англоязычные источники могут пропускать локальный контекст;
- heuristic layer может давать false positives и false negatives;
- LLM-анализ точнее, но медленнее и дороже;
- дедупликация похожих новостей всегда остаётся компромиссом.

## Структура репозитория

```text
.
|-- src/indrive_media/
|-- tests/
|-- docs/
|-- examples/
|-- main.py
|-- pyproject.toml
|-- requirements.txt
|-- requirements-dev.txt
```

## Что можно улучшить дальше

- добавить screenshot или short demo GIF c результатом в Notion;
- сократить путь от raw output к “executive summary”;
- вынести providers и exporters в более расширяемые adapters;
- добавить scheduled run для полностью автоматического мониторинга.

## License

MIT — см. [LICENSE](/C:/Users/shar0/Desktop/indrive_feedback/LICENSE).
