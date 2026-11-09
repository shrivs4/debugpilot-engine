"""Ollama HTTP client.

Replaces the urllib calls in poc/embed_chunks.py and poc/generate_hypothesis.py.
The base URL and model names come from settings; connection failures become
`OllamaUnavailableError` with an actionable message.
"""

import time
from typing import Any

import httpx

from app.config import get_settings


class OllamaError(RuntimeError):
    """Base class for Ollama failures surfaced to the API caller."""


class OllamaUnavailableError(OllamaError):
    pass


class OllamaModelMissingError(OllamaError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _unavailable(self, error: Exception) -> OllamaUnavailableError:
        return OllamaUnavailableError(
            f"Ollama is not reachable at {self.base_url} ({error.__class__.__name__}). "
            "Start Ollama on the host (the Ollama app or `ollama serve`) and check "
            "OLLAMA_BASE_URL. From Docker it must be http://host.docker.internal:11434."
        )

    def _request(self, method: str, path: str, json: dict | None = None, timeout: float | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                timeout=timeout or self.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise self._unavailable(error) from error
        except httpx.TimeoutException as error:
            raise OllamaError(
                f"Ollama at {self.base_url} timed out after {timeout or self.timeout_seconds}s on {path}."
            ) from error
        except httpx.HTTPError as error:
            raise self._unavailable(error) from error

        if response.status_code == 404 and json and "model" in json:
            model = json["model"]
            raise OllamaModelMissingError(
                f"Model '{model}' is not available in Ollama. Run: ollama pull {model}"
            )
        if response.status_code >= 400:
            raise OllamaError(f"Ollama returned HTTP {response.status_code} for {path}: {response.text[:500]}")
        return response.json()

    def version(self) -> str:
        return self._request("GET", "/api/version", timeout=5)["version"]

    def list_models(self) -> list[str]:
        return [model["name"] for model in self._request("GET", "/api/tags", timeout=5).get("models", [])]

    def status(self, required_models: list[str]) -> dict:
        try:
            version = self.version()
            available = self.list_models()
        except OllamaError as error:
            return {"ok": False, "base_url": self.base_url, "version": None, "models": {}, "error": str(error)}

        models = {model: has_model(available, model) for model in required_models}
        missing = [model for model, present in models.items() if not present]
        return {
            "ok": not missing,
            "base_url": self.base_url,
            "version": version,
            "models": models,
            "error": f"Missing models: {', '.join(missing)}. Run: ollama pull <model>" if missing else None,
        }

    def ensure_models(self, required_models: list[str]) -> None:
        available = self.list_models()
        for model in required_models:
            if not has_model(available, model):
                raise OllamaModelMissingError(
                    f"Model '{model}' is not available in Ollama. Run: ollama pull {model}"
                )

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        result = self._request("POST", "/api/embed", json={"model": model, "input": texts, "truncate": True})
        embeddings = result.get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            raise OllamaError(f"Ollama returned {len(embeddings or [])} embeddings for {len(texts)} inputs.")
        return embeddings

    def generate(self, prompt: str, model: str, response_format: Any = None, options: dict | None = None) -> dict:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False, "options": options or {}}
        if response_format is not None:
            body["format"] = response_format
        started = time.monotonic()
        result = self._request("POST", "/api/generate", json=body)
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result


def has_model(available: list[str], model: str) -> bool:
    candidates = {model} if ":" in model else {model, f"{model}:latest"}
    return any(name in candidates for name in available)


def get_ollama_client() -> OllamaClient:
    settings = get_settings()
    return OllamaClient(settings.ollama_base_url, settings.ollama_timeout_seconds)
