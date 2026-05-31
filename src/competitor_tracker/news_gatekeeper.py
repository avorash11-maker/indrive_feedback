"""Semantic gatekeeper for post-prefilter competitor candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from .analyzer import AnalysisResult
from .config import TrackerConfig
from .models import CandidateArticle, DroppedArticle, RawArticle
from .normalization import extract_domain
from .product_logic import DIGEST_COMPETITOR_ACTION_TERMS, normalize_topic_group_name


@dataclass(frozen=True, slots=True)
class GatekeeperDecision:
    """Canonical accept/reject output for the News Gatekeeper role."""

    accepted: bool
    canonical_topic: str
    relevance_reason: str
    priority_hint: str
    rejection_reason: str = ""


class NewsGatekeeper:
    """Semantic gate that rejects noisy candidates before editorial enrichment."""

    GENERIC_BRAND_NOISE_PATTERNS: tuple[str, ...] = (
        "brand ranking",
        "most valuable brands",
        "best apps of the year",
        "award shortlist",
        "celebrity",
        "influencer",
        "stock price",
        "investor sentiment",
    )
    BROAD_INDUSTRY_FLUFF_PATTERNS: tuple[str, ...] = (
        "market size",
        "market outlook",
        "industry analysis",
        "industry trends",
        "research report",
        "forecast 20",
        "market forecast",
        "global ride hailing market",
    )
    OFF_SCOPE_PATTERNS: tuple[str, ...] = (
        "logo redesign",
        "new logo",
        "banner campaign",
        "billboard creative",
        "social listening dashboard",
        "social scraping",
        "scraped from instagram",
        "scraped from tiktok",
        "visual asset monitoring",
    )
    MENTION_ONLY_PATTERNS: tuple[str, ...] = (
        "mentioned",
        "included in a list",
        "one of the brands",
        "among the brands",
        "alongside uber",
        "alongside grab",
    )
    STRONG_SIGNAL_TERMS: tuple[str, ...] = DIGEST_COMPETITOR_ACTION_TERMS + (
        "driver support",
        "driver benefits",
        "rider trust",
        "safety feature",
        "service expansion",
        "commission cut",
        "promo code",
        "discounted rides",
        "driver recruitment",
        "ugc",
        "creator campaign",
    )
    LOW_SIGNAL_DOMAINS: frozenset[str] = frozenset(
        {
            "openpr.com",
            "openpr",
            "prlog.org",
            "einnews.com",
        }
    )
    ALWAYS_HIGH_PRIORITY_TOPICS: frozenset[str] = frozenset(
        {"market_expansion", "strategic_operations", "pricing_promo"}
    )
    MEDIUM_PRIORITY_TOPICS: frozenset[str] = frozenset(
        {"campaign_launches", "performance_growth", "product_features_innovation"}
    )

    def __init__(self, *, config: TrackerConfig | None = None) -> None:
        self.config = config

    def evaluate(self, candidate: CandidateArticle) -> GatekeeperDecision:
        canonical_topic = normalize_topic_group_name(candidate.topic_group)
        text_blob = self._candidate_text_blob(candidate)
        domain = extract_domain(candidate.url)

        if self._contains_any(text_blob, self.OFF_SCOPE_PATTERNS):
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Text points to visual or social monitoring outside the MVP scope.",
                priority_hint="LOW",
                rejection_reason="off_scope_material",
            )

        if domain in self.LOW_SIGNAL_DOMAINS and candidate.score < 8:
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Low-signal source without enough concrete competitive action.",
                priority_hint="LOW",
                rejection_reason="generic_brand_noise",
            )

        if self._contains_any(text_blob, self.GENERIC_BRAND_NOISE_PATTERNS):
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Brand mention is generic and not tied to a meaningful market move.",
                priority_hint="LOW",
                rejection_reason="generic_brand_noise",
            )

        if canonical_topic == "core_industry_terms" and self._contains_any(
            text_blob, self.BROAD_INDUSTRY_FLUFF_PATTERNS
        ):
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Industry article is broad market fluff without actionable competitor movement.",
                priority_hint="LOW",
                rejection_reason="broad_industry_fluff",
            )

        if self._is_mention_only_story(candidate, text_blob=text_blob, canonical_topic=canonical_topic):
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Competitor is only mentioned, without a clear move relevant to digest decisions.",
                priority_hint="LOW",
                rejection_reason="mention_only_story",
            )

        if canonical_topic == "core_industry_terms" and not self._has_concrete_action_signal(
            candidate,
            text_blob=text_blob,
        ):
            return GatekeeperDecision(
                accepted=False,
                canonical_topic=canonical_topic,
                relevance_reason="Core industry term coverage lacks a concrete competitor action signal.",
                priority_hint="LOW",
                rejection_reason="broad_industry_fluff",
            )

        priority_hint = self._priority_hint(candidate, canonical_topic=canonical_topic, text_blob=text_blob)
        return GatekeeperDecision(
            accepted=True,
            canonical_topic=canonical_topic,
            relevance_reason=self._acceptance_reason(
                candidate,
                canonical_topic=canonical_topic,
                text_blob=text_blob,
            ),
            priority_hint=priority_hint,
        )

    def filter_analysis(self, analysis: AnalysisResult) -> AnalysisResult:
        accepted_candidates: list[CandidateArticle] = []
        dropped_articles = list(analysis.dropped_articles)
        dropped_count = analysis.dropped_count

        for candidate in analysis.candidates:
            decision = self.evaluate(candidate)
            accepted_candidate = self._apply_decision(candidate, decision)
            if decision.accepted:
                accepted_candidates.append(accepted_candidate)
                continue
            dropped_count += 1
            dropped_articles.append(
                DroppedArticle(
                    url=candidate.url,
                    title=candidate.title,
                    reason=decision.rejection_reason or "news_gatekeeper_reject",
                    details={
                        "agent_role": "news_gatekeeper",
                        "canonical_topic": decision.canonical_topic,
                        "relevance_reason": decision.relevance_reason,
                        "priority_hint": decision.priority_hint,
                    },
                )
            )

        return AnalysisResult(
            candidates=accepted_candidates,
            dropped_count=dropped_count,
            dropped_articles=dropped_articles,
        )

    def filter_candidates(
        self,
        candidates: Iterable[CandidateArticle],
    ) -> tuple[list[CandidateArticle], list[DroppedArticle]]:
        kept: list[CandidateArticle] = []
        dropped: list[DroppedArticle] = []
        for candidate in candidates:
            decision = self.evaluate(candidate)
            if decision.accepted:
                kept.append(self._apply_decision(candidate, decision))
            else:
                dropped.append(
                    DroppedArticle(
                        url=candidate.url,
                        title=candidate.title,
                        reason=decision.rejection_reason or "news_gatekeeper_reject",
                        details={
                            "agent_role": "news_gatekeeper",
                            "canonical_topic": decision.canonical_topic,
                            "relevance_reason": decision.relevance_reason,
                            "priority_hint": decision.priority_hint,
                        },
                    )
                )
        return kept, dropped

    def _apply_decision(
        self,
        candidate: CandidateArticle,
        decision: GatekeeperDecision,
    ) -> CandidateArticle:
        metadata = dict(candidate.raw_article.metadata)
        metadata.update(
            {
                "news_gatekeeper_accept": decision.accepted,
                "news_gatekeeper_canonical_topic": decision.canonical_topic,
                "news_gatekeeper_relevance_reason": decision.relevance_reason,
                "news_gatekeeper_priority_hint": decision.priority_hint,
                "news_gatekeeper_rejection_reason": decision.rejection_reason,
            }
        )
        updated_raw_article = replace(candidate.raw_article, metadata=metadata)
        return replace(candidate, raw_article=updated_raw_article)

    def _is_mention_only_story(
        self,
        candidate: CandidateArticle,
        *,
        text_blob: str,
        canonical_topic: str,
    ) -> bool:
        if self._contains_any(text_blob, self.MENTION_ONLY_PATTERNS):
            return True
        if canonical_topic == "campaign_launches" and candidate.score <= 5 and candidate.country_hint is None:
            return not self._has_concrete_action_signal(candidate, text_blob=text_blob)
        return False

    def _has_concrete_action_signal(
        self,
        candidate: CandidateArticle,
        *,
        text_blob: str,
    ) -> bool:
        if any(term in text_blob for term in self.STRONG_SIGNAL_TERMS):
            return True
        if candidate.country_hint and candidate.score >= 6:
            return True
        return bool(re.search(r"\blaunch|expand|partner|promo|discount|feature|driver|rider|commission\b", text_blob))

    def _priority_hint(
        self,
        candidate: CandidateArticle,
        *,
        canonical_topic: str,
        text_blob: str,
    ) -> str:
        if candidate.score >= 9 or canonical_topic in self.ALWAYS_HIGH_PRIORITY_TOPICS:
            return "HIGH"
        if canonical_topic in self.MEDIUM_PRIORITY_TOPICS:
            return "HIGH" if "strategic partnership" in text_blob and candidate.score >= 8 else "MEDIUM"
        return "MEDIUM" if self._has_concrete_action_signal(candidate, text_blob=text_blob) else "LOW"

    def _acceptance_reason(
        self,
        candidate: CandidateArticle,
        *,
        canonical_topic: str,
        text_blob: str,
    ) -> str:
        if canonical_topic == "market_expansion":
            return "Concrete market expansion or launch signal with clear business relevance."
        if canonical_topic == "pricing_promo":
            return "Pricing or promo move can directly affect competitive positioning and response."
        if canonical_topic == "campaign_launches":
            if any(term in text_blob for term in ("ugc", "creator campaign", "reels", "tiktok strategy")):
                return "Campaign signal is accepted as text-described marketing activity, not social scraping."
            return "Campaign or messaging move is concrete enough for digest review."
        if canonical_topic == "strategic_operations":
            return "Operational or market-entry move is concrete and strategically relevant."
        if canonical_topic == "performance_growth":
            return "Growth or incentives signal may affect supply, demand, or narrative."
        if canonical_topic == "product_features_innovation":
            return "Product or service innovation signal is concrete enough for competitive tracking."
        return "Competitor action is concrete enough to pass the semantic relevance gate."

    @staticmethod
    def _candidate_text_blob(candidate: CandidateArticle) -> str:
        return " ".join(
            part.casefold()
            for part in (
                candidate.title,
                candidate.summary,
                candidate.raw_article.title,
                candidate.raw_article.snippet,
                candidate.raw_article.source,
            )
            if part
        )

    @staticmethod
    def _contains_any(text_blob: str, patterns: Sequence[str]) -> bool:
        return any(pattern in text_blob for pattern in patterns)
