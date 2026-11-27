"""Read-only, offline artifact viewer. Does not import the extraction pipeline or LLM."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .util import digest, file_hash, iter_jsonl, read_json, within
from .console import configure_console

ASSETS = Path(__file__).parent / "resources" / "viewer"
KINDS = ("entities", "relations", "attributes", "rules", "candidates", "conflicts")


def record_id(kind: str, row: dict) -> str:
    return kind + ":" + digest(row, 32)


def schema_version_time(row):
    """Compare version instants across old UTC and Beijing timestamp formats."""
    try:
        value = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(hours=8)))
        return value.timestamp()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        return float("inf")


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Output directory not found: {root}")
        self.runs = {}

    def safe(self, path: Path):
        if not within(path, self.root):
            raise ValueError("Artifact resolves outside configured output root")
        return path

    def load(self, path: Path, default=None):
        self.safe(path)
        return read_json(path) if path.is_file() else default

    def listing(self):
        paths = sorted(self.root.rglob("run_manifest.json"))
        # Also accept older completed runs that predate the run manifest.
        paths += sorted(self.root.rglob("metrics.json"))
        outputs = {p.parent for p in paths}
        # Local discovery and interrupted merges need not have final metrics/schema.
        outputs.update(p.parent for p in self.root.rglob("schema") if p.is_dir())
        runs, warnings, discovered = [], [], {}
        for output in outputs:
            try:
                self.safe(output)
                if len(output.parents) < 4 or output.parents[2].name != "runs":
                    continue
                snap = output.parents[3]
                if not (snap / "manifest.json").is_file():
                    continue
                state = self.load(output / "run_manifest.json", {})
                metrics = self.load(output / "metrics.json", {})
                manifest = self.load(snap / "manifest.json", {})
                rel = output.relative_to(self.root).as_posix()
                rid = digest(rel, 32)
                fixture = "fixture" in str(state.get("settings", {}).get("model", "")).lower() or bool(metrics.get("test_mode")) or "fixture" in output.parents[1].name.lower()
                status = state.get("stages", {}).get("execution", {}).get("state", metrics.get("status", "unknown"))
                item = {"id": rid, "path": rel, "snapshot": manifest.get("snapshot_id", snap.name),
                        "repo": Path(manifest.get("repo", "unknown")).name,
                        "experiment": output.parents[1].name, "view": output.parent.name,
                        "profile": output.name, "model": state.get("settings", {}).get("model", "unknown"),
                        "status": status, "fixture": fixture, "metrics": metrics,
                        "created_at": state.get("created_at", manifest.get("created_at", ""))}
                runs.append(item)
                discovered[rid] = (output, snap, item)
            except (ValueError, OSError, KeyError) as error:
                warnings.append({"path": str(output), "error": str(error)[:300]})
        runs.sort(key=lambda r: (r["fixture"], r["status"] != "success", r["experiment"], r["view"], r["profile"]))
        self.runs = discovered
        return {"runs": runs, "warnings": warnings, "read_only": True}

    def locate(self, rid):
        if rid not in self.runs:
            self.listing()
        if rid not in self.runs:
            raise KeyError("Unknown run; refresh experiment list")
        output, snap, item = self.runs[rid]
        self.safe(output); self.safe(snap)
        return output, snap, item

    def detail(self, rid):
        output, snap, item = self.locate(rid)
        context = self.schema_context(rid)
        manifest = self.load(snap / "manifest.json", {})
        prepare = self.load(snap / "prepared" / item["view"] / "index.json", {})
        selection = self.load(output / "schema" / "selection.json", {})
        return {"run": {**item, "metrics": self.load(output / "metrics.json", {})}, **context,
                "stages": self.load(output / "run_manifest.json", {}).get("stages", {}),
                "source": {"file_count": len(manifest.get("files", [])), "warnings": manifest.get("warnings", []),
                           "skipped_count": len(manifest.get("skipped", []))},
                "prepared": {k: prepare.get(k) for k in ("packs", "context_truncations", "token_estimator")},
                "discovery": {"selected": len(selection.get("selected", [])), "unselected": len(selection.get("unselected", [])), "total": selection.get("total")},
                "validation": self.load(output / "validation.json"),
                "evaluation": self.load(output / "evaluation.json"),
                "provenance": self.load(output / "schema" / "provenance.json"),
                "schema_review": self.load(output / "schema/review-status.json", {}),
                "schema_scope": self.load(output / "schema-scope.json", {}),
                "schema_history": self.schema_history(rid),
                "graphs": {v: self.load(snap / "normalized" / v / "index.json") for v in ("ast", "cfg", "cpg")}}

    def schema_history(self, rid):
        output, _, _ = self.locate(rid)
        history = self.safe(output / "schema/history")
        rows = []
        for path in sorted(history.glob("*/manifest.json"), reverse=True):
            try:
                manifest = self.load(path, {})
                for entry in manifest.get("snapshots", []):
                    for relative in entry.get("schemas", []):
                        schema_path = path.parent / relative
                        if not within(schema_path, history) or not schema_path.is_file():
                            continue
                        self.safe(schema_path)
                        rows.append({"path": schema_path.relative_to(history).as_posix(),
                                     "file": schema_path.relative_to(output / "schema").as_posix(),
                                     "attempt": path.parent.name, "stage": manifest["stage"],
                                     "attempt_state": manifest["state"], "state": entry["state"],
                                     "location": entry["location"], "created_at": entry["created_at"]})
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                continue
        # Whitelisted, read-only legacy/checkpoint sources. Each fragment remains
        # separate: concatenating schemas would silently collide local IDs.
        root = self.safe(output / "schema")

        def append_file(path, stage, wrapped=False, collection=False, state="accepted"):
            try:
                if not within(path, root):
                    return
                data = self.load(path)
                values = data.get("schemas", []) if collection else [data.get("schema") if wrapped else data]
                for index, schema in enumerate(values):
                    if not isinstance(schema, dict) or not all(isinstance(schema.get(k), list) for k in ("classes", "relations", "properties")):
                        continue
                    relative = path.relative_to(root).as_posix()
                    rows.append({"path": "artifact:" + relative + (f"#{index}" if collection else ""),
                                 "file": relative, "index": index if collection else None, "wrapped": wrapped,
                                 "stage": stage, "state": state, "attempt_state": "checkpoint",
                                 "location": relative + (f" · 片段 {index + 1}" if collection else ""),
                                 "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone(timedelta(hours=8))).isoformat()})
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                # Running/older outputs can contain unfinished JSON. Other results
                # must remain inspectable; no files or stage states are rewritten.
                return

        for path in sorted((root / "local").glob("*.json")):
            if not path.name.endswith((".task.json", ".candidate.json")):
                append_file(path, "local", wrapped=True)
        if not any(row["stage"] == "local" for row in rows):
            append_file(root / "local-manifest.json", "local", collection=True)
        append_file(root / "module-schemas.json", "merge", collection=True, state="unaligned")
        for path in sorted((root / "merge").glob("*.json")):
            if path.name.endswith(".task.json"):
                continue
            candidate = path.name.endswith(".candidate.json")
            append_file(path, "merge", wrapped=not candidate,
                        state="candidate_not_validated" if candidate else "accepted")
        for path in sorted((root / "rounds").glob("*.json")):
            append_file(path, "merge", collection=True)
        for row in rows:
            if schema_version_time(row) == float("inf"):
                row["created_at"] = datetime.fromtimestamp(
                    self.safe(root / row["file"]).stat().st_mtime,
                    timezone(timedelta(hours=8))).isoformat()
        return sorted(rows, key=lambda row: (schema_version_time(row), row["path"]))

    def schema_snapshot(self, rid, relative):
        from .models import Ontology
        output, _, _ = self.locate(rid)
        entry = next((row for row in self.schema_history(rid) if row["path"] == relative), None)
        if entry is None:
            raise KeyError("Unknown schema history snapshot")
        data = self.load(output / "schema" / entry["file"])
        if entry.get("index") is not None:
            try:
                data = data["schemas"][entry["index"]]
            except (IndexError, TypeError, KeyError) as error:
                raise KeyError("Schema fragment changed; refresh artifacts") from error
        elif entry.get("wrapped"):
            data = data["schema"]
        schema = Ontology.model_validate(data).model_dump()
        return {"schema": schema, "path": relative, "selected_snapshot": relative, "schema_source": entry,
                "schema_record_ids": {kind: {row["id"]: record_id("schema." + kind, row)
                                     for row in schema[kind]} for kind in ("classes", "relations", "properties")}}

    def schema_context(self, rid, include_final=True):
        output, _, _ = self.locate(rid)
        paths = (["final/schema.json"] if include_final else []) + ["schema/schema.json"]
        for relative in paths:
            schema = self.load(output / relative)
            if schema is not None:
                return {"schema": schema, "selected_snapshot": "", "schema_source": {"location": relative, "stage": "current", "state": "current"},
                        "schema_record_ids": {kind: {row["id"]: record_id("schema." + kind, row) for row in schema.get(kind, [])}
                                              for kind in ("classes", "relations", "properties")}}
        rows = sorted(self.schema_history(rid), key=lambda row: (
            row["state"] == "accepted" and row["stage"] != "local" and row["location"] != "input",
            row["state"] == "accepted", schema_version_time(row)), reverse=True)
        for row in rows:
            try:
                return self.schema_snapshot(rid, row["path"])
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return {"schema": {"classes": [], "relations": [], "properties": [], "unresolved": []},
                "selected_snapshot": "", "schema_source": {"stage": "empty", "state": "empty", "location": "暂无可用 Schema"},
                "schema_record_ids": {kind: {} for kind in ("classes", "relations", "properties")}}

    def records(self, rid, kind, offset=0, limit=50, query=""):
        if kind not in KINDS:
            raise ValueError("Unknown record collection")
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("offset >= 0 and limit between 1 and 1000 required")
        output, _, _ = self.locate(rid)
        path = self.safe(output / "final" / f"{kind}.jsonl")
        rows, total = [], 0
        if path.is_file():
            for row in iter_jsonl(path):
                if query and query.casefold() not in json.dumps(row, ensure_ascii=False).casefold():
                    continue
                if offset <= total < offset + limit:
                    rows.append({"record_id": record_id(kind, row), "payload": row})
                total += 1
        return {"rows": rows, "total": total, "offset": offset, "limit": limit}

    def evidence(self, rid, eid):
        output, snap, _ = self.locate(rid)
        path = self.safe(output / "evidence.jsonl")
        ref = next((r for r in iter_jsonl(path) if r.get("evidence_id") == eid), None) if path.is_file() else None
        if ref is None:
            raise KeyError("Evidence ID does not belong to this run")
        source = (snap / "source").resolve()
        file = source / ref["file"]
        if not within(file, source):
            raise ValueError("Evidence source resolves outside snapshot source directory")
        self.safe(file)
        manifest = self.load(snap / "manifest.json", {})
        if ref["file"] not in {r["file"] for r in manifest.get("files", [])}:
            raise ValueError("Evidence file not listed in snapshot")
        all_lines = file.read_text(encoding="utf-8").splitlines()
        start, end = int(ref["start_line"]), int(ref["end_line"])
        if not 1 <= start <= end <= len(all_lines):
            raise ValueError("Evidence line range no longer matches snapshot")
        low, high = max(1, start - 3), min(len(all_lines), max(start, min(end, start + 199)) + 3)
        lines = [{"number": i, "text": all_lines[i - 1][:2000], "highlight": start <= i <= end} for i in range(low, high + 1)]
        return {"ref": ref, "lines": lines, "source_hash_matches": file_hash(file) == ref.get("source_sha256"),
                "truncated": end > start + 199 or any(len(all_lines[i - 1]) > 2000 for i in range(low, high + 1))}

    def artifact(self, rid, name):
        output, snap, item = self.locate(rid)
        allowed = {"manifest": snap / "manifest.json", "normalized": snap / "normalized" / item["view"] / "index.json",
                   "prepared": snap / "prepared" / item["view"] / "index.json",
                   "selection": output / "schema" / "selection.json", "provenance": output / "schema" / "provenance.json",
                   "schema_review": output / "schema/review-report.json", "schema_scope": output / "schema-scope.json",
                   "extraction_selection": output / "extraction" / "selection.json",
                   "metrics": output / "metrics.json", "validation": output / "validation.json", "evaluation": output / "evaluation.json"}
        if name not in allowed:
            raise ValueError("Unknown artifact name")
        return {"name": name, "path": allowed[name].relative_to(snap).as_posix(), "data": self.load(allowed[name])}


def make_server(root: Path, port=8765):
    store = ArtifactStore(root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                url = urlsplit(self.path)
                args = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path.startswith("/api/"):
                    rid = args.get("id", "")
                    if url.path == "/api/runs":
                        data = store.listing()
                    elif url.path == "/api/run":
                        data = store.detail(rid)
                    elif url.path == "/api/records":
                        data = store.records(rid, args.get("kind", "entities"), int(args.get("offset", 0)), int(args.get("limit", 50)), args.get("q", ""))
                    elif url.path == "/api/schema-snapshot":
                        data = store.schema_snapshot(rid, args.get("path", ""))
                    elif url.path == "/api/evidence":
                        data = store.evidence(rid, args.get("evidence_id", ""))
                    elif url.path == "/api/artifact":
                        data = store.artifact(rid, args.get("name", ""))
                    else:
                        raise KeyError("Unknown API")
                    self.send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                else:
                    assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/style.css": ("style.css", "text/css; charset=utf-8")}
                    if url.path not in assets:
                        raise KeyError("Unknown page")
                    name, mime = assets[url.path]
                    self.send((ASSETS / name).read_bytes(), mime)
            except KeyError as error:
                self.send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 404)
            except (ValueError, OSError, TypeError) as error:
                self.send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 400)

        def send(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description="只读本体产物浏览器；不调用模型，不修改流水线产物")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent / "outputs")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.root, args.port)
    print(f"Read-only viewer: http://127.0.0.1:{server.server_port}  (Ctrl+C to stop)", flush=True)
    print(f"Artifacts: {args.root.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
