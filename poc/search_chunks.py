import json
import math
import sys
from pathlib import Path

from embed_chunks import create_embedding


def cosine_similarity(
    first_vector: list[float],
    second_vector: list[float],
) -> float:
    dot_product = sum(
        first * second
        for first, second in zip(first_vector, second_vector)
    )

    first_magnitude = math.sqrt(
        sum(value * value for value in first_vector)
    )

    second_magnitude = math.sqrt(
        sum(value * value for value in second_vector)
    )

    if first_magnitude == 0 or second_magnitude == 0:
        return 0.0

    return dot_product / (
        first_magnitude * second_magnitude
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python search_chunks.py "
            '"your debugging question"'
        )
        raise SystemExit(1)

    query = " ".join(sys.argv[1:])
    query_embedding = create_embedding(query)

    chunks = json.loads(
        Path("embedded_chunks.json").read_text(
            encoding="utf-8"
        )
    )

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

    print(f"Query: {query}")
    print()
    print("Top results:")

    for rank, item in enumerate(scored_chunks[:5], start=1):
        chunk = item["chunk"]

        print("=" * 80)
        print(f"Result {rank}")
        print(f"Score: {item['score']:.4f}")
        print(f"Type: {chunk['chunk_type']}")
        print(f"Symbol: {chunk['symbol']}")
        print(f"File: {chunk['file_path']}")
        print(f"Lines: {chunk['start_line']}-{chunk['end_line']}")
        print("-" * 80)
        print(chunk["content"])
        print()


if __name__ == "__main__":
    main()