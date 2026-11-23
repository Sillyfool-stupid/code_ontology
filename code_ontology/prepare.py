from __future__ import annotations

import json
import re
from pathlib import Path

from .joern import EDGE_SETS, database
from .repository import SOURCE_EXTENSIONS, stage_state, verify_sources
from .util import canonical, digest, file_hash, iter_jsonl, read_json, token_bound, write_json, write_jsonl


def chunks(text: str, start: int, budget: int):
    """Split along source lines; oversized single lines use fragments with the same line range."""
    buffer, first, line_number = [], start, start
    size = 0
    for line in text.splitlines(keepends=True):
        # Character fragments preserve Unicode; the byte budget is checked after accumulation.
        fragments = []
        fragment = ""
        for char in line:
            if fragment and token_bound(fragment + char) > budget:
                fragments.append(fragment); fragment = ""
            fragment += char
        if fragment:
            fragments.append(fragment)
        for piece in fragments:
            if buffer and size + token_bound(piece) > budget:
                yield first, line_number - (0 if len(fragments) > 1 else 1), "".join(buffer)
                buffer, size, first = [], 0, line_number
            if not buffer:
                first = line_number
            buffer.append(piece); size += token_bound(piece)
        line_number += 1
    if buffer:
        yield first, max(first, line_number - 1), "".join(buffer)


def resolve_file(filename: str, dest: Path, manifest: dict) -> str | None:
    filename = filename.replace("\\", "/")
    available = {r["file"] for r in manifest["files"]}
    if filename in available:
        return filename
    for root in (str(dest / "source"), manifest["source_dir"], manifest["repo"]):
        prefix = root.replace("\\", "/").rstrip("/") + "/"
        if filename.startswith(prefix) and filename[len(prefix):] in available:
            return filename[len(prefix):]
    return None


def cmake_declarations(text: str):
    names = "add_library|add_executable|target_link_libraries|target_compile_definitions|target_include_directories|add_subdirectory"
    return [{"command": m.group(1).lower(), "arguments_raw": m.group(2).strip(), "resolved": False,
             "line": text.count("\n", 0, m.start()) + 1}
            for m in re.finditer(r"\b(" + names + r")\s*\(([^)]*)\)", text, re.I | re.S)]


def type_end(lines: list[str], start: int) -> int:
    """Recover class/enum body extent when Joern omits LINE_NUMBER_END.

    Ignore strings/comments (including raw strings) so literal braces do not close a class.
    A forward declaration stops at its semicolon. This is only a range helper, not a C++ parser.
    """
    text = "".join(lines[start - 1:])
    tokens = re.compile(r'R"(?P<delim>[^ ()\\\t\r\n]{0,16})\([\s\S]*?\)(?P=delim)"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/|[{};]')
    depth = 0
    for match in tokens.finditer(text):
        token = match.group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return start + text.count("\n", 0, match.end())
        elif token == ";" and depth == 0:
            return start + text.count("\n", 0, match.end())
    return start


def compact_node(node: dict) -> dict:
    result = {**node, "properties": dict(node["properties"])}
    props = result["properties"]
    if node["label"] in {"METHOD", "TYPE_DECL", "FILE"}:
        # The source snippet already supplies the body. Duplicating it in graph properties
        # can consume the whole budget before control-flow/call nodes are included.
        props.pop("CODE", None)
    elif len(str(props.get("CODE", ""))) > 240:
        props["CODE_PREVIEW"] = str(props.pop("CODE"))[:240]
        props["CODE_TRUNCATED"] = True
    return result


