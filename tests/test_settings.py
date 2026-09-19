from pathlib import Path

import pytest
from pydantic import ValidationError

from brand_evidence.config.settings import Settings
from tests.conftest import make_settings


def test_missing_required_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_path=tmp_path / "x.db")  # type: ignore[call-arg]


def test_csv_parsing(tmp_path: Path) -> None:
    s = make_settings(tmp_path, brand_terms="A, B ,C", google_alerts_feeds="https://a,https://b")
    assert s.brand_terms == ["A", "B", "C"]
    assert s.google_alerts_feeds == ["https://a", "https://b"]


def test_classification_requires_a_provider_and_a_model(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="BE_LLM_PROVIDER and BE_LLM_MODEL"):
        make_settings(tmp_path, enable_classification=True)


def test_classification_requires_a_key_unless_the_model_is_local(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="BE_LLM_API_KEY"):
        make_settings(
            tmp_path, enable_classification=True, llm_provider="openai", llm_model="gpt-4.1-mini"
        )
    # A model on this machine needs no key, and asking for one would be a lie.
    local = make_settings(
        tmp_path, enable_classification=True, llm_provider="ollama", llm_model="qwen2.5:14b"
    )
    assert local.classification_key == ""


def test_a_gateway_of_your_own_needs_its_address(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="BE_LLM_BASE_URL"):
        make_settings(
            tmp_path,
            enable_classification=True,
            llm_provider="chat",
            llm_model="internal",
            llm_api_key="k",
        )


def test_an_existing_anthropic_key_keeps_working(tmp_path: Path) -> None:
    """BE_ANTHROPIC_API_KEY predates the provider setting; an .env that has it
    should not stop working because the code grew more options."""
    settings = make_settings(
        tmp_path,
        enable_classification=True,
        llm_provider="anthropic",
        llm_model="claude-sonnet-5",
        anthropic_api_key="sk-old",
    )
    assert settings.classification_key == "sk-old"


def test_half_reddit_credentials_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Reddit"):
        make_settings(tmp_path, reddit_client_id="id")


def test_unknown_archive_provider_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, archive_provider="archive.today")


def test_wayback_requires_keys(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="WAYBACK"):
        make_settings(tmp_path, wayback_access_key="", wayback_secret_key="")
