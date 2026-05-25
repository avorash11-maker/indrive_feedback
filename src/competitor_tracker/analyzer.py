"""Analysis layer for competitor mentions."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, List, Mapping, Optional, Sequence

import openai

from .config import TrackerConfig
from .formatter import format_alert_card
from .models import (
    AlertSchema,
    ArticleContext,
    CandidateArticle,
    RawArticle,
    ResolvedPublicationDateSource,
)


logger = logging.getLogger(__name__)

UNDATED_FALLBACK_PUBLICATION_DATE = date.min


COUNTRY_ALIAS_MAP = {
    "ae": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "u a e": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "sa": "Saudi Arabia",
    "ksa": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "saudi arabia": "Saudi Arabia",
    "qa": "Qatar",
    "jo": "Jordan",
    "bh": "Bahrain",
    "kw": "Kuwait",
    "om": "Oman",
    "iq": "Iraq",
    "lb": "Lebanon",
    "eg": "Egypt",
    "egypt": "Egypt",
    "mx": "Mexico",
    "mexico": "Mexico",
    "br": "Brazil",
    "brazil": "Brazil",
    "brasil": "Brazil",
    "co": "Colombia",
    "colombia": "Colombia",
    "pe": "Peru",
    "peru": "Peru",
    "cl": "Chile",
    "chile": "Chile",
    "ar": "Argentina",
    "argentina": "Argentina",
    "ec": "Ecuador",
    "ecuador": "Ecuador",
    "uy": "Uruguay",
    "py": "Paraguay",
    "bo": "Bolivia",
    "cr": "Costa Rica",
    "do": "Dominican Republic",
    "gt": "Guatemala",
    "pa": "Panama",
    "za": "South Africa",
    "south africa": "South Africa",
    "ke": "Kenya",
    "kenya": "Kenya",
    "ng": "Nigeria",
    "nigeria": "Nigeria",
    "ma": "Morocco",
    "morocco": "Morocco",
    "gh": "Ghana",
    "tz": "Tanzania",
    "ug": "Uganda",
    "ci": "Cote d'Ivoire",
    "cote d'ivoire": "Cote d'Ivoire",
    "cote divoire": "Cote d'Ivoire",
    "côte d'ivoire": "Cote d'Ivoire",
    "côte divoire": "Cote d'Ivoire",
    "ivory coast": "Ivory Coast",
    "sn": "Senegal",
    "tn": "Tunisia",
    "dz": "Algeria",
    "algeria": "Algeria",
    "et": "Ethiopia",
    "id": "Indonesia",
    "indonesia": "Indonesia",
    "th": "Thailand",
    "thailand": "Thailand",
    "vn": "Vietnam",
    "vietnam": "Vietnam",
    "ph": "Philippines",
    "philippines": "Philippines",
    "sg": "Singapore",
    "singapore": "Singapore",
    "my": "Malaysia",
    "malaysia": "Malaysia",
    "kh": "Cambodia",
    "la": "Laos",
    "mm": "Myanmar",
    "bn": "Brunei",
    "ru": "Russia",
    "russia": "Russia",
    "russian federation": "Russia",
    "kz": "Kazakhstan",
    "kazakhstan": "Kazakhstan",
    "uz": "Uzbekistan",
    "uzbekistan": "Uzbekistan",
    "by": "Belarus",
    "belarus": "Belarus",
    "kg": "Kyrgyzstan",
    "kyrgyzstan": "Kyrgyzstan",
    "ge": "Georgia",
    "georgia": "Georgia",
    "am": "Armenia",
    "armenia": "Armenia",
    "az": "Azerbaijan",
    "tj": "Tajikistan",
    "mn": "Mongolia",
}


def _coerce_publication_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    candidate = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", candidate)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except Exception:
            return None

    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def resolve_final_publication_date(
    alert_schema: Mapping[str, Any],
    candidate_raw: RawArticle,
) -> tuple[date, ResolvedPublicationDateSource]:
    """Resolve one canonical publication date for the whole tracker."""
    resolved_value = _coerce_publication_date(alert_schema.get("resolved_publication_date"))
    resolved_source = str(
        alert_schema.get("resolved_publication_date_source") or ""
    ).strip().lower()
    if (
        resolved_value is not None
        and resolved_source in {"provider", "html_scraped", "llm", "undated_fallback"}
    ):
        return resolved_value, resolved_source

    llm_date = _coerce_publication_date(
        alert_schema.get("_llm_publication_date")
        or (
            alert_schema.get("published_date")
            if str(alert_schema.get("published_date_source") or "").strip().lower() == "llm"
            else None
        )
    )
    if llm_date is not None:
        return llm_date, "llm"

    html_date = _coerce_publication_date(alert_schema.get("_html_scraped_publication_date"))
    if html_date is not None:
        return html_date, "html_scraped"

    provider_date = _coerce_publication_date(candidate_raw.published_at)
    if provider_date is not None:
        return provider_date, "provider"

    return UNDATED_FALLBACK_PUBLICATION_DATE, "undated_fallback"


@dataclass(slots=True)
class AnalysisResult:
    """Analyzer output for a batch of mentions."""

    candidates: List[CandidateArticle] = field(default_factory=list)
    dropped_count: int = 0


class CompetitorAnalyzer:
    """Applies lightweight prefiltering and scoring rules before any LLM step."""

    def __init__(
        self,
        min_score: int = 5,
        config: Optional[TrackerConfig] = None,
    ) -> None:
        self.min_score = min_score
        self.config = config

    def analyze(
        self, candidates: Iterable[CandidateArticle]
    ) -> AnalysisResult:
        kept: List[CandidateArticle] = []
        dropped = 0
        for candidate in candidates:
            if candidate.score >= self.min_score:
                kept.append(candidate)
            else:
                dropped += 1
        return AnalysisResult(candidates=kept, dropped_count=dropped)

    def prefilter_raw_articles(
        self,
        raw_articles: Iterable[RawArticle],
        *,
        regions: Optional[Sequence[str]] = None,
    ) -> AnalysisResult:
        """Build candidate articles from raw hits using cheap rule-based logic."""
        if self.config is None:
            raise ValueError("TrackerConfig is required for raw article prefiltering")

        candidates: List[CandidateArticle] = []
        dropped = 0
        for article in raw_articles:
            candidate = self._build_candidate(article, regions=regions)
            if candidate is None:
                dropped += 1
                continue
            if candidate.score >= self.min_score:
                candidates.append(candidate)
            else:
                dropped += 1
        return AnalysisResult(candidates=candidates, dropped_count=dropped)

    def _build_candidate(
        self,
        article: RawArticle,
        *,
        regions: Optional[Sequence[str]] = None,
    ) -> Optional[CandidateArticle]:
        if self.config is None:
            return None

        text_blob = self._article_text_blob(article)
        geo_text_blob = self._geo_text_blob(article)
        selected_regions = tuple(regions or self.config.regions.keys())
        competitor = self._detect_competitor(article, text_blob, selected_regions)
        if not competitor:
            return None

        topic_group, matched_keywords = self._detect_topic(text_blob)
        if not topic_group:
            return None

        region_key, country_hint = self._detect_region(article, geo_text_blob, selected_regions)
        region_key, country_hint = self._validate_candidate_region(
            competitor=competitor,
            region_key=region_key,
            country_hint=country_hint,
            selected_regions=selected_regions,
        )
        if region_key is None and country_hint is None and self._is_region_mismatch(
            competitor=competitor,
            article=article,
            text_blob=geo_text_blob,
            selected_regions=selected_regions,
        ):
            return None
        language_hint = self._detect_language(article, text_blob, region_key)
        score, reasons = self._score_candidate(
            article=article,
            competitor=competitor,
            topic_group=topic_group,
            matched_keywords=matched_keywords,
            region_key=region_key,
            country_hint=country_hint,
            language_hint=language_hint,
            text_blob=text_blob,
        )
        return CandidateArticle(
            raw_article=article,
            competitor=competitor,
            topic_group=topic_group,
            score=score,
            matched_keywords=matched_keywords,
            region=region_key,
            country_hint=country_hint,
            language_hint=language_hint,
            reasons=tuple(reasons),
            summary=self._build_summary(competitor, topic_group, article.title, country_hint),
        )

    @staticmethod
    def _article_text_blob(article: RawArticle) -> str:
        return " ".join(
            value.casefold()
            for value in (
                article.title,
                article.snippet,
                article.query,
                article.source,
                article.region or "",
                article.language or "",
            )
            if value
        )

    @staticmethod
    def _geo_text_blob(article: RawArticle) -> str:
        return " ".join(
            value.casefold()
            for value in (
                article.title,
                article.snippet,
                article.source,
            )
            if value
        )

    def _detect_competitor(
        self,
        article: RawArticle,
        text_blob: str,
        regions: Sequence[str],
    ) -> Optional[str]:
        if self.config is None:
            return None

        region_competitors = []
        for region in regions:
            region_competitors.extend(self.config.competitors_by_region.get(region, ()))
        competitors = tuple(dict.fromkeys(region_competitors)) or self.config.all_competitors()

        hint_map = {hint.casefold(): hint for hint in article.competitor_hints}
        for competitor in competitors:
            if competitor.casefold() in hint_map:
                return competitor
        for competitor in competitors:
            if competitor.casefold() in text_blob:
                return competitor
        return None

    def _validate_candidate_region(
        self,
        *,
        competitor: str,
        region_key: Optional[str],
        country_hint: Optional[str],
        selected_regions: Sequence[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if self.config is None:
            return region_key, country_hint

        if region_key and self.config.is_competitor_allowed_in_region(competitor, region_key):
            return region_key, country_hint
        if region_key:
            return None, None

        return None, None

    def _is_region_mismatch(
        self,
        *,
        competitor: str,
        article: RawArticle,
        text_blob: str,
        selected_regions: Sequence[str],
    ) -> bool:
        if self.config is None:
            return False

        region_markers = []
        if article.region and article.region in self.config.regions:
            region_markers.append(article.region)
        for region in selected_regions:
            region_config = self.config.regions[region]
            for geo_term in region_config.geo_terms:
                if geo_term.casefold() in text_blob:
                    region_markers.append(region)
                    break

        unique_markers = tuple(dict.fromkeys(region_markers))
        return any(
            not self.config.is_competitor_allowed_in_region(competitor, region)
            for region in unique_markers
        )

    def _detect_topic(self, text_blob: str) -> tuple[Optional[str], tuple[str, ...]]:
        if self.config is None:
            return None, ()

        best_topic: Optional[str] = None
        best_keywords: tuple[str, ...] = ()
        best_score = 0
        for topic_name, keywords in self.config.topic_groups.items():
            matched = tuple(keyword for keyword in keywords if keyword.casefold() in text_blob)
            if len(matched) > best_score:
                best_topic = topic_name
                best_keywords = matched
                best_score = len(matched)
        return best_topic, best_keywords

    def _detect_region(
        self,
        article: RawArticle,
        text_blob: str,
        regions: Sequence[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if self.config is None:
            return None, None

        if article.region and article.region in self.config.regions:
            region_config = self.config.regions[article.region]
            for geo_term in region_config.geo_terms:
                if geo_term.casefold() in text_blob:
                    return article.region, geo_term
            return article.region, None

        for region in regions:
            region_config = self.config.regions[region]
            for geo_term in region_config.geo_terms:
                if geo_term.casefold() in text_blob:
                    return region, geo_term
        return None, None

    def _detect_language(
        self,
        article: RawArticle,
        text_blob: str,
        region_key: Optional[str],
    ) -> Optional[str]:
        if self.config is None:
            return article.language

        if article.language:
            return article.language
        if not region_key:
            return None
        for hint in self.config.regions[region_key].language_hints:
            if f" {hint.casefold()} " in f" {text_blob} ":
                return hint
        return self.config.regions[region_key].language_hints[0]

    def _score_candidate(
        self,
        *,
        article: RawArticle,
        competitor: str,
        topic_group: str,
        matched_keywords: Sequence[str],
        region_key: Optional[str],
        country_hint: Optional[str],
        language_hint: Optional[str],
        text_blob: str,
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        if competitor.casefold() in text_blob:
            score += 5
            reasons.append("competitor_mentioned")
        if article.competitor_hints:
            score += 1
            reasons.append("provider_competitor_hint")

        topic_points = min(len(matched_keywords), 3) * 2
        if topic_points:
            score += topic_points
            reasons.append(f"topic_match:{topic_group}")

        if region_key:
            score += 2
            reasons.append(f"region_match:{region_key}")
        if country_hint:
            score += 1
            reasons.append(f"country_hint:{country_hint}")
        if language_hint:
            score += 1
            reasons.append(f"language_hint:{language_hint}")

        priority_terms = {
            "pricing": ("commission", "discount", "fare", "price", "pricing"),
            "regulation": ("ban", "permit", "license", "compliance", "regulation", "regulatory approval"),
            "safety": ("incident", "security", "insurance", "background check", "safety"),
            "product_launch": ("launch", "rollout", "expansion", "partnership", "pilot"),
            "market_expansion": (
                "launch",
                "launching in",
                "new city",
                "entering market",
                "market entry",
                "expansion",
                "license obtained",
                "regulatory approval",
            ),
            "campaign_launches": (
                "campaign",
                "partnership",
                "brand ambassador",
                "new feature",
                "strategic partnership",
                "driver recruitment campaign",
            ),
            "pricing_promo": (
                "discount",
                "promo code",
                "price cut",
                "subscription",
                "first ride free",
                "discounted rides",
                "referral bonus",
                "loyalty program",
                "low commission",
                "bonus for new drivers",
            ),
            "industry_context": (
                "ride-hailing",
                "e-hailing",
                "on-demand mobility",
                "ride-sharing",
                "taxi app",
                "vtc",
                "maas",
                "mobility as a service",
            ),
            "strategic_operations": (
                "market entry",
                "launching operations",
                "license obtained",
                "regulatory approval",
                "strategic partnership",
                "driver recruitment campaign",
            ),
            "performance_growth": (
                "first ride free",
                "discounted rides",
                "referral bonus",
                "loyalty program",
                "low commission",
                "bonus for new drivers",
            ),
            "product_features_innovation": (
                "intercity",
                "delivery",
                "courier service",
                "freight",
                "fixed price",
                "bidding model",
                "safety features",
            ),
        }
        if any(term in text_blob for term in priority_terms.get(topic_group, ())):
            score += 2
            reasons.append("priority_signal")

        return min(score, 10), reasons

    @staticmethod
    def _build_summary(
        competitor: str,
        topic_group: str,
        title: str,
        country_hint: Optional[str],
    ) -> str:
        if country_hint:
            return f"{competitor} / {topic_group} / {country_hint}: {title}"
        return f"{competitor} / {topic_group}: {title}"


class CompetitorAlertAnalyzer:
    """LLM-powered alert analyzer for competitor tracker candidates."""

    INSUFFICIENT_SOURCE_DATA_MESSAGE = (
        "Недостаточно данных для анализа, так как сайт источника недоступен"
    )
    def __init__(
        self,
        use_llm: bool = True,
        model: Optional[str] = None,
        config: Optional[TrackerConfig] = None,
    ) -> None:
        self.use_llm = use_llm and bool(os.getenv("OPENAI_API_KEY"))
        self.client = None
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.config = config

        if self.use_llm:
            try:
                self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            except Exception as exc:
                logger.warning(
                    "Competitor alert LLM client initialization failed; using rule-based fallback. error=%s",
                    exc,
                )
                self.client = None
                self.use_llm = False

    @staticmethod
    def _reference_today() -> date:
        """Return the current local date for prompt-relative date resolution."""
        return date.today()

    def _reference_today_iso(self) -> str:
        return self._reference_today().isoformat()

    def analyze_candidate(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> AlertSchema:
        """Return normalized competitor alert schema for one candidate."""
        fallback = self._fallback_alert(candidate, article_context=article_context)
        if not self.use_llm or self.client is None:
            return fallback

        llm_result = self._llm_alert_analysis(candidate, article_context=article_context)
        if not llm_result:
            return fallback
        merged = {**fallback, **llm_result}
        merged.pop("resolved_publication_date", None)
        merged.pop("resolved_publication_date_source", None)
        llm_publication_date = self._normalize_published_date(
            llm_result.get("published_date") or llm_result.get("published_at")
        )
        if llm_publication_date:
            merged["_llm_publication_date"] = llm_publication_date
        if (
            article_context is not None
            and article_context.published_at_source == "html_scraped"
        ):
            merged["_html_scraped_publication_date"] = article_context.published_at
        return self._normalize_alert(merged, candidate=candidate)

    def analyze_candidates(
        self,
        candidates: Iterable[CandidateArticle],
    ) -> list[AlertSchema]:
        """Return alert schema objects for multiple candidates."""
        return [self.analyze_candidate(candidate) for candidate in candidates]

    def format_alert(self, alert: Mapping[str, Any], *, source_url: str = "") -> str:
        """Render a readable alert text block from the analyzer schema."""
        return format_alert_card(alert, source_url=source_url)

    def _llm_alert_analysis(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> Optional[dict[str, Any]]:
        reference_today = self._reference_today_iso()
        system_prompt = """You are a senior international marketing strategist for inDrive with deep experience in ride-hailing, mobility marketplaces, regional go-to-market, growth, brand strategy, and competitor response.

