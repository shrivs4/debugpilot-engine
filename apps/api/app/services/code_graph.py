"""Relationships between indexed code chunks.

Built at query time from the chunk contents already in the index (no reindex
needed) with the same tree-sitter grammars the chunker uses. For every chunk we
record the functions it calls, the types it uses, the properties it reads and
writes and, from the file's import statements, which module each imported name
comes from. Those facts become edges:

  calls / called_by           a chunk calls a function defined in another chunk
  uses_type / type_used_by    a chunk references a type defined elsewhere
  references / referenced_by  a chunk uses a (non-call) name imported from another file
  shared_property             a chunk reads `obj.prop` and another chunk writes `prop`
                              (only for rare properties, or ones the runtime evidence names)

"Forward" edges point from a chunk to the code that can influence its result,
so walking them from a runtime entry point approximates the execution path,
and walking them from the chunk that computes a mismatched value finds the
code upstream of that value. Nothing here is specific to a repository.
"""

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CodeChunk
from app.services.chunker import FUNCTION_VALUE_TYPES, PARSER_BY_EXTENSION, _parser


FORWARD_EDGES = ("calls", "uses_type", "references", "shared_property")
REVERSE_EDGES = {"calls": "called_by", "uses_type": "type_used_by", "references": "referenced_by", "shared_property": "property_read_by"}
EDGE_WEIGHTS = {
    "calls": 1.0,
    "shared_property": 0.9,
    "uses_type": 0.7,
    "references": 0.7,
    "called_by": 0.6,
    "type_used_by": 0.4,
    "referenced_by": 0.5,
    "property_read_by": 0.6,
}
DEPTH_DECAY = 0.75
SHARED_PROPERTY_MAX_CHUNKS = 6
MAX_GRAPH_CHUNKS = 5000
COMMON_PROPERTIES = frozenset(
    "length prototype constructor then catch finally toString valueOf push pop shift map filter reduce forEach "
    "find some every includes indexOf join split slice keys values entries log error warn info debug id name type "
    "data value default exports".split()
)
MODULE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
MUTATING_METHODS = frozenset("push unshift splice set add delete clear fill sort reverse pop shift".split())
TYPE_KINDS = frozenset({"interface", "type_alias", "enum"})
BUILD_OUTPUT_DIRS = ("dist", "build", "out", "lib")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|__mocks__|fixtures?)(/|$)|\.(test|spec)\.[jt]sx?$")
# Executing code runs what it calls; imported values it references are evaluated too. Types never execute.
EXECUTION_EDGES = ("calls", "references")
DATA_FLOW_EDGES = ("calls", "references", "shared_property")
EXECUTION_DEPTH = 5
RUNNER_EXECUTABLES = {"node", "tsx", "ts-node", "bun", "deno", "npx", "vitest", "jest", "mocha"}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn"}

CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-\s.]+")


def identifier_parts(name: str) -> list[str]:
    """'calculateSubtotal' -> ['calculate', 'subtotal']; 'order_total' -> ['order', 'total']."""
    return [part.lower() for part in CAMEL_BOUNDARY.split(name or "") if part]


def name_matches_field(name: str, field_name: str) -> bool:
    """True when an identifier names a (normalized) runtime field: exact, snake_case or as one camelCase part."""
    if not name or not field_name:
        return False
    parts = identifier_parts(name)
    return "_".join(parts) == field_name or field_name in parts


@dataclass
class ChunkFacts:
    calls: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    defines: set[str] = field(default_factory=set)
    # assigned name -> names used on the right-hand side (local data flow inside the chunk)
    assigns: dict[str, set[str]] = field(default_factory=dict)
    # Properties this chunk writes *data* into: assigned from other values, updated in place
    # (`+=`, `++`) or mutated (`x.items.push(...)`). Literal initializers (`items: []`) are excluded.
    data_writes: set[str] = field(default_factory=set)


@dataclass
class GraphNode:
    id: int
    file_path: str
    symbol: str
    chunk_type: str
    language: str
    start_line: int
    end_line: int
    content: str
    facts: ChunkFacts


@dataclass(frozen=True)
class Edge:
    target: int
    type: str
    via: str


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _key(node) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


def _names_in(node, source: bytes) -> set[str]:
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"identifier", "property_identifier", "shorthand_property_identifier"}:
            names.add(_text(current, source))
        stack.extend(current.named_children)
    return names


def _assigned_name(node, source: bytes) -> str | None:
    if node.type in {"identifier", "property_identifier"}:
        return _text(node, source)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return _text(prop, source) if prop is not None else None
    return None


