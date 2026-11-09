from pathlib import Path
import json
import sys

from tree_sitter_language_pack import get_parser


def walk_tree(node):
    yield node

    for child in node.children:
        yield from walk_tree(child)


def extract_function_chunks(file_path: Path) -> list[dict]:
    source_bytes = file_path.read_bytes()

    parser = get_parser("typescript")
    tree = parser.parse(source_bytes)

    chunks = []

    for node in walk_tree(tree.root_node):
        if node.type != "function_declaration":
            continue

        name_node = node.child_by_field_name("name")
        symbol_name = (
            source_bytes[name_node.start_byte:name_node.end_byte]
            .decode("utf-8")
            if name_node
            else "<anonymous>"
        )

        content = source_bytes[
            node.start_byte:node.end_byte
        ].decode("utf-8")

        chunks.append(
            {
                "chunk_type": "function",
                "symbol": symbol_name,
                "file_path": str(file_path),
                "language": "typescript",
                "start_line": node.start_point.row + 1,
                "end_line": node.end_point.row + 1,
                "content": content,
            }
        )

    return chunks


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python extract_functions.py <typescript-file>")
        raise SystemExit(1)

    file_path = Path(sys.argv[1]).resolve()
    chunks = extract_function_chunks(file_path)

    print(json.dumps(chunks, indent=2))


if __name__ == "__main__":
    main()