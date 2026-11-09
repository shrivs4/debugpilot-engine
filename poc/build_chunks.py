from pathlib import Path
import json
import sys

from repository_scanner import scan_repository
from extract_declarations import extract_declaration_chunks


CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}


def create_file_chunk(
    file_path: Path,
    repository_root: Path,
    content: str,
    language: str,
) -> dict:
    return {
        "chunk_type": "file",
        "symbol": file_path.name,
        "file_path": str(
            file_path.relative_to(repository_root)
        ),
        "language": language,
        "start_line": 1,
        "end_line": len(content.splitlines()),
        "content": content,
    }


def build_repository_chunks(repository_root: Path) -> list[dict]:
    all_chunks = []

    discovered_files = scan_repository(repository_root)

    for file_info in discovered_files:
        file_path = repository_root / file_info["path"]
        content = file_path.read_text(encoding="utf-8")

        if file_path.suffix.lower() in CODE_EXTENSIONS:
            chunks = extract_declaration_chunks(
                file_path=file_path,
                repository_root=repository_root,
            )

            if chunks:
                all_chunks.extend(chunks)
            else:
                all_chunks.append(
                    create_file_chunk(
                        file_path,
                        repository_root,
                        content,
                        file_info["language"],
                    )
                )
        else:
            all_chunks.append(
                create_file_chunk(
                    file_path,
                    repository_root,
                    content,
                    file_info["language"],
                )
            )

    return all_chunks


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python build_chunks.py <repository-root>")
        raise SystemExit(1)

    repository_root = Path(sys.argv[1]).resolve()
    chunks = build_repository_chunks(repository_root)

    output_path = Path("chunks.json")
    output_path.write_text(
        json.dumps(chunks, indent=2),
        encoding="utf-8",
    )

    print(f"Created {len(chunks)} chunks")
    print(f"Output: {output_path.resolve()}")

    chunk_types = {}

    for chunk in chunks:
        chunk_type = chunk["chunk_type"]
        chunk_types[chunk_type] = chunk_types.get(chunk_type, 0) + 1

    print("Chunk types:")

    for chunk_type, count in sorted(chunk_types.items()):
        print(f"  {chunk_type}: {count}")


if __name__ == "__main__":
    main()