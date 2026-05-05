import argparse
import logging

from notion_integration import NotionExporter
from scraper import DEFAULT_QUERIES, InDriveMentionScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect and analyze media mentions of inDrive for product managers."
    )
    parser.add_argument("--days", type=int, default=30, help="Search window in days.")
    parser.add_argument(
        "--min-score",
        type=int,
        default=6,
        help="Minimum relevance score to keep in the report.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for JSON, CSV, and Markdown reports.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use deterministic keyword scoring only, without OpenAI analysis.",
    )
    parser.add_argument(
        "--query",
        action="append",
        help="Custom query. Can be passed multiple times. Defaults cover taxi, delivery, drivers, safety, pricing, and regulation.",
    )
    parser.add_argument(
        "--to-notion",
        action="store_true",
        help="Export relevant mentions to NOTION_DATABASE_ID after collecting them.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()

    scraper = InDriveMentionScraper(
        days=args.days,
        output_dir=args.output_dir,
        min_score=args.min_score,
        use_llm=not args.no_llm,
    )
    mentions = scraper.run(queries=args.query or DEFAULT_QUERIES)
    print(f"Saved {len(mentions)} relevant mentions to {args.output_dir}")

    if args.to_notion:
        stats = NotionExporter().export_mentions(mentions)
        print(f"Notion export complete: {stats}")


if __name__ == "__main__":
    main()
