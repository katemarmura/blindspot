"""Read-only structural review of one saved clarification session (round 1, and round 2).

Usage (from the repository root):
    uv run --locked python .claude/skills/inspect-session/inspect_session.py <path>

<path> is a `reassessment-*.json`, a `clarification.json`, or a `clarify-*` directory.
The session is found only through saved lineage: a round-2 artifact's `previous` block
names the exact round-1 artifact and reassessment. Newer or neighbouring files are never
chosen because of their date or folder.

It prints the exact artifacts, each call's outcome and usage (once each), question and
finding IDs, how recorded answers were carried between rounds, whether current response
files still match what was recorded, reference checks, and a list of structural defects
and limitations. It prints no plan, answer, or report prose (read the JSON for that),
never writes files, never reads .env, and never calls an API. Standard library only; it
reuses the inspect-run helper's parsing.
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inspect-run"))
from summarize_run import (  # noqa: E402
    ADDRESSING_STATUSES,
    _call_diag,
    _derived_sources,
    _diag,
    _load,
    _normalized,
    _research_used,
    _secs,
    _takeaway_state,
    _unresolved,
    _usage,
)

SECTIONS = ("remaining_considerations", "addressed_concerns", "unresolved_uncertainties")
SHORT = {
    "remaining_considerations": "R",
    "addressed_concerns": "A",
    "unresolved_uncertainties": "U",
}


def _json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = _load(path)
    if not isinstance(data, dict) or "__unreadable__" in data:
        return None
    return data


def _answers(entries: list[Any], round_number: int | None = None) -> dict[str, tuple[Any, Any]]:
    """question_id -> (status, answer), optionally only entries tagged with one round."""
    if round_number is not None:
        entries = [e for e in entries if e.get("round", 1) == round_number]
    return {str(q): (s, a) for q, s, a in _normalized(entries)}


@dataclass
class Session:
    """The artifacts of one session, located by lineage, plus what was found about them."""

    input_path: Path
    r1_artifact_path: Path | None = None
    r1_artifact: dict[str, Any] | None = None
    r1_reassessment_path: Path | None = None
    r1_reassessment: dict[str, Any] | None = None
    r2_artifact_path: Path | None = None
    r2_artifact: dict[str, Any] | None = None
    r2_reassessment_paths: list[Path] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _artifact_for(reassessment_path: Path, reassessment: dict[str, Any], s: Session) -> Path | None:
    recorded = reassessment.get("clarification_file")
    candidates = [Path(recorded)] if isinstance(recorded, str) else []
    candidates.append(reassessment_path.parent / "clarification.json")
    for candidate in candidates:
        artifact = _json(candidate)
        if artifact is None:
            continue
        if artifact.get("clarification_id") != reassessment.get("clarification_id"):
            s.defects.append(
                f"{reassessment_path.name}: clarification_id does not match {candidate}"
            )
        return candidate
    s.limitations.append(f"{reassessment_path.name}: its clarification artifact was not found")
    return None


def _reassessments_of(directory: Path, clarification_id: Any) -> list[Path]:
    return [
        p
        for p in sorted(directory.glob("reassessment-*.json"))
        if (_json(p) or {}).get("clarification_id") == clarification_id
    ]


def resolve(path: Path) -> Session:
    s = Session(input_path=path)
    reassessment_path: Path | None = None
    if path.is_dir():
        artifact_path: Path | None = path / "clarification.json"
    elif path.name.startswith("reassessment-"):
        reassessment_path = path
        reassessment = _json(path)
        if reassessment is None:
            s.limitations.append(f"{path} is not a readable reassessment")
            return s
        artifact_path = _artifact_for(path, reassessment, s)
    else:
        artifact_path = path
    artifact = _json(artifact_path)
    if artifact_path is None or artifact is None:
        if artifact_path is not None:
            s.limitations.append(f"{artifact_path} is not a readable clarification artifact")
        return s

    if artifact.get("round", 1) == 2:
        s.r2_artifact_path, s.r2_artifact = artifact_path, artifact
        s.r2_reassessment_paths = (
            [reassessment_path]
            if reassessment_path is not None
            else _reassessments_of(artifact_path.parent, artifact.get("clarification_id"))
        )
        _follow_lineage(s)
    else:
        s.r1_artifact_path, s.r1_artifact = artifact_path, artifact
        if reassessment_path is not None:
            s.r1_reassessment_path = reassessment_path
            s.r1_reassessment = _json(reassessment_path)
        else:
            found = _reassessments_of(artifact_path.parent, artifact.get("clarification_id"))
            if len(found) == 1:
                s.r1_reassessment_path, s.r1_reassessment = found[0], _json(found[0])
            elif found:
                s.limitations.append(
                    f"{len(found)} reassessments of this round-1 batch exist "
                    f"({', '.join(p.name for p in found)}); pass one explicitly to review a "
                    "session. None was chosen."
                )
        _find_followups(s)
    return s


def _follow_lineage(s: Session) -> None:
    assert s.r2_artifact is not None
    previous = s.r2_artifact.get("previous")
    if not isinstance(previous, dict):
        s.limitations.append("round-2 artifact has no `previous` lineage; round 1 not located")
        return
    for key, kind in (("clarification_file", "artifact"), ("reassessment_file", "reassessment")):
        value = previous.get(key)
        path = Path(value) if isinstance(value, str) else None
        data = _json(path)
        if path is None or data is None:
            s.limitations.append(f"round-1 {kind} named by lineage not found: {value}")
            continue
        if kind == "artifact":
            s.r1_artifact_path, s.r1_artifact = path, data
            if data.get("clarification_id") != previous.get("clarification_id"):
                s.defects.append("lineage clarification_id does not match the round-1 artifact")
            if data.get("round", 1) != 1:
                s.defects.append("lineage points to an artifact that is not round 1")
        else:
            s.r1_reassessment_path, s.r1_reassessment = path, data
            if data.get("reassessment_id") != previous.get("reassessment_id"):
                s.defects.append("lineage reassessment_id does not match the round-1 reassessment")
            if data.get("clarification_id") != previous.get("clarification_id"):
                s.defects.append("round-1 reassessment belongs to a different clarification batch")


def _find_followups(s: Session) -> None:
    """Round-2 batches whose saved lineage names this round-1 reassessment (not by date)."""
    if s.r1_artifact_path is None or s.r1_reassessment is None:
        return
    rid = s.r1_reassessment.get("reassessment_id")
    parent = s.r1_artifact_path.parent.parent
    linked = [
        d.name
        for d in sorted(parent.glob("clarify-*"))
        if ((_json(d / "clarification.json") or {}).get("previous") or {}).get("reassessment_id")
        == rid
    ]
    if linked:
        s.notes.append(
            f"follow-up batches whose lineage names this reassessment: {linked} "
            "(pass one to review the two-round session)"
        )


def _generation_line(label: str, artifact: dict[str, Any] | None) -> str:
    if artifact is None:
        return f"  {label}: artifact unavailable"
    g = artifact.get("question_generation") or {}
    if "usage" not in g and "model" not in g:
        return f"  {label}: no call recorded (e.g. a hand-written fixture)"
    count = len(artifact.get("questions") or [])
    outcome = artifact.get("status")
    if outcome == "success" and count == 0:
        outcome = "success, zero questions (no useful follow-up / no questions)"
    line = (
        f"  {label}: {outcome} ({count} questions) model={g.get('model')} "
        f"{_secs(g.get('elapsed_seconds'))} {_usage(g.get('usage'))}{_diag(g.get('diagnostics'))}"
        f"\n      {_call_diag(g.get('call_diagnostics'))}"
    )
    if g.get("no_useful_followup"):
        line += (
            "\n      no useful follow-up: the conclusion available is the preceding "
            f"reassessment {g.get('conclusion_reassessment_file')} (not a new conclusion)"
        )
    return line


def _reassessment_line(label: str, path: Path | None, data: dict[str, Any] | None) -> str:
    if path is None or data is None:
        return f"  {label}: none in this session"
    if data.get("usage") is None and not data.get("model"):
        call = "no call recorded (e.g. a hand-written fixture)"
        return f"  {label}: {path.name}: {data.get('status')}, {call}"
    return (
        f"  {label}: {path.name}: {data.get('status')} model={data.get('model')} "
        f"{_secs(data.get('elapsed_seconds'))} {_usage(data.get('usage'))}"
        f"{_diag(data.get('diagnostics'))}\n      {_call_diag(data.get('call_diagnostics'))}"
        f"\n      {_takeaway_state(data)}"
        f"\n      {_research_used(data)}"
    )


def _check_questions(s: Session, known: set[str]) -> list[str]:
    lines: list[str] = []
    r1_questions = (s.r1_artifact or {}).get("questions") or []
    r2_prior = (s.r2_artifact or {}).get("prior_questions") or []
    r2_new = (s.r2_artifact or {}).get("questions") or []
    for label, questions in (("round 1", r1_questions), ("round 2 (new)", r2_new)):
        if s.r1_artifact is None and label == "round 1":
            questions = r2_prior
        if questions or label == "round 1":
            ids = ", ".join(f"{q.get('id')}->{q.get('source_finding_ids')}" for q in questions)
            lines.append(f"  {label}: {ids or 'none'}")
        bad = _unresolved(questions, "source_finding_ids", known)
        if bad:
            s.defects.append(f"{label} questions cite unknown finding IDs: {bad}")
    if (
        s.r2_artifact is not None
        and s.r1_artifact is not None
        and _strip(r2_prior) != _strip(r1_questions)
    ):
        s.defects.append("round-2 prior_questions differ from the round-1 artifact's questions")
    all_ids = [q.get("id") for q in [*r2_prior, *r2_new]] or [q.get("id") for q in r1_questions]
    if len(set(all_ids)) != len(all_ids):
        s.defects.append(f"duplicate question IDs across rounds: {all_ids}")
    return lines


def _strip(questions: list[Any]) -> list[Any]:
    keys = ("id", "question", "why_it_matters", "source_finding_ids")
    return [{k: q.get(k) for k in keys} for q in questions]


def _compare_answers(
    s: Session, label: str, expected: dict[str, tuple[Any, Any]], actual: dict[str, tuple[Any, Any]]
) -> str:
    dropped = sorted(set(expected) - set(actual))
    added = sorted(set(actual) - set(expected))
    changed = sorted(q for q in set(expected) & set(actual) if expected[q] != actual[q])
    if dropped:
        s.defects.append(f"{label}: earlier answers dropped: {dropped}")
    if changed:
        s.defects.append(f"{label}: earlier answers changed (status or text): {changed}")
    if added:
        s.defects.append(f"{label}: answers appear that round 1 did not record: {added}")
    return "same" if not (dropped or changed or added) else "DIFFERENT (see defects)"


def _check_answers(s: Session) -> list[str]:
    lines: list[str] = []
    r1 = s.r1_reassessment
    r1_answers = _answers((r1 or {}).get("recorded_responses") or []) if r1 else None
    if r1_answers is not None:
        lines.append(f"  round 1 recorded (in the round-1 reassessment): {_statuses(r1_answers)}")
        lines.append(f"    unanswered: {(r1 or {}).get('unanswered_question_ids')}")
    if s.r2_artifact is not None:
        carried = _answers(s.r2_artifact.get("prior_responses") or [])
        if r1_answers is not None:
            verdict = _compare_answers(s, "round-2 artifact prior_responses", r1_answers, carried)
            lines.append(f"  carried into the round-2 artifact: {verdict}")
            if sorted(s.r2_artifact.get("prior_unanswered_question_ids") or []) != sorted(
                (r1 or {}).get("unanswered_question_ids") or []
            ):
                s.defects.append("round-2 artifact's prior unanswered IDs differ from round 1")
        else:
            lines.append(f"  carried into the round-2 artifact (unverified): {_statuses(carried)}")
        new_ids = {q.get("id") for q in s.r2_artifact.get("questions") or []}
        for path in s.r2_reassessment_paths:
            data = _json(path) or {}
            recorded = data.get("recorded_responses") or []
            if data.get("clarification_round", 1) != 2:
                s.defects.append(f"{path.name}: not marked clarification_round 2")
            used_r1 = _answers(recorded, 1)
            verdict = _compare_answers(s, f"{path.name} round-1 answers", carried, used_r1)
            used_r2 = _answers(recorded, 2)
            stray = sorted(set(used_r2) - {str(q) for q in new_ids})
            if stray:
                s.defects.append(f"{path.name}: round-2 answers to non-round-2 questions: {stray}")
            lines.append(
                f"  {path.name} used: round-1 answers {verdict}; round 2 {_statuses(used_r2)}"
            )
            lines.append(f"    unanswered: {data.get('unanswered_question_ids')}")
    return lines


def _statuses(answers: dict[str, tuple[Any, Any]]) -> str:
    return "{" + ", ".join(f"{q}: {s}" for q, (s, _) in sorted(answers.items())) + "}"


def _check_current_files(s: Session) -> list[str]:
    """Current response files vs what was recorded. Current files are not historical evidence."""
    lines: list[str] = []
    checks: list[tuple[dict[str, Any] | None, int | None, Path | None]] = [
        (s.r1_reassessment, None, s.r1_reassessment_path)
    ]
    checks += [(_json(p), 2, p) for p in s.r2_reassessment_paths]
    for data, round_number, path in checks:
        if data is None or path is None:
            continue
        name = Path(str(data.get("responses_file") or "responses.json")).name
        current = _json(path.parent / name)
        if current is None:
            lines.append(f"  {path.name}: {name} not found")
            continue
        recorded = _answers(data.get("recorded_responses") or [], round_number)
        if _answers(current.get("responses") or []) == recorded:
            lines.append(f"  {path.name}: {name} still matches what was recorded")
        else:
            lines.append(
                f"  {path.name}: {name} has CHANGED since it was recorded "
                "(use recorded_responses as the evidence)"
            )
    return lines


def _check_reports(s: Session, known: set[str]) -> list[str]:
    lines: list[str] = []
    all_questions = [
        *(
            (s.r2_artifact or {}).get("prior_questions")
            or (s.r1_artifact or {}).get("questions")
            or []
        ),
        *((s.r2_artifact or {}).get("questions") or []),
    ]
    question_ids = {str(q.get("id")) for q in all_questions}
    sessions: list[tuple[Path | None, dict[str, Any] | None]] = [
        (s.r1_reassessment_path, s.r1_reassessment)
    ]
    sessions += [(p, _json(p)) for p in s.r2_reassessment_paths]
    for path, data in sessions:
        if path is None or data is None:
            continue
        report = data.get("report")
        if not isinstance(report, dict):
            lines.append(
                f"  {path.name}: no report (call failed or not made): not an empty analysis"
            )
            continue
        answers = _answers(data.get("recorded_responses") or [])
        counts = " ".join(f"{SHORT[x]}={len(report.get(x) or [])}" for x in SECTIONS)
        lines.append(f"  {path.name}: {counts}")
        for section in SECTIONS:
            for i, item in enumerate(report.get(section) or []):
                lines.append(
                    f"    {SHORT[section]}{i}: findings {item.get('source_finding_ids')} "
                    f"questions {item.get('question_ids')}"
                )
            items = report.get(section) or []
            bad = _unresolved(items, "source_finding_ids", known) + _unresolved(
                items, "question_ids", question_ids
            )
            if bad:
                s.defects.append(f"{path.name}: {section} cites unknown IDs {bad}")
            if section == "addressed_concerns":
                weak = sorted(
                    {
                        q
                        for item in items
                        for q in item.get("question_ids") or []
                        if (answers.get(q) or (None,))[0] not in ADDRESSING_STATUSES
                    }
                )
                if weak:
                    s.defects.append(f"{path.name}: addressed by a non-answer: {weak}")
    return lines


def _research_lines(s: Session) -> list[str]:
    """Research artifacts used by this session's reassessments, each reported once."""
    used: dict[str, Path] = {}
    for path in [s.r1_reassessment_path, *s.r2_reassessment_paths]:
        record = (_json(path) or {}).get("research")
        if isinstance(record, dict) and isinstance(record.get("research_file"), str):
            used.setdefault(record["research_file"], Path(record["research_file"]))
    lines: list[str] = []
    for name, path in used.items():
        data = _json(path)
        if data is None:
            s.limitations.append(f"research artifact named by a reassessment not found: {name}")
            continue
        snapshot = data.get("input_snapshot") or {}
        batches = {(a or {}).get("clarification_id") for a in (s.r1_artifact, s.r2_artifact) if a}
        if snapshot.get("clarification_id") not in batches:
            s.defects.append(f"{path.name}: research belongs to a different clarification batch")
        tools = data.get("server_tool_usage") or {}
        lines.append(
            f"  research {path.name}: {data.get('status')} {_usage(data.get('usage'))} "
            f"searches={tools.get('web_search_requests', 'unknown')} "
            f"fetches={tools.get('web_fetch_requests', 'unknown')}"
            f"; timings (seconds): {data.get('timings', 'unavailable')}"
            f"; research reference: "
            f"{(data.get('settings') or {}).get('research_reference_time', 'unavailable')}"
        )
    return lines


