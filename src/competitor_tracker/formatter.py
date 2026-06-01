"""Text formatters for competitor tracker delivery payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


LABELS = {
    "en": {
        "headline": "Competitor Alert",
        "unknown_market": "Unknown Market",
        "unknown_where": "Unknown",
        "competitor": "Competitor",
        "event": "Event",
        "priority": "Priority",
        "what_happened": "What happened",
        "where": "Where",
        "source": "Source",
        "why_it_matters": "Why it matters",
        "potential_impact": "Potential impact",
        "product_take": "Product take",
        "product_risk": "Product risk",
        "product_follow_up": "Product follow-up",
    },
    "ru": {
        "headline": "Алерт по конкуренту",
        "unknown_market": "Неизвестный рынок",
        "unknown_where": "Неизвестно",
        "competitor": "Конкурент",
        "event": "Событие",
        "priority": "Приоритет",
        "what_happened": "Что произошло",
        "where": "Где",
        "source": "Источник",
        "why_it_matters": "Почему это важно",
        "potential_impact": "Потенциальное влияние",
        "product_take": "Продуктовый вывод",
        "product_risk": "Продуктовый риск",
        "product_follow_up": "Что проверить продуктово",
    },
}


def build_alert_headline(alert: Mapping[str, Any], *, locale: str = "en") -> str:
    """Build a brief-style alert headline."""
    labels = LABELS.get(locale, LABELS["en"])
    market = _clean(
        alert.get("country")
        or alert.get("region")
        or labels["unknown_market"]
    )
    return f"{labels['headline']} — {market}"


def format_alert_card(
    alert: Mapping[str, Any],
    *,
    source_url: str = "",
    headline: Optional[str] = None,
    locale: str = "en",
) -> str:
    """Format one alert card in a Telegram-friendly delivery style."""
    labels = LABELS.get(locale, LABELS["en"])
    resolved_source_url = _clean(source_url or alert.get("source_url"))
    resolved_market = _clean(alert.get("country") or alert.get("region") or labels["unknown_where"])
    event = _clean(alert.get("event") or alert.get("topic"))
    what_happened = _clean(alert.get("what_happened"))
    if what_happened and not what_happened.endswith("."):
        what_happened = f"{what_happened}."
    why_it_matters = _clean(alert.get("why_it_matters"))
    potential_impact = _clean(alert.get("potential_impact"))
    product_take, product_risk, product_follow_up = _localized_product_block(
        alert,
        locale=locale,
    )
    lines = [
        headline or build_alert_headline(alert, locale=locale),
        "",
        f"{labels['competitor']}: {_clean(alert.get('competitor'))}",
        f"{labels['event']}: {event}",
        f"{labels['priority']}: {_clean(str(alert.get('priority', '')).upper())}",
        "",
        f"{labels['what_happened']}:",
        what_happened,
        f"{labels['where']}: {resolved_market}",
        "",
        f"{labels['source']}:",
        resolved_source_url,
        "",
        f"{labels['why_it_matters']}:",
        why_it_matters,
        "",
        f"{labels['potential_impact']}:",
        potential_impact,
    ]
    if product_take:
        lines.extend(
            [
                "",
                f"{labels['product_take']}:",
                product_take,
            ]
        )
    if product_risk:
        lines.extend(
            [
                "",
                f"{labels['product_risk']}:",
                product_risk,
            ]
        )
    if product_follow_up:
        lines.extend(
            [
                "",
                f"{labels['product_follow_up']}:",
                product_follow_up,
            ]
        )
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
    title: str = "Competitor Tracker Digest Preview",
    generated_at: Optional[str] = None,
) -> str:
    """Format a markdown digest that mirrors the final alert contract."""
    timestamp = _clean(generated_at) or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    lines = [
        f"# {title}",
        "",
        f"- Generated at: {timestamp}",
        f"- Alerts: {len(alerts)}",
    ]
    if not alerts:
        lines.extend(["", "_No alerts were selected for this digest._"])
        return "\n".join(lines)

    for index, alert in enumerate(alerts, start=1):
        source_url = ""
        if source_urls is not None and index - 1 < len(source_urls):
            source_url = source_urls[index - 1]
        market = _clean(alert.get("country") or alert.get("region") or "Unknown")
        event = _clean(alert.get("event") or alert.get("topic"))
        lines.extend(
            [
                "",
                f"## {index}. {build_alert_headline(alert)}",
                "",
                f"- Competitor: {_clean(alert.get('competitor'))}",
                f"- Event: {event}",
                f"- Priority: {_clean(str(alert.get('priority', '')).upper())}",
                f"- Where: {market}",
                f"- Confidence: {_clean(alert.get('confidence'))}",
                f"- Validation: competitor={_clean(alert.get('competitor_source') or 'n/a')}, "
                f"region={_clean(alert.get('region_source') or 'n/a')}, "
                f"country={_clean(alert.get('country_source') or 'n/a')}, "
                f"date={_clean(alert.get('published_date_source') or 'unknown')}, "
                f"fallback={_clean(alert.get('geo_validation_fallback'))}",
                "",
                "### What happened",
                _clean(alert.get("what_happened")),
                "",
                "### Why it matters",
                _clean(alert.get("why_it_matters")),
                "",
                "### Potential impact",
                _clean(alert.get("potential_impact")),
                "",
                "### Source",
                _clean(source_url or alert.get("source_url")),
            ]
        )
        product_take = _clean(alert.get("product_take"))
        product_risk = _clean(alert.get("product_risk"))
        product_follow_up = _clean(alert.get("product_follow_up"))
        if product_take:
            lines.extend(["", "### Product take", product_take])
        if product_risk:
            lines.extend(["", "### Product risk", product_risk])
        if product_follow_up:
            lines.extend(["", "### Product follow-up", product_follow_up])
    return "\n".join(lines)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _localized_product_block(
    alert: Mapping[str, Any],
    *,
    locale: str,
) -> tuple[str, str, str]:
    raw_take = _clean(alert.get("product_take"))
    raw_risk = _clean(alert.get("product_risk"))
    raw_follow_up = _clean(alert.get("product_follow_up"))
    if locale != "ru":
        return raw_take, raw_risk, raw_follow_up

    trigger = _clean(alert.get("product_strategist_trigger"))
    if not trigger:
        return raw_take, raw_risk, raw_follow_up

    market = _clean(
        alert.get("country")
        or alert.get("region")
        or LABELS["ru"]["unknown_where"]
    )
    localized = {
        "pricing_promo": (
            f"Этот ценовой ход может изменить восприятие ценности для райдеров и экономики заработка для водителей в {market}.",
            "Есть риск давления на value proposition, если конкурент задаст более сильный локальный ориентир по цене или incentives.",
            "Проверьте локальную ценовую архитектуру, промо-ограничения и нужен ли inDrive более точный ответ по value proposition.",
        ),
        "product_features_innovation": (
            f"Этот продуктовый или сервисный запуск может поднять ожидания по feature parity для райдеров или водителей в {market}.",
            "Есть риск восприятия продуктового отставания, если конкурент превратит эту возможность в заметное пользовательское обещание.",
            "Проверьте релевантность для parity, скорость rollout и нужен ли ответ через продукт, GTM или messaging.",
        ),
        "strategic_operations": (
            f"Этот операционный ход может улучшить надёжность сервиса, качество supply или эффективность выхода на рынок в {market}.",
            "Есть риск усиления локального execution advantage, если партнёрства, market-entry mechanics или сервисные операции улучшатся быстрее ответа inDrive.",
            "Проверьте операционные зависимости, friction при выходе на рынок и нужен ли ответ через продукт или ops, а не только через коммуникации.",
        ),
        "performance_growth": (
            f"Этот growth-сигнал, вероятно, связан с продуктовыми или операционными механиками в {market}.",
            "Есть риск усиления давления на marketplace quality или value perception, если underlying mechanism масштабируется локально.",
            "Проверьте, должен ли ответ идти через ops levers, driver experience или service design.",
        ),
    }
    return localized.get(trigger, (raw_take, raw_risk, raw_follow_up))
