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


def test_classification_requires_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC"):
        make_settings(tmp_path, enable_classification=True)


def test_half_reddit_credentials_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Reddit"):
        make_settings(tmp_path, reddit_client_id="id")


def test_unknown_archive_provider_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, archive_provider="archive.today")


def test_wayback_requires_keys(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="WAYBACK"):
        make_settings(tmp_path, wayback_access_key="", wayback_secret_key="")
