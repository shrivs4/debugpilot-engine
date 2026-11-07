import json
import sys
from pathlib import Path
import argparse
from embed_chunks import create_embedding
from search_chunks import cosine_similarity


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--runtime-evidence",
        default="runtime_failure.json",
        help="Runtime evidence JSON file",
    )

    parser.add_argument(
        "--git-evidence",
        default="git_evidence.json",
        help="Git evidence JSON file",
    )

    parser.add_argument(
        "query",
        nargs="+",
        help="Debugging question",
    )

    args = parser.parse_args()

    query = " ".join(args.query)

    chunks = json.loads(Path("embedded_chunks.json").read_text(encoding="utf-8"))

    runtime_evidence = json.loads(
        Path(args.runtime_evidence).read_text(encoding="utf-8")
    )

    git_evidence = json.loads(
        Path(args.git_evidence).read_text(encoding="utf-8")
    )

    git_text = f"""Git history evidence

        Commit summary:
        {git_evidence["commit_summary"]}

        Relevant diff:
        {git_evidence["relevant_diff"]}

        Line history:
        {git_evidence["line_history"]}
        """

    runtime_text = f"""Runtime evidence
    Command: {runtime_evidence["command"]}
    Exit code: {runtime_evidence["exit_code"]}
    Success: {runtime_evidence["success"]}
    Standard output:
    {runtime_evidence["stdout"]}
    Error output and stack trace:
    {runtime_evidence["stderr"]}"""

    query_embedding = create_embedding(query)
    scored_chunks = []

    for chunk in chunks:
        score = cosine_similarity(
            query_embedding,
            chunk["embedding"],
        )

        scored_chunks.append(
            {
                "score": score,
                "chunk": chunk,
            }
        )

    scored_chunks.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    top_results = scored_chunks[:5]

    retrieved_chunks = []

    for item in top_results:
        chunk = item["chunk"]

        retrieved_chunks.append(
            {
                "score": round(item["score"], 4),
                "chunk_type": chunk["chunk_type"],
                "symbol": chunk["symbol"],
                "file_path": chunk["file_path"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "content": chunk["content"],
            }
        )

    evidence_sections = []

    for index, item in enumerate(retrieved_chunks, start=1):
        evidence_sections.append(
            f"""Evidence {index}
File: {item["file_path"]}
Symbol: {item["symbol"]}
Lines: {item["start_line"]}-{item["end_line"]}
Similarity score: {item["score"]}

{item["content"]}"""
        )

    llm_prompt = f"""You are debugging a TypeScript codebase.

        Debugging question:
        {query}

        Use only the source-code, runtime, and Git evidence below.

        {runtime_text}

        {git_text}

        Source-code evidence:
        {chr(10).join(evidence_sections)}

        Produce:
        1. The most likely root-cause hypothesis.
        2. The exact file and line responsible.
        3. The execution path that leads to the failure.
        4. The Git commit that introduced or changed the behavior.
        5. Any missing evidence needed to validate the hypothesis.

        Cite relevant file paths, line numbers, and commit hashes.
        Do not propose a code fix yet.
        Do not claim certainty if the evidence is incomplete.
        """

    context_package = {
        "query": query,
        "runtime_evidence": runtime_evidence,
        "git_evidence": git_evidence,
        "retrieved_chunks": retrieved_chunks,
        "llm_prompt": llm_prompt,
    }

    output_path = Path("context_package.json")

    output_path.write_text(
        json.dumps(
            context_package,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Created: {output_path.resolve()}")
    print(f"Retrieved chunks: {len(retrieved_chunks)}")
    print()
    print(llm_prompt)


if __name__ == "__main__":
    main()
