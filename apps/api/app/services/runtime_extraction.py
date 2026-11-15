"""Structured extraction from command output.

The raw stdout/stderr are always stored; this module adds whatever structure
can be recovered from them: expected/actual values and the fields that differ,
error lines, file:line locations, JSON / logfmt log lines and a small numeric
analysis of the mismatches. Every parser is a generic pattern (no knowledge of
any particular repository) and plain text simply yields `extracted: False`.
"""

import json
import re
from pathlib import Path


MAX_ITEMS = 20
RATIO_TOLERANCE = 1e-3

_BULLET = r"(?:[-*•>|]\s*)?"
_FIELD = r"[A-Za-z_][\w.\- ]{0,40}?"

# "expected subtotal: 105", "- actual total = 58.5", "received count: 2"
KIND_FIRST = re.compile(
    rf"^\s*{_BULLET}(?P<kind>expected|actual|received|got)\s+(?P<field>{_FIELD})\s*[:=]\s*(?P<value>\S.*?)\s*,?\s*$",
    re.IGNORECASE,
)
# "subtotal (expected): 105", "subtotal expected = 105"
FIELD_FIRST = re.compile(
    rf"^\s*{_BULLET}(?P<field>{_FIELD})\s*[\(\[]?(?P<kind>expected|actual|received|got)[\)\]]?\s*[:=]\s*(?P<value>\S.*?)\s*,?\s*$",
    re.IGNORECASE,
)
# "subtotal: expected 105 but got 65", "total expected 94.5, received 58.5"
INLINE = re.compile(
    rf"(?P<field>[A-Za-z_][\w.\-]*)\s*[:=]?\s*expected\s+(?P<expected>[^\s,;]+)[,;]?\s+(?:but\s+)?(?:got|received|was|actual(?:ly)?)\s*[:=]?\s*(?P<actual>[^\s,;]+)",
    re.IGNORECASE,
)
# Jest / Vitest: "Expected: 105" / "Received: 65"
JEST_LINE = re.compile(r"^\s*(?P<kind>Expected|Received)(?:\s*\([^)]*\))?:\s*(?P<value>\S.*?)\s*$")
# Node assert: "65 !== 105"
STRICT_NE = re.compile(r"^\s*(?P<actual>[^\s!=][^!=]*?)\s+!==\s+(?P<expected>[^!=]+?)\s*$")
# "expected: { a: 1, b: 2 }" or a heading that opens a block: "Expected:" / "actual = {"
SECTION_HEADING = re.compile(
    rf"^\s*{_BULLET}(?P<kind>expected|actual|received)(?:\s+\w+)?\s*[:=]?\s*(?P<rest>\{{.*)?$", re.IGNORECASE
)
KEY_VALUE = re.compile(r"""^\s*['"]?(?P<key>[A-Za-z_][\w.\-]*)['"]?\s*[:=]\s*(?P<value>[^,{}]+?)\s*,?\s*$""")
INLINE_PAIRS = re.compile(r"""['"]?(?P<key>[A-Za-z_][\w.\-]*)['"]?\s*:\s*(?P<value>[^,{}]+)""")

ERROR_HINT = re.compile(r"(?:^|\s)(?:[A-Z][A-Za-z]*(?:Error|Exception)\b|FAIL\b|✗|×|✖)|\bError:")
LOCATION = re.compile(
    r"(?<![\w/.])(?P<path>(?:\.{0,2}/)?(?:[\w@.\-]+/)*[\w.\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rb|java))"
    r":(?P<line>\d+)(?::(?P<column>\d+))?"
)
LOGFMT_PAIR = re.compile(r"""(?P<key>[A-Za-z_][\w.]*)=(?P<value>"[^"]*"|\S+)""")
LOGFMT_KEYS = {"level", "lvl", "msg", "message", "severity"}

KIND_ALIASES = {"expected": "expected", "actual": "actual", "received": "actual", "got": "actual"}


def normalize_field(label: str) -> str:
    """'Order Subtotal' -> 'order_subtotal'; the key used to pair expected and actual values."""
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _clean_value(value: str) -> str:
    return value.strip().rstrip(",;").strip().strip("'\"`")


