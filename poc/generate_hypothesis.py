import json
import urllib.request
from pathlib import Path


OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:7b"


def main() -> None:
    context_package = json.loads(
        Path("context_package.json").read_text(
            encoding="utf-8"
        )
    )

    request_body = {
        "model": MODEL_NAME,
        "prompt": context_package["llm_prompt"],
        "stream": False,
        "options": {
            "temperature": 0.1,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    print(result)

    hypothesis = result["response"]

    output = {
        "model": MODEL_NAME,
        "query": context_package["query"],
        "hypothesis": hypothesis,
    }

    Path("hypothesis.json").write_text(
        json.dumps(output, indent=2),
        encoding="utf-8",
    )

    print("Generated hypothesis:")
    print("=" * 80)
    print(hypothesis)
    print()
    print("Saved: hypothesis.json")


if __name__ == "__main__":
    main()