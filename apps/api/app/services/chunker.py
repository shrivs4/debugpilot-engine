"""AST-aware chunking.

Derived from poc/extract_declarations.py and poc/build_chunks.py. Changes:

* the parser is chosen per extension (TypeScript, TSX, JavaScript) instead of
  always using TypeScript;
* top-level statements that are not declarations (script bodies, calls,
  `module.exports = ...`) are grouped into `module` chunks, so stack frames in
  script files such as `reproduceLegacyBug.ts` still map to a chunk;
* leading comments (JSDoc) are attached to the declaration they precede;
* const/let bound to arrow functions are labelled `function`;
* Markdown is split by heading and JSON / long files by line windows.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from tree_sitter_language_pack import get_parser

from app.services.scanner import ScannedFile


PARSER_BY_EXTENSION = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
}

DECLARATION_TYPES = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "enum_declaration": "enum",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
}

FUNCTION_VALUE_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}

TEXT_WINDOW_LINES = 120


@dataclass(frozen=True)
class Chunk:
    chunk_type: str
    symbol: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    content: str


@lru_cache
def _parser(name: str):
    return get_parser(name)


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _declaration_of(node):
    """Return the declaration node for a top-level statement, unwrapping `export`."""
    if node.type in DECLARATION_TYPES:
        return node
    if node.type == "export_statement":
        for child in node.named_children:
            if child.type in DECLARATION_TYPES:
                return child
    return None


def _symbol_and_type(declaration, source: bytes) -> tuple[str, str]:
    chunk_type = DECLARATION_TYPES[declaration.type]

    name_node = declaration.child_by_field_name("name")
    if name_node is not None:
        return _text(name_node, source), chunk_type

    if declaration.type in {"lexical_declaration", "variable_declaration"}:
        for child in declaration.named_children:
            if child.type != "variable_declarator":
                continue
            variable_name = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if value is not None and value.type in FUNCTION_VALUE_TYPES:
                chunk_type = "function"
            if variable_name is not None:
                return _text(variable_name, source), chunk_type

    return "<anonymous>", chunk_type


def chunk_code(file_path: str, content: str, language: str) -> list[Chunk]:
    parser_name = PARSER_BY_EXTENSION[PurePosixPath(file_path).suffix.lower()]
    source = content.encode("utf-8")
    tree = _parser(parser_name).parse(source)

    chunks: list[Chunk] = []
    module_run: list = []
    pending_comments: list = []

    def make_chunk(chunk_type: str, symbol: str, first, last) -> Chunk:
        return Chunk(
            chunk_type=chunk_type,
            symbol=symbol,
            file_path=file_path,
            language=language,
            start_line=first.start_point.row + 1,
            end_line=last.end_point.row + 1,
            content=source[first.start_byte:last.end_byte].decode("utf-8", errors="replace"),
        )

    def flush_module_run() -> None:
        # A run made only of comments carries no executable context; drop it.
        if any(node.type != "comment" for node in module_run):
            chunks.append(make_chunk("module", "<module>", module_run[0], module_run[-1]))
        module_run.clear()

    for node in tree.root_node.named_children:
        if node.type == "comment":
            pending_comments.append(node)
            continue

        if node.type == "import_statement":
            pending_comments.clear()
            flush_module_run()
            continue

        declaration = _declaration_of(node)
        if declaration is not None:
            flush_module_run()
            symbol, chunk_type = _symbol_and_type(declaration, source)
            first = pending_comments[0] if pending_comments else node
            chunks.append(make_chunk(chunk_type, symbol, first, node))
            pending_comments.clear()
            continue

        module_run.extend(pending_comments)
        pending_comments.clear()
        module_run.append(node)

    flush_module_run()

    if not chunks and content.strip():
        return chunk_text(file_path, content, language, chunk_type="file")
    return chunks


def _line_windows(lines: list[str], offset: int, size: int) -> list[tuple[int, int, str]]:
    windows = []
    for start in range(0, len(lines), size):
        window = lines[start:start + size]
        windows.append((offset + start + 1, offset + start + len(window), "\n".join(window)))
    return windows


def chunk_text(file_path: str, content: str, language: str, chunk_type: str = "file") -> list[Chunk]:
    lines = content.splitlines()
    if not lines:
        return []

    file_name = PurePosixPath(file_path).name
    windows = _line_windows(lines, 0, TEXT_WINDOW_LINES)
    multiple = len(windows) > 1

    return [
        Chunk(
            chunk_type=f"{chunk_type}_part" if multiple else chunk_type,
            symbol=f"{file_name} (lines {start}-{end})" if multiple else file_name,
            file_path=file_path,
            language=language,
            start_line=start,
            end_line=end,
            content=text,
        )
        for start, end, text in windows
    ]


def chunk_markdown(file_path: str, content: str) -> list[Chunk]:
    lines = content.splitlines()
    sections: list[tuple[int, str]] = []  # (start index, heading)
    in_fence = False

    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                sections.append((index, heading))

    if not sections or sections[0][0] != 0:
        sections.insert(0, (0, PurePosixPath(file_path).name))

    chunks: list[Chunk] = []
    for position, (start, heading) in enumerate(sections):
        end = sections[position + 1][0] if position + 1 < len(sections) else len(lines)
        section_lines = lines[start:end]
        if not "\n".join(section_lines).strip():
            continue
        for window_start, window_end, text in _line_windows(section_lines, start, TEXT_WINDOW_LINES):
            chunks.append(
                Chunk(
                    chunk_type="section",
                    symbol=heading,
                    file_path=file_path,
                    language="markdown",
                    start_line=window_start,
                    end_line=window_end,
                    content=text,
                )
            )
    return chunks


def chunk_file(scanned: ScannedFile) -> list[Chunk]:
    suffix = PurePosixPath(scanned.path).suffix.lower()
    if suffix in PARSER_BY_EXTENSION:
        return chunk_code(scanned.path, scanned.content, scanned.language)
    if suffix == ".md":
        return chunk_markdown(scanned.path, scanned.content)
    return chunk_text(scanned.path, scanned.content, scanned.language)
