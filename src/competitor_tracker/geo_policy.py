"""Geo-policy helpers for region detection and non-target geography filtering."""

from __future__ import annotations

import re
from typing import Optional, Sequence

from .config import TrackerConfig


class GeoPolicy:
    """Encapsulates geo matching rules used by the analyzer prefilter."""

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config

    @staticmethod
    def contains_geo_term(text_blob: str, geo_term: str) -> bool:
        normalized_term = geo_term.casefold().strip()
        if not normalized_term:
            return False
        pattern = r"(?<!\w)" + re.escape(normalized_term).replace(r"\ ", r"\s+") + r"(?!\w)"
        return re.search(pattern, text_blob) is not None

    def ignored_geo_decision(
        self,
        *,
        geo_text_blob: str,
        selected_regions: Sequence[str],
    ) -> Optional[dict[str, str]]:
        if not self.config.ignored_geo_terms:
            return None

        ignored_markers = tuple(
            geo_term
            for geo_term in self.config.ignored_geo_terms
            if self.contains_geo_term(geo_text_blob, geo_term)
        )
        if not ignored_markers:
            return None

        has_target_confirmation = any(
            self.contains_geo_term(geo_text_blob, geo_term)
            for geo_term in self.target_geo_terms(selected_regions)
        )
        if has_target_confirmation:
            return None

        return {
            "ignored_geo_terms": " | ".join(ignored_markers),
            "selected_regions": " | ".join(selected_regions),
        }

    def detect_region(
        self,
        *,
        article_region: Optional[str],
        geo_text_blob: str,
        regions: Sequence[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if article_region and article_region in self.config.regions:
            region_config = self.config.regions[article_region]
            for geo_term in region_config.geo_terms:
                if self.contains_geo_term(geo_text_blob, geo_term):
                    return article_region, geo_term
            return article_region, None

        for region in regions:
            region_config = self.config.regions[region]
            for geo_term in region_config.geo_terms:
                if self.contains_geo_term(geo_text_blob, geo_term):
                    return region, geo_term
        return None, None

    def region_markers(
        self,
        *,
        article_region: Optional[str],
        geo_text_blob: str,
        selected_regions: Sequence[str],
    ) -> tuple[str, ...]:
        region_markers = []
        if article_region and article_region in self.config.regions:
            region_markers.append(article_region)
        for region in selected_regions:
            region_config = self.config.regions[region]
            for geo_term in region_config.geo_terms:
                if self.contains_geo_term(geo_text_blob, geo_term):
                    region_markers.append(region)
                    break
        return tuple(dict.fromkeys(region_markers))

    def target_geo_terms(self, selected_regions: Sequence[str]) -> tuple[str, ...]:
        target_geo_terms = []
        for region in selected_regions:
            if region not in self.config.regions:
                continue
            region_config = self.config.regions[region]
            target_geo_terms.append(region_config.label)
            target_geo_terms.extend(region_config.geo_terms)
            target_geo_terms.extend(region_config.country_validation_terms)
        return tuple(dict.fromkeys(target_geo_terms))
