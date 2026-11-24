from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .config import RESOURCE, Settings
from .repository import stage_state, verify_sources
from .util import digest, file_hash, iter_jsonl, now, read_json, write_json, write_jsonl

EDGE_SETS = {
    "ast": {"AST", "CONDITION"},
    "cfg": {"CFG"},
    "cpg": {"AST", "CONDITION", "CFG", "CALL", "REF", "EVAL_TYPE", "INHERITS_FROM", "SOURCE_FILE",
            "BINDS", "BINDS_TO", "ARGUMENT", "RECEIVER", "ALIAS_OF"},
}
KEEP_PROPS = {"NAME", "FULL_NAME", "METHOD_FULL_NAME", "SIGNATURE", "FILENAME", "CODE", "LINE_NUMBER", "LINE_NUMBER_END",
              "COLUMN_NUMBER", "TYPE_FULL_NAME", "IS_EXTERNAL", "DISPATCH_TYPE", "CONTROL_STRUCTURE_TYPE", "VALUE",
              "ORDER", "ARGUMENT_INDEX", "INHERITS_FROM_TYPE_FULL_NAME", "AST_PARENT_FULL_NAME", "AST_PARENT_TYPE",
              "PARSER_NODE_TYPE", "PARSER_HAS_ERROR", "PARSER_MISSING", "IS_DEFINITION", "CODE_TRUNCATED"}


def executable(settings: Settings, name: str) -> Path:
    suffix = ".bat" if os.name == "nt" else ""
    path = Path(settings.joern_home) / (name + suffix)
    if settings.joern_home and path.is_file():
        return path.resolve()
    found = shutil.which(name + suffix) or shutil.which(name)
    if found:
        return Path(found)
    raise ValueError(f"Cannot find {name}; set JOERN_HOME")


def invoke(args: list[str], cwd: Path, logfile: Path, timeout: int = 600):
    cwd.mkdir(parents=True, exist_ok=True)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    # No shell=True and no user-supplied command templates. Windows .bat requires cmd;
    # reject its metacharacters rather than allowing repository paths to become code.
    command = args
    if os.name == "nt" and Path(args[0]).suffix.lower() in {".bat", ".cmd"}:
        if any(any(c in arg for c in '\r\n&|<>^%!"') for arg in args):
            raise ValueError("Windows batch arguments contain unsupported shell characters")
        # Quoting EVERY argument is essential: cmd otherwise splits key=value parameters
        # before the Scala launcher sees them. The outer quotes belong to /s /c.
        command = 'cmd.exe /d /s /c "' + ' '.join('"' + arg + '"' for arg in args) + '"'
    with logfile.open("w", encoding="utf-8") as log:
        log.write(f"Started: {now()}\n")
        log.flush()
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.kill()
            process.wait()
            raise RuntimeError(f"Joern timeout; see {logfile}")
    if rc:
        raise RuntimeError(f"Joern failed ({rc}); see {logfile}")


def database(dest: Path):
    db = sqlite3.connect(dest / "normalized" / "graph.sqlite")
    db.row_factory = sqlite3.Row
    return db


