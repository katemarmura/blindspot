import pydantic
import pytest

from blindspot.schemas import ExpertReport, Finding

_VALID_FINDING: dict[str, object] = {
    "title": "No cash-flow buffer",
    "observation": "The plan doesn't mention a reserve for slow months.",
    "why_it_matters": "A single bad month could force the shop to close early.",
    "assumptions": ["Revenue varies significantly by season"],
    "question_to_check": "How many months of expenses could the business cover with no revenue?",
}


def test_valid_finding() -> None:
    finding = Finding.model_validate(_VALID_FINDING)
    assert finding.title == _VALID_FINDING["title"]
    assert finding.assumptions == ["Revenue varies significantly by season"]


def test_finding_allows_empty_assumptions() -> None:
    finding = Finding.model_validate({**_VALID_FINDING, "assumptions": []})
    assert finding.assumptions == []


def test_report_with_zero_findings_is_valid() -> None:
    report = ExpertReport.model_validate({"findings": []})
    assert report.findings == []


def test_report_with_three_findings_is_valid() -> None:
    report = ExpertReport.model_validate({"findings": [_VALID_FINDING] * 3})
    assert len(report.findings) == 3


def test_report_rejects_more_than_three_findings() -> None:
    with pytest.raises(pydantic.ValidationError):
        ExpertReport.model_validate({"findings": [_VALID_FINDING] * 4})


@pytest.mark.parametrize("field", ["title", "observation", "why_it_matters", "question_to_check"])
def test_finding_rejects_blank_string_fields(field: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        Finding.model_validate({**_VALID_FINDING, field: "   "})


def test_finding_rejects_blank_assumption_item() -> None:
    with pytest.raises(pydantic.ValidationError):
        Finding.model_validate({**_VALID_FINDING, "assumptions": [""]})


def test_finding_rejects_missing_field() -> None:
    incomplete = dict(_VALID_FINDING)
    del incomplete["why_it_matters"]
    with pytest.raises(pydantic.ValidationError):
        Finding.model_validate(incomplete)


def test_finding_rejects_unexpected_field() -> None:
    with pytest.raises(pydantic.ValidationError):
        Finding.model_validate({**_VALID_FINDING, "severity": 0.8})


def test_report_rejects_unexpected_field() -> None:
    with pytest.raises(pydantic.ValidationError):
        ExpertReport.model_validate({"findings": [], "summary": "all good"})
