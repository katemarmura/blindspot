"""Optional, bounded current-source research over a saved clarification batch.

Run with:
    uv run python -m blindspot.research \\
        --clarification runs/team-<id>/clarify-<id>/clarification.json \\
        [--responses .../responses.json] [--allowed-domain gov.example] [--yes]

Then, optionally:
    uv run python -m blindspot.clarification reassess --clarification ... --responses ... \\
        --research runs/team-<id>/clarify-<id>/research-<id>.json

Integration: Anthropic's server-side web tools (`web_search_20250305`,
`web_fetch_20250910`) inside ONE Messages API request, using the existing
ANTHROPIC_API_KEY and the configured model. Fetching runs on Anthropic's servers,
which only fetch URLs already present in the conversation and refuse private
addresses (provider-documented). No browser, forms, logins, or other writes.

Limits (application constants; see `LIMITS`): at most 1 research question, 2
searches, 2 fetches, about 6,000 tokens of text per fetched page, 15,000 output
tokens, a 180 s timeout, one request, no SDK retries, and no continuation after
`pause_turn`. Search and fetch counts are enforced by the provider (`max_uses`)
and re-checked here; the per-page token cap is provider-controlled and approximate.

Evidence: each quote the model returns must appear (after whitespace
normalization only) in the text of a page fetched in this request, at a public
http(s) URL. Anything else is recorded as rejected, never as evidence. A valid
source ID establishes traceability, not a faithful interpretation.
"""

import argparse
import asyncio
import ipaddress
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import anthropic
import pydantic
from anthropic.types import (
    Message,
    PlainTextSource,
    ServerToolUseBlock,
    TextBlock,
    WebFetchBlock,
    WebFetchToolResultBlock,
    WebSearchResultBlock,
    WebSearchToolResultBlock,
)

from blindspot.clarification import (
    TEMPLATE_FILENAME,
    CallDiagnostics,
    ClarificationInputError,
    _is_json,
    _usage_json,
    answers_snapshot,
    load_artifact,
    load_responses,
    question_perspectives,
)
from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import (
    FailureDiagnostics,
    UsageSummary,
    ValidationIssue,
    _describe_validation_error,
    _validation_issues,
)
from blindspot.prompts import RESEARCH_SYSTEM_PROMPT
from blindspot.runs import git_state, utc_now, write_json
from blindspot.schemas import ResearchFinding, ResearchReport
from blindspot.synthesis import build_input

MAX_RESEARCH_QUESTIONS = 1  # live-run limit; saved reports may have up to three
MAX_SEARCHES = 2  # web_search max_uses (provider-enforced), re-checked here
MAX_FETCHES = 2  # web_fetch max_uses (provider-enforced), re-checked here
MAX_FETCH_CONTENT_TOKENS = 6000  # web_fetch max_content_tokens (provider, approximate)
MAX_OUTPUT_TOKENS = 15000
REQUEST_TIMEOUT_SECONDS = 180.0
MAX_MODEL_REQUESTS = 1  # no pause_turn continuation, no retries, no repair
MAX_VERIFY_CHARS = 60_000  # retrieved text considered per page when checking quotes
MAX_QUOTE_CHARS = 400
MAX_EXCERPTS_PER_FINDING = 3

LIMITS: dict[str, Any] = {
    "max_research_questions": MAX_RESEARCH_QUESTIONS,
    "max_searches": MAX_SEARCHES,
    "max_fetches": MAX_FETCHES,
    "max_fetch_content_tokens": MAX_FETCH_CONTENT_TOKENS,
    "max_output_tokens": MAX_OUTPUT_TOKENS,
    "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    "max_model_requests": MAX_MODEL_REQUESTS,
    "sdk_max_retries": 0,
    "max_verify_chars_per_page": MAX_VERIFY_CHARS,
    "max_quote_chars": MAX_QUOTE_CHARS,
    "max_excerpts_per_finding": MAX_EXCERPTS_PER_FINDING,
    "provider_enforced": ["max_searches", "max_fetches", "max_fetch_content_tokens"],
}

CHARGES_NOTICE = (
    "Research makes ONE Anthropic API request with web search and web fetch enabled. "
    f"Charges: up to {MAX_SEARCHES} web searches (Anthropic lists $10 per 1,000 searches), "
    "plus standard token costs for the request, including search results and up to "
    f"{MAX_FETCHES} fetched pages (about {MAX_FETCH_CONTENT_TOKENS} tokens each, approximate; "
    f"PDFs are not capped), and up to {MAX_OUTPUT_TOKENS} output tokens. Fetching has no "
    "separate charge. No retries or continuations are made."
)


