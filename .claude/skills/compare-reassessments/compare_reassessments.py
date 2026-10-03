"""Read-only, deterministic comparison of two saved Blindspot reassessments.

Usage (from the repository root):
    uv run --locked python .claude/skills/compare-reassessments/compare_reassessments.py \\
        runs/.../reassessment-A.json runs/.../reassessment-B.json
    uv run --locked python .claude/skills/compare-reassessments/compare_reassessments.py \\
        --batch runs/team-<id>/clarify-<id>
    uv run --locked python .claude/skills/compare-reassessments/compare_reassessments.py

With no arguments it picks the latest two reassessments in the newest clarification batch
that has at least two. It reports what changed between the inputs (context, recorded
responses, exact prompt text, models, settings, git state) and classifies the comparison.
It also reports each call's outcome and usage, and maps every finding and question ID to
the report sections that cite it, so concerns can be matched by source rather than by
position.

It prints no model-generated prose; read the JSON for that. It never writes files, reads
.env, or calls an API. Standard library only. It reuses the inspect-run helper's parsing
(`summarize_run.py`), and missing metadata is reported as unknown.
"""

import argparse
import difflib
import hashlib
import json
import sys
from dataclasses import dataclass
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
    _secs,
    _takeaway_state,
    _unresolved,
    _usage,
)

RUNS_DIR = Path("runs")
SECTIONS = ("remaining_considerations", "addressed_concerns", "unresolved_uncertainties")
SHORT = {
    "remaining_considerations": "R",
    "addressed_concerns": "A",
    "unresolved_uncertainties": "U",
}
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Side:
    """One reassessment, with the clarification artifact it was made from."""

    path: Path
    data: dict[str, Any]
    clarification_path: Path | None
    clarification: dict[str, Any] | None


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_side(path: Path) -> Side:
    data = _load(path)
    if not isinstance(data, dict) or "__unreadable__" in data:
        raise SystemExit(f"Not a readable reassessment file: {path}")
    candidates = [path.parent / "clarification.json"]
    recorded = data.get("clarification_file")
    if isinstance(recorded, str):
        candidates.insert(0, Path(recorded))
    for candidate in candidates:
        artifact = _load(candidate)
        if isinstance(artifact, dict) and "__unreadable__" not in artifact:
            return Side(path, data, candidate, artifact)
    return Side(path, data, None, None)


def latest_pair(batch: Path) -> tuple[Path, Path]:
    files = sorted(batch.glob("reassessment-*.json"))  # names start with a UTC timestamp
    if len(files) < 2:
        raise SystemExit(f"Fewer than two reassessments in {batch}")
    return files[-2], files[-1]


def newest_batch(runs_dir: Path = RUNS_DIR) -> Path:
    batches = [d for d in runs_dir.glob("**/clarify-*") if d.is_dir()]
    batches = [d for d in batches if len(list(d.glob("reassessment-*.json"))) >= 2]
    if not batches:
        raise SystemExit(f"No clarification batch under {runs_dir} has two reassessments")
    return max(batches, key=lambda d: max(p.name for p in d.glob("reassessment-*.json")))


def _context(side: Side) -> dict[str, Any]:
    artifact = side.clarification or {}
    return {
        "clarification_id": side.data.get("clarification_id"),
        "plan": _digest(artifact.get("plan")),
        "findings": _digest([e.get("report") for e in artifact.get("experts") or []] or None),
        "questions": _digest(artifact.get("questions")),
    }


def _prompt(side: Side) -> str | None:
    prompt = (side.data.get("settings") or {}).get("system_prompt")
    return prompt if isinstance(prompt, str) else None


def _research_id(side: Side) -> str:
    """The research artifact a reassessment used ("none" when it used none)."""
    used = side.data.get("research")
    return str(used.get("research_id")) if isinstance(used, dict) else "none"


def _settings(side: Side) -> dict[str, Any]:
    return {
        "requested_model": side.data.get("requested_model"),
        "max_output_tokens": (side.data.get("settings") or {}).get("max_output_tokens"),
    }


def _responses(side: Side) -> dict[str, tuple[Any, Any]] | None:
    recorded = side.data.get("recorded_responses")
    if not isinstance(recorded, list):
        return None
    return {str(qid): (status, answer) for qid, status, answer in _normalized(recorded)}


