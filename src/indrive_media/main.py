import argparse
import logging
from typing import List, Optional

from .notion_integration import NotionExporter
from .scraper import DEFAULT_QUERIES, InDriveMentionScraper


def parse_args() -> argparse.Namespace:
    """Описывает параметры запуска из командной строки."""
    parser = argparse.ArgumentParser(
        description="Собирает и анализирует упоминания inDrive в новостях."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="За сколько последних дней искать новости. По умолчанию: 30.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=6,
        help="Минимальный score релевантности для попадания в итоговый отчет.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Отключить OpenAI-анализ и использовать только эвристику.",
    )
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Свой поисковый запрос. Можно передать несколько раз.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Папка для JSON, CSV, audit и Markdown-отчета.",
    )
    parser.add_argument(
        "--to-notion",
        action="store_true",
        help="После сбора отправить найденные упоминания в Notion.",
    )
    parser.add_argument(
        "--notion-ensure-schema",
        action="store_true",
        help="Перед экспортом создать недостающие колонки в Notion. По умолчанию схема не меняется.",
    )
    parser.add_argument(
        "--notion-dry-run",
        action="store_true",
        help="Показать план create/update/skip для Notion без записи в базу.",
    )
    return parser.parse_args()


def run_pipeline(
    days: int = 30,
    min_score: int = 6,
    use_llm: bool = True,
    queries: Optional[List[str]] = None,
    output_dir: str = "output",
    to_notion: bool = False,
    notion_ensure_schema: bool = False,
    notion_dry_run: bool = False,
) -> List[dict]:
    """Запускает весь пайплайн: сбор новостей, анализ, сохранение и optional Notion export."""
    # Scraper сам собирает новости, удаляет дубли, фильтрует по score и сохраняет файлы.
    scraper = InDriveMentionScraper(
        days=days,
        output_dir=output_dir,
        min_score=min_score,
        use_llm=use_llm,
    )

    # Если queries не переданы, используются стандартные запросы из scraper.py.
    mentions = scraper.run(queries=queries or DEFAULT_QUERIES)
    print(f"Сохранено релевантных упоминаний: {len(mentions)}. Папка: {output_dir}.")

    # Notion трогаем только по явному флагу --to-notion.
    if to_notion:
        exporter = NotionExporter(ensure_schema=notion_ensure_schema)
        stats = exporter.export_mentions(mentions, dry_run=notion_dry_run)
        print(f"Экспорт в Notion завершен: {stats}")

    return mentions


def main() -> None:
    # INFO-логи показывают ход сбора, но не перегружают вывод техническими деталями.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    run_pipeline(
        days=args.days,
        min_score=args.min_score,
        use_llm=not args.no_llm,
        queries=args.queries,
        output_dir=args.output_dir,
        to_notion=args.to_notion,
        notion_ensure_schema=args.notion_ensure_schema,
        notion_dry_run=args.notion_dry_run,
    )


if __name__ == "__main__":
    main()