def normalize_whitespace(text: str) -> str:
    """The only normalization applied when matching quotes: collapse whitespace runs."""
    return " ".join(text.split())


def canonical_url(url: str) -> str:
    """URL used to match citations to fetched pages: drop the fragment and a trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def is_public_http_url(url: str) -> bool:
    """Public http(s) only: no credentials, localhost, internal names, or private IPs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


class ResearchError(Exception):
    """The research request failed (API error, refusal, truncation, pause, invalid output)."""

    def __init__(self, message: str, diagnostics: FailureDiagnostics | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass
class Retrieved:
    """Everything the provider actually returned: queries, search hits, fetched pages."""

    searches: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)  # source id -> verification text
    page_age: dict[str, str] = field(default_factory=dict)  # canonical url -> page_age
    final_text: str | None = None

    def source_for(self, url: str) -> dict[str, Any] | None:
        target = canonical_url(url)
        return next((s for s in self.sources if canonical_url(s["url"]) == target), None)


def collect(response: Message) -> Retrieved:
    """Read the provider's blocks. Only fetched public text pages become sources."""
    found = Retrieved()
    uses: dict[str, ServerToolUseBlock] = {}
    last_tool_index = -1
    for index, block in enumerate(response.content):
        if isinstance(block, ServerToolUseBlock):
            uses[block.id] = block
            last_tool_index = index
        elif isinstance(block, WebSearchToolResultBlock):
            last_tool_index = index
            use = uses.get(block.tool_use_id)
            query = use.input.get("query") if use is not None else None
            if isinstance(block.content, list):
                hits = [r for r in block.content if isinstance(r, WebSearchResultBlock)]
                for hit in hits:
                    if hit.page_age:
                        found.page_age[canonical_url(hit.url)] = hit.page_age
                found.searches.append(
                    {
                        "query": query,
                        "outcome": "ok",
                        "results": [
                            {"url": h.url, "title": h.title, "page_age": h.page_age} for h in hits
                        ],
                    }
                )
            else:
                code = getattr(block.content, "error_code", "unknown")
                found.searches.append({"query": query, "outcome": f"error:{code}", "results": []})
        elif isinstance(block, WebFetchToolResultBlock):
            last_tool_index = index
            requested = (
                uses[block.tool_use_id].input.get("url") if block.tool_use_id in uses else None
            )
            content = block.content
            if not isinstance(content, WebFetchBlock):
                code = getattr(content, "error_code", "unknown")
                found.fetches.append({"url": requested, "outcome": f"error:{code}"})
                continue
            if not is_public_http_url(content.url):
                found.fetches.append({"url": content.url, "outcome": "rejected:non_public_url"})
                continue
            source = content.content.source
            if not isinstance(source, PlainTextSource):
                found.fetches.append({"url": content.url, "outcome": "unverifiable:binary_content"})
                continue
            text = source.data
            source_id = f"s{len(found.sources) + 1}"
            found.texts[source_id] = normalize_whitespace(text[:MAX_VERIFY_CHARS])
            found.sources.append(
                {
                    "id": source_id,
                    "url": content.url,
                    "title": content.content.title,
                    "retrieved_at": content.retrieved_at,
                    # Only the search provider's page_age; never invented or inferred.
                    "page_age": found.page_age.get(canonical_url(content.url)),
                    "content_chars": len(text),
                    "verification_truncated": len(text) > MAX_VERIFY_CHARS,
                }
            )
            found.fetches.append({"url": content.url, "outcome": "ok", "source_id": source_id})
    tail = [
        b.text
        for i, b in enumerate(response.content)
        if isinstance(b, TextBlock) and i > last_tool_index
    ]
    found.final_text = "".join(tail).strip() or None
    # page_age may arrive after the fetch in block order; fill it in.
    for record in found.sources:
        record["page_age"] = record["page_age"] or found.page_age.get(canonical_url(record["url"]))
    return found


def _parse_report(text: str | None) -> ResearchReport:
    if text is None:
        raise ResearchError("Research returned no final JSON report.")
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    start, end = body.find("{"), body.rfind("}")
    candidate = body[start : end + 1] if start != -1 and end > start else body
    try:
        return ResearchReport.model_validate_json(candidate)
    except pydantic.ValidationError as exc:
        raise ResearchError(
            f"Research output was invalid ({_describe_validation_error(exc)})",
            FailureDiagnostics(
                failure_stage="research",
                stop_reason="end_turn",
                validation_errors=_validation_issues("research_report", exc),
            ),
        ) from exc