def _original_usage(s: Session) -> str:
    artifact = s.r1_artifact or s.r2_artifact or {}
    directory = (artifact.get("source_run") or {}).get("directory")
    result = _json(Path(directory) / "result.json") if isinstance(directory, str) else None
    if result is None:
        return f"  unavailable (source run {directory!r} has no readable result.json)"
    return f"  {directory}: {_usage(result.get('total_usage'))} (not included in the calls above)"


def review(s: Session) -> list[str]:
    lines = ["Session artifacts (located by saved lineage only):"]
    lines.append(f"  input: {s.input_path}")
    lines.append(f"  round 1 artifact: {s.r1_artifact_path or 'unavailable'}")
    lines.append(f"  round 1 reassessment: {s.r1_reassessment_path or 'unavailable'}")
    if s.r2_artifact_path is not None:
        previous = (s.r2_artifact or {}).get("previous") or {}
        lines.append(f"  round 2 artifact: {s.r2_artifact_path}")
        lines.append(
            f"    lineage: follows {previous.get('clarification_id')} / reassessment "
            f"{previous.get('reassessment_id')}"
        )
        names = [p.name for p in s.r2_reassessment_paths] or ["none"]
        lines.append(f"  round 2 reassessment(s): {', '.join(names)}")
        if len(s.r2_reassessment_paths) > 1:
            s.limitations.append(
                "several round-2 reassessments share this batch; each is checked. Pass one "
                "explicitly to review a single path."
            )

    lines.append("\nCalls (each once; nothing summed across files):")
    lines.append(_generation_line("round 1 question generation", s.r1_artifact))
    lines.append(
        _reassessment_line("round 1 reassessment", s.r1_reassessment_path, s.r1_reassessment)
    )
    if s.r2_artifact is not None:
        lines.append(_generation_line("round 2 follow-up generation", s.r2_artifact))
        if not s.r2_reassessment_paths:
            lines.append("  round 2 reassessment: none (not run yet, or nothing new to reassess)")
        for path in s.r2_reassessment_paths:
            lines.append(_reassessment_line("round 2 reassessment", path, _json(path)))
    lines += _research_lines(s)
    lines.append("Original investigation usage (separate):")
    lines.append(_original_usage(s))

    artifact = s.r1_artifact or s.r2_artifact or {}
    known = _derived_sources([{"report": e.get("report")} for e in artifact.get("experts") or []])
    lines.append("\nQuestions (id -> cited findings):")
    lines += _check_questions(s, known)
    lines.append("\nRecorded answers (the evidence for what each call received):")
    lines += _check_answers(s)
    lines.append("\nCurrent response files (not historical evidence):")
    lines += _check_current_files(s) or ["  none checked"]
    lines.append("\nReports (citations by section):")
    lines += _check_reports(s, known) or ["  none"]

    lines.append("\nSTRUCTURAL DEFECTS:" + ("" if s.defects else " none"))
    lines += [f"  - {d}" for d in s.defects]
    lines.append("LIMITATIONS:" + ("" if s.limitations else " none"))
    lines += [f"  - {x}" for x in s.limitations]
    if s.notes:
        lines.append("NOTES:")
        lines += [f"  - {n}" for n in s.notes]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "path", type=Path, help="A reassessment-*.json, clarification.json, or clarify-* dir."
    )
    args = parser.parse_args(argv)
    print("\n".join(review(resolve(args.path))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
