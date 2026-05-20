"""Text formatters for competitor tracker delivery payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


def build_alert_headline(alert: Mapping[str, Any]) -> str:
    """Build a brief-style alert headline."""
    market = _clean(
        alert.get("country")
        or alert.get("region")
        or "Unknown Market"
    )
    return f"Competitor Alert — {market}"


def format_alert_card(
    alert: Mapping[str, Any],
    *,
    source_url: str = "",
    headline: Optional[str] = None,
) -> str:
    """Format one alert card in the brief delivery style."""
    lines = [
        headline or build_alert_headline(alert),
        f"Competitor: {_clean(alert.get('competitor'))}",
        f"Event: {_clean(alert.get('topic'))}",
        f"Priority: {_clean(str(alert.get('priority', '')).upper())}",
        "What happened:",
        _clean(alert.get("what_happened")),
        f"Where: {_clean(alert.get('country') or alert.get('region') or 'Unknown')}",
        "Source link:",
        _clean(source_url or alert.get("source_url")),
        "Why it matters:",
        _clean(alert.get("why_it_matters")),
        "Potential impact:",
        _clean(alert.get("potential_impact")),
        "What to do:",
        _clean(alert.get("recommended_action")),
    ]
    return "\n".join(lines)


def format_daily_digest(
    alerts: Sequence[Mapping[str, Any]],
    *,
    source_urls: Optional[Sequence[str]] = None,
    title: str = "Competitor Daily Digest",
    generated_at: Optional[str] = None,
) -> str:
    """Format a local text digest from ranked alert schema objects."""
    timestamp = _clean(generated_at) or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    lines = [
        title,
        f"Generated at: {timestamp}",
        f"Alerts: {len(alerts)}",
    ]
    if not alerts:
        lines.extend(["", "No alerts selected for this digest."])
        return "\n".join(lines)

    for index, alert in enumerate(alerts, start=1):
        source_url = ""
        if source_urls is not None and index - 1 < len(source_urls):
            source_url = source_urls[index - 1]
        lines.extend(
            [
                "",
                f"{index}. {build_alert_headline(alert)}",
                format_alert_card(alert, source_url=source_url),
            ]
        )
    return "\n".join(lines)


def format_daily_digest_markdown(
    alerts: Sequence[Mapping[str, Any]],
    *,
    source_urls: Optional[Sequence[str]] = None,
    title: str = "Ежедневный превью-дайджест competitor tracker",
    generated_at: Optional[str] = None,
) -> str:
    """Format a markdown digest that is comfortable to review manually."""
    timestamp = _clean(generated_at) or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    lines = [
        f"# {title}",
        "",
        f"- Сгенерировано: {timestamp}",
        f"- Алертов в дайджесте: {len(alerts)}",
    ]
    if not alerts:
        lines.extend(["", "_Сегодня релевантных alert'ов для ручной проверки не нашлось._"])
        return "\n".join(lines)

    for index, alert in enumerate(alerts, start=1):
        source_url = ""
        if source_urls is not None and index - 1 < len(source_urls):
            source_url = source_urls[index - 1]
        market = _clean(alert.get("country") or alert.get("region") or "Unknown")
        lines.extend(
            [
                "",
                f"## {index}. {build_alert_headline(alert)}",
                "",
                f"- Конкурент: {_clean(alert.get('competitor'))}",
                f"- Тема: {_clean(alert.get('topic'))}",
                f"- Приоритет: {_clean(str(alert.get('priority', '')).upper())}",
                f"- Рынок: {market}",
                f"- Уверенность: {_clean(alert.get('confidence'))}",
                "",
                "### Что произошло",
                _clean(alert.get("what_happened")),
                "",
                "### Почему это важно",
                _clean(alert.get("why_it_matters")),
                "",
                "### Потенциальное влияние",
                _clean(alert.get("potential_impact")),
                "",
                "### Что делать",
                _clean(alert.get("recommended_action")),
                "",
                "### Источник",
                _clean(source_url or alert.get("source_url")),
            ]
        )
    return "\n".join(lines)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()