def verify(
    report: ResearchReport, found: Retrieved, finding_ids: set[str]
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """Keep only evidence that verifiably comes from retrieved text; record every rejection."""
    questions: list[dict[str, Any]] = []
    issues: list[ValidationIssue] = []
    for qi, question in enumerate(report.questions):
        for j, fid in enumerate(question.source_finding_ids):
            if fid not in finding_ids:
                issues.append(
                    ValidationIssue(
                        schema="research_report",
                        loc=f"questions.{qi}.source_finding_ids.{j}",
                        type="unknown_source_id",
                    )
                )
        supported: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for fi, finding in enumerate(question.findings):
            evidence, reasons = _verify_finding(finding, found)
            for reason in reasons:
                rejected.append({"finding": fi, **reason})
            if evidence:
                supported.append(
                    {
                        "source_statement": finding.source_statement,
                        "interpretation": finding.interpretation,
                        "applicability_limits": finding.applicability_limits,
                        "evidence": evidence,
                    }
                )
        status = question.status
        limitations: list[str] = []
        if question.findings and not supported:
            limitations.append("No finding had a quote verified against retrieved text.")
            if status in ("answered", "conflicting_sources"):
                status = "insufficient_evidence"
                limitations.append(f"Status changed from '{question.status}' for that reason.")
        if rejected:
            limitations.append(f"{len(rejected)} evidence rejection(s); see rejected_evidence.")
        questions.append(
            {
                "id": f"r{qi + 1}",
                "question": question.question,
                "source_finding_ids": question.source_finding_ids,
                "reported_status": question.status,
                "status": status,
                "supported_findings": supported,
                "missing_information": [m.model_dump() for m in question.missing_information],
                "conflict_notes": question.conflict_notes,
                "limitations": limitations,
                "rejected_evidence": rejected,
            }
        )
    return questions, issues


def _verify_finding(
    finding: ResearchFinding, found: Retrieved
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    evidence: list[dict[str, str]] = []
    rejected: list[dict[str, Any]] = []
    if not finding.excerpts:
        return [], [{"excerpt": None, "reason": "missing_excerpts", "quote_chars": 0}]
    for ei, excerpt in enumerate(finding.excerpts):
        reason = None
        source = found.source_for(excerpt.source_url)
        if ei >= MAX_EXCERPTS_PER_FINDING:
            reason = "too_many_excerpts"
        elif len(excerpt.quote) > MAX_QUOTE_CHARS:
            reason = "quote_too_long"
        elif source is None:
            reason = "source_not_fetched"  # model-generated or search-only URL
        elif normalize_whitespace(excerpt.quote) not in found.texts[source["id"]]:
            reason = "quote_not_in_retrieved_text"
        if reason is not None:
            # Only the reason and size are kept; an unverified URL or quote is not evidence.
            rejected.append({"excerpt": ei, "reason": reason, "quote_chars": len(excerpt.quote)})
        else:
            assert source is not None
            evidence.append({"source_id": source["id"], "quote": excerpt.quote})
    return evidence, rejected


def _tools(allowed_domains: list[str] | None) -> list[Any]:
    domains = {"allowed_domains": allowed_domains} if allowed_domains else {}
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES, **domains},
        {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": MAX_FETCHES,
            "max_content_tokens": MAX_FETCH_CONTENT_TOKENS,
            **domains,
        },
    ]


def _input(artifact: Any, snapshot: dict[str, Any], researched_at: str) -> str:
    context = artifact.context()
    questions = [
        {**q.model_dump(), "perspectives": question_perspectives(q, context.sources)}
        for q in artifact.all_questions
    ]
    return (
        f"Research reference time (UTC): {researched_at}\n"
        "This dates the research, not the user's plan or travel.\n\n"
        f"{build_input(context.plan, context.experts)}\n\n"
        f"Clarification questions:\n{json.dumps(questions, indent=2, ensure_ascii=False)}\n\n"
        "Recorded responses (reported by the user; not independently verified):\n"
        f"{json.dumps(snapshot['recorded_responses'], indent=2, ensure_ascii=False)}\n\n"
        "Questions without a response:\n"
        f"{json.dumps(snapshot['unanswered_question_ids'], ensure_ascii=False)}\n\n"
        f"Research budget: at most {MAX_RESEARCH_QUESTIONS} question, "
        f"{MAX_SEARCHES} searches, {MAX_FETCHES} fetches. "
        "Select the one external fact most likely to change the decision. "
        "Other concerns remain unresearched, not resolved."
    )


@dataclass(frozen=True)
class ResearchRun:
    path: Path
    data: dict[str, Any]


