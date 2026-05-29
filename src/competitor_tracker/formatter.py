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
    return f"🚨 Competitor Alert — {market}"


def format_alert_card(
    alert: Mapping[str, Any],
    *,
    source_url: str = "",
    headline: Optional[str] = None,
) -> str:
    """Format one alert card in a Telegram-friendly delivery style."""
    resolved_source_url = _clean(source_url or alert.get("source_url"))
    resolved_market = _clean(alert.get("country") or alert.get("region") or "Unknown")
    what_happened = _clean(alert.get("what_happened"))
    if what_happened and not what_happened.endswith("."):
        what_happened = f"{what_happened}."
    why_it_matters = _clean(alert.get("why_it_matters"))
    potential_impact = _clean(alert.get("potential_impact"))
    recommended_action = _clean(alert.get("recommended_action"))
    lines = [
        headline or build_alert_headline(alert),
        "",
        f"Конкурент: {_clean(alert.get('competitor'))}",
        f"Событие: {_clean(alert.get('topic'))}",
        f"Приоритет: {_clean(str(alert.get('priority', '')).upper())}",
        "",
        "Что произошло:",
        what_happened,
        f"Где: {resolved_market}",
        "",
        "Источник:",
        resolved_source_url,
        "",
        "Почему это важно:",
        why_it_matters,
        "",
        "Потенциальное влияние:",
        potential_impact,
        "",
        "Что делать:",
        recommended_action,
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
                f"- Валидация geo/date: competitor={_clean(alert.get('competitor_source') or 'n/a')}, "
                f"region={_clean(alert.get('region_source') or 'n/a')}, "
                f"country={_clean(alert.get('country_source') or 'n/a')}, "
                f"date={_clean(alert.get('published_date_source') or 'unknown')}, "
                f"fallback={_clean(alert.get('geo_validation_fallback'))}",
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