def extract_facts(content: str, file_path: str) -> ChunkFacts:
    facts = ChunkFacts()
    parser_name = PARSER_BY_EXTENSION.get(PurePosixPath(file_path).suffix.lower())
    if parser_name is None or not content.strip():
        return facts
    source = content.encode("utf-8")
    try:
        root = _parser(parser_name).parse(source).root_node
    except Exception:  # noqa: BLE001 - a chunk that fails to parse simply has no relationships
        return facts

    callee_nodes: set[tuple] = set()
    write_nodes: set[tuple] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        kind = node.type
        if kind in {"call_expression", "new_expression"}:
            target = node.child_by_field_name("function") or node.child_by_field_name("constructor")
            if target is not None:
                callee_nodes.add(_key(target))
                if target.type == "identifier":
                    facts.calls.add(_text(target, source))
                elif target.type == "member_expression":
                    prop = target.child_by_field_name("property")
                    if prop is not None:
                        facts.calls.add(_text(prop, source))
                        # `order.items.push(x)` mutates `items`.
                        owner = target.child_by_field_name("object")
                        if _text(prop, source) in MUTATING_METHODS and owner is not None and owner.type == "member_expression":
                            mutated = _assigned_name(owner, source)
                            if mutated:
                                facts.writes.add(mutated)
                                facts.data_writes.add(mutated)
        elif kind in {"assignment_expression", "augmented_assignment_expression"}:
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            assigned = _assigned_name(left, source) if left is not None else None
            if assigned and right is not None:
                facts.assigns.setdefault(assigned, set()).update(_names_in(right, source))
                if left.type == "member_expression" and (kind == "augmented_assignment_expression" or _names_in(right, source)):
                    facts.data_writes.add(assigned)
            if left is not None:
                write_nodes.add(_key(left))
                if left.type == "identifier":
                    facts.writes.add(_text(left, source))
                if kind == "augmented_assignment_expression" and left.type == "member_expression":
                    prop = left.child_by_field_name("property")
                    if prop is not None:
                        facts.reads.add(_text(prop, source))
        elif kind == "update_expression":
            argument = node.child_by_field_name("argument")
            if argument is not None and argument.type == "member_expression":
                updated = _assigned_name(argument, source)
                if updated:
                    facts.writes.add(updated)
                    facts.data_writes.add(updated)
        elif kind == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is not None and _key(node) not in callee_nodes:
                name = _text(prop, source)
                (facts.writes if _key(node) in write_nodes else facts.reads).add(name)
        elif kind == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if key is not None and value is not None and value.type in FUNCTION_VALUE_TYPES:
                facts.defines.add(_text(key, source))  # `{ name: (args) => ... }` defines a method
            if key is not None and key.type in {"property_identifier", "identifier"}:
                facts.writes.add(_text(key, source))
                if value is not None:
                    facts.assigns.setdefault(_text(key, source), set()).update(_names_in(value, source))
                    if _names_in(value, source):
                        facts.data_writes.add(_text(key, source))
        elif kind == "shorthand_property_identifier":
            name = _text(node, source)
            facts.writes.add(name)
            facts.data_writes.add(name)
            facts.identifiers.add(name)
        elif kind == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is not None and name.type == "identifier":
                facts.writes.add(_text(name, source))
                if value is not None:
                    facts.assigns.setdefault(_text(name, source), set()).update(_names_in(value, source))
        elif kind == "type_identifier":
            facts.types.add(_text(node, source))
        elif kind == "identifier":
            facts.identifiers.add(_text(node, source))
        elif kind in {"function_declaration", "generator_function_declaration", "class_declaration", "method_definition",
                      "interface_declaration", "type_alias_declaration", "enum_declaration", "abstract_class_declaration"}:
            name = node.child_by_field_name("name")
            if name is not None:
                facts.defines.add(_text(name, source))
        stack.extend(node.named_children)
    return facts


def parse_imports(content: str, file_path: str) -> dict[str, str]:
    """Local name -> module specifier, from `import ... from` and `require(...)` statements."""
    parser_name = PARSER_BY_EXTENSION.get(PurePosixPath(file_path).suffix.lower())
    if parser_name is None:
        return {}
    source = content.encode("utf-8")
    try:
        root = _parser(parser_name).parse(source).root_node
    except Exception:  # noqa: BLE001
        return {}

    imports: dict[str, str] = {}
    for statement in root.named_children:
        if statement.type == "import_statement":
            module = statement.child_by_field_name("source")
            if module is None:
                continue
            specifier = _text(module, source).strip("'\"`")
            stack = [child for child in statement.named_children if child.type == "import_clause"]
            while stack:
                node = stack.pop()
                if node.type == "import_specifier":
                    alias = node.child_by_field_name("alias") or node.child_by_field_name("name")
                    if alias is not None:
                        imports[_text(alias, source)] = specifier
                    continue
                if node.type == "identifier":
                    imports[_text(node, source)] = specifier
                stack.extend(node.named_children)
        elif statement.type in {"lexical_declaration", "variable_declaration"}:
            for declarator in statement.named_children:
                value = declarator.child_by_field_name("value") if declarator.type == "variable_declarator" else None
                name = declarator.child_by_field_name("name") if declarator.type == "variable_declarator" else None
                if value is None or name is None or value.type != "call_expression":
                    continue
                function = value.child_by_field_name("function")
                arguments = value.child_by_field_name("arguments")
                if function is None or _text(function, source) != "require" or arguments is None:
                    continue
                strings = [child for child in arguments.named_children if child.type == "string"]
                if not strings:
                    continue
                specifier = _text(strings[0], source).strip("'\"`")
                names = [name] if name.type == "identifier" else [
                    child for child in name.named_children if child.type in {"shorthand_property_identifier_pattern", "identifier"}
                ]
                for item in names:
                    imports[_text(item, source)] = specifier
    return imports


