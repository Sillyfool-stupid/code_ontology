"""Deterministic transport fixture. NOT a real LLM and NOT an extraction-quality benchmark."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def fixture_response(payload):
    stage = payload.get("stage")
    if stage == "review":
        return {"ontology": payload["current_schema"], "source_mapping": [s["current_mapping"] for s in payload["source_items"]], "issues": []}
    if stage == "merge":
        candidates = payload["candidates"]
        evidence = sorted({e for c in candidates for category in ("classes", "relations", "properties") for row in c[category] for e in row["evidence_ids"]})
    else:
        evidence = sorted({e for p in payload.get("packs", []) for e in p["evidence_ids"]})
    if stage in {"discover", "merge"}:
        schema = {"version": "0.1", "profile": payload.get("profile", ""),
                "classes": [{"id": "device", "name": "设备", "definition": "测试替身生成的概念，不是实际模型发现结果",
                             "aliases": ["Device"], "parents": [], "evidence_ids": evidence}],
                "relations": [], "properties": [], "unresolved": ["This response is a synthetic fixture"]}
        if stage == "merge":
            return {"ontology": schema, "source_mapping": [
                {"source_id": row["id"], "target_id": "device", "action": "merge", "reason": "Synthetic fixture equivalent"}
                for candidate in payload["candidates"] for row in candidate["classes"]]}
        return schema
    if stage == "extract":
        classes = payload["schema"]["classes"]
        return {"entities": ([{"id": "e1", "name": "测试定义", "type_id": classes[0]["id"],
                              "scope": "example", "basis": "direct", "evidence_ids": evidence}] if classes else []),
                "relations": [], "attributes": [], "rules": [], "candidates": []}
    return {"ok": True}


@contextmanager
def fake_server(mode="normal"):
    state = {"requests": 0, "payloads": [], "mode": mode}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(size))
            state["requests"] += 1
            state["payloads"].append(request)
            if mode == "rate_limit_once" and state["requests"] == 1:
                self.respond(429, {"error": {"message": "rate limited", "type": "rate_limit_error", "code": None}})
                return
            if mode == "unsupported_json" and "response_format" in request:
                self.respond(400, {"error": {"message": "response_format json_object is not supported", "type": "invalid_request_error", "code": None}})
                return
            if mode == "auth":
                self.respond(401, {"error": {"message": "invalid API key", "type": "authentication_error", "code": None}})
                return
            user = next((m["content"] for m in request["messages"] if m["role"] == "user"), "{}")
            payload = json.loads(user)
            stage = payload.get("stage")
            state.setdefault("stage_counts", {})
            state["stage_counts"][stage] = state["stage_counts"].get(stage, 0) + 1
            if stage == "merge" and state.get("fail_merge_on") == state["stage_counts"][stage]:
                self.respond(504, {"error": {"message": "fixture gateway timeout", "type": "timeout"}})
                return
            if stage == "merge" and state.get("merge_delay"):
                time.sleep(state["merge_delay"])
            if stage == "review" and state.get("fail_review_on") == state["stage_counts"][stage]:
                self.respond(504, {"error": {"message": "fixture review timeout", "type": "timeout"}})
                return
            value = state.get("response_factory", fixture_response)(payload)
            content = json.dumps(value, ensure_ascii=False)
            if mode == "invalid_once" and state["requests"] == 1:
                content = "not JSON"
            if mode == "bad_evidence_once" and state["requests"] == 1:
                value["classes"][0]["evidence_ids"] = ["fabricated"]
                content = json.dumps(value)
            if mode == "empty_once" and state["requests"] == 1:
                content = ""
            truncated = mode == "truncated" or state.pop("truncate_next", False)
            self.respond(200, {"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture-only",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                            "finish_reason": "length" if truncated else "stop"}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

        def respond(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