async def run_research(
    artifact_path: Path, responses_path: Path, *, allowed_domains: list[str] | None = None
) -> ResearchRun:
    """Validate inputs, make the one research request, verify evidence, save a new file.

    Raises `ClarificationInputError` (before any call) for invalid inputs or missing
    configuration. A failed request is saved as `status: "failed"` and returned.
    """
    artifact = load_artifact(artifact_path)
    if artifact.prior_research is not None:
        # Its reassessment would refuse a second research artifact, so don't spend a call.
        raise ClarificationInputError(
            "This follow-up batch already carries the round-1 research "
            f"({artifact.prior_research.research_id}), which its reassessment uses "
            "automatically. A second research artifact is not supported. No request was made."
        )
    responses = load_responses(responses_path, artifact)
    snapshot = answers_snapshot(artifact, responses)
    finding_ids = set(artifact.context().sources)
    api_key = get_anthropic_api_key()  # loaded only now that research was requested
    model = get_anthropic_model()

    usage = UsageSummary()
    call = CallDiagnostics(requested_model=model, max_output_tokens=MAX_OUTPUT_TOKENS, usage=usage)
    found = Retrieved()
    questions: list[dict[str, Any]] = []
    error: ResearchError | None = None
    server_usage: dict[str, Any] | None = None
    processing_start: float | None = None
    researched_at = utc_now()
    start = time.monotonic()
    usage.model_calls += 1
    try:
        async with anthropic.AsyncAnthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
        ) as client:
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=RESEARCH_SYSTEM_PROMPT,
                    messages=[
                        {"role": "user", "content": _input(artifact, snapshot, researched_at)}
                    ],
                    tools=_tools(allowed_domains),
                )
            except anthropic.APIConnectionError as exc:
                raise ResearchError(f"Connection error: {exc}") from exc
            except anthropic.APIStatusError as exc:
                raise ResearchError(f"API error ({exc.status_code}): {exc.message}") from exc
        call.elapsed_seconds = time.monotonic() - start
        processing_start = time.monotonic()
        usage.record(response)
        call.record_response(response)
        tool_usage = response.usage.server_tool_use
        server_usage = (
            {
                "web_search_requests": tool_usage.web_search_requests,
                "web_fetch_requests": tool_usage.web_fetch_requests,
            }
            if tool_usage is not None
            else None
        )
        found = collect(response)
        call.parse_text_is_valid_json = (
            None if found.final_text is None else _is_json(_json_body(found.final_text))
        )
        diagnostics = FailureDiagnostics(failure_stage="research", stop_reason=response.stop_reason)
        if response.stop_reason in ("refusal", "max_tokens", "pause_turn"):
            raise ResearchError(
                f"Research stopped early ({response.stop_reason}); it was not continued, "
                "because that would need another request.",
                diagnostics,
            )
        searches = _tool_count(response, "web_search")
        fetches = _tool_count(response, "web_fetch")
        if searches > MAX_SEARCHES or fetches > MAX_FETCHES:
            raise ResearchError(
                f"Research exceeded its budget ({searches} searches, {fetches} fetches).",
                diagnostics,
            )
        report = _parse_report(found.final_text)
        if len(report.questions) > MAX_RESEARCH_QUESTIONS:
            raise ResearchError(
                f"Research returned more than {MAX_RESEARCH_QUESTIONS} question; "
                "the extra questions were not silently selected or retried.",
                diagnostics,
            )
        questions, issues = verify(report, found, finding_ids)
        if issues:
            raise ResearchError(
                f"Research cited {len(issues)} unknown finding ID(s).",
                FailureDiagnostics(
                    failure_stage="research",
                    stop_reason="end_turn",
                    validation_errors=tuple(issues),
                ),
            )
    except ResearchError as exc:
        if call.elapsed_seconds is None:
            call.elapsed_seconds = time.monotonic() - start
        error = exc
        questions = []

    processing_seconds = (
        time.monotonic() - processing_start if processing_start is not None else None
    )
    research_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    path = artifact_path.parent / f"research-{research_id}.json"
    data: dict[str, Any] = {
        "research_id": research_id,
        "created_at": utc_now(),
        "status": "failed" if error is not None else "completed",
        "lineage": {
            "clarification_id": artifact.clarification_id,
            "clarification_file": str(artifact_path),
            "clarification_round": artifact.round,
            "responses_file": str(responses_path),
            "source_run": artifact.source_run,
        },
        "input_snapshot": snapshot,
        "questions": questions,
        "sources": found.sources,
        "searches": found.searches,
        "fetches": found.fetches,
        "error": str(error) if error is not None else None,
        "diagnostics": (
            error.diagnostics.to_json() if error is not None and error.diagnostics else None
        ),
        "usage": _usage_json(usage),
        "server_tool_usage": server_usage,
        "call_diagnostics": call.to_json(),
        "timings": {
            "request_seconds": call.elapsed_seconds,
            "local_processing_seconds": processing_seconds,
        },
        "settings": {
            "provider": "anthropic server tools (web_search_20250305, web_fetch_20250910)",
            "requested_model": model,
            "system_prompt": RESEARCH_SYSTEM_PROMPT,
            "research_reference_time": researched_at,
            "allowed_domains": allowed_domains or None,
            "limits": LIMITS,
            "quote_matching": "exact after collapsing whitespace runs to single spaces",
            "git": git_state(),
        },
    }
    write_json(path, data)
    return ResearchRun(path, data)