def _normalize(path: PurePosixPath | str) -> str:
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {".", "/"}:
            parts.append(part)
    return "/".join(parts)


def _module_file(target: str, known_files: set[str]) -> str | None:
    stem = target
    for extension in MODULE_EXTENSIONS:
        if target.endswith(extension):
            stem = target[: -len(extension)]
            break
    candidates = [target, *(stem + extension for extension in MODULE_EXTENSIONS)]
    candidates += [f"{target}/index{extension}" for extension in MODULE_EXTENSIONS]
    return next((candidate for candidate in candidates if candidate in known_files), None)


@dataclass(frozen=True)
class PathAliases:
    """`compilerOptions.baseUrl` / `paths` from tsconfig.json (or jsconfig.json)."""

    base_url: str | None = None
    paths: tuple[tuple[str, tuple[str, ...]], ...] = ()


def load_path_aliases(repository_root: Path | None) -> PathAliases:
    if repository_root is None:
        return PathAliases()
    for name in ("tsconfig.json", "jsconfig.json"):
        config_file = repository_root / name
        if not config_file.is_file():
            continue
        text = config_file.read_text(encoding="utf-8", errors="replace")
        # tsconfig allows comments and trailing commas.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"(?m)^\s*//.*$|(?<=[,{\[\s])//.*$", "", text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            options = json.loads(text).get("compilerOptions") or {}
        except (ValueError, AttributeError):
            return PathAliases()
        base_url = options.get("baseUrl")
        paths = options.get("paths") or {}
        return PathAliases(
            base_url=_normalize(base_url) if isinstance(base_url, str) else None,
            paths=tuple(
                (pattern, tuple(target for target in targets if isinstance(target, str)))
                for pattern, targets in paths.items()
                if isinstance(targets, list)
            ),
        )
    return PathAliases()


def resolve_module(importer: str, specifier: str, known_files: set[str], aliases: PathAliases | None = None) -> str | None:
    if specifier.startswith("."):
        return _module_file(_normalize(PurePosixPath(importer).parent / specifier), known_files)
    if aliases is None:
        return None
    base = aliases.base_url or ""
    for pattern, targets in aliases.paths:
        if pattern.endswith("*") and specifier.startswith(pattern[:-1]):
            rest = specifier[len(pattern) - 1:]
        elif pattern == specifier:
            rest = ""
        else:
            continue
        for target in targets:
            found = _module_file(_normalize(PurePosixPath(base) / target.replace("*", rest)), known_files)
            if found:
                return found
    if aliases.base_url is not None:
        return _module_file(_normalize(PurePosixPath(base) / specifier), known_files)
    return None