Your task is to analyze a competitor article and produce a sharp, practical alert for the inDrive Marcom and growth team.

Your output must help the team:
- understand exactly what happened
- understand why it matters strategically
- estimate likely impact on perception, positioning, growth, driver/rider trust, or market narrative
- decide what inDrive should do better, faster, or differently

Think like a senior operator responsible for competitor response, local market messaging, regional GTM, and strategic brand reaction.

Rules:
- Use only evidence from the provided article context.
- Do not invent facts, metrics, partnerships, timelines, internal intent, or campaign performance.
- If evidence is limited, be explicit and stay cautious.
- Do not overstate strategic meaning when the source signal is weak.
- Treat `competitor` and `region` from candidate metadata as pre-detected pipeline signals, not as free fields for guesswork.
- Do not override the provided `competitor` or `region` unless the article contains explicit evidence that the pipeline signal is wrong.
- If the signal is ambiguous, mixed, or weak, preserve the provided pipeline `competitor` and `region`.
- Resolve the final publication date with this priority: LLM inference from article evidence, then HTML-scraped date, then provider-normalized metadata, then undated fallback when nothing trustworthy exists.
- If the provider `published_at` field is None or missing, carefully inspect the article body for publication dates or temporal markers such as "last Thursday", "yesterday", or "two days ago". Resolve them relative to today's date: {reference_today}. Write the final date into `published_date` using YYYY-MM-DD format.
- Think like a high-level international marketer, not a generic summarizer.
- Recommended actions must be concrete and useful for brand, growth, communications, partnerships, creative strategy, regional GTM, or driver/rider messaging.
- Recommended actions must be applicable to inDrive, not generic advice for "a company".
- Avoid vague advice like "monitor this" unless no stronger action is justified by the article.
- Keep wording concise, executive-friendly, and actionable.
- Return only strict JSON without markdown.
- `priority` must be one of LOW, MEDIUM, HIGH.
- `confidence` must be a number from 0 to 1.
- `published_date_source` must be one of provider, html_scraped, llm, undated_fallback.

