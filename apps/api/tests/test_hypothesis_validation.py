from pathlib import Path

import pytest

from app.services.hypothesis import parse_hypothesis_response
from app.services.validation import error_identifiers, validate_hypothesis


COMMIT = "6a872565cbdc3aa55449eb220658152b18355505"
FILE = "src/checkout/discountService.ts"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    lines = [f"// line {number}" for number in range(1, 30)]
    lines[15] = "  const normalizedType = coupon.discountType.toUpperCase();"
    target = tmp_path / FILE
    target.parent.mkdir(parents=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def evidence(repository: Path) -> dict:
    frame = {
        "function": "calculateDiscount",
        "path": str(repository / FILE),
        "relative_path": FILE,
        "line": 16,
        "column": 46,
        "in_repository": True,
    }
    return {
        "analysis_runtime": {"exit_code": 1, "timed_out": False},
        "stack_trace": {
            "error_type": "TypeError",
            "error_message": "Cannot read properties of undefined (reading 'toUpperCase')",
            "frames": [frame],
            "top_repository_frame": frame,
        },
        "git_evidence": {"suspect_commit": {"commit": COMMIT, "short": COMMIT[:8], "summary": "Add discountType"}},
        "retrieved": [{"file_path": FILE, "symbol": "calculateDiscount", "start_line": 4, "end_line": 27}],
        "repository_root": repository,
        "indexed_symbols": {(FILE, "calculateDiscount")},
    }


def correct_hypothesis(**overrides) -> dict:
    return {
        "summary": "Legacy coupons crash checkout",
        "root_cause": "calculateDiscount calls coupon.discountType.toUpperCase() but legacy coupons have no discountType.",
        "file_path": FILE,
        "line": 16,
        "symbol": "calculateDiscount",
        "introducing_commit": COMMIT,
        **overrides,
    }


def statuses(result: dict) -> dict:
    return {check["name"]: check["status"] for check in result["checks"]}


def test_correct_hypothesis_is_validated(repository):
    result = validate_hypothesis(correct_hypothesis(), **evidence(repository))

    assert result["overall_result"] == "validated"
    assert set(statuses(result).values()) == {"passed"}


def test_wrong_line_needs_review(repository):
    result = validate_hypothesis(correct_hypothesis(line=12), **evidence(repository))

    assert result["overall_result"] == "needs_review"
    assert statuses(result)["hypothesis_line_matches_runtime"] == "failed"
    assert statuses(result)["failing_line_matches_error"] == "failed"


def test_wrong_commit_needs_review(repository):
    result = validate_hypothesis(correct_hypothesis(introducing_commit="b476c27"), **evidence(repository))

    assert result["overall_result"] == "needs_review"
    assert statuses(result)["introducing_commit_matches_blame"] == "failed"


def test_short_hash_with_trailing_text_and_bare_file_name_are_accepted(repository):
    hypothesis = correct_hypothesis(introducing_commit="6a872565 (Add discountType)", file_path="discountService.ts")

    result = validate_hypothesis(hypothesis, **evidence(repository))

    assert result["overall_result"] == "validated"


def test_successful_reproduction_needs_review(repository):
    data = evidence(repository) | {"analysis_runtime": {"exit_code": 0, "timed_out": False}}

    result = validate_hypothesis(correct_hypothesis(), **data)

    assert statuses(result)["reproduction_failed"] == "failed"
    assert result["overall_result"] == "needs_review"


def test_advisory_checks_do_not_block_validation(repository):
    result = validate_hypothesis(correct_hypothesis(symbol="notInIndex", root_cause="Something unrelated."), **evidence(repository))

    assert statuses(result)["symbol_exists_in_index"] == "failed"
    assert statuses(result)["root_cause_references_line"] == "failed"
    assert result["overall_result"] == "validated"


def test_error_identifiers():
    assert error_identifiers("Cannot read properties of undefined (reading 'toUpperCase')") == ["toUpperCase"]
    assert error_identifiers("boom") == []


def test_parse_structured_and_free_text_responses():
    parsed, ok = parse_hypothesis_response('{"file_path": "./src/a.ts", "line": "16", "confidence": "HIGH"}')
    assert ok and parsed["file_path"] == "src/a.ts" and parsed["line"] == 16 and parsed["confidence"] == "high"

    parsed, ok = parse_hypothesis_response("The bug is in `src/checkout/discountService.ts:16` from commit 6a872565.")
    assert not ok
    assert (parsed["file_path"], parsed["line"], parsed["introducing_commit"]) == (FILE, 16, "6a872565")
