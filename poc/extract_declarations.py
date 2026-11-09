from pathlib import Path
import json
import sys

from tree_sitter_language_pack import get_parser


DECLARATION_TYPES = {
    "function_declaration": "function",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "class_declaration": "class",
    "enum_declaration": "enum",
    "lexical_declaration": "variable",
}


def walk_tree(node):
    yield node

    for child in node.children:
        yield from walk_tree(child)


def is_top_level_declaration(node) -> bool:
    if node.parent is None:
        return False

    return node.parent.type in {"program", "export_statement"}


def get_symbol_name(node, source_bytes: bytes) -> str:
    name_node = node.child_by_field_name("name")

    if name_node is not None:
        return source_bytes[
            name_node.start_byte:name_node.end_byte
        ].decode("utf-8")

    if node.type == "lexical_declaration":
        for child in node.named_children:
            if child.type != "variable_declarator":
                continue

            variable_name = child.child_by_field_name("name")

            if variable_name is not None:
                return source_bytes[
                    variable_name.start_byte:variable_name.end_byte
                ].decode("utf-8")

    return "<anonymous>"


def get_content_node(node):
    if node.parent is not None and node.parent.type == "export_statement":
        return node.parent

    return node


def extract_declaration_chunks(
    file_path: Path,
    repository_root: Path,
) -> list[dict]:
    source_bytes = file_path.read_bytes()

    parser = get_parser("typescript")
    tree = parser.parse(source_bytes)

    chunks = []

    for node in walk_tree(tree.root_node):
        if node.type not in DECLARATION_TYPES:
            continue

        if not is_top_level_declaration(node):
            continue

        content_node = get_content_node(node)
        content = source_bytes[
            content_node.start_byte:content_node.end_byte
        ].decode("utf-8")

        chunks.append(
            {
                "chunk_type": DECLARATION_TYPES[node.type],
                "symbol": get_symbol_name(node, source_bytes),
                "file_path": str(
                    file_path.relative_to(repository_root)
                ),
                "language": "typescript",
                "start_line": content_node.start_point.row + 1,
                "end_line": content_node.end_point.row + 1,
                "content": content,
            }
        )

    return chunks


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage: python extract_declarations.py "
            "<repository-root> <typescript-file>"
        )
        raise SystemExit(1)

    repository_root = Path(sys.argv[1]).resolve()
    file_path = Path(sys.argv[2]).resolve()

    chunks = extract_declaration_chunks(
        file_path=file_path,
        repository_root=repository_root,
    )

    print(json.dumps(chunks, indent=2))


if __name__ == "__main__":
    main()