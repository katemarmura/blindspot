"""Contract checks for the committed question-generation evaluation fixtures.

These keep `evals/question-generation/*/result.json` compatible with the real
`load_run` / `prepare` path and in sync with their checklists. They do not (and
cannot) judge question quality; see each folder's CHECKLIST.md for the manual,
live assessment.
"""

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, TextBlock, Usage

from blindspot import clarification

ROOT = Path(__file__).resolve().parents[1] / "evals" / "question-generation"
CASES = ["pet-before-travel", "small-business", "software-delivery"]


def test_fixture_folders_match_the_documented_cases() -> None:
    assert sorted(p.name for p in ROOT.iterdir() if p.is_dir()) == sorted(CASES)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for case in CASES:
        assert f"`{case}/`" in readme


@pytest.mark.parametrize("case", CASES)
def test_fixture_loads_through_the_real_loader(case: str) -> None:
    run_id, context = clarification.load_run(ROOT / case)

    assert run_id == f"eval-{case}"
    assert context.plan.startswith("FICTIONAL EVALUATION FIXTURE.")
    assert context.sources, "a fixture needs successful findings to ask about"
    # load_run has already checked the saved `sources` against the reports.
    checklist = (ROOT / case / "CHECKLIST.md").read_text(encoding="utf-8")
    for source_id in context.sources:
        assert f"`{source_id}`" in checklist


def test_small_business_has_a_failed_perspective() -> None:
    _, context = clarification.load_run(ROOT / "small-business")

    failed = [e.assignment.perspective for e in context.experts if e.report is None]

    assert failed == ["Legal & Regulatory"]
    assert not any(sid.startswith("e2-") for sid in context.sources)


# --- The real prepare path, with a fake client, on a temporary copy ---


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = self
        self._text = text

    async def create(self, **_: Any) -> Message:
        return Message(
            id="msg_test",
            type="message",
            role="assistant",
            model="claude-haiku-4-5-20260101",
            content=[TextBlock(type="text", text=self._text)],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _digest(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


@pytest.mark.usefixtures("_env")
@pytest.mark.parametrize("case", CASES)
def test_prepare_runs_on_a_copy_of_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    before = _digest(ROOT / case)
    run_dir = tmp_path / "runs" / "evals" / f"team-{case}"
    shutil.copytree(ROOT / case, run_dir)
    _, context = clarification.load_run(run_dir)
    first, *rest = list(context.sources)
    batch = {
        "questions": [
            {
                "question": "What arrangement exists?",
                "why_it_matters": "It could change the analysis.",
                "source_finding_ids": [first, rest[-1]],
            }
        ]
    }
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(json.dumps(batch)))

    prepared = asyncio.run(clarification.prepare(run_dir))

    assert prepared.error is None
    assert prepared.directory.parent == run_dir
    (question,) = prepared.artifact.questions
    perspectives = clarification.question_perspectives(
        question, prepared.artifact.context().sources
    )
    assert perspectives[0] == context.sources[first].perspective
    assert (prepared.directory / "responses.json").exists()
    assert _digest(ROOT / case) == before  # the committed fixture is never written to
