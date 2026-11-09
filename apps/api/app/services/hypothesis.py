"""Hypothesis generation with the local generation model.

Derived from poc/generate_hypothesis.py. Uses Ollama structured output (a JSON
schema in `format`) and tolerates malformed responses by falling back to regex
extraction, so a run never fails only because the model drifted from JSON.
The model only produces text; it never touches the repository.
"""

import json
import re
from dataclasses import dataclass

from app.config import Settings
from app.services.ollama_client import OllamaClient


HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "file_path": {"type": "string"},
        "line": {"type": "integer"},
        "symbol": {"type": "string"},
        "execution_path": {"type": "array", "items": {"type": "string"}},
        "introducing_commit": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "root_cause",
        "file_path",
        "line",
        "symbol",
        "execution_path",
        "introducing_commit",
        "confidence",
        "missing_evidence",
    ],
}


@dataclass
class GeneratedHypothesis:
    model: str
    raw_response: str
    fields: dict
    parse_ok: bool
    duration_ms: int | None


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize_fields(data: dict) -> dict:
    confidence = str(data.get("confidence") or "").lower()
    return {
        "summary": str(data.get("summary") or "").strip() or None,
        "root_cause": str(data.get("root_cause") or "").strip() or None,
        "file_path": str(data.get("file_path") or "").strip().removeprefix("./") or None,
        "line": _as_int(data.get("line")),
        "symbol": str(data.get("symbol") or "").strip() or None,
        "execution_path": _as_list(data.get("execution_path")),
        "introducing_commit": str(data.get("introducing_commit") or "").strip() or None,
        "confidence": confidence if confidence in {"low", "medium", "high"} else None,
        "missing_evidence": _as_list(data.get("missing_evidence")),
    }


def parse_hypothesis_response(raw: str) -> tuple[dict, bool]:
    for candidate in (raw, *re.findall(r"\{.*\}", raw or "", flags=re.DOTALL)):
        try:
            data = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return normalize_fields(data), True

    # Free-text fallback: pull out the first file:line style citation and commit hash.
    location = re.search(r"([\w./-]+\.(?:tsx?|jsx?))(?::|`?,? line:? )(\d+)", raw or "", flags=re.IGNORECASE)
    commit = re.search(r"\b([0-9a-f]{40}|[0-9a-f]{7,12})\b", raw or "")
    return normalize_fields(
        {
            "root_cause": raw,
            "file_path": location.group(1) if location else None,
            "line": location.group(2) if location else None,
            "introducing_commit": commit.group(1) if commit else None,
        }
    ), False


def generate_hypothesis(ollama: OllamaClient, prompt: str, settings: Settings) -> GeneratedHypothesis:
    result = ollama.generate(
        prompt,
        settings.generation_model,
        response_format=HYPOTHESIS_SCHEMA,
        options={"temperature": settings.generation_temperature, "num_ctx": settings.generation_num_ctx},
    )
    raw = result.get("response", "")
    fields, parse_ok = parse_hypothesis_response(raw)
    return GeneratedHypothesis(
        model=settings.generation_model,
        raw_response=raw,
        fields=fields,
        parse_ok=parse_ok,
        duration_ms=result.get("duration_ms"),
    )
