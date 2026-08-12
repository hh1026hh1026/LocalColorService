"""Common provider and candidate contracts for AI and deterministic Looks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ProviderCapabilities(BaseModel):
    provider: str
    supports_text: bool = False
    supports_reference: bool = False
    direct_lut_output: bool = True
    requires_lut_fitting: bool = False
    recommended_scope: str = "scene_group"
    experimental: bool = False
    available: bool = True
    detail: str = ""


class CandidateScore(BaseModel):
    technical_safety: float = Field(default=1.0, ge=0.0, le=1.0)
    continuity: float = Field(default=1.0, ge=0.0, le=1.0)
    reference_match: float = Field(default=0.0, ge=0.0, le=1.0)
    total: float = Field(default=0.0, ge=0.0, le=1.0)


class LookCandidate(BaseModel):
    id: str
    label: str
    provider: str
    lut_path: str
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    score: CandidateScore = Field(default_factory=CandidateScore)
    passed: bool = True
    warnings: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class LookProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def generate_candidates(self, context: dict[str, Any], count: int = 3) -> list[LookCandidate]:
        raise NotImplementedError

