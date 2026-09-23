import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_anthropic_env() -> Iterator[None]:
    """Force-remove ANTHROPIC_API_KEY/ANTHROPIC_MODEL before and after every test.

    get_anthropic_api_key()/get_anthropic_model() load values from .env
    directly into os.environ, bypassing monkeypatch's own tracking, so
    monkeypatch alone can't guarantee cleanup here.
    """
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_MODEL", None)
    yield
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_MODEL", None)


def test_loads_key_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=dummy-key-from-dotenv\n")

    from blindspot.config import get_anthropic_api_key

    assert get_anthropic_api_key() == "dummy-key-from-dotenv"


def test_existing_env_var_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=dummy-key-from-dotenv\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key-from-shell")

    from blindspot.config import get_anthropic_api_key

    assert get_anthropic_api_key() == "dummy-key-from-shell"


def test_missing_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    from blindspot.config import get_anthropic_api_key

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_anthropic_api_key()


def test_empty_value_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=\n")

    from blindspot.config import get_anthropic_api_key

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_anthropic_api_key()


def test_whitespace_only_value_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

    from blindspot.config import get_anthropic_api_key

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_anthropic_api_key()


def test_loads_model_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_MODEL=claude-haiku-4-5\n")

    from blindspot.config import get_anthropic_model

    assert get_anthropic_model() == "claude-haiku-4-5"


def test_missing_model_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    from blindspot.config import get_anthropic_model

    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        get_anthropic_model()


def test_whitespace_only_model_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_MODEL", "   ")

    from blindspot.config import get_anthropic_model

    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        get_anthropic_model()


def test_import_and_health_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    from fastapi.testclient import TestClient

    from blindspot.main import app

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