def classify(a: Side, b: Side) -> str:
    """One label for what differs between the two reassessments' inputs."""
    ctx_a, ctx_b = _context(a), _context(b)
    if ctx_a["plan"] != ctx_b["plan"] or ctx_a["findings"] != ctx_b["findings"]:
        return "different investigations (plan or findings differ): do not compare quality"
    round_a = a.data.get("clarification_round", 1)
    round_b = b.data.get("clarification_round", 1)
    if round_a != round_b:
        return (
            f"round change (round {round_a} -> round {round_b}): different question sets and "
            "accumulated answers; describe differences without attributing them to one cause"
        )
    changes: list[str] = []
    unknown: list[str] = []
    for name, left, right in (
        ("questions", ctx_a["questions"], ctx_b["questions"]),
        ("answers", _responses(a), _responses(b)),
        ("prompt", _prompt(a), _prompt(b)),
        ("settings", _settings(a), _settings(b)),
        ("research", _research_id(a), _research_id(b)),
    ):
        if left is None or right is None:
            unknown.append(name)
        elif left != right:
            changes.append(name)
    suffix = f" (unknown: {', '.join(unknown)})" if unknown else ""
    if not changes:
        return "same inputs and settings: run-to-run variation" + suffix
    if changes == ["prompt"]:
        return "prompt change: same context and answers" + suffix
    if changes == ["answers"]:
        return "answer change: same context and prompt" + suffix
    if changes == ["settings"]:
        return "settings change only" + suffix
    if changes == ["research"]:
        return "research change: same context, answers, and prompt; evidence differs" + suffix
    return f"multiple changes ({', '.join(changes)}): do not attribute to one cause" + suffix


def _citations(side: Side, key: str) -> dict[str, list[str]]:
    """ID -> ["R0", "U1", ...] for every report section item that cites it."""
    report = side.data.get("report")
    out: dict[str, list[str]] = {}
    if not isinstance(report, dict):
        return out
    for section in SECTIONS:
        for i, item in enumerate(report.get(section) or []):
            for ref in item.get(key) or []:
                out.setdefault(str(ref), []).append(f"{SHORT[section]}{i}")
    return out


def _structure_notes(side: Side) -> list[str]:
    report = side.data.get("report")
    if not isinstance(report, dict):
        return ["no report (the call failed or was not saved): not an empty analysis"]
    notes: list[str] = []
    artifact = side.clarification or {}
    known_sources = _derived_sources(
        [{"report": e.get("report")} for e in artifact.get("experts") or []]
    )
    question_ids = {
        str(q.get("id"))
        for q in [*(artifact.get("prior_questions") or []), *(artifact.get("questions") or [])]
    }
    responses = _responses(side) or {}
    for section in SECTIONS:
        items = report.get(section) or []
        if side.clarification is not None:
            bad = _unresolved(items, "source_finding_ids", known_sources)
            bad += _unresolved(items, "question_ids", question_ids)
            if bad:
                notes.append(f"UNRESOLVED in {section}: {bad}")
        if section == "addressed_concerns":
            weak = sorted(
                {
                    q
                    for item in items
                    for q in item.get("question_ids") or []
                    if (responses.get(q) or (None,))[0] not in ADDRESSING_STATUSES
                }
            )
            if weak:
                notes.append(f"ADDRESSED BY NON-ANSWER: {weak}")
    remaining = set(_ids(report, "remaining_considerations"))
    unresolved = set(_ids(report, "unresolved_uncertainties"))
    both = sorted(remaining & unresolved)
    if both:
        notes.append(f"findings cited in both R and U (check for repetition): {both}")
    return notes


def _ids(report: dict[str, Any], section: str) -> list[str]:
    return [
        str(r) for item in report.get(section) or [] for r in item.get("source_finding_ids") or []
    ]


def _counts(side: Side) -> str:
    report = side.data.get("report")
    if not isinstance(report, dict):
        return "no report"
    return " ".join(f"{SHORT[s]}={len(report.get(s) or [])}" for s in SECTIONS)


def _show(value: Any) -> str:
    return UNKNOWN if value is None else str(value)


