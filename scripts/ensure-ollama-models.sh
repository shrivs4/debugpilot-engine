#!/usr/bin/env bash
# Pull the Ollama models DebugPilot needs, skipping any that are already present.
set -euo pipefail

EMBEDDING_MODEL="${EMBEDDING_MODEL:-nomic-embed-text}"
GENERATION_MODEL="${GENERATION_MODEL:-qwen2.5-coder:7b}"

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama is not installed. Install it from https://ollama.com/download" >&2
  exit 1
fi

if ! installed="$(ollama list 2>/dev/null)"; then
  echo "Ollama is not running. Start the Ollama app or run: ollama serve" >&2
  exit 1
fi

for model in "$EMBEDDING_MODEL" "$GENERATION_MODEL"; do
  name="$model"
  [[ "$name" == *:* ]] || name="$name:latest"
  if awk 'NR > 1 { print $1 }' <<<"$installed" | grep -Fxq "$name"; then
    echo "✓ $model already installed"
  else
    echo "→ pulling $model"
    ollama pull "$model"
  fi
done
