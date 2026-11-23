from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any, length: int = 20) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:length]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same-directory atomic replacement; a killed stage cannot leave valid-looking partial JSON.
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSONL: {path}:{line_number}: {e}") from e


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(canonical(row) + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def decode(data: bytes, fallback: str = "gb18030") -> tuple[str, str]:
    for enc in ("utf-8-sig", fallback):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    raise ValueError(f"Cannot decode source as UTF-8 or {fallback}")


def token_bound(value: Any) -> int:
    """Conservative upper bound for byte-level tokenizers, not a token count claim."""
    text = value if isinstance(value, str) else canonical(value)
    return len(text.encode("utf-8"))


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def unique(rows):
    seen = set()
    for row in rows:
        key = canonical(row)
        if key not in seen:
            seen.add(key)
            yield row
