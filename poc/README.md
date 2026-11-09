# POC scripts (preserved)

These are the original learning experiments, moved here unchanged (with
`git mv`, so history is kept). They still run from this directory. They read
and write JSON files in the current directory and expect Ollama at
`localhost:11434`:

```bash
cd poc
../.venv/bin/python repository_scanner.py <repo-path>
../.venv/bin/python build_chunks.py <repo-path>        # -> chunks.json
../.venv/bin/python embed_chunks.py                    # -> embedded_chunks.json
../.venv/bin/python search_chunks.py "question"
../.venv/bin/python collect_runtime.py <repo-path> --output runtime_failure.json -- npm run repro:legacy
../.venv/bin/python collect_git_evidence.py <repo-path> --commit <sha> --diff-files <f> --blame-file <f> --blame-lines 10:20 --output git_regression.json
../.venv/bin/python build_context.py --runtime-evidence runtime_failure.json --git-evidence git_regression.json "question"
../.venv/bin/python generate_hypothesis.py             # -> hypothesis.json
../.venv/bin/python validate_hypothesis.py --runtime-evidence runtime_failure.json --git-evidence git_regression.json
```

The generated `*.json` files are git-ignored.

## Where each idea lives in the application

| POC script | Application module (`apps/api/app/services/`) | What changed |
| --- | --- | --- |
| `repository_scanner.py` | `scanner.py` | Prunes ignored directories instead of walking them; skips symlinks; adds content hashes; only `.ts .tsx .js .jsx .json .md` |
| `extract_declarations.py`, `extract_functions.py`, `inspect_structure.py` | `chunker.py` | Picks the parser per extension (TypeScript, TSX, JavaScript); adds `module` chunks for top-level script code; attaches JSDoc comments; splits Markdown by heading |
| `build_chunks.py` | `chunker.py` + `indexer.py` | File fallback kept; chunks stored in Postgres |
| `embed_chunks.py` | `ollama_client.py` + `indexer.py` | Same embedding text; batched `/api/embed`; vectors in pgvector instead of JSON |
| `search_chunks.py` | `retrieval.py` | pgvector cosine search plus keyword matching; both scores reported |
| `collect_runtime.py` | `command_runner.py` + `stack_trace.py` | Still no shell; rejects shell operators; reduced environment; kills the process group on timeout; parses stack traces |
| `collect_git_evidence.py` | `git_service.py` + `git_evidence.py` | Blame target and suspect commit derived from the stack trace instead of CLI flags |
| `build_context.py` | `context_builder.py` | Same instructions; line-numbered evidence; asks for JSON |
| `generate_hypothesis.py` | `hypothesis.py` | Ollama structured output with a free-text fallback |
| `validate_hypothesis.py` | `validation.py` | Same checks, generalized: the expected identifier comes from the error message (not hardcoded `discountType`) and the expected commit from `git blame` |