def compare(a: Side, b: Side, *, prompt_diff: bool = True) -> None:
    print(f"A: {a.path} (round {a.data.get('clarification_round', 1)})")
    print(f"B: {b.path} (round {b.data.get('clarification_round', 1)})")
    print(f"Classification: {classify(a, b)}")

    print("\nContext (from each side's clarification.json):")
    ctx_a, ctx_b = _context(a), _context(b)
    for key in ("clarification_id", "plan", "findings", "questions"):
        left, right = ctx_a[key], ctx_b[key]
        same = "same" if left == right and left is not None else "DIFFERENT"
        if left is None or right is None:
            same = UNKNOWN
        print(f"  {key}: {same}" + ("" if same == "same" else f" (A={left}, B={right})"))

    print("\nResponses actually used (recorded_responses; not the current responses file):")
    ra, rb = _responses(a), _responses(b)
    if ra is None or rb is None:
        print(f"  unknown (A recorded: {ra is not None}, B recorded: {rb is not None})")
    else:
        for qid in sorted(set(ra) | set(rb)):
            (sa, ta), (sb, tb) = ra.get(qid, (None, None)), rb.get(qid, (None, None))
            if (sa, ta) == (sb, tb):
                print(f"  {qid}: same ({sa})")
            else:
                text = "answer text changed" if ta != tb else "answer text same"
                print(f"  {qid}: status {sa} -> {sb}; {text}")
        print(
            f"  unanswered: A={a.data.get('unanswered_question_ids')} "
            f"B={b.data.get('unanswered_question_ids')}"
        )

    print("\nPrompt (settings.system_prompt):")
    pa, pb = _prompt(a), _prompt(b)
    if pa is None or pb is None:
        print(f"  {UNKNOWN} (A saved: {pa is not None}, B saved: {pb is not None})")
    elif pa == pb:
        print(f"  identical (sha {_digest(pa)})")
    else:
        print(f"  DIFFERENT (A sha {_digest(pa)}, B sha {_digest(pb)})")
        if prompt_diff:
            diff = difflib.unified_diff(
                pa.splitlines(), pb.splitlines(), "A prompt", "B prompt", n=0, lineterm=""
            )
            for line in diff:
                print(f"    {line}")

    print("\nModels, settings, git:")
    for label, side in (("A", a), ("B", b)):
        settings = side.data.get("settings") or {}
        git = settings.get("git") or {}
        print(
            f"  {label}: requested {_show(side.data.get('requested_model'))}, returned "
            f"{_show(side.data.get('model'))}, max_out {_show(settings.get('max_output_tokens'))}, "
            f"git {_show(git.get('revision'))} dirty={_show(git.get('dirty'))}"
        )

    print("\nOperational outcome (each call on its own; nothing summed):")
    for label, side in (("A", a), ("B", b)):
        d = side.data
        print(
            f"  {label}: {d.get('status')} {_secs(d.get('elapsed_seconds'))} "
            f"{_usage(d.get('usage'))}{_diag(d.get('diagnostics'))}"
        )
        print(f"     {_call_diag(d.get('call_diagnostics'))}")

    print(f"\nSection counts: A {_counts(a)} | B {_counts(b)}")
    print(f"Takeaway: A {_takeaway_state(a.data)} | B {_takeaway_state(b.data)}")
    print("Citations by finding ID (R=remaining, A=addressed, U=unresolved, index in section):")
    fa, fb = _citations(a, "source_finding_ids"), _citations(b, "source_finding_ids")
    artifact = a.clarification or b.clarification or {}
    all_findings = sorted(
        _derived_sources([{"report": e.get("report")} for e in artifact.get("experts") or []])
        | set(fa)
        | set(fb)
    )
    for fid in all_findings:
        left, right = ",".join(fa.get(fid, [])) or "-", ",".join(fb.get(fid, [])) or "-"
        marker = "" if left == right else "  *"
        print(f"  {fid}: A {left:<12} B {right}{marker}")
    print("Citations by question ID:")
    qa, qb = _citations(a, "question_ids"), _citations(b, "question_ids")
    for qid in sorted(set(qa) | set(qb)):
        left, right = ",".join(qa.get(qid, [])) or "-", ",".join(qb.get(qid, [])) or "-"
        print(f"  {qid}: A {left:<12} B {right}")

    print("\nStructural notes:")
    for label, side in (("A", a), ("B", b)):
        notes = _structure_notes(side) or ["none"]
        for note in notes:
            print(f"  {label}: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="Two reassessment-*.json files.")
    parser.add_argument("--batch", type=Path, help="A clarify-* directory: compare its latest two.")
    parser.add_argument("--no-prompt-diff", action="store_true", help="Omit the prompt diff.")
    args = parser.parse_args(argv)
    if args.paths and (len(args.paths) != 2 or args.batch):
        parser.error("give exactly two reassessment files, or --batch, or nothing")
    if args.paths:
        a_path, b_path = args.paths
    else:
        batch = args.batch or newest_batch()
        a_path, b_path = latest_pair(batch)
        print(f"Selected the latest two reassessments in {batch}")
    compare(load_side(a_path), load_side(b_path), prompt_diff=not args.no_prompt_diff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
