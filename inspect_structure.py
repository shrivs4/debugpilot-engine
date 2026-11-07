from pathlib import Path
import sys

from tree_sitter_language_pack import get_parser


def inspect_file(file_path: Path) -> None:
    source_bytes = file_path.read_bytes()

    parser = get_parser("typescript")
    tree = parser.parse(source_bytes)

    print(f"\nFile: {file_path}")
    print("-" * 60)

    for node in tree.root_node.named_children:
        print(
            f"{node.type:<30}"
            f"lines {node.start_point.row + 1}-"
            f"{node.end_point.row + 1}"
        )

        for child in node.named_children:
            print(
                f"  └── {child.type:<26}"
                f"lines {child.start_point.row + 1}-"
                f"{child.end_point.row + 1}"
            )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python inspect_structure.py <file> [file...]")
        raise SystemExit(1)

    for file_name in sys.argv[1:]:
        inspect_file(Path(file_name).resolve())


if __name__ == "__main__":
    main()