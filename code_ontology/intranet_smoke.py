"""Portable offline semantic-free integration check; never uses configured model credentials."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading

from .config import PROJECT
from .util import now, write_json


@contextmanager
def fixture_server():
    state = {"requests": 0, "stages": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.loads(request["messages"][1]["content"])
            stage = payload["stage"]
            state["requests"] += 1
            state["stages"].append(stage)
            if stage == "merge":
                evidence = sorted({eid for s in payload["candidates"] for c in s["classes"] for eid in c["evidence_ids"]})
            else:
                evidence = sorted({eid for p in payload["packs"] for eid in p["evidence_ids"]})
            if stage in {"discover", "merge"}:
                result = {"classes": [{"id": "fixture", "name": "FixtureConcept", "definition": "Synthetic transport check only",
                                      "evidence_ids": evidence}], "relations": [], "properties": []}
            else:
                classes = payload["schema"]["classes"]
                result = {"entities": [{"id": "fixture", "name": "FixtureEntity", "type_id": classes[0]["id"],
                                        "scope": "example", "basis": "direct", "evidence_ids": evidence}] if classes else [],
                          "relations": [], "attributes": [], "rules": [], "candidates": []}
            response = {"id": "offline-fixture", "object": "chat.completion", "created": 0, "model": "offline-fixture",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(result)}, "finish_reason": "stop"}]}
            body = json.dumps(response).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def run(root: Path):
    from .config import Settings, load_policy
    from .intranet import execute, writer_lock, STEPS
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with fixture_server() as (url, state), tempfile.TemporaryDirectory(prefix="offline-", dir=root) as directory:
        work = Path(directory)
        # Deliberately do not load .env or system model/policy settings for this check.
        import yaml
        from .config import RESOURCE
        policy = yaml.safe_load((RESOURCE / "default.yaml").read_text(encoding="utf-8"))
        policy.update(parser_backend="tree-sitter", views=["ast"], profiles=["general"], discovery_max_packs=0)
        settings = Settings(base_url=url, api_key="offline-fixture-only", model="offline-fixture", retries=0)
        session = {"repo": str(PROJECT / "examples/maintenance"), "out": str(work / "out"),
                   "experiment": "offline-fixture", "view": "ast", "profile": "general"}
        with writer_lock(Path(session["out"])):
            for step in STEPS:
                if execute(step, session, settings, policy):
                    return 1
            before = state["requests"]
            execute("merge-schema", session, settings, policy)
            execute("extract", session, settings, policy)
            assert state["requests"] == before, "Identical merge/extract unexpectedly requested model again"
        report = {"ok": True, "mode": "real-tree-sitter-local-fake-model", "semantic_quality_tested": False,
                  "requests": before, "resume_extra_requests": state["requests"]-before, "ended_at": now()}
        write_json(root / "verification.json", report)
        print(json.dumps(report, indent=2))
        return 0
