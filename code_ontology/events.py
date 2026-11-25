"""Versioned execution events. One process owns each append-only event stream."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import re
import sys
import threading
import time
import uuid

from .util import canonical, now, write_json

_lock = threading.RLock()
_sequence = 0
_identity = None
_scope = None
_ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redact(value, secret=""):
    text = _ansi.sub("", str(value)).replace("\r", "")
    for token in (secret, os.environ.get("OPENAI_API_KEY", "")):
        if token and token != "unused":
            text = text.replace(token, "<redacted>")
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1<redacted>@", text)
    text = re.sub(r"(?i)(https?://[^\s?\"'<>]+)\?[^\s\"'<>]+", r"\1?<redacted>", text)
    text = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',}]+", r"\1<redacted>", text)
    return text


def emit(kind, *, stage="", message="", level="info", **data):
    global _sequence, _identity
    context = _scope or {"execution_id": os.environ.get("CO_EXECUTION_ID", ""),
                         "experiment": os.environ.get("CO_EVENT_EXPERIMENT", ""),
                         "stage": os.environ.get("CO_EVENT_STAGE", ""),
                         "file": os.environ.get("CO_EVENT_FILE", "")}
    if not context.get("execution_id"):
        return None
    with _lock:
        if _identity != context["execution_id"]:
            _identity, _sequence = context["execution_id"], 0
        _sequence += 1
        row = {"version": 1, "execution_id": _identity, "experiment": context["experiment"],
               "seq": _sequence, "time": now(), "stage": stage or context["stage"],
               "kind": kind, "level": level, "message": message, **data}
        # Scrub before either the file or stdout sees the serialized event.
        def clean(value):
            if isinstance(value, str):
                return redact(value, context.get("secret", ""))
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        row = clean(row)
        line = canonical(row)
        if context.get("file"):
            path = Path(context["file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        # print() writes its newline separately. Another worker's normal log can
        # otherwise land inside the protocol line and break the parent parser.
        sys.stdout.write("\nCO_EVENT " + line + "\n")
        sys.stdout.flush()
        return row


@contextmanager
def execution(root, experiment, stage, secret=""):
    global _scope
    directory = Path(root) / "executions" / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:8])
    metadata = {"execution_id": directory.name, "experiment": experiment, "stage": stage,
                "state": "running", "started_at": now(), "events": str(directory / "events.jsonl")}
    write_json(directory / "execution.json", metadata)
    previous = _scope
    _scope = {**metadata, "file": metadata["events"], "secret": secret}
    started = time.monotonic()
    try:
        emit("stage_started", message="阶段开始")
        yield metadata
    except BaseException as error:
        metadata.update(state="interrupted" if isinstance(error, KeyboardInterrupt) else "failed")
        emit("stage_ended", state=metadata["state"], level="error", message=redact(error, secret))
        raise
    finally:
        metadata.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
        write_json(directory / "execution.json", metadata)
        _scope = previous


def read_events(path, offset=0, level="", stage="", query=""):
    if offset < 0:
        raise ValueError("Invalid event offset")
    path = Path(path)
    rows = []
    if not path.is_file():
        return {"rows": [], "next_offset": 0, "has_more": False}
    with path.open("rb") as handle:
        handle.seek(offset)
        consumed = 0
        while consumed < 65536 and len(rows) < 100:
            position = handle.tell()
            line = handle.readline(65537)
            if not line or not line.endswith(b"\n"):
                handle.seek(position)
                break
            consumed += len(line)
            row = json.loads(line)
            if level and row.get("level") != level or stage and row.get("stage") != stage:
                continue
            if query and query.casefold() not in line.decode("utf-8").casefold():
                continue
            rows.append(row)
        next_offset = handle.tell()
    return {"rows": rows, "next_offset": next_offset, "has_more": next_offset < path.stat().st_size}
