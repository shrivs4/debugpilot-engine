from app.services.runtime_extraction import extract_runtime_evidence, normalize_field
from app.services.stack_trace import parse_stack_trace
from tests.synthetic_project import CHECK_OUTPUT


def test_expected_actual_lines_become_mismatches():
    result = extract_runtime_evidence(CHECK_OUTPUT, "", 1)

    assert result["extracted"] is True
    assert result["exit_code"] == 1
    assert result["expected"] == {"gross": "55", "reduction": "5.5", "net": "49.5"}
    assert result["actual"] == {"gross": "35", "reduction": "3.5", "net": "31.5"}
    assert [item["field"] for item in result["mismatches"]] == ["gross", "reduction", "net"]
    gross = result["mismatches"][0]
    assert gross["numeric_delta"] == -20.0
    assert gross["source_line"] == "actual gross: 35"


def test_proportional_mismatches_are_grouped():
    analysis = extract_runtime_evidence(CHECK_OUTPUT, "", 1)["mismatch_analysis"]

    assert analysis["proportional_groups"] == [{"ratio": 0.636364, "fields": ["gross", "reduction", "net"]}]
    assert analysis["earliest_field"] == "gross"
    assert analysis["root_fields"] == ["gross"]
    assert analysis["method"] == "output_order"
    assert "single upstream cause" in analysis["notes"][0]


def test_matching_values_are_not_mismatches():
    result = extract_runtime_evidence("expected count: 3\nactual count: 3.0\nexpected label: a\nactual label: b\n", "", 1)

    assert [item["field"] for item in result["mismatches"]] == ["label"]
    assert result["mismatches"][0]["ratio"] is None


def test_object_blocks_and_inline_formats():
    block = "Expected: {\n  itemCount: 4,\n  status: 'open'\n}\nActual: { itemCount: 2, status: 'open' }\n"
    assert [(m["field"], m["expected"], m["actual"]) for m in extract_runtime_evidence(block, "", 1)["mismatches"]] == [
        ("itemcount", "4", "2")
    ]

    inline = extract_runtime_evidence("balance: expected 100 but got 80", "", 1)["mismatches"]
    assert [(m["field"], m["expected"], m["actual"]) for m in inline] == [("balance", "100", "80")]

    field_first = extract_runtime_evidence("- Total Weight (expected): 12\n- Total Weight (actual): 9", "", 1)["mismatches"]
    assert field_first[0]["field"] == "total_weight"


def test_test_runner_and_assert_output():
    jest = extract_runtime_evidence("", "    Expected: 5\n    Received: 4\n", 1)
    assert [(m["expected"], m["actual"]) for m in jest["mismatches"]] == [("5", "4")]
    assert jest["assertions"][0]["kind"] == "assertion"

    node_assert = extract_runtime_evidence(
        "", "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n4 !== 5\n", 1
    )
    assert [(m["expected"], m["actual"]) for m in node_assert["mismatches"]] == [("5", "4")]
    assert any("AssertionError" in line for line in node_assert["errors"])


def test_structured_logs_errors_and_locations(tmp_path):
    stdout = '{"level":"error","msg":"stock mismatch","field":"onHand","expected":7,"actual":5}\n' \
             "level=warn msg=\"slow query\" ms=830\n"
    stderr = f"TypeError: x is not a function\n    at load ({tmp_path}/src/store.js:12:5)\n" \
             "see src/util/format.ts:44 for details\n"
    stack = parse_stack_trace(stderr, tmp_path)

    result = extract_runtime_evidence(stdout, stderr, 1, stack_trace=stack, repository_root=tmp_path)

    assert [log["format"] for log in result["structured_logs"]] == ["json", "logfmt"]
    assert result["mismatches"][0]["field"] == "onhand"
    assert result["errors"][0] == "TypeError: x is not a function"
    assert result["error_message"] == "x is not a function"
    assert result["locations"] == [
        {"file_path": "src/store.js", "line": 12, "function": "load", "source": "stack_trace"},
        {"file_path": "src/util/format.ts", "line": 44, "function": None, "source": "output"},
    ]


def test_plain_text_output_still_works():
    result = extract_runtime_evidence("Compiling...\nDone in 2s\n", "", 0)

    assert result["extracted"] is False
    assert result["mismatches"] == [] and result["errors"] == [] and result["locations"] == []
    assert result["exit_code"] == 0
    assert result["mismatch_analysis"]["root_fields"] == []


def test_timeout_and_empty_output():
    result = extract_runtime_evidence(None, None, None, timed_out=True)

    assert result["timed_out"] is True
    assert result["extracted"] is False


def test_normalize_field():
    assert normalize_field("Order Subtotal") == "order_subtotal"
    assert normalize_field("net-total") == "net_total"
