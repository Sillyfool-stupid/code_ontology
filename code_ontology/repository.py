from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from . import __version__
from .config import PROJECT, RESOURCE
from .util import decode, digest, file_hash, now, read_json, within, write_json, write_jsonl, write_text

SOURCE_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"}
TEXT_EXTENSIONS = SOURCE_EXTENSIONS | {".cmake", ".md", ".json", ".yaml", ".yml", ".ini", ".sh", ".bat", ".ps1"}


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) or p in Path(path).parts for p in patterns)


def snapshot(repo: Path, out: Path | None, policy: dict, joern_home: str = "") -> Path:
    repo = repo.resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository directory not found: {repo}")
    root = out.resolve() if out else PROJECT / "outputs" / f"{repo.name}-{digest(str(repo), 8)}"
    if repo == root or within(repo, root):
        raise ValueError("Output must not be the repository directory or its ancestor")
    records, skipped = [], []
    for current, directories, files in os.walk(repo, followlinks=False):
        kept = []
        for name in sorted(directories):
            path = Path(current) / name
            rel = path.relative_to(repo).as_posix()
            if path.is_symlink() or within(path, root) or matches(rel, policy["exclude"]):
                skipped.append({"file": rel + "/", "reason": "excluded_directory"})
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = Path(current) / name
            rel = path.relative_to(repo).as_posix()
            if path.suffix.lower() not in TEXT_EXTENSIONS and name != "CMakeLists.txt":
                continue
            reason = None
            if path.is_symlink() or matches(rel, policy["exclude"]):
                reason = "excluded"
            elif policy["include"] and not matches(rel, policy["include"]):
                reason = "outside_include"
            elif policy["max_files"] and len(records) >= policy["max_files"]:
                reason = "max_files"
            elif path.stat().st_size > policy["max_file_bytes"]:
                reason = "oversize"
            if reason:
                skipped.append({"file": rel, "reason": reason})
                continue
            try:
                data = path.read_bytes()
                text, encoding = decode(data, policy["encoding_fallback"])
                if "\x00" in text:
                    raise ValueError("NUL bytes in text file")
                records.append({"file": rel, "sha256": file_hash(path), "encoding": encoding,
                                "bytes": len(data), "is_code": path.suffix.lower() in SOURCE_EXTENSIONS})
            except (ValueError, OSError) as e:
                skipped.append({"file": rel, "reason": "unreadable", "detail": str(e)})
    if not any(r["is_code"] for r in records):
        raise ValueError("No C/C++ files selected; inspect include/exclude configuration")
    compilation = Path(policy["compile_commands"]) if policy.get("compile_commands") else repo / "compile_commands.json"
    compile_hash = file_hash(compilation) if compilation.is_file() else None
    parser_config = {k: policy[k] for k in ("includes", "defines", "encoding_fallback")}
    # Include installed distribution names: upgrading Joern must invalidate the parse cache.
    libs = Path(joern_home) / "lib"
    parser_config["distribution"] = sorted(p.name for p in libs.glob("*semanticcpg*")) if libs.is_dir() else []
    parser_config["export_script"] = file_hash(RESOURCE / "export.sc")
    parser_config["compile_hash"] = compile_hash
    from .config import Settings
    from .parsing import select_backend
    parser = select_backend(policy, Settings(joern_home=joern_home))
    if parser["backend"] == "tree-sitter":
        # Keep legacy Joern identities, but never mix AST-only caches with a Joern snapshot.
        parser_config["backend"] = "tree-sitter"
        parser_config["versions"] = parser["versions"]
        parser_config["adapter_hash"] = parser["adapter_hash"]
        parser_config.pop("distribution", None)
        parser_config.pop("export_script", None)
    identity = [{"file": r["file"], "sha256": r["sha256"]} for r in records]
    sid = digest({"files": identity, "parse": parser_config, "format": 1})
    dest = root / sid
    if (dest / "manifest.json").is_file():
        existing = read_json(dest / "manifest.json")
        if existing["repo"] != str(repo):
            raise ValueError("Snapshot directory belongs to another repository")
        return dest
    source = dest / "source"
    for row in records:
        original = repo / row["file"]
        if file_hash(original) != row["sha256"]:
            raise ValueError(f"Repository changed while scanning: {row['file']}; retry")
        text, _ = decode(original.read_bytes(), policy["encoding_fallback"])
        write_text(source / row["file"], text)
        row["snapshot_sha256"] = file_hash(source / row["file"])
    warnings = []
    compile_out = None
    if compile_hash:
        commands = json.loads(compilation.read_text(encoding="utf-8-sig"))
        if not isinstance(commands, list):
            raise ValueError("compile_commands.json must contain an array")

        def relocate(text):
            return text.replace(str(repo).replace("\\", "/"), str(source).replace("\\", "/")).replace(str(repo), str(source))

        selected = {r["file"] for r in records if r["is_code"]}
        remapped = []
        for command in commands:
            original = Path(command.get("directory", str(repo))) / command["file"]
            try:
                relative = original.resolve().relative_to(repo).as_posix()
            except ValueError:
                continue
            if relative not in selected:
                continue
            copied = {k: ([relocate(x) for x in v] if isinstance(v, list) else relocate(v) if isinstance(v, str) else v)
                      for k, v in command.items()}
            copied["file"] = str(source / relative)
            # Preserve relative -I semantics with an absolute original directory unless inside repo.
            copied["directory"] = relocate(str(Path(command.get("directory", str(repo))).resolve()))
            remapped.append(copied)
        compile_out = dest / "inputs" / "compile_commands.json"
        write_json(compile_out, remapped)
        if not remapped:
            warnings.append("Compilation database has no selected translation units")
    else:
        warnings.append("No compilation database; build macros and include paths may be incomplete")
    write_json(dest / "manifest.json", {
        "format_version": 1, "tool_version": __version__, "created_at": now(), "snapshot_id": sid,
        "repo": str(repo), "source_dir": str(source), "files": records, "skipped": skipped,
        "parse_config": parser_config, "compile_commands": str(compile_out) if compile_out else None,
        "warnings": warnings + parser["limitations"], "stages": {}, "parser": parser,
    })
    write_json(dest / "policy.json", policy)
    return dest


def stage_state(dest: Path, name: str, state: str, **details):
    path = dest / "manifest.json"
    manifest = read_json(path)
    manifest["stages"][name] = {"state": state, "updated_at": now(), **details}
    write_json(path, manifest)


def verify_sources(dest: Path):
    manifest = read_json(dest / "manifest.json")
    for item in manifest["files"]:
        path = dest / "source" / item["file"]
        if not path.is_file() or file_hash(path) != item["snapshot_sha256"]:
            raise ValueError(f"Snapshot source modified or missing: {item['file']}; create a new snapshot")
    return manifest
