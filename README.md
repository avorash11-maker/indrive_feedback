# inDrive media intelligence

Инструмент собирает упоминания inDrive в СМИ, новостях и статьях, затем отбирает материалы, полезные для product managers: такси, ride-hailing, доставка, курьеры, водители, пассажиры, тарифы, безопасность, регулирование и конкуренты.

## Что делает

- Ищет новости через NewsAPI, GDELT и Google News RSS.
- Удаляет дубликаты по URL.
- Оценивает релевантность по шкале 0-10.
- При наличии `OPENAI_API_KEY` дополнительно делает LLM-анализ.
- Сохраняет результат в `output/indrive_mentions.json`, `output/indrive_mentions.csv` и `output/indrive_pm_report.md`.
- Может отправлять результат в базу данных Notion.

## Настройка

Создайте `.env`:

```env
OPENAI_API_KEY=your_openai_key
NEWS_API_KEY=your_newsapi_key
OPENAI_MODEL=gpt-4o-mini

NOTION_TOKEN=secret_your_notion_integration_token
NOTION_DATABASE_ID=your_notion_database_id
```

`OPENAI_API_KEY` и `NEWS_API_KEY` опциональны. Без них останутся GDELT, Google News RSS и базовая эвристическая оценка.

## Запуск сбора

```bash
pip install -r requirements.txt
python main.py --days 30 --min-score 6
```

Без OpenAI:

```bash
python main.py --days 30 --min-score 6 --no-llm
```

Своими запросами:

```bash
python main.py --query "\"inDrive\" delivery" --query "\"inDrive\" taxi safety"
```

## Notion

В Notion нужна database с такими колонками:

| Колонка | Тип |
| --- | --- |
| `Название статьи` | Title |
| `Дата` | Date |
| `Ссылка на статью` | URL |
| `Контекст` | Text / Rich text |
| `Почему важно для PM` | Text / Rich text |

Что нужно сделать:

1. Создать integration в Notion: https://www.notion.so/my-integrations
2. Скопировать `Internal Integration Secret` в `.env` как `NOTION_TOKEN`.
3. Открыть нужную database в Notion и дать доступ integration через `... -> Connections`.
4. Скопировать ID базы из URL и добавить в `.env` как `NOTION_DATABASE_ID`.

Отправить уже готовый отчет в Notion:

```bash
python notion_integration.py --input output/indrive_mentions.json
```

Собрать новости и сразу отправить их в Notion:

```bash
python main.py --days 30 --min-score 6 --to-notion
```

Экспортер проверяет дубликаты по колонке `Ссылка на статью`: если запись уже есть, она обновляется, а не создается заново.
Колонка `Контекст` заполняется на русском: суть статьи и описание, в каком контексте упоминается inDrive.
Колонка `Почему важно для PM` заполняется отдельным выводом: что стоит проверить в продукте, операциях, safety, pricing, supply/demand, локальной регуляторике или конкурентной позиции.

OpenAI получает постоянный контекст компании из `company_context.md`. Это файл-память проекта: обновляйте его, если нужно уточнить продуктовые направления, рынки, конкурентов или критерии полезности для PM.

## Как читать отчет

Открывайте `output/indrive_pm_report.md`. Для каждой новости там есть:

- источник и дата;
- релевантность;
- тема и тональность;
- краткое резюме;
- `PM insight`: что проверить продуктовой или операционной команде.
