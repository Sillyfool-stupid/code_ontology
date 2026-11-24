"""Parser selection and truthful capability metadata, shared by CLI and workbench."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .joern import executable
from .util import file_hash, read_json

BACKENDS = ("auto", "joern", "tree-sitter")
TREE_LIMITATIONS = [
    "tree-sitter produces syntax AST only; no CFG, CPG, resolved calls or data flow",
    "Build macros, includes and compilation commands are recorded but not evaluated",
    "Preprocessor branches may coexist in the AST; syntax errors are retained in the parse report",
]


def tree_versions():
    result = {}
    for name in ("tree-sitter", "tree-sitter-c", "tree-sitter-cpp"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "missing"
    return result


def select_backend(policy, settings):
    requested = policy.get("parser_backend", "auto")
    if requested not in BACKENDS:
        raise ValueError("parser_backend must be auto, joern or tree-sitter")
    missing = []
    if requested == "auto":
        for tool in ("c2cpg", "joern"):
            try:
                executable(settings, tool)
            except ValueError:
                missing.append(tool)
    backend = "tree-sitter" if requested == "tree-sitter" or requested == "auto" and missing else "joern"
    result = {"requested": requested, "backend": backend,
              "available_views": ["ast"] if backend == "tree-sitter" else ["ast", "cfg", "cpg"],
              "reason": "Missing Joern tools: " + ", ".join(missing) if missing else "Explicit selection" if requested != "auto" else "Joern tools available",
              "limitations": TREE_LIMITATIONS if backend == "tree-sitter" else []}
    if backend == "tree-sitter":
        result["versions"] = tree_versions()
        result["adapter_hash"] = file_hash(Path(__file__).with_name("treesitter.py"))
    return result


def snapshot_parser(dest):
    manifest = read_json(Path(dest) / "manifest.json")
    return manifest.get("parser", {"backend": "joern", "available_views": ["ast", "cfg", "cpg"], "limitations": []})


def parser_views(info, requested):
    return ["ast"] if info["backend"] == "tree-sitter" else list(requested)


def parse_repository(dest, settings, policy, views, force=False, raw_only=False):
    info = snapshot_parser(dest)
    if info["backend"] == "tree-sitter":
        from .treesitter import parse
        return parse(dest, settings, policy, ["ast"], force, raw_only=raw_only)
    from .joern import parse
    return parse(dest, settings, policy, views, force, raw_only=raw_only)


def normalize_repository(dest, views):
    from .joern import normalize
    from .repository import verify_sources, stage_state
    verify_sources(dest)
    info = snapshot_parser(dest)
    effective = parser_views(info, views)
    raw = Path(dest) / "raw" / ("tree-sitter" if info["backend"] == "tree-sitter" else "cpg")
    if not (raw / "export.json").is_file():
        raise ValueError("No completed raw graph; run parse first")
    stage_state(dest, "normalize", "running")
    normalize(dest, effective, raw_directory=raw, parser_info=info)
    stage_state(dest, "normalize", "success", views=effective)
