import argparse
import json
import logging
import os
import time
from datetime import datetime
from datetime import date as date_type
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .analyzer import MentionAnalyzer
from .title_matching import (
    canonical_title_tokens,
    is_semantic_title_duplicate,
    is_title_contained_duplicate,
    normalize_title,
)


load_dotenv()

logger = logging.getLogger(__name__)


NOTION_API_VERSION = "2022-06-28"

DEFAULT_PROPERTY_NAMES = {
    "number": "№",
    "title": "Название статьи",
    "date": "Дата",
    "url": "Ссылка на статью",
    "context": "Контекст",
    "pm_importance": "Почему важно для PM",
}


class NotionExporter:
    def __init__(
        self,
        token: Optional[str] = None,
        database_id: Optional[str] = None,
        property_names: Optional[Dict[str, str]] = None,
        ensure_schema: bool = False,
    ):
        self.token = token or os.getenv("NOTION_TOKEN")
        self.database_id = database_id or os.getenv("NOTION_DATABASE_ID")
        self.property_names = property_names or DEFAULT_PROPERTY_NAMES

        if not self.token:
            raise ValueError("NOTION_TOKEN is missing in .env")
        if not self.database_id:
            raise ValueError("NOTION_DATABASE_ID is missing in .env")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            }
        )

        if ensure_schema:
            self.ensure_database_schema()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def ensure_database_schema(self) -> None:
        database = self.get_database()
        existing = database.get("properties", {})
        required = {
            self.property_names["title"]: {"title": {}},
            self.property_names["number"]: {"number": {"format": "number"}},
            self.property_names["date"]: {"date": {}},
            self.property_names["url"]: {"url": {}},
            self.property_names["context"]: {"rich_text": {}},
            self.property_names["pm_importance"]: {"rich_text": {}},
        }
        missing = {
            name: definition
            for name, definition in required.items()
            if name not in existing
        }
        if not missing:
            return

        endpoint = f"https://api.notion.com/v1/databases/{self.database_id}"
        response = self.session.patch(endpoint, json={"properties": missing}, timeout=30)
        response.raise_for_status()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def get_database(self) -> Dict:
        endpoint = f"https://api.notion.com/v1/databases/{self.database_id}"
        response = self.session.get(endpoint, timeout=30)
        response.raise_for_status()
        return response.json()

    def export_mentions(
        self,
        mentions: Iterable[Dict],
        update_existing: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, int]:
        stats = {"created": 0, "updated": 0, "skipped": 0, "would_create": 0, "would_update": 0}
        mentions = sorted(
            self.deduplicate_mentions(list(mentions)),
            key=lambda mention: self.parse_date(mention.get("published_at", "")) or "",
            reverse=True,
        )

        for index, mention in enumerate(mentions, start=1):
            url = mention.get("url", "")
            if not url:
                logger.info(
                    "Notion export skip; reason=missing_url title=%r",
                    self._trim(mention.get("title", ""), 120),
                )
                stats["skipped"] += 1
                continue

            page_id = self.find_page_by_url(url) if update_existing else None
            properties = self.build_properties(mention, row_number=index)

            if page_id:
                logger.info(
                    "Notion export %s; page_id=%s url=%s title=%r",
                    "would_update" if dry_run else "update",
                    page_id,
                    url,
                    self._trim(mention.get("title", ""), 120),
                )
                if dry_run:
                    stats["would_update"] += 1
                else:
                    self.update_page(page_id, properties)
                    stats["updated"] += 1
                    time.sleep(0.5)  # Rate limit: delay between updates
            else:
                logger.info(
                    "Notion export %s; url=%s title=%r",
                    "would_create" if dry_run else "create",
                    url,
                    self._trim(mention.get("title", ""), 120),
                )
                if dry_run:
                    stats["would_create"] += 1
                else:
                    self.create_page(properties)
                    stats["created"] += 1
                    time.sleep(0.5)  # Rate limit: delay between creates

        if not dry_run:
            self.renumber_database()
        logger.info("Notion export finished; stats=%s", stats)
        return stats

    def deduplicate_mentions(self, mentions: List[Dict]) -> List[Dict]:
        keepers: List[Dict] = []
        for mention in mentions:
            if any(self._is_duplicate_mention(mention, keeper) for keeper in keepers):
                continue
            keepers.append(mention)
        return keepers

    def _is_duplicate_mention(self, mention: Dict, keeper: Dict) -> bool:
        mention_key = normalize_title(mention.get("title", ""))
        keeper_key = normalize_title(keeper.get("title", ""))
        if not mention_key or not keeper_key:
            return False
        if mention_key == keeper_key or is_title_contained_duplicate(mention_key, keeper_key):
            return True

        mention_date = self.parse_date(mention.get("published_at", "")) or ""
        keeper_date = self.parse_date(keeper.get("published_at", "")) or ""
        if mention_date and keeper_date and self._date_distance_days(mention_date, keeper_date) > 7:
            return False
        if is_semantic_title_duplicate(mention_key, keeper_key):
            return True
        return SequenceMatcher(None, mention_key, keeper_key).ratio() >= 0.72

    def deduplicate_database(self) -> Dict[str, int]:
        pages = self.query_database_pages()
        duplicate_ids = self.find_duplicate_page_ids(pages)
        for page_id in duplicate_ids:
            self.archive_page(page_id)
            time.sleep(0.5)  # Rate limit: delay between archives
        renumbered = self.renumber_database()
        return {"archived": len(duplicate_ids), "renumbered": renumbered}

    def archive_urls(self, urls: Iterable[str]) -> Dict[str, int]:
        stats = {"archived": 0, "missing": 0}
        for url in urls:
            page_id = self.find_page_by_url(url)
            if not page_id:
                logger.info("Notion archive by URL skipped; reason=missing url=%s", url)
                stats["missing"] += 1
                continue

            logger.info("Notion archive by URL; page_id=%s url=%s", page_id, url)
            self.archive_page(page_id)
            stats["archived"] += 1
            time.sleep(0.5)  # Rate limit: delay between archives

        if stats["archived"]:
            self.renumber_database()
        return stats

    def refresh_existing_analysis(self, use_llm: bool = True) -> Dict[str, int]:
        analyzer = MentionAnalyzer(use_llm=use_llm)
        pages = self.query_database_pages()
        pages.sort(key=self._page_sort_key, reverse=True)
        stats = {"updated": 0, "skipped": 0}

        for page in pages:
            mention = self.page_to_mention(page)
            if not mention.get("title"):
                stats["skipped"] += 1
                continue

            analysis = analyzer.analyze_article(
                title=mention["title"],
                text=mention.get("text", ""),
                url=mention.get("url", ""),
            )
            mention["analysis"] = analysis
            self.update_page(
                page["id"],
                {
                    self.property_names["context"]: {
                        "rich_text": [{"text": {"content": self._trim(self.build_context(mention), 2000)}}]
                    },
                    self.property_names["pm_importance"]: {
                        "rich_text": [{"text": {"content": self._trim(self.build_pm_importance(mention), 2000)}}]
                    },
                },
            )
            stats["updated"] += 1

        self.renumber_database()
        return stats

    def remove_context_label(self) -> Dict[str, int]:
        pages = self.query_database_pages()
        stats = {"updated": 0, "skipped": 0}

        for page in pages:
            current = self._page_rich_text(page, self.property_names["context"])
            updated = current.replace(" Контекст упоминания inDrive:", "")
            updated = updated.replace("Контекст упоминания inDrive:", "")
            updated = self._trim(updated, 2000)

            if updated == current:
                stats["skipped"] += 1
                continue

            self.update_page(
                page["id"],
                {
                    self.property_names["context"]: {
                        "rich_text": [{"text": {"content": updated}}]
                    }
                },
            )
            stats["updated"] += 1

        return stats

    def page_to_mention(self, page: Dict) -> Dict:
        properties = page.get("properties", {})
        url = properties.get(self.property_names["url"], {}).get("url") or ""
        context_text = self._page_rich_text(page, self.property_names["context"])
        pm_text = self._page_rich_text(page, self.property_names["pm_importance"])
        title = self._page_title(page)
        return {
            "title": title,
            "url": url,
            "published_at": self._page_date(page),
            "snippet": context_text,
            "text": (
                f"Дата: {self._page_date(page)}\n"
                f"Название: {title}\n"
                f"Текущий контекст: {context_text}\n"
                f"Текущая важность для PM: {pm_text}\n"
                f"URL: {url}"
            ),
        }

    def find_duplicate_page_ids(self, pages: List[Dict]) -> List[str]:
        sorted_pages = sorted(pages, key=self._page_sort_key, reverse=True)
        keepers: List[Dict] = []
        duplicate_ids: List[str] = []

        for page in sorted_pages:
            if any(self._is_duplicate_page(page, keeper) for keeper in keepers):
                duplicate_ids.append(page["id"])
            else:
                keepers.append(page)

        return duplicate_ids

    def _is_duplicate_page(self, page: Dict, keeper: Dict) -> bool:
        page_title = self._page_title(page)
        keeper_title = self._page_title(keeper)
        page_key = normalize_title(page_title)
        keeper_key = normalize_title(keeper_title)

        if not page_key or not keeper_key:
            return False
        if page_key == keeper_key:
            return True
        if is_title_contained_duplicate(page_key, keeper_key):
            return True

        page_date = self._page_date(page)
        keeper_date = self._page_date(keeper)
        if page_date and keeper_date and self._date_distance_days(page_date, keeper_date) > 7:
            return False

        if is_semantic_title_duplicate(page_key, keeper_key):
            return True

        similarity = SequenceMatcher(None, page_key, keeper_key).ratio()
        return similarity >= 0.72

    @staticmethod
    def _is_title_contained_duplicate(left: str, right: str) -> bool:
        return is_title_contained_duplicate(left, right)

    @staticmethod
    def _is_semantic_title_duplicate(left: str, right: str) -> bool:
        return is_semantic_title_duplicate(left, right)

    @staticmethod
    def _canonical_title_tokens(title: str) -> set[str]:
        return canonical_title_tokens(title)

    def renumber_database(self, descending: bool = False) -> int:
        pages = self.query_database_pages()
        pages.sort(key=self._page_sort_key, reverse=descending)

        for index, page in enumerate(pages, start=1):
            self.update_page(
                page["id"],
                {self.property_names["number"]: {"number": index}},
            )
        return len(pages)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def query_database_pages(self) -> List[Dict]:
        endpoint = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        pages = []
        payload: Dict = {"page_size": 100}

        while True:
            response = self.session.post(endpoint, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            pages.extend(data.get("results", []))

            if not data.get("has_more"):
                break
            payload["start_cursor"] = data.get("next_cursor")
            time.sleep(0.5)  # Rate limit: delay between pagination requests

        return pages

    def build_properties(self, mention: Dict, row_number: Optional[int] = None) -> Dict:
        names = self.property_names
        title = self._trim(mention.get("title", "Untitled"), 2000)
        url = mention.get("url", "")
        context = self._trim(self.build_context(mention), 2000)
        pm_importance = self._trim(self.build_pm_importance(mention), 2000)
        published_date = self.parse_date(mention.get("published_at", ""))

        properties = {
            names["title"]: {"title": [{"text": {"content": title}}]},
            names["number"]: {"number": row_number},
            names["url"]: {"url": url},
            names["context"]: {"rich_text": [{"text": {"content": context}}]},
            names["pm_importance"]: {"rich_text": [{"text": {"content": pm_importance}}]},
        }
        if published_date:
            properties[names["date"]] = {"date": {"start": published_date}}
        return properties

    @staticmethod
    def build_context(mention: Dict) -> str:
        analysis = mention.get("analysis", {}) or {}
        essence = analysis.get("article_essence_ru") or analysis.get("summary")
        context = analysis.get("mention_context_ru")

        if not essence:
            essence = f"Статья сообщает: {mention.get('title', 'суть статьи не определена')}."
        if not context:
            context = "inDrive упоминается в материале, но точный контекст нужно проверить по ссылке."

        return f"{essence} {context}"

    @staticmethod
    def build_pm_importance(mention: Dict) -> str:
        analysis = mention.get("analysis", {}) or {}
        return (
            analysis.get("pm_importance_ru")
            or analysis.get("pm_insight")
            or "Проверить возможное влияние на продукт, операции или репутацию inDrive."
        )

    def find_page_by_url(self, url: str) -> Optional[str]:
        endpoint = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        payload = {
            "filter": {
                "property": self.property_names["url"],
                "url": {"equals": url},
            },
            "page_size": 1,
        }
        response = self.session.post(endpoint, json=payload, timeout=30)
        response.raise_for_status()
        results = response.json().get("results", [])
        return results[0]["id"] if results else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def create_page(self, properties: Dict) -> Dict:
        endpoint = "https://api.notion.com/v1/pages"
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        response = self.session.post(endpoint, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def update_page(self, page_id: str, properties: Dict) -> Dict:
        endpoint = f"https://api.notion.com/v1/pages/{page_id}"
        response = self.session.patch(endpoint, json={"properties": properties}, timeout=30)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def archive_page(self, page_id: str) -> Dict:
        endpoint = f"https://api.notion.com/v1/pages/{page_id}"
        response = self.session.patch(endpoint, json={"archived": True}, timeout=30)
        response.raise_for_status()
        return response.json()

    def _page_sort_key(self, page: Dict) -> tuple:
        date_value = self._page_date(page)
        title_value = self._page_title(page)
        return (date_value, title_value)

    def _page_date(self, page: Dict) -> str:
        properties = page.get("properties", {})
        date_property = properties.get(self.property_names["date"], {}).get("date") or {}
        return date_property.get("start") or ""

    def _page_title(self, page: Dict) -> str:
        properties = page.get("properties", {})
        title_property = properties.get(self.property_names["title"], {}).get("title") or []
        return "".join(item.get("plain_text", "") for item in title_property)

    def _page_rich_text(self, page: Dict, property_name: str) -> str:
        properties = page.get("properties", {})
        rich_text = properties.get(property_name, {}).get("rich_text") or []
        return " ".join(item.get("plain_text", "") for item in rich_text)

    @staticmethod
    def parse_date(value: str) -> Optional[str]:
        if not value:
            return None
        try:
            if "," in value and "GMT" in value:
                return parsedate_to_datetime(value).date().isoformat()
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            return None

    @staticmethod
    def _trim(value: str, limit: int) -> str:
        value = " ".join(str(value or "").split())
        return value[:limit]

    @staticmethod
    def normalize_title(title: str) -> str:
        return normalize_title(title)

    @staticmethod
    def _date_distance_days(left: str, right: str) -> int:
        try:
            left_date = date_type.fromisoformat(left)
            right_date = date_type.fromisoformat(right)
            return abs((left_date - right_date).days)
        except Exception:
            return 9999


def load_mentions(path: str) -> List[Dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export inDrive mentions to a Notion database.")
    parser.add_argument("--input", default="output/indrive_mentions.json")
    parser.add_argument("--no-update", action="store_true", help="Always create new Notion pages.")
    parser.add_argument(
        "--renumber-only",
        action="store_true",
        help="Only recalculate the № property for existing Notion database rows.",
    )
    parser.add_argument(
        "--dedupe-notion",
        action="store_true",
        help="Archive duplicate Notion rows by normalized/fuzzy title, then renumber rows.",
    )
    parser.add_argument(
        "--archive-url",
        action="append",
        default=[],
        help="Archive a Notion row by exact article URL. Can be passed multiple times.",
    )
    parser.add_argument(
        "--refresh-existing-analysis",
        action="store_true",
        help="Re-analyze existing Notion rows only and update context/PM columns without creating rows.",
    )
    parser.add_argument(
        "--remove-context-label",
        action="store_true",
        help="Remove the repeated 'Контекст упоминания inDrive:' label from existing Notion context cells.",
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Create missing Notion database properties before running. Off by default.",
    )
    parser.add_argument(
        "--no-ensure-schema",
        action="store_true",
        help="Deprecated no-op. Schema changes are disabled by default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan create/update/skip actions without writing pages or renumbering the database.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exporter = NotionExporter(ensure_schema=args.ensure_schema)
    if args.dedupe_notion:
        stats = exporter.deduplicate_database()
        print(f"Notion dedupe complete: {stats}")
        return

    if args.archive_url:
        stats = exporter.archive_urls(args.archive_url)
        print(f"Notion archive by URL complete: {stats}")
        return

    if args.refresh_existing_analysis:
        stats = exporter.refresh_existing_analysis(use_llm=True)
        print(f"Notion existing analysis refresh complete: {stats}")
        return

    if args.remove_context_label:
        stats = exporter.remove_context_label()
        print(f"Notion context label cleanup complete: {stats}")
        return

    if args.renumber_only:
        count = exporter.renumber_database()
        print(f"Notion renumber complete: {count} rows")
        return

    stats = exporter.export_mentions(
        load_mentions(args.input),
        update_existing=not args.no_update,
        dry_run=args.dry_run,
    )
    print(f"Notion export complete: {stats}")


if __name__ == "__main__":
    main()
