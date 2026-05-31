"""Agent-role contracts and orchestration seams for competitor tracker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence

from .config import TrackerConfig
from .models import AlertSchema, ArticleContext, CandidateArticle


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

PRODUCT_STRATEGIST_RESERVED_FIELDS: tuple[str, ...] = (
    "strategic_take",
    "follow_up_question",
    "watchouts",
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
            summary="Reserved seam for product and strategic interpretation.",
            execution_layer="advisory_noop",
            responsibilities=(
                "Future layer for product and market-structure interpretation.",
                "May add optional strategy notes without mutating deterministic truth fields.",
            ),
            pipeline_owned_fields=shared_pipeline_fields,
            agent_owned_fields=PRODUCT_STRATEGIST_RESERVED_FIELDS,
            notes=(
                "Current MVP wires the role boundary but keeps runtime behavior as a no-op for safety.",
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
    """Safe no-op strategist seam reserved for future agent logic."""

    role_name: AgentRoleName = "product_strategist"

    def review_alert(
        self,
        alert_schema: AlertSchema,
        *,
        candidate: CandidateArticle,
        article_context: Optional[ArticleContext] = None,
    ) -> AlertSchema:
        return dict(alert_schema)


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
                self._strategist.role_name,
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
