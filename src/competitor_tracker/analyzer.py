"""Analysis layer for competitor mentions."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, List, Optional, Sequence

import openai

from .config import TrackerConfig
from .formatter import format_alert_card
from .models import ArticleContext, CandidateArticle, RawArticle


logger = logging.getLogger(__name__)


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
        selected_regions = tuple(regions or self.config.regions.keys())
        competitor = self._detect_competitor(article, text_blob, selected_regions)
        if not competitor:
            return None

        topic_group, matched_keywords = self._detect_topic(text_blob)
        if not topic_group:
            return None

        region_key, country_hint = self._detect_region(article, text_blob, selected_regions)
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
            return article.region, region_config.geo_terms[0] if region_config.geo_terms else None

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
    LLM_REFERENCE_DATE = "2026-05-21"

    def __init__(
        self,
        use_llm: bool = True,
        model: Optional[str] = None,
    ) -> None:
        self.use_llm = use_llm and bool(os.getenv("OPENAI_API_KEY"))
        self.client = None
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

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

    def analyze_candidate(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> dict[str, Any]:
        """Return normalized competitor alert schema for one candidate."""
        fallback = self._fallback_alert(candidate)
        if not self.use_llm or self.client is None:
            return fallback

        llm_result = self._llm_alert_analysis(candidate, article_context=article_context)
        if not llm_result:
            return fallback
        metadata_published_at = (
            article_context.published_at if article_context is not None else None
        ) or candidate.raw_article.published_at
        merged = {**fallback, **llm_result}
        normalized_metadata_published_date = self._normalize_published_date(
            metadata_published_at
        )
        if normalized_metadata_published_date:
            merged["published_date"] = normalized_metadata_published_date
            merged["published_date_source"] = "metadata"
        merged["published_date_source"] = self._infer_published_date_source(
            published_date=merged.get("published_date") or merged.get("published_at"),
            metadata_published_at=metadata_published_at,
            requested_source=merged.get("published_date_source"),
        )
        return self._normalize_alert(merged)

    def analyze_candidates(
        self,
        candidates: Iterable[CandidateArticle],
    ) -> list[dict[str, Any]]:
        """Return alert schema objects for multiple candidates."""
        return [self.analyze_candidate(candidate) for candidate in candidates]

    def format_alert(self, alert: dict[str, Any], *, source_url: str = "") -> str:
        """Render a readable alert text block from the analyzer schema."""
        return format_alert_card(alert, source_url=source_url)

    def _llm_alert_analysis(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> Optional[dict[str, Any]]:
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
- If the article metadata field `published_at` is None or missing, carefully inspect the article body for publication dates or temporal markers such as "last Thursday", "yesterday", or "two days ago". Resolve them relative to today's date: 2026-05-21. Write the final date into `published_date` using YYYY-MM-DD format.
- Think like a high-level international marketer, not a generic summarizer.
- Recommended actions must be concrete and useful for brand, growth, communications, partnerships, creative strategy, regional GTM, or driver/rider messaging.
- Recommended actions must be applicable to inDrive, not generic advice for "a company".
- Avoid vague advice like "monitor this" unless no stronger action is justified by the article.
- Keep wording concise, executive-friendly, and actionable.
- Return only strict JSON without markdown.
- `priority` must be one of LOW, MEDIUM, HIGH.
- `confidence` must be a number from 0 to 1.
- `published_date_source` must be one of metadata, llm, unknown.

Return this schema:
{
  "competitor": "string",
  "region": "string",
  "country": "string",
  "topic": "string",
  "priority": "LOW|MEDIUM|HIGH",
  "published_date": "YYYY-MM-DD",
  "published_date_source": "metadata|llm|unknown",
  "what_happened": "string",
  "why_it_matters": "string",
  "potential_impact": "string",
  "recommended_action": "string",
  "confidence": 0.0
}"""

        user_prompt = """Candidate metadata:
{candidate_payload}

Article title: {title}
Article snippet: {snippet}
Article body: {article_body}
Article published_at metadata: {published_at}
Source query: {query}
Source URL: {url}

Today's date for reference: 2026-05-21. Если точной даты нет, используй контекст текста для вычисления.

Write the alert for the inDrive Marcom / growth team.

Focus especially on:
- competitor strategy
- market narrative
- campaign or messaging angle
- likely effect on driver/rider perception
- what inDrive can do better or differently

When writing:
- "what_happened" should state the event clearly and concretely
- "why_it_matters" should explain the strategic meaning, not just restate the article
- "potential_impact" should focus on likely effects on trust, perception, positioning, growth, or supply-demand narrative
- "recommended_action" should give specific next moves for inDrive, ideally in messaging, creative, partnerships, GTM, driver/rider value proposition, or local communications"""

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
            return self._normalize_alert(
                {
                    "competitor": candidate.competitor,
                    "region": candidate.region or "",
                    "country": candidate.country_hint or "",
                    "topic": candidate.topic_group.replace("_", " "),
                    "priority": self._priority_from_score(candidate.score),
                    "published_date": self._normalize_published_date(
                        context.published_at or candidate.raw_article.published_at
                    ),
                    "published_date_source": self._infer_published_date_source(
                        published_date=context.published_at or candidate.raw_article.published_at,
                        metadata_published_at=context.published_at or candidate.raw_article.published_at,
                    ),
                    "what_happened": candidate.summary or candidate.title,
                    "why_it_matters": self.INSUFFICIENT_SOURCE_DATA_MESSAGE,
                    "potential_impact": "Potential impact remains unclear and needs validation.",
                    "recommended_action": self.INSUFFICIENT_SOURCE_DATA_MESSAGE,
                    "confidence": 0.0,
                }
            )
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
            return self._normalize_alert(json.loads(content))
        except Exception as exc:
            logger.warning(
                "Competitor alert LLM analysis failed; falling back to rule-based alert. title=%r url=%r error=%s",
                candidate.title[:120],
                candidate.url,
                exc,
                exc_info=True,
            )
            return None

    def _fallback_alert(self, candidate: CandidateArticle) -> dict[str, Any]:
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
        return self._normalize_alert(
            {
                "competitor": candidate.competitor,
                "region": region,
                "country": country,
                "topic": topic,
                "priority": priority,
                "published_date": self._normalize_published_date(candidate.published_date),
                "published_date_source": self._infer_published_date_source(
                    published_date=candidate.published_date,
                    metadata_published_at=candidate.raw_article.published_at,
                ),
                "what_happened": what_happened,
                "why_it_matters": why_it_matters,
                "potential_impact": potential_impact,
                "recommended_action": recommended_action,
                "confidence": confidence,
            }
        )

    @staticmethod
    def _priority_from_score(score: int) -> str:
        if score >= 9:
            return "HIGH"
        if score >= 6:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _normalize_alert(alert: dict[str, Any]) -> dict[str, Any]:
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
        normalized["published_date"] = CompetitorAlertAnalyzer._normalize_published_date(
            normalized.get("published_date") or normalized.get("published_at")
        )
        published_date_source = str(
            normalized.get("published_date_source") or "unknown"
        ).strip().lower()
        if published_date_source not in {"metadata", "llm", "unknown"}:
            published_date_source = "unknown"
        if not normalized["published_date"]:
            published_date_source = "unknown"
        normalized["published_date_source"] = published_date_source
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
        return normalized

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

    @staticmethod
    def _infer_published_date_source(
        *,
        published_date: Any,
        metadata_published_at: Any,
        requested_source: Any = None,
    ) -> str:
        normalized_date = CompetitorAlertAnalyzer._normalize_published_date(published_date)
        if not normalized_date:
            return "unknown"

        normalized_metadata = CompetitorAlertAnalyzer._normalize_published_date(
            metadata_published_at
        )
        requested = str(requested_source or "").strip().lower()
        if requested in {"metadata", "llm"}:
            return requested
        if normalized_metadata and normalized_date == normalized_metadata:
            return "metadata"
        return "llm"