def parse_reexports(content: str, file_path: str) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Barrel re-exports: ({exported name: (specifier, original name)}, [specifiers of `export *`])."""
    parser_name = PARSER_BY_EXTENSION.get(PurePosixPath(file_path).suffix.lower())
    if parser_name is None:
        return {}, []
    source = content.encode("utf-8")
    try:
        root = _parser(parser_name).parse(source).root_node
    except Exception:  # noqa: BLE001
        return {}, []
    named: dict[str, tuple[str, str]] = {}
    star: list[str] = []
    for statement in root.named_children:
        module = statement.child_by_field_name("source") if statement.type == "export_statement" else None
        if module is None:
            continue
        specifier = _text(module, source).strip("'\"`")
        clauses = [child for child in statement.named_children if child.type == "export_clause"]
        if not clauses:
            star.append(specifier)
            continue
        for specifier_node in clauses[0].named_children:
            if specifier_node.type != "export_specifier":
                continue
            original = specifier_node.child_by_field_name("name")
            alias = specifier_node.child_by_field_name("alias") or original
            if original is not None:
                named[_text(alias, source)] = (specifier, _text(original, source))
    return named, star


class CodeGraph:
    def __init__(self, nodes: list[GraphNode]) -> None:
        self.nodes: dict[int, GraphNode] = {node.id: node for node in nodes}
        self.edges: dict[int, list[Edge]] = {node.id: [] for node in nodes}
        self.definitions: dict[str, list[int]] = {}
        self.readers: dict[str, set[int]] = {}
        self.writers: dict[str, set[int]] = {}
        self.data_writers: dict[str, set[int]] = {}
        for node in nodes:
            for name in node.facts.data_writes:
                self.data_writers.setdefault(name, set()).add(node.id)
        for node in nodes:
            for name in {node.symbol, *node.facts.defines}:
                self.definitions.setdefault(name, []).append(node.id)

    # ----- construction -------------------------------------------------

    def _add(self, source: int, target: int, edge_type: str, via: str) -> None:
        if source == target:
            return
        edge = Edge(target, edge_type, via)
        if edge not in self.edges[source]:
            self.edges[source].append(edge)
        reverse = Edge(source, REVERSE_EDGES[edge_type], via)
        if reverse not in self.edges[target]:
            self.edges[target].append(reverse)

    def _resolve(self, name: str, node: GraphNode, imports: dict[str, str]) -> list[int]:
        candidates = [candidate for candidate in self.definitions.get(name, []) if candidate != node.id]
        if not candidates:
            return []
        imported_file = imports.get(name)
        if imported_file:
            preferred = [candidate for candidate in candidates if self.nodes[candidate].file_path == imported_file]
            if preferred:
                return preferred[:3]
        same_file = [candidate for candidate in candidates if self.nodes[candidate].file_path == node.file_path]
        if same_file:
            return same_file[:3]
        imported_files = set(imports.values())
        from_imports = [candidate for candidate in candidates if self.nodes[candidate].file_path in imported_files]
        if from_imports:
            return from_imports[:3]
        return candidates[:3] if len(candidates) <= 3 else []

    def link(self, file_imports: dict[str, dict[str, str]], focus_properties: set[str]) -> None:
        type_kinds = {"interface", "type_alias", "class", "enum"}
        for node in self.nodes.values():
            imports = file_imports.get(node.file_path, {})
            for name in node.facts.calls:
                for target in self._resolve(name, node, imports):
                    self._add(node.id, target, "calls", name)
            for name in node.facts.types:
                for target in self._resolve(name, node, imports):
                    if self.nodes[target].chunk_type in type_kinds:
                        self._add(node.id, target, "uses_type", name)
            for name in node.facts.identifiers - node.facts.calls:
                if name in imports:
                    for target in self._resolve(name, node, imports):
                        self._add(node.id, target, "references", name)

        writers, readers = self.writers, self.readers
        for node in self.nodes.values():
            for name in node.facts.writes:
                writers.setdefault(name, set()).add(node.id)
            for name in node.facts.reads:
                readers.setdefault(name, set()).add(node.id)
        for name, reading in readers.items():
            writing = writers.get(name, set())
            if not writing or name in COMMON_PROPERTIES:
                continue
            focused = any(name_matches_field(name, item) for item in focus_properties)
            if not focused and len(reading | writing) > SHARED_PROPERTY_MAX_CHUNKS:
                continue
            for reader in reading:
                for writer in writing:
                    if writer != reader:
                        self._add(reader, writer, "shared_property", name)

    def link_properties(self, names: set[str]) -> set[str]:
        """Add reader -> data-writer edges for these properties even when they are common in the repository.

        `link` skips common properties to avoid noise; data-flow analysis calls
        this for the specific properties that feed a mismatched value. Only
        writers that put data into the property count (not literal initializers).
        """
        linked = set()
        for name in names:
            if name in COMMON_PROPERTIES:
                continue
            for reader in self.readers.get(name, set()):
                for writer in self.data_writers.get(name, set()):
                    if writer != reader:
                        self._add(reader, writer, "shared_property", name)
                        linked.add(name)
        return linked

    # ----- queries ------------------------------------------------------

    def chunk_at(self, file_path: str, line: int) -> GraphNode | None:
        matches = [
            node for node in self.nodes.values()
            if node.file_path == file_path and node.start_line <= line <= node.end_line
        ]
        return min(matches, key=lambda node: node.end_line - node.start_line) if matches else None

    def chunks_in_file(self, file_path: str) -> list[GraphNode]:
        return [node for node in self.nodes.values() if node.file_path == file_path]

    def symbol_nodes(self, symbol: str, file_path: str | None = None) -> list[GraphNode]:
        nodes = [self.nodes[node_id] for node_id in self.definitions.get(symbol, [])]
        if file_path:
            in_file = [node for node in nodes if _paths_match(node.file_path, file_path)]
            return in_file or nodes
        return nodes

    def reachable(self, sources: list[int], max_depth: int, edge_types=FORWARD_EDGES) -> dict[int, list[Edge]]:
        """Breadth-first reachability; the value is the edge path from a source to the node."""
        paths: dict[int, list[Edge]] = {source: [] for source in sources if source in self.nodes}
        queue = deque((source, 0) for source in paths)
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.edges.get(current, []):
                if edge.type in edge_types and edge.target not in paths:
                    paths[edge.target] = [*paths[current], edge]
                    queue.append((edge.target, depth + 1))
        return paths

    def connected(self, left: int, right: int) -> Edge | None:
        return next((edge for edge in self.edges.get(left, []) if edge.target == right), None)

    def describe_path(self, source: int, path: list[Edge]) -> str:
        steps = [self.nodes[source].symbol]
        for edge in path:
            steps.append(f"-[{edge.type}:{edge.via}]-> {self.nodes[edge.target].symbol}")
        return " ".join(steps)


def _paths_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def build_graph(
    chunks: list[dict],
    repository_root: Path | None = None,
    focus_properties: set[str] | None = None,
    file_contents: dict[str, str] | None = None,
) -> CodeGraph:
    """`chunks` are dicts with id, file_path, symbol, chunk_type, language, start_line, end_line, content."""
    nodes = [
        GraphNode(
            id=chunk["id"],
            file_path=chunk["file_path"],
            symbol=chunk["symbol"],
            chunk_type=chunk["chunk_type"],
            language=chunk.get("language", ""),
            start_line=chunk["start_line"],
            end_line=chunk["end_line"],
            content=chunk["content"],
            facts=extract_facts(chunk["content"], chunk["file_path"]),
        )
        for chunk in chunks
        if PurePosixPath(chunk["file_path"]).suffix.lower() in PARSER_BY_EXTENSION
    ]
    graph = CodeGraph(nodes)
    known_files = {node.file_path for node in nodes}
    aliases = load_path_aliases(repository_root)
    contents: dict[str, str] = {}
    for file_path in known_files:
        content = (file_contents or {}).get(file_path)
        if content is None and repository_root is not None:
            absolute = (repository_root / file_path).resolve()
            if repository_root.resolve() in absolute.parents and absolute.is_file():
                content = absolute.read_text(encoding="utf-8", errors="replace")
        if content is not None:
            contents[file_path] = content
    reexports = {file_path: parse_reexports(content, file_path) for file_path, content in contents.items()}
    defined_in: dict[str, set[str]] = {}
    for node in nodes:
        for name in {node.symbol, *node.facts.defines}:
            defined_in.setdefault(node.file_path, set()).add(name)

    def through_barrels(file_path: str, name: str, depth: int = 0) -> str:
        """Follow `export ... from` re-exports to the file that actually defines `name`."""
        if depth >= 4 or name in defined_in.get(file_path, set()):
            return file_path
        named, star = reexports.get(file_path, ({}, []))
        if name in named:
            specifier, original = named[name]
            target = resolve_module(file_path, specifier, known_files, aliases)
            return through_barrels(target, original, depth + 1) if target else file_path
        for specifier in star:
            target = resolve_module(file_path, specifier, known_files, aliases)
            if target:
                resolved = through_barrels(target, name, depth + 1)
                if name in defined_in.get(resolved, set()):
                    return resolved
        return file_path

    file_imports: dict[str, dict[str, str]] = {}
    for file_path, content in contents.items():
        resolved = {}
        for name, specifier in parse_imports(content, file_path).items():
            target = resolve_module(file_path, specifier, known_files, aliases)
            if target:
                resolved[name] = through_barrels(target, name)
        file_imports[file_path] = resolved
    graph.link(file_imports, focus_properties or set())
    return graph


def load_chunks(db: Session, repository_id: int, limit: int = MAX_GRAPH_CHUNKS) -> list[dict]:
    rows = db.execute(
        select(
            CodeChunk.id, CodeChunk.file_path, CodeChunk.symbol, CodeChunk.chunk_type, CodeChunk.language,
            CodeChunk.start_line, CodeChunk.end_line, CodeChunk.content,
        )
        .where(CodeChunk.repository_id == repository_id)
        .order_by(CodeChunk.file_path, CodeChunk.start_line)
        .limit(limit)
    ).all()
    return [row._asdict() for row in rows]


def expand_related(
    graph: CodeGraph,
    primary: list[dict],
    max_depth: int,
    limit: int,
) -> list[dict]:
    """Chunks related to the primary evidence, with the relationship that pulled each one in.

    `primary` items need `chunk_id` and `combined_score`. Scores decay with
    depth and edge type, so direct callees and data writers rank above
    distant references.
    """
    primary_ids = {item["chunk_id"] for item in primary}
    best: dict[int, dict] = {}
    for item in primary:
        source = item["chunk_id"]
        if source not in graph.nodes:
            continue
        frontier = [(source, 1.0, [])]
        for depth in range(1, max_depth + 1):
            next_frontier = []
            for current, weight, path in frontier:
                for edge in graph.edges.get(current, []):
                    if edge.target in primary_ids or edge.target == source:
                        continue
                    edge_weight = weight * EDGE_WEIGHTS.get(edge.type, 0.3) * (DEPTH_DECAY ** (depth - 1))
                    score = round(float(item.get("combined_score") or 0) * edge_weight, 4)
                    new_path = [*path, edge]
                    if edge.target not in best or score > best[edge.target]["score"]:
                        best[edge.target] = {"score": score, "source": source, "path": new_path, "depth": depth}
                    next_frontier.append((edge.target, edge_weight, new_path))
            frontier = sorted(next_frontier, key=lambda entry: entry[1], reverse=True)[:25]

    related = []
    for node_id, info in sorted(best.items(), key=lambda entry: entry[1]["score"], reverse=True)[:limit]:
        node = graph.nodes[node_id]
        source = graph.nodes[info["source"]]
        last = info["path"][-1]
        related.append(
            {
                "chunk_id": node.id,
                "score": info["score"],
                "relation": {
                    "type": last.type,
                    "via": last.via,
                    "depth": info["depth"],
                    "from_chunk_id": source.id,
                    "from_symbol": source.symbol,
                    "from_file": source.file_path,
                    "path": graph.describe_path(source.id, info["path"]),
                },
            }
        )
    return related


def computing_nodes(graph: CodeGraph, field_name: str) -> list[GraphNode]:
    """Chunks that compute a runtime field: their symbol names it, or they assign it from other values.

    Assigning only literals (`{ total: 42 }`, e.g. an expected-values object) is not computing it.
    """
    def computes(node: GraphNode) -> bool:
        if name_matches_field(node.symbol, field_name):
            return True
        for name in node.facts.writes:
            if not name_matches_field(name, field_name):
                continue
            used = node.facts.assigns.get(name)
            # Shorthand properties and augmented writes carry no right-hand side here; count them.
            if used is None or used - {name}:
                return True
        return False

    return [node for node in graph.nodes.values() if computes(node)]


def _field_inputs(node: GraphNode, field_name: str) -> set[str]:
    """Names the chunk uses to compute `field_name`: the right-hand sides of its assignments,
    or the whole chunk when the chunk itself is named for the field (e.g. a `computeX` function)."""
    inputs: set[str] = set()
    for assigned, used in node.facts.assigns.items():
        if name_matches_field(assigned, field_name):
            inputs |= {name for name in used if not name_matches_field(name, field_name)}
    if name_matches_field(node.symbol, field_name):
        inputs |= node.facts.identifiers | node.facts.reads | node.facts.calls
    return inputs


def field_dependencies(graph: CodeGraph, analysis: dict, mismatches: list[dict]) -> dict:
    """Order mismatched fields by code dependency: X depends on Y when the code computing X uses Y.

    Root fields have no mismatched upstream field; a field whose upstream
    mismatch deviates by the same factor is marked as propagated.
    """
    fields = [item["field"] for item in mismatches]
    if len(fields) < 2:
        return analysis
    computing = {name: computing_nodes(graph, name) for name in fields}
    computing_ids = {name: {node.id for node in nodes} for name, nodes in computing.items()}
    dependencies: dict[str, list[str]] = {}
    for name in fields:
        inputs: set[str] = set()
        called: set[int] = set()
        for node in computing[name]:
            node_inputs = _field_inputs(node, name)
            inputs |= node_inputs
            called |= {edge.target for edge in graph.edges[node.id] if edge.type == "calls" and edge.via in node_inputs}
        dependencies[name] = [
            other
            for other in fields
            if other != name
            and (any(name_matches_field(token, other) for token in inputs) or called & computing_ids[other])
        ]

    if not any(dependencies.values()):
        return analysis

    ratios = {item["field"]: item.get("ratio") for item in mismatches}
    same_group = {
        (left, right)
        for group in analysis.get("proportional_groups", [])
        for left in group["fields"]
        for right in group["fields"]
    }
    roots = [name for name in fields if not dependencies[name]] or [fields[0]]
    propagated = [
        name
        for name in fields
        if dependencies[name]
        and any((name, upstream) in same_group or ratios.get(name) is None for upstream in dependencies[name])
    ]
    notes = list(analysis.get("notes", []))
    for name in propagated:
        notes.append(
            f"'{name}' is computed from {', '.join(repr(item) for item in dependencies[name])}; its deviation is "
            "consistent with propagation from that upstream value."
        )
    return analysis | {
        "root_fields": roots,
        "propagated_fields": propagated,
        "dependencies": dependencies,
        "method": "code_dependencies",
        "computing_symbols": {
            name: [f"{node.file_path}:{node.start_line} {node.symbol}" for node in computing[name][:5]] for name in fields
        },
        "notes": notes,
    }


def focus_properties(runtime_structured: dict | None) -> set[str]:
    return set((runtime_structured or {}).get("mismatched_fields", []))


def entry_files_from_command(argv: list[str], repository_root: Path) -> list[str]:
    """Repository files a command executes directly, e.g. `node scripts/x.js` or `npm run x` -> its script."""
    if not argv:
        return []

    def inside(candidate: str) -> bool:
        path = (repository_root / candidate).resolve()
        return repository_root.resolve() in path.parents and path.is_file()

    def existing(tokens: list[str]) -> list[str]:
        found = []
        for token in tokens:
            candidate = token.removeprefix("./")
            if PurePosixPath(candidate).suffix.lower() not in (*MODULE_EXTENSIONS, ".py"):
                continue
            if inside(candidate):
                found.append(candidate)
                continue
            # Compiled output (dist/x.js) is not indexed: map it back to the source file.
            parts = PurePosixPath(candidate).parts
            if parts and parts[0] in BUILD_OUTPUT_DIRS:
                stem = str(PurePosixPath(*parts[1:]).with_suffix(""))
                for prefix in ("src/", ""):
                    source = next((f"{prefix}{stem}{ext}" for ext in MODULE_EXTENSIONS if inside(f"{prefix}{stem}{ext}")), None)
                    if source:
                        found.append(source)
                        break
        return found

    executable = PurePosixPath(argv[0]).name
    if executable in PACKAGE_MANAGERS:
        arguments = [argument for argument in argv[1:] if not argument.startswith("-")]
        if not arguments:
            return []
        script = arguments[1] if arguments[0] in {"run", "run-script"} and len(arguments) > 1 else arguments[0]
        package = repository_root / "package.json"
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            return []
        command = scripts.get(script)
        if not isinstance(command, str):
            return []
        files = existing(re.split(r"[\s;&|]+", command))
        nested = re.search(r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?([\w:.\-]+)", command)
        if not files and nested and nested.group(1) != script and isinstance(scripts.get(nested.group(1)), str):
            files = existing(re.split(r"[\s;&|]+", scripts[nested.group(1)]))
        return files
    if executable in RUNNER_EXECUTABLES or executable.startswith("python"):
        return existing(argv[1:])
    return existing(argv[:1])


def suspect_node(graph: CodeGraph, file_path: str | None, line: int | None, symbol: str | None) -> GraphNode | None:
    if file_path and line:
        candidates = [node for node in graph.nodes.values() if _paths_match(node.file_path, file_path)
                      and node.start_line <= line <= node.end_line]
        if candidates:
            return min(candidates, key=lambda node: node.end_line - node.start_line)
    if symbol:
        nodes = graph.symbol_nodes(symbol, file_path)
        if nodes:
            return nodes[0]
    return None



def field_influence(graph: CodeGraph, field_name: str, max_depth: int = 3) -> dict[int, str]:
    """Chunks that can change the value of a runtime field, with a short explanation each.

    Seeds are the chunks that compute the field. A chunk named for the field
    (e.g. `computeX`) is followed completely; for a chunk that only assigns the
    field (`x = f(a)`), only the calls and properties on the right-hand side
    are followed, so unrelated work in the same function is not included.
    """
    return {node_id: detail for node_id, (detail, _) in field_influence_depths(graph, field_name, max_depth).items()}


def field_influence_depths(
    graph: CodeGraph, field_name: str, max_depth: int = 3, edge_types: tuple[str, ...] = FORWARD_EDGES
) -> dict[int, tuple[str, int]]:
    """`field_influence` plus the number of hops from the code that computes the field."""
    influence: dict[int, tuple[str, int]] = {}
    seeds: list[int] = []
    for node in computing_nodes(graph, field_name):
        influence[node.id] = (f"{node.symbol} computes {field_name}", 0)
        if name_matches_field(node.symbol, field_name):
            seeds.append(node.id)
            continue
        inputs = _field_inputs(node, field_name)
        for edge in graph.edges[node.id]:
            if edge.type in edge_types and edge.via in inputs and edge.target not in influence:
                influence[edge.target] = (
                    f"{node.symbol} computes {field_name} using {edge.via} ({graph.nodes[edge.target].symbol})", 0
                )
                seeds.append(edge.target)
    for seed in seeds:
        for node_id, path in graph.reachable([seed], max_depth, edge_types).items():
            if node_id not in influence or len(path) < influence[node_id][1]:
                if node_id in influence and influence[node_id][1] == 0:
                    continue
                influence[node_id] = (f"{graph.describe_path(seed, path)} (feeds {field_name})", len(path))
    return influence


def computation_inputs(graph: CodeGraph, field_name: str, max_depth: int = 3) -> tuple[set[int], set[str]]:
    """The code that computes a field through calls (no data hops), and the properties it reads to do so.

    For a chunk that only assigns the field (`x = f(order.items)`), only the
    properties on the right-hand side count, plus everything the called
    functions read; a chunk named for the field contributes all its reads.
    """
    core: set[int] = set()
    properties: set[str] = set()
    for node in computing_nodes(graph, field_name):
        core.add(node.id)
        if name_matches_field(node.symbol, field_name):
            properties |= node.facts.reads
            callees = [edge.target for edge in graph.edges[node.id] if edge.type == "calls"]
        else:
            inputs = _field_inputs(node, field_name)
            properties |= node.facts.reads & inputs
            callees = [edge.target for edge in graph.edges[node.id] if edge.type == "calls" and edge.via in inputs]
        for callee in graph.reachable(callees, max_depth, ("calls",)):
            if graph.nodes[callee].chunk_type not in TYPE_KINDS:
                core.add(callee)
                properties |= graph.nodes[callee].facts.reads
    return core, properties


def computation_core(graph: CodeGraph, field_name: str, max_depth: int = 3) -> set[int]:
    return computation_inputs(graph, field_name, max_depth)[0]


def link_field_data_flow(graph: CodeGraph, fields: list[str]) -> set[str]:
    """Link the properties that feed mismatched fields, so their upstream writers become reachable.

    Collects the properties read by the code that computes each field (the
    computing chunk and the functions it calls) and links readers to writers
    for exactly those properties, however common they are in the repository.
    Writers' own inputs are not followed further, to keep the expansion tight.
    """
    properties: set[str] = set()
    for field_name in fields:
        properties |= computation_inputs(graph, field_name)[1]
    return graph.link_properties({name for name in properties if name in graph.data_writers})


def is_test_path(file_path: str) -> bool:
    return bool(TEST_PATH.search(file_path))


def trace_issue_flow(graph: CodeGraph, entry_ids: list[int], analysis: dict | None, mismatches: list[dict], limit: int | None) -> list[dict]:
    """Rank chunks on the executed path and in the data flow of the mismatched values.

    Each candidate gets a `kind` and recorded reasons:
      upstream_writer  writes data that the root field's computation reads     +0.5 +0.25
      computes_root    computes a root mismatched field (or is called to)      +0.5
      feeds_root       reaches a root field through more data/call hops        +0.2..0.4
      downstream       only influences propagated fields (a likely symptom)    +0.15
      executed         directly called from the reproduction entry module      +0.3
                       (reached only through references: +0.1; the entry module itself: +0.1)
    Type declarations are skipped (they never execute or write data) and test or
    fixture code is down-weighted (x0.3). Without mismatched fields only the
    execution path is used, closest steps first.
    """
    analysis = analysis or {}
    fields = [item["field"] for item in mismatches]
    root_fields = [name for name in (analysis.get("root_fields") or fields[:1]) if name in fields]
    other_fields = [name for name in fields if name not in root_fields]
    entry_set = set(entry_ids)

    # node -> (entry it was reached from, edge path), keeping the shortest path; calls-only paths first.
    executed: dict[int, tuple[int, list[Edge], bool]] = {}
    for edge_types, direct in ((("calls",), True), (EXECUTION_EDGES, False)):
        for entry in entry_ids:
            for node_id, path in graph.reachable([entry], EXECUTION_DEPTH, edge_types).items():
                if node_id not in executed:
                    executed[node_id] = (entry, path, direct)

    root_influence = {name: field_influence_depths(graph, name, 4, DATA_FLOW_EDGES) for name in root_fields}
    other_influence = {name: field_influence_depths(graph, name, 4, DATA_FLOW_EDGES) for name in other_fields}
    root_inputs = {name: computation_inputs(graph, name) for name in root_fields}

    candidates = set(executed)
    for influence in [*root_influence.values(), *other_influence.values()]:
        candidates |= set(influence)

    ranked = []
    for node_id in candidates:
        node = graph.nodes[node_id]
        if node.chunk_type in TYPE_KINDS:
            continue
        score, reasons, kind = 0.0, [], "executed"
        roots = [name for name, influence in root_influence.items() if node_id in influence]
        if roots:
            computes = [name for name in fields if node_id in root_inputs.get(name, (set(), set()))[0]]
            written = sorted({
                prop for name in roots for prop in node.facts.data_writes & root_inputs[name][1]
            })
            if written and not computes:
                kind = "upstream_writer"
                score += 0.75
                reasons.append(
                    f"upstream writer: writes {', '.join(written)}, which the computation of "
                    f"{', '.join(roots)} reads"
                )
            elif computes:
                kind = "computes_root"
                score += 0.5
            else:
                kind = "feeds_root"
                hops = min(root_influence[name][node_id][1] for name in roots)
                score += max(0.2, 0.5 - 0.1 * max(1, hops))
            reasons.extend(f"feeds root field {name}: {root_influence[name][node_id][0]}" for name in roots)
        else:
            downstream = [name for name, influence in other_influence.items() if node_id in influence]
            if downstream:
                kind = "downstream"
                score += 0.15
                reasons.append(
                    f"affects only downstream field(s) {', '.join(downstream)}, which follow from "
                    f"{', '.join(root_fields) or 'an upstream value'}; likely a symptom"
                )
        if node_id in executed:
            source, path, direct = executed[node_id]
            if node_id in entry_set:
                score += 0.1
                reasons.append("is part of the reproduction entry module")
            elif mismatches:
                score += 0.3 if direct else 0.1
            else:
                score += (0.4 if direct else 0.2) / (1 + len(path))
            if path:
                label = "direct call path" if direct else "execution path"
                reasons.append(f"{label} from the reproduction entry module: {graph.describe_path(source, path)}")
        if is_test_path(node.file_path):
            score *= 0.3
            reasons.append("test or fixture code (down-weighted)")
        if score > 0:
            ranked.append({
                "chunk_id": node_id,
                "score": round(score, 4),
                "kind": kind,
                "executed": node_id in executed and executed[node_id][2],
                "test": is_test_path(node.file_path),
                "reasons": reasons,
            })
    ranked.sort(key=lambda item: (-item["score"], graph.nodes[item["chunk_id"]].file_path, graph.nodes[item["chunk_id"]].start_line))
    return ranked if limit is None else ranked[:limit]
