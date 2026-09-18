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


def resolve_sources_path(configured: Path | None, env_file: Path | None) -> Path:
    """BE_SOURCES_FILE, else sources.yaml beside the project's .env, else the
    packaged defaults. A configured file that is missing is an error, not a
    silent fall back to another project's terms."""
    if configured is not None:
        if not configured.exists():
            raise FileNotFoundError(f"BE_SOURCES_FILE={configured} does not exist")
        return configured
    if env_file is not None:
        beside = env_file.parent / "sources.yaml"
        if beside.exists():
            return beside
    return DEFAULT_PATH


def load_sources_config(path: Path | None = None) -> SourcesConfig:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    return SourcesConfig.model_validate(raw)