def _tool_count(response: Message, name: str) -> int:
    """Count attempts except those explicitly refused by the provider's usage cap.

    Ordinary failures still consume a use. Match the result type and tool-use ID
    so an unrelated rejection cannot hide an overrun; missing results count too.
    Keep provider usage and collected error outcomes unchanged for diagnostics.
    """
    result_type = WebSearchToolResultBlock if name == "web_search" else WebFetchToolResultBlock
    refused = {
        block.tool_use_id
        for block in response.content
        if isinstance(block, result_type)
        and getattr(block.content, "error_code", None) == "max_uses_exceeded"
    }
    return sum(
        1
        for block in response.content
        if isinstance(block, ServerToolUseBlock) and block.name == name and block.id not in refused
    )


def _json_body(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _summary(run: ResearchRun) -> list[str]:
    data = run.data
    lines = [f"Research {data['status']}: {run.path}"]
    timings = data.get("timings")
    if timings:
        local = timings["local_processing_seconds"]
        local_label = "not reached" if local is None else f"{local:.3f}s"
        lines.append(
            f"Timing: request {timings['request_seconds']:.2f}s; "
            f"local processing {local_label} "
            "(provider search/fetch/generation breakdown unavailable)."
        )
    if data["status"] != "completed":
        lines.append(f"Error: {data['error']}")
        lines.append("No evidence was produced; do not read this as 'no concerns found'.")
    for q in data["questions"]:
        verified = sum(len(f["evidence"]) for f in q["supported_findings"])
        lines.append(f"{q['id']} [{q['status']}] {q['question']}")
        cited = ", ".join(q["source_finding_ids"])
        lines.append(f"  findings: {cited}; verified quotes: {verified}")
        for missing in q["missing_information"]:
            lines.append(f"  missing: {missing['fact']} ({missing['why_needed']})")
        for limitation in q["limitations"]:
            lines.append(f"  limitation: {limitation}")
    for source in data["sources"]:
        lines.append(f"{source['id']}: {source['url']} (retrieved {source['retrieved_at']})")
    tools = data["server_tool_usage"] or {}
    usage = data["usage"] or {}
    lines.append(
        f"Usage (this request only): input tokens {usage.get('input_tokens')}, output tokens "
        f"{usage.get('output_tokens')}, searches {tools.get('web_search_requests', 'unknown')}, "
        f"fetches {tools.get('web_fetch_requests', 'unknown')}"
        + (" (incomplete)" if usage.get("usage_incomplete") else "")
    )
    return lines


def main(argv: list[str] | None = None, ask: Any = input) -> int:
    parser = argparse.ArgumentParser(
        prog="blindspot.research",
        description="Optional, bounded current-source research for a clarification batch.",
    )
    parser.add_argument("--clarification", required=True, type=Path)
    parser.add_argument("--responses", type=Path, help="Default: responses.json next to the batch.")
    parser.add_argument(
        "--allowed-domain",
        action="append",
        dest="allowed_domains",
        help="Restrict search and fetch to this domain (repeatable).",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the charges confirmation.")
    args = parser.parse_args(argv)
    responses = args.responses or args.clarification.parent / TEMPLATE_FILENAME
    print(CHARGES_NOTICE)
    if not args.yes:
        try:
            if ask("Proceed with research? [y/N] ").strip().lower() != "y":
                print("Research cancelled; no request was made.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nResearch cancelled; no request was made.")
            return 0
    try:
        run = asyncio.run(
            run_research(args.clarification, responses, allowed_domains=args.allowed_domains)
        )
    except ValueError as exc:  # invalid inputs or missing configuration, before any call
        print(f"Error: {exc}", file=sys.stderr)
        print("No request was made.", file=sys.stderr)
        return 1
    print("\n".join(_summary(run)))
    if run.data["status"] == "completed":
        print(
            "To use it: uv run python -m blindspot.clarification reassess "
            f"--clarification {args.clarification} --responses {responses} --research {run.path}"
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