Return this schema:
{
  "competitor": "string",
  "region": "string",
  "country": "string",
  "topic": "string",
  "priority": "LOW|MEDIUM|HIGH",
  "published_date": "YYYY-MM-DD",
  "published_date_source": "provider|html_scraped|llm|undated_fallback",
  "what_happened": "string",
  "why_it_matters": "string",
  "potential_impact": "string",
  "recommended_action": "string",
  "confidence": 0.0
}""".replace("{reference_today}", reference_today)

        user_prompt = """Candidate metadata:
{candidate_payload}

Article title: {title}
Article snippet: {snippet}
Article body: {article_body}
Article published_at metadata: {published_at}
Source query: {query}
Source URL: {url}

Today's date for reference: {reference_today}. Если точной даты нет, используй контекст текста для вычисления.

Write the alert for the inDrive Marcom / growth team.

Focus especially on:
- competitor strategy
- market narrative
- campaign or messaging angle
- likely effect on driver/rider perception
- what inDrive can do better or differently

When writing:
- treat candidate `competitor` and `region` as pipeline-detected inputs that should stay unchanged by default
- change `competitor` or `region` only if the article explicitly proves the pipeline signal is wrong
- if the article is ambiguous, preserve the provided pipeline `competitor` and `region`
- "what_happened" should state the event clearly and concretely
- "why_it_matters" should explain the strategic meaning, not just restate the article
- "potential_impact" should focus on likely effects on trust, perception, positioning, growth, or supply-demand narrative
- "recommended_action" should give specific next moves for inDrive, ideally in messaging, creative, partnerships, GTM, driver/rider value proposition, or local communications""".replace(
            "{reference_today}", reference_today
        )

        candidate_payload = {
            "competitor": candidate.competitor,
            "region": candidate.region,
            "country_hint": candidate.country_hint,
            "topic_group": candidate.topic_group,
            "score": candidate.score,
            "matched_keywords": list(candidate.matched_keywords),
            "reasons": list(candidate.reasons),
            "language_hint": candidate.language_hint,
            "published_at": article_context.published_at if article_context else candidate.raw_article.published_at,
        }
        context = article_context or ArticleContext(
            title=candidate.title,
            snippet=candidate.raw_article.snippet,
            source_url=candidate.url,
            article_body="",
            published_at=candidate.raw_article.published_at,
        )
        if article_context is not None and (
            not context.article_body or context.article_body == "Unavailable"
        ):
            undeliverable_alert: dict[str, Any] = {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group.replace("_", " "),
                "priority": self._priority_from_score(candidate.score),
                "published_date": self._normalize_published_date(
                    context.published_at or candidate.raw_article.published_at
                ),
                "what_happened": candidate.summary or candidate.title,
                "why_it_matters": self.INSUFFICIENT_SOURCE_DATA_MESSAGE,
                "potential_impact": "Potential impact remains unclear and needs validation.",
                "recommended_action": self.INSUFFICIENT_SOURCE_DATA_MESSAGE,
                "confidence": 0.0,
            }
            if context.published_at_source == "html_scraped":
                undeliverable_alert["_html_scraped_publication_date"] = context.published_at
            return self._normalize_alert(undeliverable_alert, candidate=candidate)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": user_prompt.format(
                            candidate_payload=json.dumps(
                                candidate_payload, ensure_ascii=False, indent=2
                            ),
                            title=context.title,
                            snippet=context.snippet,
                            published_at=context.published_at or candidate.raw_article.published_at or "None",
                            query=candidate.raw_article.query,
                            url=context.source_url,
                            article_body=context.article_body or "Unavailable",
                        ),
                    },
                ],
                temperature=0,
                max_tokens=700,
            )
            content = response.choices[0].message.content.strip()
            content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.I | re.S)
            return self._normalize_alert(
                json.loads(content),
                apply_truth_validation=False,
            )
        except Exception as exc:
            logger.warning(
                "Competitor alert LLM analysis failed; falling back to rule-based alert. title=%r url=%r error=%s",
                candidate.title[:120],
                candidate.url,
                exc,
                exc_info=True,
            )
            return None

    def _fallback_alert(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> AlertSchema:
        priority = self._priority_from_score(candidate.score)
        topic = candidate.topic_group.replace("_", " ")
        country = candidate.country_hint or ""
        region = candidate.region or ""
        matched = ", ".join(candidate.matched_keywords) or topic
        what_happened = (
            f"{candidate.competitor} appears in coverage related to {topic}. "
            f"Detected signals: {matched}."
        )
        why_it_matters = (
            f"This may indicate a competitor move in {topic} that can influence local market messaging, "
            "supply dynamics, regulation, or user perception."
        )
        potential_impact = (
            f"Potential impact on {candidate.competitor}'s positioning in "
            f"{country or region or 'the market'}, with possible downstream effects on driver or rider perception."
        )
        recommended_action = (
            f"Review the signal, validate local market context, and decide whether {topic} needs a response "
            "in messaging, product, pricing, or operations."
        )
        confidence = min(0.95, max(0.35, candidate.score / 10))
        fallback_alert: dict[str, Any] = {
            "competitor": candidate.competitor,
            "region": region,
            "country": country,
            "topic": topic,
            "priority": priority,
            "published_date": self._normalize_published_date(candidate.published_date),
            "what_happened": what_happened,
            "why_it_matters": why_it_matters,
            "potential_impact": potential_impact,
            "recommended_action": recommended_action,
            "confidence": confidence,
        }
        if (
            article_context is not None
            and article_context.published_at_source == "html_scraped"
        ):
            fallback_alert["_html_scraped_publication_date"] = article_context.published_at
        return self._normalize_alert(fallback_alert, candidate=candidate)

    @staticmethod
    def _priority_from_score(score: int) -> str:
        if score >= 9:
            return "HIGH"
        if score >= 6:
            return "MEDIUM"
        return "LOW"

    def _normalize_alert(
        self,
        alert: dict[str, Any],
        *,
        candidate: Optional[CandidateArticle] = None,
        apply_truth_validation: bool = True,
    ) -> AlertSchema:
        normalized = dict(alert)
        normalized["competitor"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("competitor") or "Unknown competitor"
        )
        normalized["region"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("region") or ""
        )
        normalized["country"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("country") or ""
        )
        normalized["topic"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("topic") or "general movement"
        )
        priority = str(normalized.get("priority") or "LOW").upper()
        if priority not in {"LOW", "MEDIUM", "HIGH"}:
            priority = "LOW"
        normalized["priority"] = priority
        normalized_published_date = CompetitorAlertAnalyzer._normalize_published_date(
            normalized.get("published_date") or normalized.get("published_at")
        )
        normalized["published_date"] = normalized_published_date
        normalized["what_happened"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("what_happened") or "No clear event description available."
        )
        normalized["why_it_matters"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("why_it_matters")
            or "This signal may matter for competitor positioning or market perception."
        )
        normalized["potential_impact"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("potential_impact")
            or "Potential impact remains unclear and needs validation."
        )
        normalized["recommended_action"] = CompetitorAlertAnalyzer._clean_text(
            normalized.get("recommended_action")
            or "Review the signal and decide whether any local response is needed."
        )
        try:
            confidence = float(normalized.get("confidence", 0))
        except Exception:
            confidence = 0.0
        normalized["confidence"] = max(0.0, min(1.0, confidence))
        if candidate is None:
            requested_publication_source = str(
                normalized.get("published_date_source") or ""
            ).strip().lower()
            if requested_publication_source not in {
                "provider",
                "html_scraped",
                "llm",
                "undated_fallback",
            }:
                requested_publication_source = (
                    "llm" if normalized_published_date else "undated_fallback"
                )
            normalized["published_date_source"] = requested_publication_source
        else:
            resolved_publication_date, resolved_publication_date_source = (
                resolve_final_publication_date(normalized, candidate.raw_article)
            )
            normalized["resolved_publication_date"] = resolved_publication_date
            normalized["resolved_publication_date_source"] = resolved_publication_date_source
            normalized["published_date"] = (
                ""
                if resolved_publication_date_source == "undated_fallback"
                else resolved_publication_date.isoformat()
            )
            normalized["published_date_source"] = resolved_publication_date_source
        if not apply_truth_validation:
            return normalized  # type: ignore[return-value]
        return self._enforce_competitor_region_truth(normalized, candidate=candidate)

    def _enforce_competitor_region_truth(
        self,
        alert: dict[str, Any],
        *,
        candidate: Optional[CandidateArticle] = None,
    ) -> dict[str, Any]:
        if self.config is None:
            return alert

        normalized = dict(alert)
        fallback_competitor = candidate.competitor if candidate is not None else ""
        fallback_region = candidate.region if candidate is not None else ""
        fallback_country = candidate.country_hint if candidate is not None else ""

        competitor = normalized.get("competitor", "")
        geo_validation_fallback = False

        if fallback_competitor and competitor != fallback_competitor:
            normalized["competitor"] = fallback_competitor
            competitor = fallback_competitor
            geo_validation_fallback = True

        resolved_region, region_fallback, region_source_hint = self._resolve_safe_region(
            competitor=competitor,
            llm_region=normalized.get("region", ""),
            llm_country=normalized.get("country", ""),
            fallback_region=fallback_region,
            fallback_country=fallback_country,
        )
        normalized["region"] = resolved_region
        geo_validation_fallback = geo_validation_fallback or region_fallback

        country_value, country_source, country_fallback = self._resolve_safe_country(
            llm_country=normalized.get("country", ""),
            region=normalized.get("region", ""),
            fallback_region=fallback_region,
            fallback_country=fallback_country,
        )
        normalized["country"] = country_value
        geo_validation_fallback = geo_validation_fallback or country_fallback
        normalized["competitor_source"] = self._resolve_identity_source(
            final_value=normalized.get("competitor", ""),
            llm_value=alert.get("competitor", ""),
            pipeline_value=fallback_competitor,
        )
        normalized["region_source"] = region_source_hint or self._resolve_identity_source(
            final_value=normalized.get("region", ""),
            llm_value=alert.get("region", ""),
            pipeline_value=fallback_region,
        )
        normalized["country_source"] = country_source
        normalized["geo_validation_fallback"] = geo_validation_fallback

        return normalized

    def _resolve_safe_region(
        self,
        *,
        competitor: str,
        llm_region: str,
        llm_country: str,
        fallback_region: str,
        fallback_country: str,
    ) -> tuple[str, bool, Optional[str]]:
        if self.config is None:
            cleaned_region = self._clean_text(llm_region)
            return cleaned_region, False, None

        cleaned_llm_region = self._clean_text(llm_region)
        cleaned_fallback_region = self._clean_text(fallback_region)
        allowed_regions = self.config.region_for_competitor(competitor)

        if cleaned_fallback_region:
            region_changed = cleaned_llm_region not in {"", cleaned_fallback_region}
            return cleaned_fallback_region, region_changed, "pipeline"

        if not allowed_regions:
            return "", bool(cleaned_llm_region), None

        if len(allowed_regions) == 1:
            only_region = allowed_regions[0]
            if not cleaned_llm_region:
                return only_region, False, None
            return only_region, cleaned_llm_region != only_region, None

        country_regions = self._regions_matching_country(
            fallback_country or llm_country,
            allowed_regions=allowed_regions,
        )
        if len(country_regions) == 1:
            resolved_region = country_regions[0]
            if cleaned_llm_region and cleaned_llm_region != resolved_region:
                return resolved_region, True, "geo_country_override"
            return resolved_region, False, "geo_country_override"

        if cleaned_llm_region and cleaned_llm_region not in allowed_regions:
            return "", True, None
        if cleaned_llm_region:
            return "", True, None
        return "", False, None

    def _resolve_safe_country(
        self,
        *,
        llm_country: str,
        region: str,
        fallback_region: str,
        fallback_country: str,
    ) -> tuple[str, str, bool]:
        if self.config is None:
            cleaned_country = self._clean_text(llm_country)
            return cleaned_country, ("llm" if cleaned_country else "empty"), False

        cleaned_country = self._clean_text(llm_country)
        cleaned_fallback_country = self._clean_text(fallback_country)
        cleaned_region = self._clean_text(region)

        if cleaned_fallback_country:
            if not cleaned_country:
                return cleaned_fallback_country, "pipeline", False
            if cleaned_region == fallback_region and cleaned_country != cleaned_fallback_country:
                return cleaned_fallback_country, "pipeline", True
            return cleaned_fallback_country, "pipeline", cleaned_country != cleaned_fallback_country

        if not cleaned_country or not cleaned_region or cleaned_region not in self.config.regions:
            return "", "empty", bool(cleaned_country)

        allowed_geo_terms = self._allowed_country_terms_for_region(cleaned_region)
        normalized_country_key = self._normalize_country_key(cleaned_country)
        if normalized_country_key in allowed_geo_terms:
            return allowed_geo_terms[normalized_country_key], "llm", False
        return "", "empty", True

    @staticmethod
    def _resolve_identity_source(
        *,
        final_value: str,
        llm_value: str,
        pipeline_value: str,
    ) -> str:
        cleaned_final = CompetitorAlertAnalyzer._clean_text(final_value)
        cleaned_llm = CompetitorAlertAnalyzer._clean_text(llm_value)
        cleaned_pipeline = CompetitorAlertAnalyzer._clean_text(pipeline_value)
        if cleaned_pipeline and cleaned_final == cleaned_pipeline:
            return "pipeline"
        if cleaned_llm and cleaned_final == cleaned_llm:
            return "llm"
        return "empty"

    def _allowed_country_terms_for_region(self, region: str) -> dict[str, str]:
        allowed_terms: dict[str, str] = {}
        for term in self.config.regions[region].country_validation_terms:
            cleaned_term = self._clean_text(term)
            if not cleaned_term:
                continue
            normalized_key = self._normalize_country_key(cleaned_term)
            allowed_terms.setdefault(normalized_key, cleaned_term)
        return allowed_terms

    def _regions_matching_country(
        self,
        country: str,
        *,
        allowed_regions: Sequence[str],
    ) -> tuple[str, ...]:
        if self.config is None:
            return ()

        cleaned_country = self._clean_text(country)
        if not cleaned_country:
            return ()

        normalized_country_key = self._normalize_country_key(cleaned_country)
        matching_regions = []
        for region in allowed_regions:
            allowed_terms = self._allowed_country_terms_for_region(region)
            if normalized_country_key in allowed_terms:
                matching_regions.append(region)
        return tuple(matching_regions)

    @staticmethod
    def _normalize_country_key(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
        return re.sub(
            r"\s+",
            " ",
            COUNTRY_ALIAS_MAP.get(cleaned, cleaned).casefold(),
        ).strip()

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _normalize_published_date(value: Any) -> str:
        if not value:
            return ""

        candidate = str(value).strip()
        match = re.search(r"\d{4}-\d{2}-\d{2}", candidate)
        if match:
            return match.group(0)

        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return ""