def prepare(dest: Path, view: str, policy: dict):
    manifest = verify_sources(dest)
    if not (dest / "normalized" / view / "index.json").exists():
        raise ValueError(f"View {view} not exported; run parse --views {view}")
    key = digest({"source": manifest["snapshot_id"], "view": view,
                  "policy": {k: policy[k] for k in ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")},
                  "graph": read_json(dest / "normalized" / view / "index.json").get("graph_fingerprint"),
                  "implementation": file_hash(Path(__file__)), "format": 1})
    directory = dest / "prepared" / view
    stage_state(dest, f"prepare:{view}", "running")
    try:
        if (directory / "index.json").exists() and read_json(directory / "index.json").get("key") == key:
            stage_state(dest, f"prepare:{view}", "success", cached=True)
            return directory / "packs.jsonl"
        from functools import lru_cache
        @lru_cache(maxsize=8)
        def source(file):
            return (dest / "source" / file).read_text(encoding="utf-8").splitlines(keepends=True)

        files = {r["file"]: r for r in manifest["files"]}
        skipped = []
        count, truncations = 0, 0
        with database(dest) as db:
            def node(nid):
                row = db.execute("SELECT data FROM nodes WHERE id=?", (nid,)).fetchone()
                return json.loads(row[0]) if row else None

            def records():
                nonlocal count, truncations
                symbols = list(iter_jsonl(dest / "normalized" / "symbols.jsonl"))
                symbols.sort(key=lambda s: (str(s.get("FILENAME", "")), int(s.get("LINE_NUMBER") or 0), s["node_id"]))
                units = []
                seen = set()
                for symbol in symbols:
                    file = resolve_file(str(symbol.get("FILENAME", "")), dest, manifest)
                    line = int(symbol.get("LINE_NUMBER") or 0)
                    if not file or not line or symbol.get("IS_EXTERNAL") in (True, "true") or symbol.get("NAME") in {"<global>", "<init>", "<clinit>"}:
                        skipped.append({"node_id": symbol["node_id"], "reason": "external_synthetic_or_unlocated"})
                        continue
                    # All view variants use the same units and source boundaries.
                    descendants = [r[0] for r in db.execute("""WITH RECURSIVE tree(id) AS (
                        SELECT ? UNION SELECT e.dst FROM edges e JOIN tree t ON e.src=t.id WHERE e.label='AST')
                        SELECT id FROM tree""", (symbol["node_id"],))]
                    end = int(symbol.get("LINE_NUMBER_END") or line)
                    if symbol["kind"] == "TYPE_DECL":
                        end = max(end, type_end(source(file), line))
                    if end <= line:
                        for nid in descendants:
                            item = node(nid)
                            end = max(end, int(item["properties"].get("LINE_NUMBER_END") or item["properties"].get("LINE_NUMBER") or line))
                    end = min(end, len(source(file)))
                    line = min(line, end)
                    # Include adjacent comment lines, independently of Joern comment attachment behavior.
                    while line > 1 and source(file)[line - 2].lstrip().startswith(("//", "/*", "*")):
                        line -= 1
                    identity = (file, line, end, symbol["kind"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    units.append((file, line, end, symbol, descendants))
                for file, record in files.items():
                    if not record["is_code"]:
                        units.append((file, 1, len(source(file)), {"symbol_id": "file_" + digest(file), "NAME": file,
                                                                 "kind": "DOCUMENT", "SIGNATURE": ""}, []))
                for file, start, end, symbol, descendants in units:
                    if end < start:
                        continue
                    text = "".join(source(file)[start - 1:end])
                    for part, (a, b, code) in enumerate(chunks(text, start, max(128, policy["pack_tokens"] // 3))):
                        evidence = "ev_" + digest([file, files[file]["snapshot_sha256"], a, b, code])
                        ref = {"evidence_id": evidence, "file": file, "start_line": a, "end_line": b,
                               "source_sha256": files[file]["snapshot_sha256"], "snippet_hash": digest(code, 64)}
                        graph_nodes = []
                        for nid in descendants:
                            n = node(nid); p = n["properties"]
                            nline = int(p.get("LINE_NUMBER") or a)
                            if a <= nline <= b:
                                graph_nodes.append(compact_node(n))
                        # CFG nodes are only nodes participating in CFG (plus method anchor).
                        if view == "cfg":
                            graph_nodes = [n for n in graph_nodes if n["label"] in {"METHOD", "METHOD_RETURN"}
                                           or db.execute("SELECT 1 FROM edges WHERE label='CFG' AND (src=? OR dst=?) LIMIT 1", (n["id"], n["id"])).fetchone()]
                        original_nodes = len(graph_nodes)
                        graph_nodes = graph_nodes[:policy["graph_nodes_per_pack"]]
                        ids = {n["id"] for n in graph_nodes}
                        graph_edges, neighbors = [], []
                        neighbor_seen = set()
                        for n in graph_nodes:
                            for edge in db.execute("SELECT src,dst,label FROM edges WHERE src=?", (n["id"],)):
                                if edge["label"] not in EDGE_SETS[view]:
                                    continue
                                if edge["dst"] in ids:
                                    graph_edges.append(dict(edge))
                                elif view == "cpg" and edge["label"] in {"CALL", "REF", "INHERITS_FROM", "EVAL_TYPE"}:
                                    target = node(edge["dst"])
                                    if target and target["id"] not in neighbor_seen and len(neighbors) < policy["neighbor_limit"]:
                                        neighbors.append({"edge": dict(edge), "target": compact_node(target)})
                                        neighbor_seen.add(target["id"])
                        warning = list(manifest["warnings"])
                        if original_nodes > len(graph_nodes):
                            warning.append(f"graph_node_limit: {original_nodes - len(graph_nodes)} omitted")
                        scope = "test" if any(x in Path(file).parts for x in ("test", "tests")) else "example" if "examples" in Path(manifest["repo"]).parts else "code_definition"
                        pack = {"pack_id": "pack_" + digest([symbol["symbol_id"], evidence, view]), "view": view,
                                "symbol_id": symbol["symbol_id"], "evidence_ids": [evidence], "source_refs": [ref],
                                "source_scope": scope, "kind": symbol["kind"], "name": symbol.get("NAME", ""),
                                "signature": symbol.get("SIGNATURE", ""), "code": code,
                                "context": {"nodes": graph_nodes, "edges": graph_edges, "neighbors": neighbors},
                                "parse_warnings": warning}
                        if Path(file).name == "CMakeLists.txt" or Path(file).suffix == ".cmake":
                            pack["context"]["cmake_declarations"] = cmake_declarations(code)
                        omitted = 0
                        while token_bound(pack) > policy["pack_tokens"] and pack["context"]["nodes"]:
                            removed = pack["context"]["nodes"].pop()
                            pack["context"]["edges"] = [e for e in pack["context"]["edges"] if removed["id"] not in (e["src"], e["dst"])]
                            omitted += 1
                        if token_bound(pack) > policy["pack_tokens"]:
                            pack["context"] = {}
                        if omitted:
                            pack["parse_warnings"].append("input_budget: graph context reduced; full raw graph retained")
                            truncations += 1
                        while token_bound(pack) > policy["pack_tokens"] and pack["context"].get("nodes"):
                            removed = pack["context"]["nodes"].pop()
                            pack["context"]["edges"] = [e for e in pack["context"]["edges"] if removed["id"] not in (e["src"], e["dst"])]
                        if token_bound(pack) > policy["pack_tokens"]:
                            pack["context"] = {}
                        if token_bound(pack) > policy["pack_tokens"]:
                            raise ValueError(f"pack_tokens too small for source and metadata ({file}:{a}, {symbol.get('NAME')}, bytes={token_bound(pack)}, fields={[(k, token_bound(v)) for k,v in pack.items()]}); increase it")
                        count += 1
                        yield pack
            write_jsonl(directory / "packs.jsonl", records())
        write_json(directory / "index.json", {"key": key, "packs": count, "context_truncations": truncations,
                   "skipped_symbols": skipped, "token_estimator": "UTF-8 byte upper bound", "view": view})
        stage_state(dest, f"prepare:{view}", "success", packs=count)
        return directory / "packs.jsonl"
    except Exception as e:
        stage_state(dest, f"prepare:{view}", "failed", error=str(e))
        raise


def evidence_valid(dest: Path, pack: dict) -> bool:
    for ref in pack["source_refs"]:
        path = (dest / "source" / ref["file"]).resolve()
        try:
            path.relative_to((dest / "source").resolve())
        except ValueError:
            return False
        if not path.is_file() or file_hash(path) != ref["source_sha256"]:
            return False
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        snippet = "".join(lines[ref["start_line"] - 1:ref["end_line"]])
        if pack["code"] not in snippet or digest(pack["code"], 64) != ref["snippet_hash"]:
            return False
    return bool(pack["source_refs"])