def normalize(dest: Path, views: list[str], raw_directory: Path | None = None, parser_info: dict | None = None):
    raw_directory = raw_directory or dest / "raw" / "cpg"
    (dest / "normalized").mkdir(exist_ok=True)
    temp = dest / "normalized" / "graph.pending.sqlite"
    if temp.exists():
        temp.unlink()
    db = sqlite3.connect(temp)
    db.executescript("""
        CREATE TABLE nodes(id TEXT PRIMARY KEY, label TEXT, data TEXT);
        CREATE TABLE edges(src TEXT, dst TEXT, label TEXT);
        CREATE INDEX edge_src ON edges(src,label);
        CREATE INDEX edge_dst ON edges(dst,label);
        CREATE INDEX node_label ON nodes(label);
    """)
    import json
    fingerprint = digest({"nodes": file_hash(raw_directory / "nodes.jsonl"),
                          "edges": file_hash(raw_directory / "edges.jsonl"), "normalizer": file_hash(Path(__file__))})
    try:
        for n in iter_jsonl(raw_directory / "nodes.jsonl"):
            clean = {"id": str(n["id"]), "label": n["label"],
                     "properties": {k: v for k, v in n["properties"].items() if k in KEEP_PROPS}}
            db.execute("INSERT INTO nodes VALUES(?,?,?)", (clean["id"], clean["label"], json.dumps(clean, ensure_ascii=False)))
        db.executemany("INSERT INTO edges VALUES(?,?,?)", ((str(e["src"]), str(e["dst"]), e["label"])
                       for e in iter_jsonl(raw_directory / "edges.jsonl")))
        dangling = db.execute("SELECT count(*) FROM edges e LEFT JOIN nodes s ON s.id=e.src LEFT JOIN nodes d ON d.id=e.dst WHERE s.id IS NULL OR d.id IS NULL").fetchone()[0]
        if dangling:
            raise ValueError(f"Graph contains {dangling} dangling edges")
        db.commit()
    finally:
        db.close()
    os.replace(temp, dest / "normalized" / "graph.sqlite")
    with database(dest) as db:
        for view in views:
            labels = sorted(EDGE_SETS[view])
            where = "label IN (" + ",".join("?" for _ in labels) + ")"
            edges = lambda: ({"src": r[0], "dst": r[1], "label": r[2]} for r in db.execute(f"SELECT src,dst,label FROM edges WHERE {where}", labels))
            node_sql = f"SELECT data FROM nodes WHERE label IN ('METHOD','TYPE_DECL','FILE') OR id IN (SELECT src FROM edges WHERE {where} UNION SELECT dst FROM edges WHERE {where})"
            nodes = lambda: (json.loads(r[0]) for r in db.execute(node_sql, labels + labels))
            write_jsonl(dest / "normalized" / view / "nodes.jsonl", nodes())
            write_jsonl(dest / "normalized" / view / "edges.jsonl", edges())
            if view != "cpg":
                # These raw view projections retain original properties, via disk-backed IDs.
                db.execute("DROP TABLE IF EXISTS selected")
                db.execute("CREATE TEMP TABLE selected(id TEXT PRIMARY KEY)")
                db.executemany("INSERT OR IGNORE INTO selected VALUES(?)", ((n["id"],) for n in nodes()))
                write_jsonl(dest / "raw" / view / "nodes.jsonl", (n for n in iter_jsonl(raw_directory / "nodes.jsonl")
                             if db.execute("SELECT 1 FROM selected WHERE id=?", (str(n["id"]),)).fetchone()))
                write_jsonl(dest / "raw" / view / "edges.jsonl", edges())
            write_json(dest / "normalized" / view / "index.json", {
                "edge_labels": labels, "graph_fingerprint": fingerprint,
                "nodes": sum(1 for _ in iter_jsonl(dest / "normalized" / view / "nodes.jsonl")),
                "edges": sum(1 for _ in iter_jsonl(dest / "normalized" / view / "edges.jsonl")),
                "dangling_edges": 0, "note": "Views share source evidence; only graph relations differ",
                **({"parser": parser_info} if parser_info else {}),
            })
        symbols = []
        for row in db.execute("SELECT data FROM nodes WHERE label IN ('METHOD','TYPE_DECL')"):
            n = json.loads(row[0]); p = n["properties"]
            # Declaration/definition use a common semantic key; locations remain distinct.
            key = (n["label"], p.get("FULL_NAME"), p.get("SIGNATURE"))
            symbols.append({"symbol_id": "sym_" + digest(key), "node_id": n["id"], "kind": n["label"], **p})
        write_jsonl(dest / "normalized" / "symbols.jsonl", symbols)


def parse(dest: Path, settings: Settings, policy: dict, views: list[str], force: bool = False):
    manifest = verify_sources(dest)
    stage_state(dest, "parse", "running", views=views)
    try:
        cpg = dest / "raw" / "cpg.bin"
        marker = dest / "raw" / "parse.complete.json"
        timeout = policy["joern_timeout"]
        work = dest / "logs" / "joern-work"
        if force or not (marker.exists() and cpg.is_file()):
            marker.unlink(missing_ok=True)
            if force:
                (dest / "raw" / "cpg" / "export.json").unlink(missing_ok=True)
            cpg.parent.mkdir(parents=True, exist_ok=True)
            pending = dest / "raw" / "cpg.pending.bin"
            pending.unlink(missing_ok=True)
            args = [str(executable(settings, "c2cpg")), str(dest / "source"), "--output", str(pending),
                    "--enable-file-content", "--include-comments", "--log-problems"]
            if manifest.get("compile_commands"):
                args += ["--compilation-database", manifest["compile_commands"]]
            for flag, items in (("--include", policy["includes"]), ("--define", policy["defines"])):
                for item in items:
                    if flag == "--include":
                        include = Path(item)
                        if not include.is_absolute():
                            include = Path(manifest["repo"]) / include
                        try:
                            relative = include.resolve().relative_to(Path(manifest["repo"]).resolve())
                            copied = dest / "source" / relative
                            item = str(copied if copied.is_dir() else include.resolve())
                        except ValueError:
                            item = str(include.resolve())
                    args += [flag, str(item)]
            invoke(args, work, dest / "logs" / "c2cpg.log", timeout)
            if not pending.is_file() or pending.stat().st_size == 0:
                raise RuntimeError("Joern did not produce a CPG")
            os.replace(pending, cpg)
            write_json(marker, {"created_at": now(), "args": args})
            (dest / "raw" / "cpg" / "export.json").unlink(missing_ok=True)
        exported = dest / "raw" / "cpg"
        if force or not (exported / "export.json").exists():
            args = [str(executable(settings, "joern")), "--script", str(RESOURCE / "export.sc"),
                    "--param", f"cpgFile={cpg}", "--param", f"output={exported}"]
            invoke(args, work, dest / "logs" / "export.log", timeout)
            if not (exported / "export.json").exists():
                raise RuntimeError("Export did not complete; inspect logs/export.log (Joern version compatibility)")
        normalize(dest, views)
        stage_state(dest, "parse", "success", views=views, counts=read_json(exported / "export.json"))
    except Exception as e:
        stage_state(dest, "parse", "failed", error=str(e))
        raise
