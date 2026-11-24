"""C/C++ syntax AST adapter. No resolved references or control-flow edges are inferred."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .joern import normalize
from .parsing import snapshot_parser
from .repository import stage_state, verify_sources
from .util import canonical, digest, now, read_json, write_json

TYPES = {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}
LABELS = {"compound_statement": "BLOCK", "field_declaration": "MEMBER", "declaration": "DECLARATION",
          "return_statement": "RETURN", "call_expression": "CALL", "comment": "COMMENT",
          "if_statement": "CONTROL_STRUCTURE", "while_statement": "CONTROL_STRUCTURE",
          "for_statement": "CONTROL_STRUCTURE", "do_statement": "CONTROL_STRUCTURE",
          "switch_statement": "CONTROL_STRUCTURE", "number_literal": "LITERAL", "string_literal": "LITERAL"}


def parsers():
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_c
        import tree_sitter_cpp
        return {"c": Parser(Language(tree_sitter_c.language())), "cpp": Parser(Language(tree_sitter_cpp.language()))}
    except (ImportError, TypeError, ValueError) as e:
        raise RuntimeError("tree-sitter unavailable/incompatible; activate .venv and run python -m pip install -r requirements.lock.txt") from e


def text(node, data):
    return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""


def method_declarator(node):
    if node.type not in {"function_definition", "declaration", "field_declaration"}:
        return None
    decl = node.child_by_field_name("declarator")
    while decl:
        if decl.type == "function_declarator":
            inner = decl.child_by_field_name("declarator")
            # A function-pointer variable is data, not a method.
            if inner and inner.type in {"parenthesized_declarator", "abstract_parenthesized_declarator"}:
                return None
            return decl
        decl = decl.child_by_field_name("declarator")
    return None


def declarator_name(node, data):
    while node:
        next_node = node.child_by_field_name("declarator")
        if not next_node:
            return text(node, data)
        node = next_node
    return ""


def syntax_errors(root):
    count, samples, stack = 0, [], [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
            if len(samples) < 20:
                samples.append({"type": node.type, "missing": node.is_missing,
                                "start_line": node.start_point.row + 1, "end_line": node.end_point.row + 1})
        if node.has_error:
            stack.extend(node.children)
    return count, samples


def parse(dest: Path, settings, policy, views, force=False):
    if list(views) != ["ast"]:
        raise ValueError("tree-sitter only supports AST")
    manifest = verify_sources(dest)
    info = snapshot_parser(dest)
    raw = dest / "raw" / "tree-sitter"
    marker = raw / "export.json"
    stage_state(dest, "parse", "running", backend="tree-sitter", views=["ast"])
    try:
        marker_key = digest({"snapshot": manifest["snapshot_id"], "parser": manifest["parse_config"]})
        cached = marker.is_file() and read_json(marker).get("key") == marker_key
        cached = cached and all((raw / n).is_file() for n in ("nodes.jsonl", "edges.jsonl", "parse_report.json"))
        if force or not cached:
            engines = parsers()
            raw.mkdir(parents=True, exist_ok=True)
            marker.unlink(missing_ok=True)
            files, node_count, edge_count = [], 0, 0
            with (raw / "nodes.pending.jsonl").open("w", encoding="utf-8") as nodes, (raw / "edges.pending.jsonl").open("w", encoding="utf-8") as edges:
                for record in manifest["files"]:
                    if not record["is_code"]:
                        continue
                    name = record["file"]
                    data = (dest / "source" / name).read_bytes()
                    language = "c" if Path(name).suffix.lower() == ".c" else "cpp"
                    tree = engines[language].parse(data)
                    errors, samples = syntax_errors(tree.root_node)
                    if Path(name).suffix.lower() == ".h" and errors:
                        c_tree = engines["c"].parse(data)
                        c_errors, c_samples = syntax_errors(c_tree.root_node)
                        if c_errors < errors:
                            language, tree, errors, samples = "c", c_tree, c_errors, c_samples
                    files.append({"file": name, "language": language, "syntax_errors": errors,
                                  "error_samples": samples, "bytes": len(data)})
                    stack = [(tree.root_node, None, (), 0)]
                    while stack:
                        node, parent, scope, order = stack.pop()
                        nid = "ts_" + digest([name, node.start_byte, node.end_byte, node.type, order])
                        start = node.start_point.row + 1
                        end = max(start, node.end_point.row + (1 if node.end_point.column else 0))
                        props = {"FILENAME": name, "LINE_NUMBER": start, "LINE_NUMBER_END": end,
                                 "COLUMN_NUMBER": node.start_point.column + 1, "ORDER": order,
                                 "PARSER_NODE_TYPE": node.type, "PARSER_HAS_ERROR": node.has_error,
                                 "PARSER_MISSING": node.is_missing}
                        label = LABELS.get(node.type, node.type.upper())
                        name_node = node.child_by_field_name("name")
                        symbol_name = text(name_node, data)
                        next_scope = scope
                        if node == tree.root_node:
                            label = "FILE"; props.update(NAME=name, FULL_NAME=name)
                        elif node.type == "namespace_definition":
                            next_scope = (*scope, symbol_name or f"<anonymous@{name}:{start}>")
                        if node.type in TYPES:
                            label = "TYPE_DECL"
                            symbol_name = symbol_name or f"<anonymous@{name}:{start}:{node.start_point.column}>"
                            props.update(NAME=symbol_name, FULL_NAME="::".join((*scope, symbol_name)), IS_EXTERNAL=False)
                            next_scope = (*scope, symbol_name)
                        decl = method_declarator(node)
                        if decl:
                            label = "METHOD"
                            symbol_name = declarator_name(decl, data)
                            signature = re.sub(r"\s+", " ", text(decl, data)).strip()
                            props.update(NAME=symbol_name, FULL_NAME="::".join((*scope, symbol_name)),
                                         SIGNATURE=signature, IS_EXTERNAL=False, IS_DEFINITION=node.type == "function_definition")
                            if node.type == "function_definition":
                                next_scope = (*scope, symbol_name)
                        # Bound duplication: the exact complete source remains in the snapshot.
                        code = text(node, data)[:320] if node.end_byte - node.start_byte <= 1280 else data[node.start_byte:node.start_byte + 1280].decode("utf-8", errors="replace")[:320]
                        if label not in {"FILE", "METHOD", "TYPE_DECL"}:
                            props["CODE"] = code
                            if node.end_byte - node.start_byte > len(code.encode("utf-8")):
                                props["CODE_TRUNCATED"] = True
                        if label == "CONTROL_STRUCTURE":
                            props["CONTROL_STRUCTURE_TYPE"] = node.type
                        nodes.write(canonical({"id": nid, "label": label, "properties": props}) + "\n")
                        node_count += 1
                        if parent:
                            edges.write(canonical({"src": parent, "dst": nid, "label": "AST"}) + "\n")
                            edge_count += 1
                        children = node.named_children
                        for i in range(len(children) - 1, -1, -1):
                            stack.append((children[i], nid, next_scope, i))
            for name in ("nodes", "edges"):
                os.replace(raw / f"{name}.pending.jsonl", raw / f"{name}.jsonl")
            report = {"backend": "tree-sitter", "available_views": ["ast"], "parser": info,
                      "files": files, "syntax_errors": sum(f["syntax_errors"] for f in files),
                      "limitations": info["limitations"]}
            write_json(raw / "parse_report.json", report)
            write_json(marker, {"key": marker_key, "backend": "tree-sitter", "created_at": now(),
                                "nodes": node_count, "edges": edge_count, "files": len(files)})
        normalize(dest, ["ast"], raw_directory=raw, parser_info=info)
        report = read_json(raw / "parse_report.json")
        stage_state(dest, "parse", "success", backend="tree-sitter", views=["ast"],
                    counts=read_json(marker), syntax_errors=report["syntax_errors"], cached=bool(cached and not force))
        print(f"tree-sitter AST complete: {len(report['files'])} files, {report['syntax_errors']} syntax errors (see raw/tree-sitter/parse_report.json)", flush=True)
    except Exception as e:
        stage_state(dest, "parse", "failed", backend="tree-sitter", error=str(e))
        raise
