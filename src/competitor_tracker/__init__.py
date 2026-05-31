"""Competitor tracker package."""

from .environment import bootstrap_env

bootstrap_env()

from .config import TrackerConfig, TrackerRuntimeConfig
from .agent_contracts import AGENT_CONTRACT_VERSION, AgentRoleContract, default_agent_role_contracts
from .models import (
    Alert,
    AlertSchema,
    ArticleContext,
    CandidateArticle,
    Competitor,
    CompetitorDigest,
    DeliveryRecord,
    RawArticle,
    ResolvedPublicationDateSource,
    RunSummary,
)

__all__ = [
    "TrackerConfig",
    "TrackerRuntimeConfig",
    "AGENT_CONTRACT_VERSION",
    "AgentRoleContract",
    "default_agent_role_contracts",
    "RawArticle",
    "ArticleContext",
    "CandidateArticle",
    "Alert",
    "AlertSchema",
    "ResolvedPublicationDateSource",
    "RunSummary",
    "DeliveryRecord",
    "Competitor",
    "CompetitorDigest",
]
