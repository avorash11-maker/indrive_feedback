"""Agent-role contracts and orchestration seams for competitor tracker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence

from .config import TrackerConfig
from .models import AlertSchema, ArticleContext, CandidateArticle
from .product_logic import normalize_topic_group_name


AGENT_CONTRACT_VERSION = "2026-05-agent-foundation-v1"

AgentRoleName = Literal[
    "news_gatekeeper",
    "indrive_marcom_editor",
    "product_strategist",
]


PIPELINE_OWNED_CANDIDATE_FIELDS: tuple[str, ...] = (
    "raw_article",
    "competitor",
    "topic_group",
    "score",
    "matched_keywords",
    "summary",
    "region",
    "country_hint",
    "language_hint",
    "reasons",
)

PIPELINE_OWNED_ALERT_FIELDS: tuple[str, ...] = (
    "competitor",
    "competitor_source",
    "region",
    "region_source",
    "country",
    "country_source",
    "resolved_publication_date",
    "resolved_publication_date_source",
    "published_date",
    "published_date_source",
    "geo_validation_fallback",
)

AGENT_NARRATIVE_FIELDS: tuple[str, ...] = (
    "event",
    "what_happened",
    "why_it_matters",
    "potential_impact",
    "recommended_action",
    "confidence",
)

AGENT_PROPOSAL_FIELDS: tuple[str, ...] = (
    "country",
    "published_date",
    "published_date_source",
)

NEWS_GATEKEEPER_FIELDS: tuple[str, ...] = (
    "news_gatekeeper_accept",
    "news_gatekeeper_canonical_topic",
    "news_gatekeeper_relevance_reason",
    "news_gatekeeper_priority_hint",
    "news_gatekeeper_rejection_reason",
)

PRODUCT_STRATEGIST_FIELDS: tuple[str, ...] = (
    "product_take",
    "product_risk",
    "product_follow_up",
    "product_strategist_invoked",
    "product_strategist_trigger",
)

DETERMINISTIC_FOUNDATION_STAGES: tuple[str, ...] = (
    "providers",
    "normalization",
    "dedup",
    "geo_validation",
    "competitor_region_validation",
    "suppression_history",
    "delivery",
)


@dataclass(frozen=True, slots=True)
class AgentRoleContract:
    """Explicit ownership and execution boundary for one agent role."""

    role: AgentRoleName
    summary: str
    execution_layer: Literal["deterministic_guardrail", "agent_runtime", "advisory_noop"]
    responsibilities: tuple[str, ...]
    pipeline_owned_fields: tuple[str, ...]
    agent_owned_fields: tuple[str, ...]
    agent_proposed_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def default_agent_role_contracts() -> dict[AgentRoleName, AgentRoleContract]:
    """Return the canonical agent-role contracts for the MVP foundation."""

    shared_pipeline_fields = PIPELINE_OWNED_CANDIDATE_FIELDS + PIPELINE_OWNED_ALERT_FIELDS
    return {
        "news_gatekeeper": AgentRoleContract(
            role="news_gatekeeper",
            summary="Semantic gate over what is worth downstream agent attention.",
            execution_layer="deterministic_guardrail",
            responsibilities=(
                "Operate above the cheap prefilter without replacing it.",
                "Respect deterministic provider, normalization, geo, and history decisions.",
                "Stay text-only; do not imply social scraping, logo detection, or visual monitoring.",
                "Return accept/reject plus canonical relevance classification before editorial enrichment.",
            ),
            pipeline_owned_fields=shared_pipeline_fields,
            agent_owned_fields=NEWS_GATEKEEPER_FIELDS,
            agent_proposed_fields=("priority", "topic_group"),
            notes=(
                "Current MVP uses a rule-based semantic gatekeeper over deterministic candidates.",
            ),
        ),
        "indrive_marcom_editor": AgentRoleContract(
            role="indrive_marcom_editor",
            summary="Narrative and action editor for final inDrive-facing alert copy.",
            execution_layer="agent_runtime",
            responsibilities=(
                "Rewrite the signal into concise Marcom-ready language.",
                "Explain why the move matters for narrative, perception, growth, and GTM.",
                "Recommend concrete inDrive response options without changing pipeline truth.",
            ),
            pipeline_owned_fields=shared_pipeline_fields,
            agent_owned_fields=AGENT_NARRATIVE_FIELDS,
            agent_proposed_fields=AGENT_PROPOSAL_FIELDS,
            notes=(
                "Competitor, region, and final resolved publication date stay pipeline-validated.",
            ),
        ),
        "product_strategist": AgentRoleContract(
            role="product_strategist",
            summary="Conditional product interpretation layer for product-sensitive alerts.",
            execution_layer="agent_runtime",
            responsibilities=(
                "Run only for product-sensitive alerts after News Gatekeeper and Marcom Editor.",
                "Add optional product take, product risk, and product follow-up guidance.",
                "Preserve pipeline-owned competitor, region, country, and topic truth.",
            ),
            pipeline_owned_fields=shared_pipeline_fields,
            agent_owned_fields=PRODUCT_STRATEGIST_FIELDS,
            notes=(
                "Current MVP keeps this layer cheap by default through conditional rule-based invocation.",
            ),
        ),
    }


class CandidateAlertAnalyzer(Protocol):
    """Minimal analyzer interface needed by the Marcom editor seam."""

    use_llm: bool

    def analyze_candidate(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext] = None,
    ) -> AlertSchema:
        """Return one alert schema for a candidate."""


AnalyzerFactory = Callable[[bool, Optional[TrackerConfig]], CandidateAlertAnalyzer]


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """Context passed through the agent-led alert seam."""

    ranking_index: int
    llm_limit: int
    config: Optional[TrackerConfig] = None

    @property
    def use_llm_for_item(self) -> bool:
        return self.ranking_index < self.llm_limit


class ProductStrategistRole:
    """Conditional product-layer strategist for product-sensitive alerts."""

    role_name: AgentRoleName = "product_strategist"
    ALWAYS_TRIGGER_TOPICS = frozenset(
        {
            "product_features_innovation",
            "strategic_operations",
            "pricing_promo",
        }
    )
    CONDITIONAL_TRIGGER_TOPIC = "performance_growth"
    PERFORMANCE_GROWTH_TRIGGER_TERMS: tuple[str, ...] = (
        "commission",
        "driver incentive",
        "driver incentives",
        "driver recruitment",
        "feature",
        "subscription",
        "pricing",
        "fare",
        "discount",
        "service",
        "airport",
        "operations",
        "supply",
        "dispatch",
        "earnings",
        "marketplace",
    )

    def review_alert(
        self,
        alert_schema: AlertSchema,
        *,
        candidate: CandidateArticle,
        article_context: Optional[ArticleContext] = None,
    ) -> AlertSchema:
        if not self.should_invoke(candidate, alert_schema=alert_schema):
            return dict(alert_schema)

        product_block = self._build_product_block(
            candidate,
            alert_schema=alert_schema,
            article_context=article_context,
        )
        return {
            **dict(alert_schema),
            **product_block,
        }

    def should_invoke(
        self,
        candidate: CandidateArticle,
        *,
        alert_schema: Mapping[str, Any],
    ) -> bool:
        topic_group = normalize_topic_group_name(candidate.topic_group)
        if topic_group in self.ALWAYS_TRIGGER_TOPICS:
            return True
        if topic_group != self.CONDITIONAL_TRIGGER_TOPIC:
            return False
        text_blob = self._text_blob(candidate, alert_schema=alert_schema)
        return any(term in text_blob for term in self.PERFORMANCE_GROWTH_TRIGGER_TERMS)

    def _build_product_block(
        self,
        candidate: CandidateArticle,
        *,
        alert_schema: Mapping[str, Any],
        article_context: Optional[ArticleContext] = None,
    ) -> dict[str, Any]:
        topic_group = normalize_topic_group_name(candidate.topic_group)
        market = str(candidate.country_hint or alert_schema.get("country") or alert_schema.get("region") or "the market").strip()
        trigger = topic_group
        text_blob = self._text_blob(candidate, alert_schema=alert_schema)

        if topic_group == "pricing_promo":
            return {
                "product_take": (
                    f"This pricing move may reshape perceived rider value and driver economics in {market}."
                ),
                "product_risk": (
                    "Risk of value-positioning pressure if the competitor sets a stronger local price anchor or incentive expectation."
                ),
                "product_follow_up": (
                    "Review local price architecture, promo guardrails, and whether inDrive needs a tighter value-proposition response."
                ),
                "product_strategist_invoked": True,
                "product_strategist_trigger": trigger,
            }
        if topic_group == "product_features_innovation":
            return {
                "product_take": (
                    f"This feature or service move may shift parity expectations for riders or drivers in {market}."
                ),
                "product_risk": (
                    "Risk of feature-gap perception if the competitor turns this capability into a visible user promise."
                ),
                "product_follow_up": (
                    "Check parity relevance, rollout speed, and whether the response should be product, GTM, or messaging-led."
                ),
                "product_strategist_invoked": True,
                "product_strategist_trigger": trigger,
            }
        if topic_group == "strategic_operations":
            return {
                "product_take": (
                    f"This operational move may improve service reliability, supply quality, or route-to-market execution in {market}."
                ),
                "product_risk": (
                    "Risk of stronger local execution advantage if partnerships, market-entry mechanics, or service operations improve faster than inDrive's response."
                ),
                "product_follow_up": (
                    "Validate operational dependencies, market-entry friction, and whether inDrive needs a product or ops response beyond communications."
                ),
                "product_strategist_invoked": True,
                "product_strategist_trigger": trigger,
            }
        if topic_group == "performance_growth":
            if "commission" in text_blob or "fare" in text_blob or "pricing" in text_blob or "discount" in text_blob:
                take = f"This growth signal appears tied to price-value mechanics rather than pure marketing in {market}."
                risk = "Risk of value-proposition pressure if users or drivers start benchmarking on earnings or price architecture."
                follow_up = "Review whether pricing, incentives, or earnings messaging need a product-backed response."
            else:
                take = f"This growth signal appears tied to operational or product mechanics in {market}."
                risk = "Risk of stronger supply reliability or marketplace quality if the underlying mechanism scales locally."
                follow_up = "Check whether the response should focus on ops levers, driver experience, or service design."
            return {
                "product_take": take,
                "product_risk": risk,
                "product_follow_up": follow_up,
                "product_strategist_invoked": True,
                "product_strategist_trigger": trigger,
            }
        return {}

    @staticmethod
    def _text_blob(
        candidate: CandidateArticle,
        *,
        alert_schema: Mapping[str, Any],
    ) -> str:
        return " ".join(
            part.casefold()
            for part in (
                candidate.title,
                candidate.summary,
                candidate.raw_article.snippet,
                str(alert_schema.get("event") or ""),
                str(alert_schema.get("what_happened") or ""),
                str(alert_schema.get("why_it_matters") or ""),
            )
            if part
        )


class AgentRolePipeline:
    """Thin orchestration seam over the existing deterministic pipeline."""

    def __init__(
        self,
        *,
        analyzer_factory: AnalyzerFactory,
        contracts: Optional[Mapping[AgentRoleName, AgentRoleContract]] = None,
        strategist: Optional[ProductStrategistRole] = None,
    ) -> None:
        self._analyzer_factory = analyzer_factory
        self.contracts = dict(contracts or default_agent_role_contracts())
        self._strategist = strategist or ProductStrategistRole()
        self._marcom_analyzers: dict[bool, CandidateAlertAnalyzer] = {}

    def _marcom_analyzer(
        self,
        *,
        use_llm: bool,
        config: Optional[TrackerConfig],
    ) -> CandidateAlertAnalyzer:
        analyzer = self._marcom_analyzers.get(use_llm)
        if analyzer is None:
            analyzer = self._analyzer_factory(use_llm, config)
            self._marcom_analyzers[use_llm] = analyzer
        return analyzer

    def warmup(self, *, llm_enabled: bool, config: Optional[TrackerConfig]) -> None:
        """Preserve historical analyzer initialization order for compatibility."""

        self._marcom_analyzer(use_llm=False, config=config)
        if llm_enabled:
            self._marcom_analyzer(use_llm=True, config=config)

    def build_alert_schema(
        self,
        candidate: CandidateArticle,
        *,
        article_context: Optional[ArticleContext],
        runtime: AgentRuntimeContext,
    ) -> AlertSchema:
        marcom_analyzer = self._marcom_analyzer(
            use_llm=runtime.use_llm_for_item,
            config=runtime.config,
        )
        alert_schema = marcom_analyzer.analyze_candidate(
            candidate,
            article_context=article_context,
        )
        alert_schema = {
            **dict(alert_schema),
            "_candidate_metadata": dict(candidate.raw_article.metadata),
        }
        strategist_invoked = self._strategist.should_invoke(
            candidate,
            alert_schema=alert_schema,
        )
        alert_schema = self._strategist.review_alert(
            alert_schema,
            candidate=candidate,
            article_context=article_context,
        )
        return annotate_agent_metadata(
            alert_schema,
            executed_roles=(
                "news_gatekeeper",
                "indrive_marcom_editor",
                *((self._strategist.role_name,) if strategist_invoked else ()),
            ),
        )


def annotate_agent_metadata(
    alert_schema: Mapping[str, Any],
    *,
    executed_roles: Sequence[AgentRoleName],
) -> AlertSchema:
    """Attach explicit role-boundary metadata without changing alert behavior."""

    annotated = dict(alert_schema)
    annotated["agent_contract_version"] = AGENT_CONTRACT_VERSION
    annotated["agent_roles_available"] = tuple(default_agent_role_contracts().keys())
    annotated["agent_roles_executed"] = tuple(executed_roles)
    annotated["truth_layer_owner"] = "deterministic_pipeline"
    annotated["safety_layer_owner"] = "deterministic_pipeline"
    raw_metadata = alert_schema.get("_candidate_metadata")
    if isinstance(raw_metadata, Mapping):
        for field_name in NEWS_GATEKEEPER_FIELDS:
            if field_name in raw_metadata:
                annotated[field_name] = raw_metadata[field_name]
    annotated.pop("_candidate_metadata", None)
    return annotated  # type: ignore[return-value]
