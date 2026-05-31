import sqlite3

import pytest

from competitor_tracker.analyzer import CompetitorAlertAnalyzer
from competitor_tracker.models import CandidateArticle, RawArticle
from competitor_tracker.storage import SQLiteTrackerStorage
from competitor_tracker.telegram_sender import TelegramSender


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payload)


def build_alert(
    storage: SQLiteTrackerStorage,
    *,
    title: str = "Grab launches driver support in the Philippines",
    url: str = "https://example.com/grab-ph",
    snippet: str = "Grab promotes driver support and fuel subsidies.",
):
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title=title,
            url=url,
            provider="google_news_rss",
            source="Example News",
            published_at="2026-05-18",
            snippet=snippet,
        ),
        competitor="Grab / Move It",
        topic_group="marketing + policy narrative",
        score=8,
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        summary="Driver support programs are becoming part of public messaging.",
    )
    alert = candidate.to_alert()
    storage.insert_alert(alert)
    schema = CompetitorAlertAnalyzer(use_llm=False).analyze_candidate(candidate)
    return alert, schema


def test_telegram_sender_dry_run_logs_delivery_without_http(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    alert, schema = build_alert(storage)
    sender = TelegramSender(storage=storage, dry_run=True, chat_id="12345")

    result = sender.send_alert_card(schema, alert=alert, source_url=alert.candidate.url)

    assert result["dry_run"] is True
    assert result["message_id"] is None

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        row = connection.execute(
            """
            SELECT status, destination
            FROM delivery_log
            WHERE alert_key = ? AND channel = 'telegram'
            """,
            (alert.digest_key,),
        ).fetchone()

    assert row == ("dry_run", "12345")


def test_telegram_sender_posts_and_marks_delivered(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    alert, schema = build_alert(storage)
    session = FakeSession({"ok": True, "result": {"message_id": 777}})
    sender = TelegramSender(
        storage=storage,
        bot_token="token",
        chat_id="12345",
        session=session,
    )

    result = sender.send_daily_digest(
        [schema],
        alerts=[alert],
        source_urls=[alert.candidate.url],
        generated_at="2026-05-18T09:00:00Z",
    )

    assert result == {
        "ok": True,
        "dry_run": False,
        "message_id": "777",
        "message_ids": ["777"],
        "messages_sent": 1,
    }
    assert len(session.calls) == 1
    assert "sendMessage" in session.calls[0]["url"]
    assert "Алерт по конкуренту — Philippines" in session.calls[0]["json"]["text"]
    assert "Конкурент: Grab / Move It" in session.calls[0]["json"]["text"]
    assert storage.has_sent_alert(alert.digest_key, "telegram", "12345") is True


def test_telegram_sender_daily_digest_sends_alerts_as_separate_messages(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    first_alert, first_schema = build_alert(storage)
    second_alert, second_schema = build_alert(
        storage,
        title="Grab expands partner care in Manila",
        url="https://example.com/grab-ph-2",
        snippet="Grab expands partner care messaging in Manila.",
    )
    session = FakeSession({"ok": True, "result": {"message_id": 777}})
    sender = TelegramSender(
        storage=storage,
        bot_token="token",
        chat_id="12345",
        session=session,
    )

    result = sender.send_daily_digest(
        [first_schema, second_schema],
        alerts=[first_alert, second_alert],
        source_urls=[first_alert.candidate.url, second_alert.candidate.url],
        generated_at="2026-05-18T09:00:00Z",
    )

    assert result["ok"] is True
    assert result["messages_sent"] == 2
    assert result["message_ids"] == ["777", "777"]
    assert len(session.calls) == 2
    assert all("Алерт по конкуренту — Philippines" in call["json"]["text"] for call in session.calls)


def test_telegram_sender_requires_env_or_explicit_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is missing"):
        TelegramSender(storage=storage)
