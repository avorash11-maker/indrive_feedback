"""Competitor tracker package."""

from .environment import bootstrap_env

bootstrap_env()

from .config import TrackerConfig, TrackerRuntimeConfig
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
