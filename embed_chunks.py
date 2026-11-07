from pathlib import Path
import json
import urllib.request


EMBEDDING_MODEL = "nomic-embed-text"
OLLAMA_EMBEDDING_URL = "http://localhost:11434/api/embeddings"


def create_embedding(text: str) -> list[float]:
    request_body = {
        "model": EMBEDDING_MODEL,
        "prompt": text,
    }

    request_data = json.dumps(request_body).encode("utf-8")

    request = urllib.request.Request(
        OLLAMA_EMBEDDING_URL,
        data=request_data,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request) as response:
        response_body = response.read().decode("utf-8")

    response_data = json.loads(response_body)
    return response_data["embedding"]


def build_embedding_text(chunk: dict) -> str:
    return (
        f"File: {chunk['file_path']}\n"
        f"Symbol: {chunk['symbol']}\n"
        f"Chunk type: {chunk['chunk_type']}\n"
        f"Language: {chunk['language']}\n\n"
        f"Code or documentation:\n"
        f"{chunk['content']}"
    )


def main() -> None:
    input_path = Path("chunks.json")
    output_path = Path("embedded_chunks.json")

    chunks = json.loads(
        input_path.read_text(encoding="utf-8")
    )

    embedded_chunks = []

    for index, chunk in enumerate(chunks, start=1):
        embedding_text = build_embedding_text(chunk)
        embedding = create_embedding(embedding_text)

        embedded_chunk = {
            **chunk,
            "chunk_id": index,
            "embedding": embedding,
        }

        embedded_chunks.append(embedded_chunk)

        print(
            f"Embedded {index}/{len(chunks)}: "
            f"{chunk['file_path']} "
            f"({chunk['symbol']})"
        )

    output_path.write_text(
        json.dumps(embedded_chunks),
        encoding="utf-8",
    )

    print()
    print(f"Created: {output_path.resolve()}")
    print(f"Chunks embedded: {len(embedded_chunks)}")
    print(f"Embedding dimensions: {len(embedded_chunks[0]['embedding'])}")


if __name__ == "__main__":
    main()