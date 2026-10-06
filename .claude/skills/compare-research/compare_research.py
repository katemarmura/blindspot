"""Read-only, deterministic comparison of two saved Blindspot research artifacts.

Usage (from the repository root):
    uv run --locked python .claude/skills/compare-research/compare_research.py \\
        runs/.../research-A.json runs/.../research-B.json

Exactly two explicit files are required: the helper never picks a pair, because two
research files may use different answers or scopes. A is treated as the older run.

It reports what differed in the inputs (the saved input snapshot, exact prompt text,
model, limits, research reference time, allowed domains, git state), each run's
outcome, performance with absolute and percentage differences, and evidence coverage
(questions matched by original finding IDs, not by r1/r2 numbering; sources; fetch
outcomes; verified quotes; rejected evidence; reported versus final status).

It prints no model-written findings or interpretations: read the JSON for those. It
does not judge quality, authority, or currency. It never writes files, reads .env,
or calls an API. Standard library only; it reuses the inspect-run helper's parsing.
Missing metadata is reported as unavailable, never as zero.
"""

import argparse
import difflib
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inspect-run"))
from summarize_run import _call_diag, _derived_sources, _diag, _load, _normalized  # noqa: E402

UNAVAILABLE = "unavailable"
# Snapshot keys and what they cover. Older artifacts may lack some (reported unavailable).
SNAPSHOT_KEYS = {
    "clarification_id": "clarification batch",
    "clarification_round": "clarification round",
    "plan_sha256": "plan",
    "findings_sha256": "original findings",
    "question_ids": "question IDs",
    "questions_sha256": "question wording",
    "recorded_responses": "recorded answers",
    "unanswered_question_ids": "unanswered questions",
}
REFERENCE_TIME = "research reference time"
# Inputs that, if different, mean the runs researched different things.
INVESTIGATION_KEYS = ("clarification_id", "plan_sha256", "findings_sha256")
# Limits that change how much work a run may do (and so its duration).
SCOPE_LIMITS = ("max_research_questions", "max_searches", "max_fetches", "max_output_tokens")


class ResearchFileError(Exception):
    """A path that is missing, unreadable, or not a research artifact."""


@dataclass(frozen=True)
class Side:
    path: Path
    data: dict[str, Any]
    clarification: dict[str, Any] | None  # the batch named by the lineage, if readable


def load_side(path: Path) -> Side:
    data = _load(path)
    if data is None:
        raise ResearchFileError(f"File not found: {path}")
    if not isinstance(data, dict) or "__unreadable__" in data:
        raise ResearchFileError(f"Not readable JSON: {path}")
    if "research_id" not in data or "input_snapshot" not in data:
        raise ResearchFileError(f"Not a research artifact (no research_id/input_snapshot): {path}")
    lineage = data.get("lineage") or {}
    candidates = [path.parent / "clarification.json"]
    if isinstance(lineage.get("clarification_file"), str):
        candidates.insert(0, Path(lineage["clarification_file"]))
    for candidate in candidates:
        artifact = _load(candidate)
        if isinstance(artifact, dict) and "__unreadable__" not in artifact:
            return Side(path, data, artifact)
    return Side(path, data, None)


