# Postgres + pgvector with the DebugPilot schema baked in as an init script.
# Baking (instead of bind-mounting) avoids host file-sharing permissions on macOS.
FROM pgvector/pgvector:pg16
COPY init/ /docker-entrypoint-initdb.d/
