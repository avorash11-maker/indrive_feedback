"""Optional Notion mirror sync for competitor tracker final alerts."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional, Sequence

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import Alert


logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"

DEFAULT_PROPERTY_NAMES = {
    "title": "Alert",
    "digest_key": "Digest Key",
    "date": "Published Date",
    "url": "Source URL",
    "competitor": "Competitor",
    "region": "Region",
    "country": "Country",
    "topic": "Topic",
    "priority": "Priority",
    "what_happened": "What Happened",
    "why_it_matters": "Why It Matters",
    "potential_impact": "Potential Impact",
    "recommended_action": "What To Do",
    "confidence": "Confidence",
    "status": "Status",
}


class CompetitorNotionMirrorSync:
    """Mirror final competitor alerts into Notion without making it required."""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        database_id: Optional[str] = None,
        property_names: Optional[dict[str, str]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.token = token or os.getenv("NOTION_TOKEN", "")
        self.database_id = database_id or self._resolve_database_id()
        self.property_names = property_names or DEFAULT_PROPERTY_NAMES
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            }
        )

    def is_configured(self) -> bool:
        return bool(self.token and self.database_id)

    @staticmethod
    def _resolve_database_id() -> str:
        primary_database_id = os.getenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", "")
        if primary_database_id:
            return primary_database_id

        fallback_database_id = os.getenv("NOTION_DATABASE_ID", "")
        if fallback_database_id:
            logger.warning(
                "Competitor tracker Notion mirror is using legacy NOTION_DATABASE_ID as fallback. "
                "Set COMPETITOR_TRACKER_NOTION_DATABASE_ID to keep the new schema isolated."
            )
        return fallback_database_id

    def sync_alerts(
        self,
        items: Sequence[tuple[Alert, Mapping[str, Any]]],
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Sync final alerts to Notion or skip safely when env is missing."""
        if not self.is_configured():
            logger.warning(
                "Competitor tracker Notion mirror skipped: missing NOTION_TOKEN or COMPETITOR_TRACKER_NOTION_DATABASE_ID."
            )
            return {"created": 0, "updated": 0, "skipped": len(items), "would_create": 0, "would_update": 0}

        stats = {"created": 0, "updated": 0, "skipped": 0, "would_create": 0, "would_update": 0}
        for alert, alert_schema in items:
            properties = self.map_alert_to_properties(alert, alert_schema)
            page_id = self.find_page_by_digest_key(alert.digest_key)
            if page_id:
                if dry_run:
                    stats["would_update"] += 1
                else:
                    self.update_page(page_id, properties)
                    stats["updated"] += 1
            else:
                if dry_run:
                    stats["would_create"] += 1
                else:
                    self.create_page(properties)
                    stats["created"] += 1
        return stats

    def map_alert_to_properties(
        self,
        alert: Alert,
        alert_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Map new alert entity plus readable schema into Notion properties."""
        names = self.property_names
        published_date = self.parse_date(alert.candidate.published_date or "")
        title = self._trim(alert.headline, 2000)
        properties = {
            names["title"]: {"title": [{"text": {"content": title}}]},
            names["digest_key"]: {"rich_text": [{"text": {"content": self._trim(alert.digest_key, 2000)}}]},
            names["url"]: {"url": alert.candidate.url},
            names["competitor"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("competitor", alert.competitor), 2000)}}]},
            names["region"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("region", alert.candidate.region or ""), 2000)}}]},
            names["country"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("country", alert.candidate.country_hint or ""), 2000)}}]},
            names["topic"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("topic", alert.topic_group), 2000)}}]},
            names["priority"]: {"select": {"name": self._trim(str(alert_schema.get("priority", alert.priority)).upper(), 100)}},
            names["what_happened"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("what_happened", ""), 2000)}}]},
            names["why_it_matters"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("why_it_matters", ""), 2000)}}]},
            names["potential_impact"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("potential_impact", ""), 2000)}}]},
            names["recommended_action"]: {"rich_text": [{"text": {"content": self._trim(alert_schema.get("recommended_action", ""), 2000)}}]},
            names["confidence"]: {"number": round(float(alert.confidence), 4)},
            names["status"]: {"select": {"name": "NEW"}},
        }
        if published_date:
            properties[names["date"]] = {"date": {"start": published_date}}
        return properties

    def find_page_by_digest_key(self, digest_key: str) -> Optional[str]:
        endpoint = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        payload = {
            "filter": {
                "property": self.property_names["digest_key"],
                "rich_text": {"equals": digest_key},
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
        retry=retry_if_exception_type(requests.RequestException),
    )
    def create_page(self, properties: dict[str, Any]) -> dict[str, Any]:
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
        retry=retry_if_exception_type(requests.RequestException),
    )
    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"https://api.notion.com/v1/pages/{page_id}"
        response = self.session.patch(endpoint, json={"properties": properties}, timeout=30)
        response.raise_for_status()
        return response.json()

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
        return " ".join(str(value or "").split())[:limit]
