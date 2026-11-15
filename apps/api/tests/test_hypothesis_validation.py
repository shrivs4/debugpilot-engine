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


# ----- Phase 2: does the hypothesis explain the observed behavior? ----------

from app.services.code_graph import build_graph, field_dependencies  # noqa: E402
from app.services.runtime_extraction import extract_runtime_evidence  # noqa: E402
from tests.synthetic_project import (  # noqa: E402
    BASKET_REGRESSION,
    CHECK_OUTPUT,
    FILES_CORRECT,
    downstream_hypothesis,
    upstream_hypothesis,
)
from tests.test_code_graph import chunk_dicts  # noqa: E402

REGRESSION = "83f2d2c4698700000000000000000000000000aa"
UNRELATED = "export function formatLabel(text) {\n  return text.trim().toUpperCase();\n}\n"


@pytest.fixture
def mismatch_case(tmp_path: Path):
    files = FILES_CORRECT | {"src/basket.js": BASKET_REGRESSION, "src/format.js": UNRELATED}
    for relative, content in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(content, encoding="utf-8")
    structured = extract_runtime_evidence(CHECK_OUTPUT, "", 1)
    chunks = chunk_dicts(files)
    graph = build_graph(chunks, tmp_path, set(structured["mismatched_fields"]))
    structured["mismatch_analysis"] = field_dependencies(graph, structured["mismatch_analysis"], structured["mismatches"])
    entry_ids = [node.id for node in graph.chunks_in_file("scripts/check.js")]

    def run(hypothesis: dict, **overrides) -> dict:
        arguments = {
            "hypothesis": hypothesis,
            "analysis_runtime": {"exit_code": 1, "timed_out": False},
            "stack_trace": None,
            "git_evidence": {"suspect_commit": None},
            "retrieved": [chunk | {"chunk_id": chunk["id"]} for chunk in chunks],
            "repository_root": tmp_path,
            "indexed_symbols": {(chunk["file_path"], chunk["symbol"]) for chunk in chunks},
            "runtime_structured": structured,
            "graph": graph,
            "entry_chunk_ids": entry_ids,
            "blame_lookup": lambda path, start, end: [{"commit": REGRESSION, "line": line} for line in range(start, end + 1)],
        } | overrides
        result = validate_hypothesis(**arguments)
        result["by_name"] = {check["name"]: check for check in result["checks"]}
        return result

    return run


def test_upstream_hypothesis_explaining_the_mismatch_is_validated(mismatch_case):
    result = mismatch_case(upstream_hypothesis(REGRESSION))

    assert result["overall_result"] == "validated"
    checks = result["by_name"]
    assert checks["explains_observed_behavior"]["status"] == "passed"
    assert checks["affects_mismatched_field"]["status"] == "passed"
    assert "putLine" in checks["affects_mismatched_field"]["details"]
    assert checks["execution_path_connects"]["status"] == "passed"
    assert checks["introducing_commit_matches_blame"]["status"] == "passed"
    assert checks["focused_reproduction"]["status"] == "skipped"


def test_hypothesis_blaming_a_propagated_value_is_rejected(mismatch_case):
    result = mismatch_case(downstream_hypothesis(REGRESSION))

    assert result["overall_result"] == "rejected"
    conflict = result["by_name"]["consistent_with_stronger_evidence"]
    assert conflict["status"] == "failed" and conflict["category"] == "conflict"
    assert "gross" in conflict["details"]
    # A failed command, an existing file and a matching commit are not enough.
    assert result["by_name"]["reproduction_failed"]["status"] == "passed"
    assert result["by_name"]["cited_file_exists"]["status"] == "passed"
    assert result["by_name"]["introducing_commit_matches_blame"]["status"] == "passed"


def test_unsupported_hypothesis_needs_review(mismatch_case):
    hypothesis = {
        "summary": "Label formatting is broken.",
        "root_cause": "formatLabel upper-cases text.",
        "file_path": "src/format.js",
        "line": 2,
        "symbol": "formatLabel",
        "execution_path": [],
        "introducing_commit": REGRESSION,
        "affected_fields": [],
    }

    result = mismatch_case(hypothesis)

    assert result["overall_result"] == "needs_review"
    checks = result["by_name"]
    assert checks["explains_observed_behavior"]["status"] == "failed"
    assert checks["execution_path_connects"]["status"] == "failed"
    assert checks["affects_mismatched_field"]["status"] == "failed"


def test_nonexistent_file_is_rejected(mismatch_case):
    result = mismatch_case(upstream_hypothesis(REGRESSION) | {"file_path": "src/missing.js", "symbol": "ghost"})

    assert result["overall_result"] == "rejected"
    assert result["by_name"]["cited_file_exists"]["status"] == "failed"


def test_reported_mismatch_counts_as_reproduction_even_with_exit_zero(mismatch_case):
    result = mismatch_case(upstream_hypothesis(REGRESSION), analysis_runtime={"exit_code": 0, "timed_out": False})

    assert result["by_name"]["reproduction_failed"]["status"] == "passed"
    assert result["overall_result"] == "validated"


def test_commit_analysis_without_runtime_evidence_is_never_validated(mismatch_case):
    result = mismatch_case(
        upstream_hypothesis(REGRESSION),
        analysis_runtime=None,
        runtime_structured=None,
        commit_range={"commits": [{"sha": REGRESSION, "short": REGRESSION[:8]}]},
    )

    assert result["by_name"]["reproduction_failed"]["status"] == "skipped"
    assert result["by_name"]["introducing_commit_in_range"]["status"] == "passed"
    assert result["overall_result"] == "needs_review"


def test_without_graph_text_claims_are_checked_but_marked_unverified(mismatch_case):
    result = mismatch_case(upstream_hypothesis(REGRESSION), graph=None, entry_chunk_ids=None)

    affects = result["by_name"]["affects_mismatched_field"]
    assert affects["status"] == "passed"
    assert "not verified against code relationships" in affects["details"]
    assert result["by_name"]["execution_path_connects"]["status"] == "skipped"
