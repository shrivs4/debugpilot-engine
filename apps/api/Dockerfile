# Node is needed inside the API container because approved debug commands
# (npm install, npm run repro:legacy) run against the cloned repository here.
FROM node:22-bookworm-slim AS node

FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

ARG INSTALL_DEV=true
COPY requirements.txt requirements-dev.txt ./
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

# Commands from debug runs execute as this unprivileged user, never as root.
RUN useradd --create-home --uid 1000 debugpilot \
    && mkdir -p /workspace \
    && chown debugpilot:debugpilot /workspace
USER debugpilot

# Fetch the grammars once at build time so indexing works offline.
RUN python -c "from tree_sitter_language_pack import download, get_parser; download(['typescript', 'tsx', 'javascript']); [get_parser(name) for name in ('typescript', 'tsx', 'javascript')]"

COPY --chown=debugpilot:debugpilot . .

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
