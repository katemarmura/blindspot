"""Read-only structural summary of saved Blindspot runs (for the inspect-run skill).

Usage (from the repository root):
    uv run --locked python .claude/skills/inspect-run/summarize_run.py --list
    uv run --locked python .claude/skills/inspect-run/summarize_run.py runs/<run-dir>

Reports stages, statuses, IDs, counts, usage, diagnostics, and reference checks.
It never prints plan, finding, answer, or model prose (read the JSON for that),
never writes files, never reads .env, and never calls an API. Standard library
only. Missing files and fields from older artifact formats are reported as
missing, never as zero.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RUNS_DIR = Path("runs")
ADDRESSING_STATUSES = {"answered", "not_applicable"}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        return {"__unreadable__": type(exc).__name__}


def _usage(u: Any) -> str:
    if not isinstance(u, dict):
        return "usage unavailable"
    note = " (INCOMPLETE)" if u.get("usage_incomplete") else ""
    return (
        f"calls {u.get('model_calls', '?')}, in {u.get('input_tokens', '?')}, "
        f"out {u.get('output_tokens', '?')}{note}"
    )


def _secs(value: Any) -> str:
    return f"{value}s" if isinstance(value, (int, float)) else "elapsed n/a"


def _diag(d: Any) -> str:
    if not isinstance(d, dict):
        return ""
    parts = [f"stage={d.get('failure_stage')}", f"stop={d.get('stop_reason')}"]
    if d.get("recovery_skip_reason"):
        parts.append(f"skip={d['recovery_skip_reason']}")
    issues = d.get("validation_errors") or []
    if issues:
        parts.append(
            "issues="
            + "; ".join(f"{i.get('schema')}:{i.get('loc')}({i.get('type')})" for i in issues)
        )
    return " [" + ", ".join(parts) + "]"


def _call_diag(c: Any) -> str:
    if not isinstance(c, dict):
        return "call_diagnostics: missing (older artifact)"
    if not c.get("response_received"):
        return (
            f"call_diagnostics: requested {c.get('requested_model')}, max_out "
            f"{c.get('max_output_tokens')}, {_secs(c.get('elapsed_seconds'))}, no response received"
        )
    return (
        f"call_diagnostics: requested {c.get('requested_model')}, max_out "
        f"{c.get('max_output_tokens')}, {_secs(c.get('elapsed_seconds'))}, returned "
        f"{c.get('returned_model')}, stop {c.get('stop_reason')}, "
        f"blocks {c.get('content_blocks')}, text {c.get('text_characters')} chars, "
        f"valid JSON syntax {c.get('parse_text_is_valid_json')}"
    )


def _derived_sources(experts: list[Any]) -> set[str]:
    """Source IDs as the application derives them: e<team pos>-f<finding pos>, successes only."""
    ids: set[str] = set()
    for pos, expert in enumerate(experts, start=1):
        report = expert.get("report") if isinstance(expert, dict) else None
        if isinstance(report, dict):
            for j, _ in enumerate(report.get("findings") or [], start=1):
                ids.add(f"e{pos}-f{j}")
    return ids


def _unresolved(items: list[Any], key: str, known: set[str]) -> list[str]:
    return sorted({i for item in items for i in (item.get(key) or []) if i not in known})


def summarize_compare(directory: Path) -> None:
    manifest = _load(directory / "manifest.json")
    print(f"Comparison run: {directory}")
    if not isinstance(manifest, dict):
        print("  manifest.json: missing")
    else:
        print(
            f"  models {manifest.get('models')}, repeats {manifest.get('repeats')}, "
            f"created {manifest.get('created_at')}, git {manifest.get('git')}"
        )
    for path in sorted(directory.glob("run-*.json")):
        run = _load(path) or {}
        findings = len((run.get("report") or {}).get("findings") or [])
        diag = (
            _diag(run.get("diagnostics"))
            if "diagnostics" in run
            else " [diagnostics: older format]"
        )
        print(
            f"  {path.name}: {run.get('status')} requested={run.get('requested_model')} "
            f"returned={run.get('returned_model')} findings={findings} "
            f"{_secs(run.get('elapsed_seconds'))} {_usage(run.get('usage'))}"
            + (diag if run.get("status") == "failed" else "")
        )


def summarize_team(directory: Path) -> None:
    print(f"Auto-team run: {directory}")
    files = {
        name: (directory / name).exists() for name in ("team.json", "experts.json", "result.json")
    }
    print("  files: " + ", ".join(f"{k}={'yes' if v else 'MISSING'}" for k, v in files.items()))
    result = _load(directory / "result.json") or _load(directory / "experts.json")
    team = _load(directory / "team.json")
    source = (
        "result.json" if files["result.json"] else "experts.json" if files["experts.json"] else None
    )
    if not isinstance(result, dict):
        selection = (team.get("team_selection") if isinstance(team, dict) else None) or {}
        status = selection.get("status", "unknown")
        print(f"  team selection: {status}{_diag(selection.get('diagnostics'))}")
        print("  experts/synthesis: not reached or not saved (no result.json/experts.json)")
        return
    print(f"  (stage data from {source})")
    selection = result.get("team_selection") or {}
    print(
        f"  team selection: {selection.get('status')} model={selection.get('model')} "
        f"{_usage(selection.get('usage'))}{_diag(selection.get('diagnostics'))}"
    )
    experts = result.get("experts") or []
    print(f"  experts: overall status {result.get('status')}")
    for pos, expert in enumerate(experts, start=1):
        findings = len((expert.get("report") or {}).get("findings") or [])
        usage = expert.get("usage") or {}
        recovery = usage.get("recovery_attempted") if isinstance(usage, dict) else None
        print(
            f"    e{pos} {expert.get('perspective')!r}: {expert.get('status')} findings={findings} "
            f"recovery={recovery} {_secs(expert.get('elapsed_seconds'))} {_usage(usage)}"
            + (_diag(expert.get("diagnostics")) if expert.get("status") == "failed" else "")
        )
    derived = _derived_sources(experts)
    saved_sources = result.get("sources")
    if saved_sources is None:
        print(f"  sources: not saved (older format); derived {sorted(derived)}")
    elif set(saved_sources) != derived:
        print(f"  sources: MISMATCH saved={sorted(saved_sources)} derived={sorted(derived)}")
    else:
        print(f"  sources: {sorted(derived)}")
    synthesis = result.get("synthesis")
    if synthesis is None:
        print("  synthesis: absent (older format or not reached)")
    else:
        report = synthesis.get("report") or {}
        synthesized: list[Any] = report.get("findings") or []
        unresolved = _unresolved(synthesized, "source_finding_ids", derived)
        print(
            f"  synthesis: {synthesis.get('status')}"
            + (f" ({synthesis.get('reason')})" if synthesis.get("reason") else "")
            + f" findings={len(synthesized)} {_secs(synthesis.get('elapsed_seconds'))} "
            f"{_usage(synthesis.get('usage'))}{_diag(synthesis.get('diagnostics'))}"
        )
        for i, f in enumerate(synthesized):
            print(f"    s{i + 1} cites {f.get('source_finding_ids')}")
        if unresolved:
            print(f"    UNRESOLVED citations: {unresolved}")
    print(f"  ORIGINAL RUN USAGE (total_usage): {_usage(result.get('total_usage'))}")
    for clarify in sorted(directory.glob("clarify-*")):
        summarize_clarification(clarify)


def _normalized(responses: list[Any]) -> list[tuple[Any, Any, Any]]:
    out = []
    for r in responses:
        answer = r.get("answer")
        answer = answer if isinstance(answer, str) and answer.strip() else None
        if r.get("status") is not None:
            out.append((r.get("question_id"), r.get("status"), answer))
    return sorted(out, key=lambda t: str(t[0]))


def summarize_clarification(directory: Path) -> None:
    print(f"  Clarification: {directory.name}")
    artifact = _load(directory / "clarification.json")
    if not isinstance(artifact, dict):
        print("    clarification.json: missing or unreadable")
        return
    generation = artifact.get("question_generation") or {}
    print(
        f"    question generation: {artifact.get('status')} model={generation.get('model')} "
        f"{_secs(generation.get('elapsed_seconds'))} {_usage(generation.get('usage'))}"
        f"{_diag(generation.get('diagnostics'))}"
    )
    print(f"      {_call_diag(generation.get('call_diagnostics'))}")
    known_sources = _derived_sources(
        [{"report": e.get("report")} for e in artifact.get("experts") or []]
    )
    questions = artifact.get("questions") or []
    question_ids = [q.get("id") for q in questions]
    for q in questions:
        print(f"    {q.get('id')} cites {q.get('source_finding_ids')}")
    unresolved = _unresolved(questions, "source_finding_ids", known_sources)
    if unresolved:
        print(f"    UNRESOLVED question citations: {unresolved}")
    if len(set(question_ids)) != len(question_ids):
        print(f"    DUPLICATE question IDs: {question_ids}")

    responses_file = _load(directory / "responses.json")
    current: list[tuple[Any, Any, Any]] | None = None
    if isinstance(responses_file, dict):
        entries = responses_file.get("responses") or []
        current = _normalized(entries)
        answered = {qid: status for qid, status, _ in current}
        statuses = {qid: answered.get(qid, "unanswered") for qid in question_ids}
        print(f"    responses.json (current): {statuses}")
    else:
        print("    responses.json: missing or unreadable")

    clarification_usages = [generation.get("usage")]
    for path in sorted(directory.glob("reassessment-*.json")):
        clarification_usages.append(
            summarize_reassessment(path, question_ids, known_sources, current)
        )
    known = [u for u in clarification_usages if isinstance(u, dict)]
    incomplete = len(known) != len(clarification_usages) or any(
        u.get("usage_incomplete") for u in known
    )
    print(
        f"    CLARIFICATION USAGE ({len(clarification_usages)} attempt(s), "
        "separate from original run): "
        f"calls {sum(u.get('model_calls', 0) for u in known)}, "
        f"in {sum(u.get('input_tokens', 0) for u in known)}, "
        f"out {sum(u.get('output_tokens', 0) for u in known)}"
        + (" (INCOMPLETE: some usage unavailable)" if incomplete else "")
    )


def summarize_reassessment(
    path: Path,
    question_ids: list[Any],
    known_sources: set[str],
    current: list[tuple[Any, Any, Any]] | None,
) -> Any:
    data = _load(path) or {}
    settings = data.get("settings") or {}
    print(
        f"    {path.name}: {data.get('status')} model={data.get('model')} "
        f"max_out={settings.get('max_output_tokens')} {_secs(data.get('elapsed_seconds'))} "
        f"{_usage(data.get('usage'))}{_diag(data.get('diagnostics'))}"
    )
    print(f"      {_call_diag(data.get('call_diagnostics'))}")
    recorded = _normalized(data.get("recorded_responses") or [])
    statuses = {qid: status for qid, status, _ in recorded}
    print(f"      used responses: {statuses}, unanswered: {data.get('unanswered_question_ids')}")
    if current is not None and current != recorded:
        print("      NOTE: responses.json has changed since this reassessment was made")
    report = data.get("report")
    if not isinstance(report, dict):
        return data.get("usage")
    for section in ("remaining_considerations", "addressed_concerns", "unresolved_uncertainties"):
        items = report.get(section) or []
        print(f"      {section}: {len(items)}")
        for i, item in enumerate(items):
            findings, questions = item.get("source_finding_ids"), item.get("question_ids")
            print(f"        {i}: findings {findings} questions {questions}")
        bad_sources = _unresolved(items, "source_finding_ids", known_sources)
        bad_questions = _unresolved(items, "question_ids", {str(q) for q in question_ids})
        if bad_sources or bad_questions:
            print(f"        UNRESOLVED: findings {bad_sources} questions {bad_questions}")
        if section == "addressed_concerns":
            weak = sorted(
                {
                    q
                    for item in items
                    for q in item.get("question_ids") or []
                    if statuses.get(q) not in ADDRESSING_STATUSES
                }
            )
            if weak:
                print(f"        ADDRESSED BY NON-ANSWER: {weak}")
    return data.get("usage")


def list_runs() -> None:
    if not RUNS_DIR.is_dir():
        print("No runs/ directory.")
        return
    for directory in sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not directory.is_dir():
            continue
        if directory.name.startswith("team-"):
            stages = [
                n for n in ("team.json", "experts.json", "result.json") if (directory / n).exists()
            ]
            clarifications = sorted(p.name for p in directory.glob("clarify-*"))
            reassessments = len(list(directory.glob("clarify-*/reassessment-*.json")))
            print(
                f"{directory}  auto-team  files={stages}  clarifications={clarifications}  "
                f"reassessments={reassessments}"
            )
        elif (directory / "manifest.json").exists():
            print(f"{directory}  comparison  runs={len(list(directory.glob('run-*.json')))}")
        else:
            print(f"{directory}  unknown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, help="A runs/ directory to summarize.")
    parser.add_argument("--list", action="store_true", help="List saved runs, newest first.")
    args = parser.parse_args(argv)
    if args.list or args.path is None:
        list_runs()
        return 0
    path: Path = args.path
    if path.is_file():  # accept any artifact file: summarize its run (or clarify) directory
        path = path.parent
    if path.name.startswith("clarify-"):
        path = path.parent
    if path.name.startswith("team-"):
        summarize_team(path)
    elif (path / "manifest.json").exists():
        summarize_compare(path)
    else:
        print(f"Not a recognized run directory: {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