def value_text(value) -> str:
    """JSON values as text the way they were logged: 105, 10.5, "abc" -> abc, true, false, null."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def parse_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").lstrip("$€£")
    try:
        return float(text)
    except ValueError:
        return None


class _Collector:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, dict]] = {"expected": {}, "actual": {}}
        self.order: list[str] = []
        self.assertions: list[dict] = []

    def add(self, kind: str, label: str, value, source_line: str, raw: str | None = None, origin: str = "text") -> None:
        """Record a value. Merging is deterministic: the first report of a field wins (stdout before
        stderr, in output order); later reports with a different value are kept in `later_values`."""
        kind = KIND_ALIASES[kind.lower()]
        field = normalize_field(label)
        if not field:
            return
        if field not in self.order:
            self.order.append(field)
        text = _clean_value(value_text(value))
        existing = self.values[kind].get(field)
        if existing is None:
            self.values[kind][field] = {
                "label": label.strip(),
                "value": text,
                "source_line": source_line.strip()[:300],
                "raw": raw,
                "origin": origin,
                "later_values": [],
            }
        elif text != existing["value"] and text not in existing["later_values"]:
            existing["later_values"].append(text)

    def pair(self, label: str, expected, actual, source_line: str, kind: str, raw: str | None = None, origin: str = "text") -> None:
        self.add("expected", label, expected, source_line, raw, origin)
        self.add("actual", label, actual, source_line, raw, origin)
        self.assertions.append(
            {
                "kind": kind,
                "field": normalize_field(label),
                "expected": _clean_value(value_text(expected)),
                "actual": _clean_value(value_text(actual)),
            }
        )


def _parse_lines(lines: list[str], collector: _Collector) -> None:
    section: str | None = None
    jest: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            section = None
            continue

        heading = SECTION_HEADING.match(line)
        if heading:
            kind, rest = heading.group("kind"), heading.group("rest")
            if rest:
                for pair in INLINE_PAIRS.finditer(rest):
                    collector.add(kind, pair.group("key"), pair.group("value"), line)
            section = None if rest and rest.rstrip().rstrip(",").endswith("}") else kind
            continue

        if section:
            if stripped in {"}", "},"}:
                section = None
                continue
            if stripped.startswith("{"):
                for pair in INLINE_PAIRS.finditer(stripped):
                    collector.add(section, pair.group("key"), pair.group("value"), line)
                if stripped.rstrip(",").endswith("}"):
                    section = None
                continue
            pair = KEY_VALUE.match(line)
            if pair:
                collector.add(section, pair.group("key"), pair.group("value"), line)
                continue
            section = None

        match = KIND_FIRST.match(line) or FIELD_FIRST.match(line)
        if match:
            collector.add(match.group("kind"), match.group("field"), match.group("value"), line)
            continue

        inline = INLINE.search(line)
        if inline:
            collector.pair(inline.group("field"), inline.group("expected"), inline.group("actual"), line, "inline")
            continue

        jest_match = JEST_LINE.match(line)
        if jest_match:
            jest[jest_match.group("kind").lower()] = jest_match.group("value")
            if "expected" in jest and "received" in jest:
                collector.pair("value", jest["expected"], jest["received"], line, "assertion")
                jest = {}
            continue

        strict = STRICT_NE.match(line)
        if strict and parse_number(strict.group("actual")) is not None:
            collector.pair("value", strict.group("expected"), strict.group("actual"), line, "assertion")


MAX_JSON_BLOCK_LINES = 200
MAX_RAW_LOG_CHARS = 4_000
FIELD_KEYS = ("field", "name", "path", "key", "property", "metric")
ACTUAL_KEYS = ("actual", "received", "got", "observed")


def _json_object(lines: list[str], start: int) -> tuple[dict | None, int, str]:
    """A JSON object starting at `lines[start]`: one line, or a pretty-printed block.

    Returns (object or None, index of its last line, raw text). Malformed JSON yields None.
    """
    first = lines[start].strip()
    try:
        data = json.loads(first)
        return (data if isinstance(data, dict) else None), start, first
    except json.JSONDecodeError:
        pass
    if first != "{" and not first.endswith(("{", "[", ",")):
        return None, start, first
    depth = 0
    for end in range(start, min(len(lines), start + MAX_JSON_BLOCK_LINES)):
        depth += lines[end].count("{") + lines[end].count("[") - lines[end].count("}") - lines[end].count("]")
        if depth <= 0:
            block = "\n".join(lines[start:end + 1]).strip()
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                return None, start, first
            return (data if isinstance(data, dict) else None), end, block
    return None, start, first


def _structured_logs(lines: list[str], collector: _Collector) -> list[dict]:
    logs: list[dict] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("{"):
            data, end, raw = _json_object(lines, index)
            if data is not None:
                raw = raw[:MAX_RAW_LOG_CHARS]
                logs.append({"format": "json", "data": data, "raw": raw})
                _from_mapping(data, collector, raw)
                index = end + 1
                continue
        pairs = {match.group("key"): match.group("value").strip('"') for match in LOGFMT_PAIR.finditer(stripped)}
        if len(pairs) >= 2 and LOGFMT_KEYS & {key.lower() for key in pairs}:
            logs.append({"format": "logfmt", "data": pairs, "raw": stripped[:MAX_RAW_LOG_CHARS]})
        index += 1
    return logs[:MAX_ITEMS]


def _from_mapping(data: dict, collector: _Collector, raw: str) -> None:
    lowered = {str(key).lower(): value for key, value in data.items()}
    source_line = " ".join(raw.split())

    # {"mismatches": [{"field": ..., "expected": ..., "actual": ...}, ...]}
    mismatches = lowered.get("mismatches")
    if isinstance(mismatches, list):
        for item in mismatches:
            if not isinstance(item, dict):
                continue
            entry = {str(key).lower(): value for key, value in item.items()}
            label = next((str(entry[key]) for key in FIELD_KEYS if isinstance(entry.get(key), (str, int))), None)
            actual_key = next((key for key in ACTUAL_KEYS if key in entry), None)
            if label and "expected" in entry and actual_key:
                collector.pair(label, entry["expected"], entry[actual_key], source_line, "structured_log", raw, "structured_log")

    actual_key = next((key for key in ACTUAL_KEYS if key in lowered), None)
    if "expected" not in lowered or actual_key is None:
        return
    expected, actual = lowered["expected"], lowered[actual_key]
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            collector.add("expected", str(key), value, source_line, raw, "structured_log")
        for key, value in actual.items():
            collector.add("actual", str(key), value, source_line, raw, "structured_log")
    elif not isinstance(expected, (dict, list)) and not isinstance(actual, (dict, list)):
        label = next((str(lowered[key]) for key in FIELD_KEYS if isinstance(lowered.get(key), (str, int))), "value")
        collector.pair(label, expected, actual, source_line, "structured_log", raw, "structured_log")


def _whole_output_json(text: str, collector: _Collector) -> None:
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")) or "\n" not in stripped:
        return
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        _from_mapping(data, collector, stripped[:MAX_RAW_LOG_CHARS])


def _errors(lines: list[str], stack_trace: dict | None) -> list[str]:
    errors: list[str] = []
    if stack_trace and stack_trace.get("error_type"):
        errors.append(f"{stack_trace['error_type']}: {stack_trace.get('error_message') or ''}".strip())
    for line in lines:
        stripped = line.strip()
        if stripped and ERROR_HINT.search(stripped) and not stripped.startswith("at "):
            errors.append(stripped[:300])
    return list(dict.fromkeys(errors))[:MAX_ITEMS]


def _locations(text: str, stack_trace: dict | None, repository_root: Path | None) -> list[dict]:
    locations: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for frame in (stack_trace or {}).get("frames", []):
        if frame.get("in_repository") and (frame["relative_path"], frame["line"]) not in seen:
            seen.add((frame["relative_path"], frame["line"]))
            locations.append(
                {"file_path": frame["relative_path"], "line": frame["line"], "function": frame.get("function"), "source": "stack_trace"}
            )
    root = str(repository_root.resolve()).rstrip("/") + "/" if repository_root else None
    for match in LOCATION.finditer(text):
        path = match.group("path")
        if "node_modules" in path:
            continue
        if root and path.startswith(root):
            path = path[len(root):]
        path = path.removeprefix("./")
        if path.startswith("/"):
            continue
        key = (path, int(match.group("line")))
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file_path": path, "line": key[1], "function": None, "source": "output"})
    return locations[:MAX_ITEMS]


def analyze_mismatches(mismatches: list[dict]) -> dict:
    """Group numeric mismatches that deviate by the same factor (a sign of one upstream cause)."""
    numeric = [item for item in mismatches if item.get("ratio") is not None]
    groups: list[dict] = []
    for item in numeric:
        group = next((group for group in groups if abs(group["ratio"] - item["ratio"]) <= RATIO_TOLERANCE * max(1.0, abs(group["ratio"]))), None)
        if group is None:
            groups.append({"ratio": item["ratio"], "fields": [item["field"]]})
        else:
            group["fields"].append(item["field"])
    proportional = [group for group in groups if len(group["fields"]) > 1]
    notes = [
        f"Fields {', '.join(group['fields'])} all deviate from the expected values by the same factor "
        f"({group['ratio']:.4g}); this suggests a single upstream cause rather than independent defects."
        for group in proportional
    ]
    return {
        "proportional_groups": proportional,
        "earliest_field": mismatches[0]["field"] if mismatches else None,
        # Refined with code dependencies by code_graph.field_dependencies when a graph is available.
        "root_fields": [mismatches[0]["field"]] if mismatches else [],
        "propagated_fields": [],
        "dependencies": {},
        "method": "output_order",
        "notes": notes,
    }


def extract_runtime_evidence(
    stdout: str | None,
    stderr: str | None,
    exit_code: int | None,
    timed_out: bool = False,
    stack_trace: dict | None = None,
    repository_root: Path | None = None,
) -> dict:
    text = "\n".join(part for part in (stdout or "", stderr or "") if part)
    lines = text.splitlines()
    collector = _Collector()

    try:
        _whole_output_json(stdout or "", collector)
        logs = _structured_logs(lines, collector)
        _parse_lines(lines, collector)
    except Exception:  # noqa: BLE001 - extraction is best effort; raw output is always kept
        logs = []

    mismatches: list[dict] = []
    for field in collector.order:
        expected = collector.values["expected"].get(field)
        actual = collector.values["actual"].get(field)
        if not expected or not actual:
            continue
        expected_number, actual_number = parse_number(expected["value"]), parse_number(actual["value"])
        if expected_number is not None and actual_number is not None:
            if abs(expected_number - actual_number) <= 1e-9:
                continue
        elif expected["value"] == actual["value"]:
            continue
        both_numeric = expected_number is not None and actual_number is not None
        mismatch = {
            "field": field,
            "label": expected["label"],
            "expected": expected["value"],
            "actual": actual["value"],
            "numeric_delta": round(actual_number - expected_number, 6) if both_numeric else None,
            "ratio": round(actual_number / expected_number, 6) if both_numeric and expected_number else None,
            "source_line": actual["source_line"],
            "source": actual["origin"],
        }
        if actual["raw"]:
            mismatch["raw"] = actual["raw"]
        if expected["later_values"] or actual["later_values"]:
            mismatch["later_values"] = {"expected": expected["later_values"], "actual": actual["later_values"]}
        mismatches.append(mismatch)

    errors = _errors(lines, stack_trace)
    locations = _locations(text, stack_trace, repository_root)
    expected = {field: item["value"] for field, item in collector.values["expected"].items()}
    actual = {field: item["value"] for field, item in collector.values["actual"].items()}

    return {
        "extracted": bool(expected or actual or mismatches or errors or locations or logs),
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
        "mismatched_fields": [item["field"] for item in mismatches],
        "errors": errors,
        "error_message": (stack_trace or {}).get("error_message"),
        "locations": locations,
        "structured_logs": logs,
        "assertions": collector.assertions[:MAX_ITEMS],
        "mismatch_analysis": analyze_mismatches(mismatches),
    }
