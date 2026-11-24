"""Bounded artifact browsing and disk-backed JSONL offsets for the workbench."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading

from .util import digest, within

MAX_BYTES = 128 * 1024
PRIVATE = {".git", ".venv", ".workbench", "cache", "__pycache__"}


def visible(path: Path):
    return not any(p in PRIVATE or (p.startswith(".env") and p != ".env.example") for p in path.parts)


def checked(root: Path, relative: str):
    root = root.resolve()
    target = (root / relative).resolve()
    if not within(target, root) or not visible(Path(relative)):
        raise ValueError("路径越界或文件不允许浏览")
    # A differently named link must not expose an environment file.
    if not visible(target.relative_to(root)):
        raise ValueError("文件不允许浏览")
    return target


def listing(root: Path, relative="", offset=0, limit=150):
    target = checked(root, relative)
    if not target.is_dir():
        raise ValueError("目录不存在")
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("无效分页参数")
    entries = []
    for child in target.iterdir():
        if child.is_symlink() or not visible(child.relative_to(root)):
            continue
        try:
            entries.append({"name": child.name, "path": child.relative_to(root).as_posix(),
                            "directory": child.is_dir(), "bytes": child.stat().st_size if child.is_file() else None})
        except OSError:
            continue
    entries.sort(key=lambda x: (not x["directory"], x["name"].casefold()))
    return {"entries": entries[offset:offset + limit], "total": len(entries), "offset": offset,
            "parent": Path(relative).parent.as_posix() if relative else "", "path": relative}


class Preview:
    def __init__(self, directory: Path):
        self.dbfile = directory / "preview.sqlite"
        self.lock = threading.Lock()
        with sqlite3.connect(self.dbfile) as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(key TEXT PRIMARY KEY, signature TEXT, rows INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS offsets(key TEXT, row INTEGER, pos INTEGER, size INTEGER, PRIMARY KEY(key,row))")

    def read(self, path: Path, offset=0, limit=20, query=""):
        if offset < 0 or not 1 <= limit <= 100 or len(query) > 200:
            raise ValueError("无效预览参数")
        if not path.is_file():
            raise ValueError("文件不存在")
        if path.suffix.lower() in {".bin", ".sqlite", ".db", ".png", ".zip"}:
            return {"format": "binary", "bytes": path.stat().st_size, "message": "二进制产物，仅显示元数据"}
        if path.suffix.lower() == ".jsonl":
            return self.jsonl(path, offset, limit, query)
        # Text offset is in bytes, not lines. Large JSON is not parsed as one object.
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(MAX_BYTES)
        return {"format": "text", "text": data.decode("utf-8", errors="replace"), "offset": offset,
                "next_offset": offset + len(data), "bytes": path.stat().st_size,
                "truncated": offset + len(data) < path.stat().st_size}

    def jsonl(self, path, offset, limit, query):
        key = digest(str(path.resolve()))
        st = path.stat()
        signature = f"{st.st_size}:{st.st_mtime_ns}"
        with self.lock, sqlite3.connect(self.dbfile) as db:
            cached = db.execute("SELECT signature,rows FROM files WHERE key=?", (key,)).fetchone()
            if not cached or cached[0] != signature:
                db.execute("DELETE FROM offsets WHERE key=?", (key,))
                count = 0
                with path.open("rb") as f:
                    while True:
                        start = f.tell()
                        # Never allocate an unbounded single JSONL record during indexing.
                        line = f.readline(MAX_BYTES + 1)
                        if not line:
                            break
                        has_content = bool(line.strip())
                        while line and not line.endswith(b"\n"):
                            line = f.readline(MAX_BYTES + 1)
                        if has_content:
                            db.execute("INSERT INTO offsets VALUES(?,?,?,?)", (key, count, start, f.tell() - start))
                            count += 1
                after = path.stat()
                if f"{after.st_size}:{after.st_mtime_ns}" != signature:
                    db.rollback()
                    raise ValueError("文件正在更新，请稍后刷新预览")
                db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (key, signature, count))
                db.commit()
            total = db.execute("SELECT rows FROM files WHERE key=?", (key,)).fetchone()[0]
            rows, scanned, used = [], offset, 0
            # Search advances through bounded pages; it never scans the entire file per click.
            candidates = db.execute("SELECT row,pos,size FROM offsets WHERE key=? AND row>=? ORDER BY row LIMIT ?",
                                    (key, offset, 1000 if query else limit))
            with path.open("rb") as f:
                for number, pos, size in candidates:
                    if used >= MAX_BYTES:
                        break
                    f.seek(pos)
                    text = f.read(min(size, MAX_BYTES - used)).decode("utf-8", errors="replace")
                    scanned = number + 1
                    if query and query.casefold() not in text.casefold():
                        continue
                    used += len(text.encode("utf-8"))
                    try:
                        payload = json.loads(text) if size <= MAX_BYTES else text
                    except ValueError:
                        payload = text
                    rows.append({"row": number + 1, "payload": payload, "truncated": size > len(text.encode("utf-8"))})
                    if len(rows) == limit:
                        break
            return {"format": "jsonl", "rows": rows, "total": total, "offset": offset,
                    "next_offset": scanned, "has_more": scanned < total,
                    "search_note": "搜索每次最多检查 1000 条；大记录仅搜索预览片段" if query else ""}
