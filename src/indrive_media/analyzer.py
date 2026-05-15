import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import openai
from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)


COMPANY_TERMS = ("indrive", "indriver", "in-driver")
SERVICE_TERMS = (
    "taxi",
    "ride-hailing",
    "ride hailing",
    "rideshare",
    "ride-share",
    "driver",
    "passenger",
    "fare",
    "delivery",
    "courier",
    "freight",
    "city to city",
    "такси",
    "водитель",
    "пассажир",
    "доставка",
    "курьер",
    "груз",
)
COMPETITOR_TERMS = ("uber", "bolt", "grab", "gojek", "didi", "yandex go", "careem")
REGULATION_TERMS = (
    "regulation",
    "law",
    "ban",
    "license",
    "permit",
    "strike",
    "protest",
    "commission",
    "fuel",
    "safety",
    "pricing",
    "fare cap",
    "регулирование",
    "закон",
    "лицензия",
    "забастовка",
    "комиссия",
    "топливо",
    "безопасность",
    "тариф",
)
NOISE_TERMS = ("stock market", "crypto", "celebrity", "football", "recipe")


class MentionAnalyzer:
    def __init__(
        self,
        use_llm: bool = True,
        model: Optional[str] = None,
        company_context_path: str = "company_context.md",
    ):
        self.company_context = self._load_company_context(company_context_path)
        self.use_llm = use_llm and bool(os.getenv("OPENAI_API_KEY"))
        self.client = None

        if self.use_llm:
            self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def analyze_article(self, title: str, text: str, url: str = "") -> Dict[str, Any]:
        base = self._heuristic_analysis(title, text, url)

        if not self.use_llm or self.client is None:
            return base

        llm_result = self._llm_analysis(title, text, url, base)
        if not llm_result:
            return base

        llm_result["matched_terms"] = base["matched_terms"]
        llm_result["mentions_indrive"] = base["mentions_indrive"]
        return self._normalize_result({**base, **llm_result})

    def heuristic_analysis(self, title: str, text: str, url: str = "") -> Dict[str, Any]:
        """Public method for heuristic analysis only."""
        return self._heuristic_analysis(title, text, url)

    def _heuristic_analysis(self, title: str, text: str, url: str = "") -> Dict[str, Any]:
        content = self._normalize(" ".join([title or "", text or "", url or ""]))
        matched = {
            "company": self._matches(content, COMPANY_TERMS),
            "service": self._matches(content, SERVICE_TERMS),
            "competitors": self._matches(content, COMPETITOR_TERMS),
            "regulation": self._matches(content, REGULATION_TERMS),
            "noise": self._matches(content, NOISE_TERMS),
        }

        score = 0
        if matched["company"]:
            score += 7
        if matched["service"]:
            score += 2
        if matched["competitors"]:
            score += 1
        if matched["regulation"]:
            score += 1
        if matched["noise"] and not matched["company"]:
            score -= 2

        score = max(0, min(10, score))
        topic = self._topic(matched)
        essence = self._fallback_essence(title, text)
        context = self._fallback_context(matched, essence)
        importance = self._fallback_importance(matched, score >= 7)

        return {
            "sentiment": "neutral",
            "main_topic": topic,
            "category_ru": self._category(matched),
            "relevance_score": score,
            "is_business_critical": score >= 7,
            "summary": essence,
            "pm_insight": importance,
            "article_essence_ru": essence,
            "mention_context_ru": context,
            "pm_importance_ru": importance,
            "matched_terms": matched,
            "mentions_indrive": bool(matched["company"]),
        }

    def _llm_analysis(
        self,
        title: str,
        text: str,
        url: str,
        base: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        system_prompt = """Ты senior product intelligence analyst inDrive. Ты готовишь практичные медиа-инсайты для product managers, operations и safety/growth teams.

Контекст компании, который нужно учитывать в каждом анализе:
{company_context}

Правила:
- Пиши на русском.
- Не пиши общие фразы вроде "важно следить", "проверить влияние" без конкретики.
- В каждом выводе укажи: какой факт произошел, какой сегмент затронут, какую продуктовую область это задевает.
- Для PM importance дай 2-3 конкретных направления проверки: метрика, сценарий, команда или продуктовая поверхность.
- Используй формулировки уровня PM: onboarding, pricing, commission, driver earnings, rider trust, safety flow, supply/demand, market launch, compliance, retention, conversion, support.
- Если доступен только заголовок или короткий сниппет, честно опирайся на него и не придумывай факты.
- Верни только строгий JSON без Markdown.

Оценка релевантности:
0-3: нерелевантный шум.
4-6: общий рыночный, конкурентный или регуляторный контекст без прямого влияния на inDrive.
7-8: прямое упоминание inDrive или конкретный сигнал по такси/доставке/водителям/пассажирам.
9-10: прямое упоминание inDrive с продуктовым, рыночным, safety, pricing, driver, passenger, delivery, legal или competitive impact.

JSON schema:
{{
  "sentiment": "positive|negative|neutral",
  "main_topic": "короткая тема на русском",
  "category_ru": "Регулирование|Безопасность|Водители|Пассажиры|Тарифы|Доставка|Конкуренты|Репутация|Запуск рынка|Общее",
  "relevance_score": 0,
  "is_business_critical": false,
  "article_essence_ru": "1-2 предложения: конкретная суть статьи на русском, без воды",
  "mention_context_ru": "2 предложения: как именно упоминается inDrive, рынок/страна, затронутый сервис и stakeholder",
  "pm_importance_ru": "2-3 предложения: почему это важно для PM; какие продуктовые метрики, сценарии или команды должны это проверить",
  "summary": "короткая русская версия сути статьи",
  "pm_insight": "короткий русский вывод для PM"
}}"""

        user_prompt = """Title: {title}
URL: {url}
Available text/snippet:
{text}

Heuristic signal:
{heuristic_signal}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt.format(company_context=self.company_context)},
                    {"role": "user", "content": user_prompt.format(
                        title=title,
                        url=url,
                        text=text,
                        heuristic_signal=json.dumps(base, ensure_ascii=False, indent=2)
                    )}
                ],
                temperature=0,
                max_tokens=1000,
            )
            content = response.choices[0].message.content.strip()
            content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.I | re.S)
            result = json.loads(content)
            return self._normalize_result(result)
        except Exception as exc:
            logger.warning(
                "LLM analysis failed; falling back to heuristic result. title=%r url=%r error=%s",
                self._clean_text(title)[:120],
                url,
                exc,
                exc_info=True,
            )
            return None

    def _normalize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        result["relevance_score"] = int(result.get("relevance_score", 0) or 0)
        result["relevance_score"] = max(0, min(10, result["relevance_score"]))
        result["is_business_critical"] = bool(result.get("is_business_critical", False))
        result["article_essence_ru"] = self._clean_text(
            result.get("article_essence_ru") or result.get("summary") or "Суть статьи не определена."
        )
        result["mention_context_ru"] = self._clean_text(
            result.get("mention_context_ru") or "Контекст упоминания inDrive не определен."
        )
        result["pm_importance_ru"] = self._clean_text(
            result.get("pm_importance_ru")
            or result.get("pm_insight")
            or "Проверить возможное влияние на продукт, операции или репутацию inDrive."
        )
        result["summary"] = self._clean_text(result.get("summary") or result["article_essence_ru"])
        result["pm_insight"] = self._clean_text(result.get("pm_insight") or result["pm_importance_ru"])
        result["main_topic"] = self._clean_text(result.get("main_topic") or "Упоминание inDrive")
        result["category_ru"] = self._clean_text(result.get("category_ru") or "Общее")
        return result

    @staticmethod
    def _load_company_context(path: str) -> str:
        context_path = Path(path)
        if context_path.exists():
            return context_path.read_text(encoding="utf-8")
        return "inDrive is a global ride-hailing and urban services platform."

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold()).strip()

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _matches(content: str, terms: List[str] | tuple[str, ...]) -> List[str]:
        matches = set()
        for term in terms:
            pattern = rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])"
            if re.search(pattern, content):
                matches.add(term)
        return sorted(matches)

    @staticmethod
    def _fallback_essence(title: str, text: str) -> str:
        source = MentionAnalyzer._clean_text(title or text)
        if not source:
            return "В доступном тексте есть упоминание inDrive, но суть статьи не определена."
        return f"Статья сообщает: {source[:240]}."

    @staticmethod
    def _topic(matched: Dict[str, List[str]]) -> str:
        if matched["company"] and matched["service"]:
            return "Прямое упоминание сервиса inDrive"
        if matched["company"]:
            return "Прямое упоминание inDrive"
        if matched["regulation"]:
            return "Регулирование такси или доставки"
        if matched["competitors"]:
            return "Активность конкурентов"
        if matched["service"]:
            return "Рынок мобильности или доставки"
        return "Низкая релевантность"

    @staticmethod
    def _category(matched: Dict[str, List[str]]) -> str:
        regulation = set(matched.get("regulation", []))
        service = set(matched.get("service", []))
        if regulation & {"safety", "безопасность"}:
            return "Безопасность"
        if regulation & {"regulation", "law", "ban", "license", "permit", "strike", "protest"}:
            return "Регулирование"
        if regulation & {"commission", "fuel", "pricing", "fare cap"} or service & {"fare"}:
            return "Тарифы"
        if service & {"delivery", "courier", "freight"}:
            return "Доставка"
        if service & {"driver"}:
            return "Водители"
        if service & {"passenger"}:
            return "Пассажиры"
        if matched.get("competitors"):
            return "Конкуренты"
        return "Общее"

    @staticmethod
    def _fallback_context(matched: Dict[str, List[str]], essence: str) -> str:
        category = MentionAnalyzer._category(matched)
        if category == "Регулирование":
            return "inDrive упоминается в контексте локальных правил, разрешений, лицензий или правового статуса сервиса."
        if category == "Безопасность":
            return "inDrive упоминается в контексте безопасности пользователей, водителей или поездок."
        if category == "Тарифы":
            return "inDrive упоминается в контексте тарифов, комиссий, стоимости топлива или экономики поездки."
        if category == "Доставка":
            return "inDrive упоминается в контексте доставки, курьерских или грузовых сервисов."
        if category == "Водители":
            return "inDrive упоминается в контексте опыта, доходов, условий или поведения водителей."
        if category == "Пассажиры":
            return "inDrive упоминается в контексте опыта пассажиров, доступности поездок или доверия к сервису."
        if category == "Конкуренты":
            return "inDrive упоминается в контексте конкурентной среды ride-hailing или доставки."
        return f"inDrive упоминается как компания или сервис в медиа. Доступный сигнал: {essence}"

    @staticmethod
    def _fallback_importance(matched: Dict[str, List[str]], is_critical: bool) -> str:
        if not is_critical:
            return "Это фоновый сигнал; срочных продуктовых действий не требуется, но материал можно учитывать в мониторинге рынка."
        category = MentionAnalyzer._category(matched)
        if category == "Регулирование":
            return "PM стоит проверить, влияет ли новость на легальность сервиса, onboarding водителей, локальные ограничения и коммуникации в приложении."
        if category == "Безопасность":
            return "PM стоит проверить safety-механики, доверие пользователей, сценарии поддержки и коммуникации после инцидентов."
        if category == "Тарифы":
            return "PM стоит проверить влияние на ценообразование, комиссии, доход водителей, конверсию пассажиров и конкурентоспособность цены."
        if category == "Доставка":
            return "PM стоит проверить влияние на надежность доставки, supply курьеров, стоимость заказа и качество сервиса."
        if category == "Водители":
            return "PM стоит проверить влияние на привлечение, удержание, доход и удовлетворенность водителей."
        if category == "Пассажиры":
            return "PM стоит проверить влияние на доступность поездок, доверие, цену, ожидание и качество пассажирского опыта."
        if category == "Конкуренты":
            return "PM стоит сравнить сигнал с позиционированием inDrive, функциями конкурентов и локальной стратегией роста."
        return "PM стоит оценить возможное влияние на продукт, операции, локальный рынок или репутацию inDrive."
