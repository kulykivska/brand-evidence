"""Loader for config/sources.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_PATH = Path(__file__).with_name("sources.yaml")


class SourceToggle(BaseModel):
    enabled: bool = True


class SourcesConfig(BaseModel):
    sources: dict[str, SourceToggle] = Field(default_factory=dict)
    terms: list[str] = Field(default_factory=list)
    skip_capture_hosts: list[str] = Field(default_factory=list)

    def is_enabled(self, name: str) -> bool:
        toggle = self.sources.get(name)
        return toggle.enabled if toggle else False


def load_sources_config(path: Path | None = None) -> SourcesConfig:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    return SourcesConfig.model_validate(raw)
