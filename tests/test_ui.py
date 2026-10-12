"""Serving the answering screen, and static guards on its assets.

Behaviour in a browser is covered by the Node unit tests (`tests/ui/`) for the logic and
by manual browser checks; these tests check what the server sends and that the assets
stay free of HTML injection points.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blindspot.main import create_app

_WEB = Path(__file__).resolve().parents[1] / "src" / "blindspot" / "web"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "runs")) as test_client:
        yield test_client


def test_root_redirects_to_the_screen(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


def test_screen_is_served_with_a_strict_content_security_policy(client: TestClient) -> None:
    response = client.get("/ui")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert '<script type="module" src="/ui/assets/app.mjs">' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/ui/assets/app.mjs", "text/javascript"),
        ("/ui/assets/state.mjs", "text/javascript"),
        ("/ui/assets/app.css", "text/css"),
    ],
)
def test_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


def test_assets_cannot_reach_files_outside_the_web_folder(client: TestClient) -> None:
    for path in ("/ui/assets/../index.html", "/ui/assets/%2e%2e/%2e%2e/main.py"):
        assert client.get(path).status_code == 404


def test_page_has_no_inline_scripts_styles_or_handlers() -> None:
    html = (_WEB / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []
    assert " style=" not in html
    assert re.search(r"\son[a-z]+=", html) is None


def test_every_form_field_has_a_label() -> None:
    html = (_WEB / "index.html").read_text(encoding="utf-8")
    fields = re.findall(r"<(?:input|textarea)\b[^>]*\bid=\"([^\"]+)\"", html)
    assert fields == ["batch-id", "answer-text"]
    for field in fields:
        assert f'<label for="{field}">' in html


def test_scripts_never_build_html_from_text() -> None:
    sinks = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"setTimeout\(\s*['\"`]"
    )
    for script in sorted((_WEB / "assets").glob("*.mjs")):
        assert sinks.search(script.read_text(encoding="utf-8")) is None, script.name
