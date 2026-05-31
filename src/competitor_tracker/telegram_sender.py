"""Telegram delivery for competitor tracker alerts and digests."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .environment import get_env_value
from .formatter import format_alert_card
from .models import Alert, DeliveryRecord
from .storage import SQLiteTrackerStorage


class TelegramSender:
    """Send competitor tracker outputs to Telegram with delivery logging."""

    def __init__(
        self,
        *,
        storage: SQLiteTrackerStorage,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        session: Optional[requests.Session] = None,
        dry_run: bool = False,
    ) -> None:
        self.storage = storage
        self.bot_token = bot_token or get_env_value("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or get_env_value("TELEGRAM_CHAT_ID")
        self.session = session or requests.Session()
        self.dry_run = dry_run

        if not self.dry_run:
            if not self.bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN is missing")
            if not self.chat_id:
                raise ValueError("TELEGRAM_CHAT_ID is missing")

    def send_alert_card(
        self,
        alert_schema: Mapping[str, Any],
        *,
        alert: Alert,
        source_url: str = "",
    ) -> dict[str, Any]:
        """Send one alert card to Telegram and log the outcome."""
        text = format_alert_card(alert_schema, source_url=source_url, locale="ru")
        return self._send_text(
            text=text,
            alert_keys=[alert.digest_key],
            metadata={
                "mode": "individual_alert",
                "headline": alert.headline,
                "source_url": source_url,
            },
        )

    def send_daily_digest(
        self,
        alert_schemas: Sequence[Mapping[str, Any]],
        *,
        alerts: Sequence[Alert],
        source_urls: Optional[Sequence[str]] = None,
        title: str = "Competitor Daily Digest",
        generated_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send one Telegram card per alert while preserving digest delivery logging."""
        message_ids: list[str] = []
        total = min(len(alert_schemas), len(alerts))
        for index in range(total):
            alert_schema = alert_schemas[index]
            alert = alerts[index]
            source_url = ""
            if source_urls is not None and index < len(source_urls):
                source_url = source_urls[index]
            text = format_alert_card(alert_schema, source_url=source_url, locale="ru")
            result = self._send_text(
                text=text,
                alert_keys=[alert.digest_key],
                metadata={
                    "mode": "daily_digest_card",
                    "title": title,
                    "generated_at": generated_at or "",
                    "count": str(total),
                    "sequence": str(index + 1),
                    "source_url": source_url,
                },
            )
            message_id = str(result.get("message_id") or "")
            if message_id:
                message_ids.append(message_id)

        return {
            "ok": True,
            "dry_run": self.dry_run,
            "message_id": message_ids[-1] if message_ids else None,
            "message_ids": message_ids,
            "messages_sent": total,
        }

    def _send_text(
        self,
        *,
        text: str,
        alert_keys: Sequence[str],
        metadata: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        delivered_at = datetime.now(timezone.utc).isoformat()
        if self.dry_run:
            for alert_key in alert_keys:
                self.storage.log_delivery(
                    DeliveryRecord(
                        alert_key=alert_key,
                        channel="telegram",
                        status="dry_run",
                        destination=self.chat_id,
                        metadata=metadata or {},
                    )
                )
            return {"ok": True, "dry_run": True, "message_id": None}

        payload = self._post_message(text)
        result = payload.get("result") or {}
        message_id = str(result.get("message_id", ""))
        for alert_key in alert_keys:
            self.storage.mark_delivered(
                alert_key=alert_key,
                channel="telegram",
                delivered_at=delivered_at,
                destination=self.chat_id,
                external_id=message_id,
                metadata=metadata or {},
            )
        return {
            "ok": True,
            "dry_run": False,
            "message_id": message_id,
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _post_message(self, text: str) -> dict[str, Any]:
        response = self.session.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise requests.RequestException(str(payload))
        return payload
