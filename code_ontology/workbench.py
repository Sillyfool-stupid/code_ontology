"""Local stage workbench; independent of the existing read-only viewer."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from .config import PROJECT
from .console import configure_console
from .util import canonical
from .viewer import ASSETS as VIEWER_ASSETS, ArtifactStore, KINDS, record_id
from .workbench_files import checked
from .workbench_jobs import Conflict, Workbench

ASSETS = Path(__file__).parent / "resources" / "workbench"


class WorkspaceArtifacts(ArtifactStore):
    """Present the selected workspace first; inspect newly discovered schemas independently."""
    def __init__(self, workspace, schema_only=False):
        super().__init__(Path(workspace["out"]))
        self.workspace, self.schema_only = workspace, schema_only

    def listing(self):
        data = super().listing()
        w = self.workspace
        preferred = Path(w["snapshot"]) / "runs" / w["experiment"] / w["view"] / w["profile"] if w["snapshot"] else None
        if self.schema_only:
            data["runs"] = [r for r in data["runs"] if (self.runs[r["id"]][0] / "schema/schema.json").is_file()]
        data["runs"].sort(key=lambda r: self.runs[r["id"]][0] != preferred)
        return data

    def detail(self, rid):
        data = super().detail(rid)
        output, _, _ = self.locate(rid)
        current = self.load(output / "schema/schema.json")
        if self.schema_only:
            schema = current or {"classes": [], "relations": [], "properties": [], "unresolved": []}
            data["schema"] = schema
            data["schema_record_ids"] = {kind: {r["id"]: record_id("schema." + kind, r) for r in schema.get(kind, [])}
                                         for kind in ("classes", "relations", "properties")}
            data["run"] = {**data["run"], "metrics": {}}
            data["validation"], data["evaluation"] = None, None
            data["workbench_note"] = "当前展示 schema/schema.json 的发现结果；此模式不加载旧实例。完成 Schema 发现即可查看，无需先抽取实例。"
        elif current and current != data["schema"]:
            data["workbench_note"] = "当前发现 Schema 与 final/schema.json 不一致；此处展示与既有实例配套的最终 Schema。查看新发现结果请切换“当前发现 Schema”。"
        return data

    def records(self, rid, kind, offset=0, limit=50, query=""):
        if not self.schema_only:
            return super().records(rid, kind, offset, limit, query)
        self.locate(rid)
        if kind not in KINDS or offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("无效记录参数")
        return {"rows": [], "total": 0, "offset": offset, "limit": limit}


def make_server(state=None, port=8766, project=PROJECT, browse_roots=None):
    service = Workbench(Path(state or Path(project) / ".workbench"), project, browse_roots)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send(self, body, mime="application/json; charset=utf-8", code=200):
            if not isinstance(body, bytes):
                body = canonical(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'")
            self.end_headers(); self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.send({"error": "Host rejected"}, code=403)
            try:
                url = urlsplit(self.path)
                a = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path
                if path in {"/", "/app.js", "/style.css"}:
                    name = "index.html" if path == "/" else path[1:]
                    mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                    return self.send((ASSETS / name).read_bytes(), mime + "; charset=utf-8")
                if path == "/api/bootstrap":
                    return self.send({"token": token, "project": str(service.project), "workspaces": service.workspaces(),
                                      "default_repo": str(service.project / "examples" / "maintenance"),
                                      "default_out": str(service.project / "outputs"),
                                      "default_config": str(service.project / "configs" / "smoke.yaml"),
                                      "default_env": str(service.project / ".env"), "browse_roots": [str(p) for p in service.browse_roots]})
                if path == "/api/workspace":
                    return self.send(service.describe(a["id"]))
                if path == "/api/job":
                    return self.send(service.job(a["id"]))
                if path == "/api/log":
                    return self.send(service.logs(a["id"], int(a.get("offset", 0))))
                if path == "/api/events":
                    return self.send(service.events(a["id"], int(a.get("offset", 0)), a.get("level", ""), a.get("q", "")))
                if path == "/api/executions":
                    return self.send(service.executions(a["workspace"]))
                if path == "/api/execution-events":
                    return self.send(service.execution_events(a["workspace"], a["id"], int(a.get("offset", 0)), a.get("level", ""), a.get("q", "")))
                if path == "/api/execution-log":
                    return self.send(service.execution_log(a["workspace"], a["id"], int(a.get("offset", 0))))
                if path == "/api/attempt-file":
                    return self.send(service.attempt_file(a["id"], a["name"]))
                if path == "/api/artifact":
                    return self.send(service.artifact(a["workspace"], a["root"], a.get("path", ""),
                                                      offset=int(a.get("offset", 0)), limit=int(a.get("limit", 20)), query=a.get("q", "")))
                if path == "/api/download":
                    w = service.workspace(a["workspace"])
                    roots = service.roots(w)
                    if a["root"] not in roots:
                        raise ValueError("未知产物根目录")
                    file = checked(roots[a["root"]], a["path"])
                    if file == Path(w["env_file"]).resolve() or not file.is_file():
                        raise ValueError("文件不允许下载")
                    # Text downloads are also scrubbed; raw binary graph files contain no model key settings.
                    if file.suffix.lower() not in {".bin", ".sqlite"}:
                        raise ValueError("文本文件请使用分页预览；可在输出目录取得完整原文件")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename=artifact" + file.suffix)
                    self.send_header("Content-Length", str(file.stat().st_size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    with file.open("rb") as f:
                        while data := f.read(65536):
                            self.wfile.write(data)
                    return
                if path.startswith("/viewer"):
                    return self.viewer(path, a)
                raise KeyError("未知路由")
            except (ValueError, OSError, KeyError, TypeError) as e:
                self.send({"error": str(e)[:1500]}, code=400)

        def viewer(self, path, a):
            wid = a["workspace"]
            w = service.workspace(wid)
            mode = "schema" if a.get("mode") == "schema" else "final"
            if path == "/viewer/":
                html = (VIEWER_ASSETS / "index.html").read_text(encoding="utf-8")
                html = html.replace('href="/style.css"', f'href="/viewer-assets/style.css?workspace={wid}"')
                html = html.replace('src="/app.js"', f'src="/viewer-assets/app.js?workspace={wid}&mode={mode}"')
                return self.send(html.encode(), "text/html; charset=utf-8")
            if path == "/viewer-assets/style.css":
                return self.send((VIEWER_ASSETS / "style.css").read_bytes(), "text/css; charset=utf-8")
            if path == "/viewer-assets/app.js":
                js = (VIEWER_ASSETS / "app.js").read_text(encoding="utf-8")
                js = js.replace("`/api/${name}", "`/viewer-api/${name}")
                js = js.replace("new URLSearchParams(args)", "new URLSearchParams({...args,workspace:" + json.dumps(wid) + ",mode:" + json.dumps(mode) + "})")
                js = js.replace("const notices=[];", "const notices=[d.workbench_note].filter(Boolean);")
                return self.send(js.encode(), "text/javascript; charset=utf-8")
            store = WorkspaceArtifacts(w, a.get("mode") == "schema")
            name, rid = path.removeprefix("/viewer-api/"), a.get("id", "")
            if name == "runs": data = store.listing()
            elif name == "run": data = store.detail(rid)
            elif name == "schema-snapshot": data = store.schema_snapshot(rid, a.get("path", ""))
            elif name == "records": data = store.records(rid, a.get("kind", "entities"), int(a.get("offset", 0)), int(a.get("limit", 50)), a.get("q", ""))
            elif name == "evidence": data = store.evidence(rid, a["evidence_id"])
            elif name == "artifact": data = store.artifact(rid, a["name"])
            else: raise ValueError("未知展示接口")
            return self.send(service.redact_data(data, service.settings(w)[1].api_key))

        def do_POST(self):
            if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), token):
                return self.send({"error": "Invalid session token or Host"}, code=403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.send({"error": "Origin rejected"}, code=403)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 256 * 1024:
                    raise ValueError("请求大小无效")
                d = json.loads(self.rfile.read(size))
                path = urlsplit(self.path).path
                if path == "/api/workspace": result = service.save_workspace(d)
                elif path == "/api/browse": result = service.browse(d.get("path"))
                elif path == "/api/snapshots": result = service.snapshots(d["root"])
                elif path == "/api/plan": result = service.plan(d["workspace"], d["stage"], d.get("options"))
                elif path == "/api/start": result = service.submit(d["workspace"], d["stage"], d.get("options"))
                elif path == "/api/cancel": result = service.cancel(d["id"])
                elif path == "/api/acknowledge": result = service.acknowledge(d["id"])
                elif path == "/api/prompt": result = service.prompt_copy(d["workspace"], d["stage"], d["text"])
                else: raise KeyError("未知写操作")
                self.send(result)
            except Conflict as e:
                self.send({"error": str(e)}, code=409)
            except RuntimeError as e:
                self.send({"error": str(e)[:1500]}, code=500)
            except (ValueError, OSError, KeyError, TypeError) as e:
                self.send({"error": str(e)[:1500]}, code=400)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except Exception:
        service.close()
        raise
    server.service = service
    return server


def main(argv=None):
    configure_console()
    p = argparse.ArgumentParser(description="代码本体分阶段工作台；可执行 CLI 与调用模型")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--state", type=Path, default=PROJECT / ".workbench")
    p.add_argument("--allow-root", action="append", type=Path, help="额外允许在目录选择器浏览的根目录")
    args = p.parse_args(argv)
    server = make_server(args.state, args.port, browse_roots=[PROJECT.parent, *(args.allow_root or [])])
    print(f"Stage workbench: http://127.0.0.1:{server.server_port}", flush=True)
    print("阶段按钮会执行任务；模型按钮会调用当前配置的模型。Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.service.close()


if __name__ == "__main__":
    main()
