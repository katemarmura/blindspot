"""Tests for the develop workflow's Claude Code review-gate hook.

They run the hook logic against a temporary git repository; they don't start
Claude Code, so they say nothing about how a live session reacts to the hook.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "review_gate.py"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_gate", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_hook()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _event(name: str, **fields: Any) -> dict[str, Any]:
    return {"hook_event_name": name, "session_id": "s1", **fields}


def _prompt(state: dict[str, Any], repo: Path, text: str = "Add a feature") -> dict[str, Any]:
    result: dict[str, Any] = gate.handle(_event("UserPromptSubmit", prompt=text), state, repo, 0.0)
    return result


def _stop(state: dict[str, Any], repo: Path, now: float = 0.0) -> dict[str, Any]:
    result: dict[str, Any] = gate.handle(_event("Stop", stop_hook_active=False), state, repo, now)
    return result


def _review(state: dict[str, Any], repo: Path, agent_id: str = "a1") -> None:
    start = _event("SubagentStart", agent_type="blindspot-reviewer", agent_id=agent_id)
    gate.handle(start, state, repo, 0.0)
    stop = _event("SubagentStop", agent_type="blindspot-reviewer", agent_id=agent_id)
    gate.handle(stop, state, repo, 0.0)


def test_prompt_adds_reminder(repo: Path) -> None:
    out = _prompt({}, repo)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "develop" in out["hookSpecificOutput"]["additionalContext"]


def test_stop_without_changes_is_allowed(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    assert _stop(state, repo) == {}


def test_preexisting_changes_alone_are_not_gated(repo: Path) -> None:
    (repo / "app.py").write_text("x = 2\n")
    (repo / "notes.md").write_text("draft\n")
    state: dict[str, Any] = {}
    _prompt(state, repo)
    assert _stop(state, repo) == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda r, n: (r / "app.py").write_text(f"x = {n}\n"),
        lambda r, n: (r / "new.py").write_text(f"y = {n}\n"),
    ],
    ids=["tracked", "untracked"],
)
def test_unreviewed_edits_block_then_give_up(repo: Path, change: Any) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    change(repo, 100)
    first = _stop(state, repo)
    change(repo, 101)
    second = _stop(state, repo)
    change(repo, 102)
    third = _stop(state, repo)
    assert first["decision"] == "block" and "1/2" in first["reason"]
    assert second["decision"] == "block" and "2/2" in second["reason"]
    assert "decision" not in third
    assert "stopped asking" in third["systemMessage"]


def test_declining_after_a_block_without_edits_is_allowed_with_warning(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 3\n")
    assert _stop(state, repo)["decision"] == "block"
    declined = gate.handle(_event("Stop", stop_hook_active=True), state, repo, 0.0)
    assert "decision" not in declined
    assert "without a review" in declined["systemMessage"]
    assert state["blocks"] == 1


def test_edit_after_a_block_is_blocked_again(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 3\n")
    assert "1/2" in _stop(state, repo)["reason"]
    (repo / "app.py").write_text("x = 4\n")
    second = gate.handle(_event("Stop", stop_hook_active=True), state, repo, 0.0)
    assert second["decision"] == "block" and "2/2" in second["reason"]
    declined = _stop(state, repo)
    assert "decision" not in declined and "without a review" in declined["systemMessage"]


def test_new_prompt_forgets_the_previous_block(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 3\n")
    assert _stop(state, repo)["decision"] == "block"
    (repo / "app.py").write_text("x = 4\n")
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 3\n")  # back to the state blocked last turn
    assert "1/2" in _stop(state, repo)["reason"]


def test_ignored_files_do_not_count(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "ignored").mkdir()
    (repo / "ignored" / "run.json").write_text("{}")
    assert _stop(state, repo) == {}


def test_new_prompt_resets_block_budget(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 4\n")
    _stop(state, repo)
    _stop(state, repo)
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 5\n")
    assert _stop(state, repo)["decision"] == "block"


def test_completed_review_of_current_state_allows_stop(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 6\n")
    _review(state, repo)
    assert _stop(state, repo) == {}


def test_edit_after_review_blocks_again(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 7\n")
    _review(state, repo)
    (repo / "app.py").write_text("x = 8\n")
    assert _stop(state, repo)["decision"] == "block"


def test_tree_changed_during_review_is_not_marked_reviewed(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 9\n")
    start = _event("SubagentStart", agent_type="blindspot-reviewer", agent_id="a1")
    gate.handle(start, state, repo, 0.0)
    (repo / "app.py").write_text("x = 10\n")
    stop = _event("SubagentStop", agent_type="blindspot-reviewer", agent_id="a1")
    gate.handle(stop, state, repo, 0.0)
    assert _stop(state, repo)["decision"] == "block"


def test_other_agents_do_not_count_as_review(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 11\n")
    for name in ("SubagentStart", "SubagentStop"):
        gate.handle(_event(name, agent_type="blindspot-fixer", agent_id="f1"), state, repo, 0.0)
    assert _stop(state, repo)["decision"] == "block"


def test_running_reviewer_allows_stop_until_stale(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 12\n")
    start = _event("SubagentStart", agent_type="blindspot-reviewer", agent_id="a1")
    gate.handle(start, state, repo, 0.0)
    assert _stop(state, repo, now=60.0) == {}
    stale = gate.REVIEWER_STALE_SECONDS + 1.0
    assert _stop(state, repo, now=stale)["decision"] == "block"


SKIP_PROMPTS = [
    "Fix it, skip review",
    "Skip the review this time",
    "Please skip the develop workflow.",
    "Do this without the workflow",
    "Fix the typo without review.",
    "Rename it. No review needed",
    "Don't run the reviewer for this one",
    "Do not use the develop workflow",
    "Just tweak the copy and don\u2019t review",
    "Pause the workflow and fix the typo",
    "Fix the test,\ndon't review",
]

NOT_SKIP_PROMPTS = [
    "Fix the bug where no review results are shown",
    "Add a step without breaking develop mode",
    "Fix the case where we skip review for drafts",
    "Make sure we don't review stale state",
    "Don't skip the review",
    "Never skip review",
    "Add a workflow without retries",
    "Show no workflow errors to users",
    "Add a feature",
]


@pytest.mark.parametrize("text", SKIP_PROMPTS)
def test_skip_phrases_match(text: str) -> None:
    assert gate.SKIP_PATTERN.search(text)


@pytest.mark.parametrize("text", NOT_SKIP_PROMPTS)
def test_near_miss_phrases_do_not_match(text: str) -> None:
    assert not gate.SKIP_PATTERN.search(text)


@pytest.mark.parametrize("text", ["Fix it, skip review", "Do this without the workflow"])
def test_skip_request_exempts_the_turn(repo: Path, text: str) -> None:
    state: dict[str, Any] = {}
    assert _prompt(state, repo, text) == {}
    (repo / "app.py").write_text("x = 13\n")
    assert _stop(state, repo) == {}


def test_near_miss_prompt_still_gates(repo: Path) -> None:
    state: dict[str, Any] = {}
    assert _prompt(state, repo, "Fix the bug where no review results are shown")
    (repo / "app.py").write_text("x = 13\n")
    assert _stop(state, repo)["decision"] == "block"


def test_committing_reviewed_changes_keeps_them_reviewed(repo: Path) -> None:
    state: dict[str, Any] = {}
    _prompt(state, repo)
    (repo / "app.py").write_text("x = 20\n")
    (repo / "new.py").write_text("y = 20\n")
    _review(state, repo)
    assert _stop(state, repo) == {}
    _prompt(state, repo, "Commit it")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat")
    assert _stop(state, repo) == {}


def test_staging_and_stash_round_trip_keep_the_fingerprint(repo: Path) -> None:
    (repo / "app.py").write_text("x = 21\n")
    before = gate.fingerprint(repo)
    _git(repo, "add", "app.py")
    assert gate.fingerprint(repo) == before
    _git(repo, "stash", "-q")
    assert gate.fingerprint(repo) != before
    _git(repo, "stash", "pop", "-q")
    assert gate.fingerprint(repo) == before


def test_fingerprint_leaves_real_index_untouched(repo: Path) -> None:
    (repo / "app.py").write_text("x = 22\n")
    (repo / "new.py").write_text("y = 22\n")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    gate.fingerprint(repo)
    assert index.read_bytes() == before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout.splitlines() == [" M app.py", "?? new.py"]
    assert not list((repo / ".git" / "claude-review-gate").glob("index.*"))


def test_fingerprint_sees_same_size_rewrite_with_unchanged_stat(repo: Path) -> None:
    # Force git's "racily clean" case: the file's recorded stat still matches after a
    # same-size rewrite, and only the index's own timestamp (not older than the file's)
    # tells git to rehash it. The temp index must keep the real index's timestamp.
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "config", "core.checkStat", "minimal")
    app, index = repo / "app.py", repo / ".git" / "index"
    stamp = 1_700_000_000
    os.utime(app, (stamp, stamp))
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "stamp")
    os.utime(index, (stamp, stamp))
    before = gate.fingerprint(repo)
    app.write_text("x = 3\n")
    os.utime(app, (stamp, stamp))
    os.utime(index, (stamp, stamp))
    assert gate.fingerprint(repo) != before


def test_fingerprint_works_without_commits(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    empty = gate.fingerprint(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    changed = gate.fingerprint(tmp_path)
    assert changed != empty
    _git(tmp_path, "add", "a.py")
    assert gate.fingerprint(tmp_path) == changed


def test_stop_before_any_prompt_is_allowed(repo: Path) -> None:
    (repo / "app.py").write_text("x = 14\n")
    assert _stop({}, repo) == {}


def _run_script(cwd: Path, event: dict[str, Any], **env: str) -> subprocess.CompletedProcess[str]:
    # Drop an inherited off switch (the README suggests setting it) so tests control it.
    inherited = {k: v for k, v in os.environ.items() if k != "BLINDSPOT_REVIEW_GATE"}
    full_env = {**inherited, "CLAUDE_PROJECT_DIR": str(cwd), **env}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_script_persists_state_in_git_dir_and_blocks(repo: Path) -> None:
    assert _run_script(repo, _event("UserPromptSubmit", prompt="Add it")).returncode == 0
    (repo / "app.py").write_text("x = 15\n")
    result = _run_script(repo, _event("Stop"))
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"
    assert (repo / ".git" / "claude-review-gate" / "s1.json").is_file()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "M app.py"


def test_script_fails_open_outside_git(tmp_path: Path) -> None:
    result = _run_script(tmp_path, _event("Stop"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert "review gate skipped" in result.stderr


def test_script_can_be_disabled(repo: Path) -> None:
    _run_script(repo, _event("UserPromptSubmit", prompt="Add it"))
    (repo / "app.py").write_text("x = 16\n")
    result = _run_script(repo, _event("Stop"), BLINDSPOT_REVIEW_GATE="off")
    assert result.returncode == 0 and result.stdout == ""
