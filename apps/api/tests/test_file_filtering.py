from pathlib import Path

from app.services.chunker import chunk_file
from app.services.scanner import scan_repository


def write(root: Path, relative: str, content: str = "export const x = 1;\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scanner_filters_by_extension_directory_and_size(tmp_path: Path):
    for relative in [
        "src/a.ts",
        "src/b.tsx",
        "src/c.js",
        "src/d.jsx",
        "package.json",
        "README.md",
        "src/style.css",
        "src/script.py",
        "package-lock.json",
        "node_modules/lib/index.ts",
        ".git/hooks/x.js",
        ".venv/x.js",
        "venv/x.js",
        "dist/out.js",
        "build/out.js",
        "__pycache__/x.js",
        ".next/x.js",
        "coverage/lcov.js",
        "nested/node_modules/y.ts",
    ]:
        write(tmp_path, relative)
    write(tmp_path, "src/huge.ts", "// x\n" * 10_000)
    (tmp_path / "src/binary.ts").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "src/link.ts").symlink_to(tmp_path / "src/a.ts")

    files = scan_repository(tmp_path, max_file_size_bytes=10_000)

    assert [file.path for file in files] == [
        "README.md",
        "package.json",
        "src/a.ts",
        "src/b.tsx",
        "src/c.js",
        "src/d.jsx",
    ]
    assert {file.path: file.language for file in files} == {
        "README.md": "markdown",
        "package.json": "json",
        "src/a.ts": "typescript",
        "src/b.tsx": "tsx",
        "src/c.js": "javascript",
        "src/d.jsx": "javascript",
    }
    assert all(len(file.content_hash) == 64 for file in files)


def test_chunker_uses_language_specific_parsers(tmp_path: Path):
    write(
        tmp_path,
        "src/discount.ts",
        "import { Coupon } from './models';\n"
        "\n"
        "/** Applies a coupon. */\n"
        "export function calculateDiscount(coupon: Coupon): number {\n"
        "  return coupon.value;\n"
        "}\n"
        "\n"
        "export interface Totals { total: number }\n"
        "export type Kind = 'A' | 'B';\n"
        "const helper = (n: number) => n * 2;\n"
        "console.log(helper(2));\n",
    )
    write(tmp_path, "src/App.tsx", "export function App() {\n  return <div>Hello</div>;\n}\n")
    write(tmp_path, "src/legacy.js", "var total = 1;\nfunction* ids() { yield 1; }\nmodule.exports = { total };\n")
    write(tmp_path, "docs/guide.md", "# Guide\nintro\n## Install\nnpm install\n")

    all_chunks = [chunk for file in scan_repository(tmp_path, 100_000) for chunk in chunk_file(file)]
    chunks = {chunk.symbol: chunk for chunk in all_chunks}

    discount = chunks["calculateDiscount"]
    assert (discount.chunk_type, discount.start_line, discount.end_line) == ("function", 3, 6)
    assert discount.content.startswith("/** Applies a coupon. */")
    assert chunks["Totals"].chunk_type == "interface"
    assert chunks["Kind"].chunk_type == "type_alias"
    assert chunks["helper"].chunk_type == "function"
    assert chunks["App"].language == "tsx"
    assert chunks["ids"].chunk_type == "function"
    assert chunks["total"].language == "javascript"
    assert chunks["Install"].chunk_type == "section"
    module_chunks = [chunk for chunk in all_chunks if chunk.chunk_type == "module"]
    assert {chunk.file_path for chunk in module_chunks} >= {"src/discount.ts", "src/legacy.js"}