def _sha(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _snapshot(side: Side) -> dict[str, Any]:
    snapshot = side.data.get("input_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def _settings(side: Side) -> dict[str, Any]:
    settings = side.data.get("settings")
    return settings if isinstance(settings, dict) else {}


def _limits(side: Side) -> dict[str, Any] | None:
    limits = _settings(side).get("limits")
    return limits if isinstance(limits, dict) else None


def _prompt(side: Side) -> str | None:
    prompt = _settings(side).get("system_prompt")
    return prompt if isinstance(prompt, str) else None


# A key absent from the file (unknown) as opposed to a saved null, which can be a real
# setting: `allowed_domains: null` means unrestricted.
_MISSING: Any = object()


def _field(mapping: dict[str, Any], key: str) -> Any:
    return mapping.get(key, _MISSING)


def _known(value: Any) -> Any:
    """For type-checked helpers that return None when absent or invalid."""
    return _MISSING if value is None else value


def _same(left: Any, right: Any) -> str:
    if left is _MISSING or right is _MISSING:
        return UNAVAILABLE
    return "same" if left == right else "DIFFERENT"


# --- Inputs and configuration ---


def _domains(settings: dict[str, Any]) -> str:
    value = _field(settings, "allowed_domains")
    if value is _MISSING:
        return UNAVAILABLE
    return "unrestricted (null)" if value is None else str(value)


def input_differences(a: Side, b: Side) -> tuple[list[str], list[str]]:
    """(changed, unavailable) input dimensions, by name."""
    sa, sb = _snapshot(a), _snapshot(b)
    dims: list[tuple[str, Any, Any]] = [
        (label, _field(sa, key), _field(sb, key)) for key, label in SNAPSHOT_KEYS.items()
    ]
    ca, cb = _settings(a), _settings(b)
    dims += [
        ("prompt", _known(_prompt(a)), _known(_prompt(b))),
        ("requested model", _field(ca, "requested_model"), _field(cb, "requested_model")),
        ("limits", _known(_limits(a)), _known(_limits(b))),
        # A saved null is a real setting (unrestricted); only an absent key is unknown.
        ("allowed domains", _field(ca, "allowed_domains"), _field(cb, "allowed_domains")),
        (
            REFERENCE_TIME,
            _field(ca, "research_reference_time"),
            _field(cb, "research_reference_time"),
        ),
    ]
    changed = [name for name, left, right in dims if _same(left, right) == "DIFFERENT"]
    unavailable = [name for name, left, right in dims if _same(left, right) == UNAVAILABLE]
    return changed, unavailable


def classify(a: Side, b: Side) -> str:
    sa, sb = _snapshot(a), _snapshot(b)
    for key in INVESTIGATION_KEYS:
        if _same(_field(sa, key), _field(sb, key)) == "DIFFERENT":
            return (
                f"different investigations ({SNAPSHOT_KEYS[key]} differs): "
                "do not compare evidence quality"
            )
    changed, unavailable = input_differences(a, b)
    suffix = f" (unavailable: {', '.join(unavailable)})" if unavailable else ""
    if not changed:
        return "same inputs and configuration: run-to-run variation" + suffix
    if changed == [REFERENCE_TIME]:
        # Each run normally records its own UTC time, but it is a real model input.
        return (
            "same inputs and configuration except the research reference time: mostly "
            "run-to-run variation, though date-sensitive findings may legitimately differ" + suffix
        )
    if len(changed) == 1:
        return f"single change ({changed[0]})" + suffix
    return f"multiple changes ({', '.join(changed)}): do not attribute to one cause" + suffix


def scope_changes(a: Side, b: Side) -> list[str]:
    """Scope limits that differ, as 'name A -> B'. Empty if equal or unavailable."""
    la, lb = _limits(a), _limits(b)
    if la is None or lb is None:
        return []
    return [f"{k} {la.get(k)} -> {lb.get(k)}" for k in SCOPE_LIMITS if la.get(k) != lb.get(k)]


def _print_inputs(a: Side, b: Side, prompt_diff: bool) -> None:
    print("\nInputs (from each file's input_snapshot; what the research actually used):")
    sa, sb = _snapshot(a), _snapshot(b)
    for key, label in SNAPSHOT_KEYS.items():
        state = _same(_field(sa, key), _field(sb, key))
        detail = ""
        if state == UNAVAILABLE:
            detail = f" (A saved: {key in sa}, B saved: {key in sb}; older artifact?)"
        print(f"  {label}: {state}{detail}")
    ra, rb = sa.get("recorded_responses"), sb.get("recorded_responses")
    if isinstance(ra, list) and isinstance(rb, list):
        left = {str(q): (s, t) for q, s, t in _normalized(ra)}
        right = {str(q): (s, t) for q, s, t in _normalized(rb)}
        for qid in sorted(set(left) | set(right)):
            (s1, t1), (s2, t2) = left.get(qid, (None, None)), right.get(qid, (None, None))
            if (s1, t1) == (s2, t2):
                print(f"    {qid}: same ({s1})")
            else:
                text = "answer text changed" if t1 != t2 else "answer text same"
                print(f"    {qid}: status {s1} -> {s2}; {text}")

    print("\nConfiguration:")
    pa, pb = _prompt(a), _prompt(b)
    if pa is None or pb is None:
        print(f"  prompt: {UNAVAILABLE} (A saved: {pa is not None}, B saved: {pb is not None})")
    elif pa == pb:
        print(f"  prompt: identical (sha {_sha(pa)})")
    else:
        print(f"  prompt: DIFFERENT (A sha {_sha(pa)}, B sha {_sha(pb)})")
        if prompt_diff:
            for line in difflib.unified_diff(
                pa.splitlines(), pb.splitlines(), "A prompt", "B prompt", n=0, lineterm=""
            ):
                print(f"    {line}")
    for label, side in (("A", a), ("B", b)):
        settings = _settings(side)
        git = settings.get("git") or {}
        call = side.data.get("call_diagnostics") or {}
        print(
            f"  {label}: requested {settings.get('requested_model', UNAVAILABLE)}, returned "
            f"{call.get('returned_model', UNAVAILABLE)}, reference time "
            f"{settings.get('research_reference_time', UNAVAILABLE)}, allowed domains "
            f"{_domains(settings)}, "
            f"git {git.get('revision', UNAVAILABLE)} dirty={git.get('dirty', UNAVAILABLE)}"
        )
    la, lb = _limits(a), _limits(b)
    if la is None or lb is None:
        print(f"  limits: {UNAVAILABLE} (A saved: {la is not None}, B saved: {lb is not None})")
    else:
        diffs = [k for k in sorted(set(la) | set(lb)) if la.get(k) != lb.get(k)]
        print("  limits: identical" if not diffs else "  limits: DIFFERENT")
        for k in diffs:
            print(f"    {k}: {la.get(k, UNAVAILABLE)} -> {lb.get(k, UNAVAILABLE)}")
    ga = (_settings(a).get("git") or {}).get("revision")
    gb = (_settings(b).get("git") or {}).get("revision")
    dirty = any((_settings(s).get("git") or {}).get("dirty") for s in (a, b))
    if ga is not None and ga == gb and dirty:
        print(
            "  note: same git revision with a dirty tree; the revision does not establish "
            "what ran. Rely on the saved prompt and limits above."
        )


# --- Outcomes and performance ---


def _usage_value(side: Side, key: str) -> float | None:
    usage = side.data.get("usage")
    if not isinstance(usage, dict) or usage.get("usage_incomplete"):
        return None
    value = usage.get(key)
    return value if isinstance(value, (int, float)) else None


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def request_seconds(side: Side) -> tuple[float | None, str]:
    """Request duration and where it came from.

    `timings.request_seconds` and `call_diagnostics.elapsed_seconds` are the same
    measurement in the research code; older artifacts only have the latter.
    """
    timings = side.data.get("timings")
    if isinstance(timings, dict) and _number(timings.get("request_seconds")) is not None:
        return timings["request_seconds"], "timings"
    call = side.data.get("call_diagnostics")
    if isinstance(call, dict) and _number(call.get("elapsed_seconds")) is not None:
        return call["elapsed_seconds"], "call_diagnostics"
    return None, UNAVAILABLE


def local_seconds(side: Side) -> float | None:
    timings = side.data.get("timings")
    return _number(timings.get("local_processing_seconds")) if isinstance(timings, dict) else None


def _tool_usage(side: Side, key: str) -> float | None:
    tools = side.data.get("server_tool_usage")
    return _number(tools.get(key)) if isinstance(tools, dict) else None


def _questions(side: Side) -> list[dict[str, Any]]:
    questions = side.data.get("questions")
    return [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []


def _quotes(side: Side) -> int:
    return sum(
        len(f.get("evidence") or [])
        for q in _questions(side)
        for f in q.get("supported_findings") or []
    )


def _rejected(side: Side) -> list[dict[str, Any]]:
    return [r for q in _questions(side) for r in q.get("rejected_evidence") or []]


def metrics(side: Side) -> dict[str, float | None]:
    completed = side.data.get("status") == "completed"
    evidence: dict[str, float | None] = {
        "questions": len(_questions(side)),
        "sources (fetched, verifiable)": len(side.data.get("sources") or []),
        "verified quotes": _quotes(side),
        "rejected evidence": len(_rejected(side)),
    }
    if not completed:  # a failed run has no evidence; zero would be misleading
        evidence = dict.fromkeys(evidence)
    return {
        "request seconds": request_seconds(side)[0],
        "local processing seconds": local_seconds(side),
        "input tokens": _usage_value(side, "input_tokens"),
        "output tokens": _usage_value(side, "output_tokens"),
        "searches (provider)": _tool_usage(side, "web_search_requests"),
        "fetches (provider)": _tool_usage(side, "web_fetch_requests"),
        **evidence,
    }


def delta(a: float | None, b: float | None) -> str:
    """Absolute and percentage change from A to B; 'unavailable' if either is missing."""
    if a is None or b is None:
        return UNAVAILABLE
    diff = b - a
    absolute = f"{diff:+.3f}" if isinstance(diff, float) and not diff.is_integer() else f"{diff:+g}"
    if a == 0:
        return f"{absolute} (no %: A is zero)"
    return f"{absolute} ({diff / a * 100:+.1f}%)"


def _fmt(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    return f"{value:.3f}" if isinstance(value, float) and not value.is_integer() else f"{value:g}"


def _print_outcomes(a: Side, b: Side) -> None:
    print("\nOutcomes:")
    for label, side in (("A", a), ("B", b)):
        d = side.data
        error = d.get("error")
        print(f"  {label}: {d.get('status', UNAVAILABLE)}{_diag(d.get('diagnostics'))}")
        if error:
            print(f"     error: {error}")
        print(f"     {_call_diag(d.get('call_diagnostics'))}")
        if d.get("status") != "completed":
            print("     no evidence was produced: not an empty or reassuring result")

    print("\nPerformance (A -> B; incomplete usage counts as unavailable):")
    ma, mb = metrics(a), metrics(b)
    width = max(len(k) for k in ma)
    for key in ma:
        left, right = _fmt(ma[key]), _fmt(mb[key])
        print(f"  {key:<{width}}  A {left:>11}  B {right:>11}  {delta(ma[key], mb[key])}")
    for label, side in (("A", a), ("B", b)):
        source = request_seconds(side)[1]
        if source == "call_diagnostics":
            print(f"  {label} request seconds: from call_diagnostics.elapsed_seconds (no timings)")
    changes = scope_changes(a, b)
    if changes:
        print(
            "  note: scope limits differ (" + "; ".join(changes) + "). Duration, tokens, and "
            "tool counts are not like-for-like; do not attribute the difference to the prompt "
            "alone."
        )
    elif _limits(a) is None or _limits(b) is None:
        print("  note: limits unavailable for a side; speed comparability is unknown.")


# --- Evidence and coverage ---


def _known_findings(a: Side, b: Side) -> set[str] | None:
    artifact = a.clarification or b.clarification
    if artifact is None:
        return None
    return _derived_sources([{"report": e.get("report")} for e in artifact.get("experts") or []])


def match_questions(a: Side, b: Side) -> list[tuple[str | None, str | None, str]]:
    """Pair questions by original finding IDs (not position): (A id, B id, relation)."""
    qa = {str(q.get("id")): set(q.get("source_finding_ids") or []) for q in _questions(a)}
    qb = {str(q.get("id")): set(q.get("source_finding_ids") or []) for q in _questions(b)}
    matched: dict[str, tuple[str, str]] = {}  # A id -> (B id, relation)
    used: set[str] = set()
    # Exact finding-ID sets across the whole pair first, so an earlier partial overlap
    # can't consume a question that exactly matches a later one; then overlaps.
    for relation, fits in (
        ("same finding IDs", lambda x, y: x == y),
        ("overlapping finding IDs", lambda x, y: bool(x & y)),
    ):
        for aid, afind in qa.items():
            if aid in matched:
                continue
            bid = next((b for b, bf in qb.items() if b not in used and fits(afind, bf)), None)
            if bid is not None:
                matched[aid] = (bid, relation)
                used.add(bid)
    pairs: list[tuple[str | None, str | None, str]] = [
        (aid, *matched[aid]) if aid in matched else (aid, None, "only in A") for aid in qa
    ]
    pairs += [(None, bid, "only in B") for bid in qb if bid not in used]
    return pairs


def _question_line(side: Side, qid: str) -> str:
    q = next(x for x in _questions(side) if str(x.get("id")) == qid)
    findings = q.get("supported_findings") or []
    cited = sorted({e.get("source_id") for f in findings for e in f.get("evidence") or []})
    reasons = Counter(str(r.get("reason")) for r in q.get("rejected_evidence") or [])
    status = q.get("status", UNAVAILABLE)
    reported = q.get("reported_status", UNAVAILABLE)
    status_text = status if reported == status else f"{reported} -> {status} (downgraded)"
    return (
        f"{qid} {sorted(q.get('source_finding_ids') or [])} status {status_text}; "
        f"findings {len(findings)}, quotes "
        f"{sum(len(f.get('evidence') or []) for f in findings)}, cites {cited or '-'}; "
        f"missing info {len(q.get('missing_information') or [])}, conflict notes "
        f"{len(q.get('conflict_notes') or [])}; rejected {dict(reasons) or '-'}"
    )


def _host(url: Any) -> str:
    return urlsplit(url).netloc if isinstance(url, str) else UNAVAILABLE


def _print_evidence(a: Side, b: Side) -> None:
    print("\nQuestions matched by original finding IDs (not by r-number):")
    for aid, bid, relation in match_questions(a, b):
        print(f"  [{relation}]")
        if aid is not None:
            print(f"    A {_question_line(a, aid)}")
        if bid is not None:
            print(f"    B {_question_line(b, bid)}")
    if not _questions(a) and not _questions(b):
        print("  none on either side")

    print("\nFinding coverage (an unresearched finding is open, not resolved):")
    covered = {
        label: {f for q in _questions(side) for f in q.get("source_finding_ids") or []}
        for label, side in (("A", a), ("B", b))
    }
    known = _known_findings(a, b)
    if known is None:
        print("  (clarification.json unavailable: listing only researched finding IDs)")
    for fid in sorted((known or set()) | covered["A"] | covered["B"]):
        left = "researched" if fid in covered["A"] else "-"
        right = "researched" if fid in covered["B"] else "-"
        marker = "" if left == right else "  *"
        print(f"  {fid}: A {left:<10} B {right}{marker}")

    print("\nSources and retrieval (URLs are data; authority is a judgment, not checked here):")
    urls = {
        label: {s.get("url") for s in side.data.get("sources") or []}
        for label, side in (("A", a), ("B", b))
    }
    for label, side in (("A", a), ("B", b)):
        other = urls["B" if label == "A" else "A"]
        for s in side.data.get("sources") or []:
            shared = "shared" if s.get("url") in other else f"only {label}"
            print(
                f"  {label} {s.get('id')}: {s.get('url')} [{shared}] page_age "
                f"{s.get('page_age') or UNAVAILABLE}, chars {s.get('content_chars', UNAVAILABLE)}"
                + (", verification truncated" if s.get("verification_truncated") else "")
            )
        fetches = side.data.get("fetches") or []
        outcomes = Counter(str(f.get("outcome")) for f in fetches)
        fetched = {f.get("url") for f in fetches}
        leads = {
            r.get("url")
            for s in side.data.get("searches") or []
            for r in s.get("results") or []
            if isinstance(r, dict)
        } - fetched
        print(
            f"  {label} fetch outcomes {dict(outcomes) or '-'}; search result URLs never "
            f"fetched (leads, not evidence): {len(leads)} across "
            f"{len({_host(u) for u in leads})} host(s)"
        )
        for s in side.data.get("searches") or []:
            print(f"  {label} query {s.get('query')!r} -> {s.get('outcome')}")


def compare(a: Side, b: Side, *, prompt_diff: bool = True) -> None:
    print(f"A: {a.path} ({a.data.get('research_id')}, created {a.data.get('created_at')})")
    print(f"B: {b.path} ({b.data.get('research_id')}, created {b.data.get('created_at')})")
    print(f"Classification: {classify(a, b)}")
    _print_inputs(a, b, prompt_diff)
    _print_outcomes(a, b)
    _print_evidence(a, b)
    print(
        "\nReminder: quote verification shows the text was retrieved, not that it is "
        "authoritative, current, or correctly interpreted. One pair is not a trend."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("older", type=Path, help="Research file A (older).")
    parser.add_argument("newer", type=Path, help="Research file B (newer).")
    parser.add_argument("--no-prompt-diff", action="store_true", help="Omit the prompt diff.")
    args = parser.parse_args(argv)
    try:
        a, b = load_side(args.older), load_side(args.newer)
    except ResearchFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("No comparison made; no other file was substituted.", file=sys.stderr)
        return 2
    compare(a, b, prompt_diff=not args.no_prompt_diff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
