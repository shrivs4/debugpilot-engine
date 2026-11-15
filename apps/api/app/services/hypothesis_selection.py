"""Choosing the leading hypothesis of an issue run.

The model proposes up to a few ranked candidates. Each is validated, and the
leading hypothesis is the best-supported one, not simply the model's first
answer: validated before needs_review before rejected, then candidates whose
cited code is in the strong evidence (stack frames, or the execution / data
flow that feeds the root mismatch), then the model's own order.

When runtime evidence reports mismatched values but no model candidate
explains the root mismatch, one evidence-derived candidate is added: the top
upstream writer of the root field's inputs that the reproduction executes.
It is labelled as derived and validated by the same rules as every other
hypothesis. Nothing here names a repository, function or field.

A reproduction that succeeds without mismatches or errors yields no
hypothesis at all: "No bug detected".
"""

import re

from app.services.code_graph import CodeGraph, computation_inputs

EVIDENCE_MODEL = "evidence-derived (data flow)"
RESULT_ORDER = {"validated": 0, "needs_review": 1, "rejected": 2}
STRONG_FLOW_KINDS = frozenset({"upstream_writer", "computes_root", "feeds_root"})
MUTATORS = "push|unshift|splice|set|add|delete|clear|fill|sort|reverse|pop|shift"


def no_failure_observed(analysis_runtime: dict | None, structured: dict | None, stack_trace: dict | None) -> bool:
    """The reproduction ran, exited 0 and reported no mismatched values, errors or stack trace."""
    if analysis_runtime is None:
        return False
    structured = structured or {}
    return (
        analysis_runtime.get("exit_code") == 0
        and not analysis_runtime.get("timed_out")
        and not analysis_runtime.get("error")
        and not structured.get("mismatches")
        and not structured.get("errors")
        and not (stack_trace or {}).get("frames")
    )


ISSUE_NO_BUG_MESSAGE = (
    "No bug detected: the reproduction command succeeded (exit code 0) and reported no "
    "mismatched values, errors or stack traces, so there is no failing behavior to diagnose."
)
COMMIT_NO_BUG_MESSAGE = "No bug detected: the analysis command succeeded and reported no mismatches, errors, or stack traces."


def no_bug_conclusion(analysis_runtime: dict, message: str = ISSUE_NO_BUG_MESSAGE) -> dict:
    return {"result": "no_bug_detected", "message": message, "command": analysis_runtime.get("command")}


def is_strong(chunk: dict, has_mismatches: bool) -> bool:
    """Direct runtime evidence or the execution/data flow behind the observed failure."""
    if "stack_trace" in chunk.get("sources", []):
        return True
    kind = chunk.get("flow_kind")
    return kind in STRONG_FLOW_KINDS or (kind == "executed" and not has_mismatches)


def order_candidates(candidates: list[dict]) -> list[dict]:
    """`candidates`: [{"fields", "outcome", "grounded", "index"}]; best-supported first."""
    return sorted(
        candidates,
        key=lambda item: (RESULT_ORDER.get(item["outcome"]["overall_result"], 3), not item["grounded"], item["index"]),
    )


def _write_line(content: str, start_line: int, properties: list[str]) -> tuple[int, str] | None:
    """(line, property) of the first scalar write (`x.p = …`, `x.p += …`, `x.p++`), else the first
    mutation (`x.p.push(…)`)."""
    lines = content.splitlines()
    for pattern in (
        r"\.({props})\s*(?:\+\+|--|[-+*/%]?=(?!=))",
        r"\.({props})\.(?:" + MUTATORS + r")\(",
    ):
        regex = re.compile(pattern.format(props="|".join(re.escape(prop) for prop in properties)))
        for offset, line in enumerate(lines):
            match = regex.search(line)
            if match:
                return start_line + offset, match.group(1)
    return None


def evidence_hypothesis(
    graph: CodeGraph,
    primary: list[dict],
    structured: dict,
    entry_ids: list[int],
    blame_lookup,
) -> dict | None:
    """A candidate derived from the strongest upstream writer of the root mismatched field."""
    mismatches = structured.get("mismatches") or []
    analysis = structured.get("mismatch_analysis") or {}
    root_fields = [name for name in (analysis.get("root_fields") or []) if any(m["field"] == name for m in mismatches)]
    writer = next((chunk for chunk in primary if chunk.get("flow_kind") == "upstream_writer"), None)
    if not root_fields or writer is None or writer["chunk_id"] not in graph.nodes:
        return None
    node = graph.nodes[writer["chunk_id"]]
    root = root_fields[0]
    core, inputs = computation_inputs(graph, root)
    written = sorted(node.facts.data_writes & inputs)
    if not written:
        return None
    line, written_here = _write_line(node.content, node.start_line, written) or (node.start_line, written[0])
    # The computation step that reads the property written on the cited line (most specific first).
    readers = [graph.nodes[node_id] for node_id in sorted(core) if graph.nodes[node_id].facts.reads & set(written)]
    reader = next((item for item in readers if written_here in item.facts.reads), readers[0] if readers else None)

    steps: list[str] = []
    path_text = None
    for entry in entry_ids:
        path = graph.reachable([entry], 5, ("calls",)).get(node.id)
        if path is not None:
            entry_node = graph.nodes[entry]
            steps.append(f"{entry_node.file_path}:{entry_node.start_line} {entry_node.symbol}")
            steps.extend(f"{graph.nodes[edge.target].file_path}:{graph.nodes[edge.target].start_line} {graph.nodes[edge.target].symbol}" for edge in path)
            path_text = graph.describe_path(entry, path)
            break
    if not steps or not steps[-1].endswith(f" {node.symbol}"):
        steps.append(f"{node.file_path}:{line} {node.symbol}")
    if reader is not None:
        steps.append(f"{reader.file_path}:{reader.start_line} {reader.symbol}")

    by_field = {item["field"]: item for item in mismatches}
    observed = by_field[root]
    others = [item["label"] for item in mismatches if item["field"] != root]
    propagated = analysis.get("propagated_fields") or []
    commit = ""
    try:
        entries = blame_lookup(node.file_path, line, line) if blame_lookup else []
        commit = next((entry["commit"] for entry in entries if set(entry["commit"]) != {"0"}), "")
    except Exception:  # noqa: BLE001 - blame is optional evidence
        commit = ""

    root_cause = (
        f"{observed['label']} is {observed['actual']} instead of {observed['expected']}. "
        f"{node.symbol} ({node.file_path}:{line}) writes {', '.join(written)}, which "
        f"{reader.symbol if reader else 'the computation of ' + observed['label']} reads to compute {observed['label']}"
        + (f"; the reproduction calls it directly ({path_text})" if path_text else "")
        + "."
    )
    if others:
        root_cause += (
            f" The other mismatched values ({', '.join(others)}) "
            + ("are computed from it and deviate by the same factor." if propagated else "may follow from it.")
        )
    return {
        "summary": f"{observed['label']} is wrong because {node.symbol} writes the data it is computed from "
        f"({', '.join(written)}).",
        "root_cause": root_cause,
        "file_path": node.file_path,
        "line": line,
        "symbol": node.symbol,
        "execution_path": steps,
        "introducing_commit": commit,
        "confidence": "low",
        "missing_evidence": [
            f"A focused test of {node.symbol} that reproduces the wrong {', '.join(written)}.",
            "Derived from the execution and data-flow analysis because no model hypothesis explained the root mismatch.",
        ],
        "affected_fields": [item["field"] for item in mismatches],
    }
