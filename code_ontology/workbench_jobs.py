"""Persistent local task orchestration. All extraction is delegated to the existing CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import yaml

from .config import PROJECT, RESOURCE, Settings, load_policy, load_settings
from .pipeline import prompt
from .prepare import __file__ as prepare_implementation
from .util import canonical, digest, file_hash, now, read_json, within, write_json
from .workbench_files import Preview, checked, listing

STAGES = [
    ("doctor", "检查环境", False), ("connection", "测试模型连接", True),
    ("scan", "扫描仓库", False), ("parse", "解析并导出结构", False),
    ("prepare", "生成模型输入包", False), ("discover", "发现并合并 Schema", True),
    ("extract", "抽取实例与规则", True), ("validate", "校验结果", False),
    ("evaluate", "匹配参考概念", False), ("report", "生成对比报告", False),
    ("sample", "生成审核表", False), ("summarize", "汇总人工审核", False),
    ("experiment", "运行六组基线", True),
]
MODEL_ENV = {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY", "model": "OPENAI_MODEL",
             "timeout": "LLM_TIMEOUT_SECONDS", "retries": "LLM_MAX_RETRIES", "concurrency": "LLM_CONCURRENCY",
             "temperature": "LLM_TEMPERATURE", "context_tokens": "LLM_CONTEXT_TOKENS",
             "max_output_tokens": "LLM_MAX_OUTPUT_TOKENS", "json_mode": "LLM_JSON_MODE", "joern_home": "JOERN_HOME",
             "ca_bundle": "LLM_CA_BUNDLE", "thinking": "LLM_THINKING"}
ACTIVE = {"queued", "running", "cancelling"}


class Conflict(ValueError):
    pass


class Workbench:
    def __init__(self, state: Path, project=PROJECT, browse_roots=None):
        self.project, self.state = Path(project).resolve(), state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        # A lifetime OS lock prevents two servers from recovering the same live queue.
        self.lockfile = (self.state / "server.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lockfile.seek(0); self.lockfile.write(b"0"); self.lockfile.flush(); self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise Conflict("此控制目录已有工作台运行")
        self.dbfile = self.state / "tasks.sqlite"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.processes, self.environments = {}, {}
        self.browse_roots = [Path(p).resolve() for p in (browse_roots or [self.project.parent])]
        self.preview = Preview(self.state)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,workspace TEXT,state TEXT,data TEXT)")
            for jid, raw in db.execute("SELECT id,data FROM jobs WHERE state IN ('queued','running','cancelling')").fetchall():
                data = json.loads(raw)
                data.update(state="interrupted", ended_at=now(), error="服务上次异常退出；核对残留进程后手动恢复")
                db.execute("UPDATE jobs SET state=?,data=? WHERE id=?", ("interrupted", canonical(data), jid))
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def db(self):
        return sqlite3.connect(self.dbfile, timeout=30)

    def close(self):
        self.stop.set()
        with self.lock:
            ids = list(self.processes)
        for jid in ids:
            self.cancel(jid)
        self.wake.set()
        self.worker.join(timeout=20)
        if self.worker.is_alive():
            raise RuntimeError("任务尚未停止，保留服务锁")
        self.lockfile.close()

    def workspaces(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM workspaces ORDER BY rowid DESC")]

    def workspace(self, wid):
        with self.db() as db:
            row = db.execute("SELECT data FROM workspaces WHERE id=?", (wid,)).fetchone()
        if not row:
            raise ValueError("工作区不存在")
        return json.loads(row[0])

    def _save_workspace(self, w):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO workspaces VALUES(?,?)", (w["id"], canonical(w)))

    def save_workspace(self, fields):
        wid = fields.get("id") or uuid.uuid4().hex[:12]
        previous = self.workspace(wid) if fields.get("id") else {}
        with self.lock:
            if any(j["state"] in ACTIVE for j in self.jobs(wid)):
                raise Conflict("工作区有任务待执行，请完成或取消后修改")
            repo = Path(fields.get("repo") or previous.get("repo") or self.project / "examples" / "maintenance").resolve()
            out = Path(fields.get("out") or previous.get("out") or self.project / "outputs").resolve()
            if not repo.is_dir() or repo == out or within(repo, out):
                raise ValueError("输入仓库不存在，或输出设为了仓库根/祖先")
            out.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=out):
                pass
            snap = str(Path(fields["snapshot"]).resolve()) if fields.get("snapshot") else ""
            if snap:
                dest = Path(snap)
                if not within(dest, out) or not (dest / "manifest.json").is_file():
                    raise ValueError("请选择输出根目录内的有效快照")
                manifest = read_json(dest / "manifest.json")
                if Path(manifest["repo"]).resolve() != repo:
                    raise ValueError("快照来源与输入仓库不一致")
            view, profile = fields.get("view", "cpg"), fields.get("profile", "maintenance")
            experiment = fields.get("experiment", "workbench-01")
            if view not in {"ast", "cfg", "cpg"} or profile not in {"general", "maintenance"}:
                raise ValueError("视图/profile 无效")
            if not re.fullmatch(r"[\w-]{1,80}", experiment):
                raise ValueError("实验 ID 只允许字母、数字、下划线、短横线")
            config = Path(fields.get("config") or self.project / "configs" / "smoke.yaml").resolve()
            envfile = Path(fields.get("env_file") or self.project / ".env").resolve()
            policy = load_policy(str(config))
            settings = load_settings(str(envfile))
            w = {"id": wid, "name": str(fields.get("name") or experiment)[:100], "repo": str(repo), "out": str(out),
                 "config": str(config), "env_file": str(envfile), "snapshot": snap,
                 "view": view, "profile": profile, "experiment": experiment,
                 "gold": str(Path(fields["gold"]).resolve()) if fields.get("gold") else "",
                 "updated_at": now()}
            self._save_workspace(w)
            return self.describe(wid)

    def settings(self, w):
        policy = load_policy(w["config"])
        # Exclude control files when the input repository contains the workbench itself.
        if within(self.state, Path(w["repo"])):
            relative = self.state.relative_to(Path(w["repo"])).as_posix()
            if relative and relative not in policy["exclude"]:
                policy["exclude"] = [*policy["exclude"], relative]
        return policy, load_settings(w["env_file"])

    def fingerprint(self, w, policy, settings, stage="experiment"):
        directory = Path(policy.get("prompt_dir") or RESOURCE / "prompts")
        prompts = {str(p.relative_to(directory)): file_hash(p) for p in directory.rglob("*.txt")}
        scan_keys = ("include", "exclude", "includes", "defines", "encoding_fallback", "max_files", "max_file_bytes", "compile_commands")
        pack_keys = ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")
        def stamp(path):
            if not path or not path.is_file():
                return None
            return [path.stat().st_size, path.stat().st_mtime_ns]
        snap = Path(w["snapshot"]) if w["snapshot"] else None
        run = self.roots(w).get("run")
        graph = stamp(snap / "normalized" / w["view"] / "index.json") if snap else None
        packs = stamp(snap / "prepared" / w["view"] / "index.json") if snap else None
        schema = file_hash(run / "schema" / "schema.json") if run and (run / "schema" / "schema.json").is_file() else None
        base = {"snapshot": w["snapshot"], "view": w["view"], "profile": w["profile"], "experiment": w["experiment"]}
        if stage in {"doctor", "connection"}:
            return digest({"stage": stage, "settings": settings.public()})
        if stage in {"scan", "parse"}:
            return digest({"snapshot": w["snapshot"], "policy": {k: policy[k] for k in scan_keys}, "joern_home": settings.joern_home})
        if stage == "prepare":
            return digest({**base, "graph": graph, "pack_policy": {k: policy[k] for k in pack_keys}})
        if stage in {"discover", "extract"}:
            selected_prompts = {k: v for k, v in prompts.items() if k.startswith("common/") or k.startswith(w["profile"] + "/")}
            if stage == "extract":
                selected_prompts = {k:v for k,v in selected_prompts.items() if "/discover." not in k and "/merge." not in k}
            return digest({**base, "packs": packs, "schema": schema, "settings": settings.public(),
                           "prompts": selected_prompts, "pack_policy": {k:policy[k] for k in pack_keys},
                           "discovery": [policy["discovery_max_packs"],policy["batch_packs"],policy["merge_max_rounds"]]})
        if stage in {"validate", "evaluate", "report", "sample", "summarize"}:
            final = {p.name: stamp(p) for p in (run / "final").glob("*")} if run else {}
            reference = stamp(Path(w["gold"])) if stage == "evaluate" and w["gold"] else None
            review = stamp(self.roots(w)["reviews"] / f"{w['view']}-{w['profile']}.csv") if stage == "summarize" else None
            return digest({**base, "final": final, "gold": reference, "review": review, "packs": packs})
        return digest({**base, "policy": {**policy, "prompt_dir": "<content>"}, "settings": settings.public(), "prompts": prompts})

    def roots(self, w):
        roots = {"repo": Path(w["repo"]), "config": Path(w["config"]).parent,
                 "doctor": self.state / "diagnostics" / w["id"], "reviews": Path(w["out"]) / "reviews" / w["experiment"]}
        if w["snapshot"]:
            roots["snapshot"] = Path(w["snapshot"])
            roots["run"] = Path(w["snapshot"]) / "runs" / w["experiment"] / w["view"] / w["profile"]
        return roots

    def jobs(self, wid=None):
        with self.db() as db:
            rows = db.execute("SELECT data FROM jobs " + ("WHERE workspace=? " if wid else "") + "ORDER BY rowid DESC LIMIT 200",
                              (wid,) if wid else ()).fetchall()
        return [json.loads(r[0]) for r in rows]

    def job(self, jid):
        with self.db() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        return json.loads(row[0])

    def update_job(self, jid, **values):
        with self.lock:
            j = self.job(jid); j.update(values)
            with self.db() as db:
                db.execute("UPDATE jobs SET state=?,data=? WHERE id=?", (j["state"], canonical(j), jid))
            return j

    def _requirements(self, stage, w):
        r = self.roots(w)
        snap, run = r.get("snapshot"), r.get("run")
        required = []
        if stage in {"parse", "prepare", "discover", "extract", "validate", "evaluate", "report", "sample", "summarize"} and not snap:
            return ["请先扫描/解析或选择已有快照"]
        if stage in {"prepare", "discover", "extract"}:
            required += [snap / "normalized" / w["view"] / "index.json"]
        if stage in {"discover", "extract", "validate"}:
            required += [snap / "prepared" / w["view"] / "packs.jsonl"]
        if stage in {"discover", "extract"} and snap:
            prepared = snap / "prepared" / w["view"] / "index.json"
            graph_index = snap / "normalized" / w["view"] / "index.json"
            if prepared.is_file() and graph_index.is_file():
                policy, _ = self.settings(w)
                key = digest({"source": read_json(snap / "manifest.json")["snapshot_id"], "view": w["view"],
                              "policy": {k:policy[k] for k in ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")},
                              "graph": read_json(graph_index).get("graph_fingerprint"),
                              "implementation": file_hash(Path(prepare_implementation)), "format": 1})
                if read_json(prepared).get("key") != key:
                    return ["证据包与当前图/预算不一致，请先重新生成模型输入包"]
        if stage == "extract":
            required += [run / "schema" / "schema.json"]
        if stage in {"validate", "evaluate", "sample"}:
            required += [run / "final" / "schema.json"]
        if stage == "evaluate":
            if not w["gold"]:
                return ["请配置适用于当前仓库的人工 gold 文件"]
            required += [Path(w["gold"])]
        if stage == "summarize":
            required += [r["reviews"] / f"{w['view']}-{w['profile']}.csv"]
        return [f"缺少依赖：{p}" for p in required if not p.is_file()]

    def describe(self, wid):
        w = self.workspace(wid)
        policy, settings = self.settings(w)
        fingerprint = self.fingerprint(w, policy, settings)
        jobs = self.jobs(wid)
        stages = []
        for key, label, model in STAGES:
            latest = next((j for j in jobs if j["stage"] == key), None)
            problems = self._requirements(key, w)
            stages.append({"id": key, "label": label, "model": model, "blocked": problems,
                           "inputs": self.stage_inputs(key, w),
                           "job": latest, "stale": bool(latest and (latest["fingerprint"] != self.fingerprint(w, policy, settings, key)
                                                                    or latest.get("schema_prompt_matches") is False))})
        roots = self.roots(w)
        return {"workspace": w, "settings": settings.public(), "policy": policy, "fingerprint": fingerprint,
                "stages": stages, "jobs": jobs, "roots": {k: str(v) for k, v in roots.items()},
                "prompts": {stage: prompt(policy, w["profile"], stage) for stage in ("discover", "merge", "extract")},
                "prompt_sources": {stage: (Path(policy.get("prompt_dir") or RESOURCE / "prompts") / w["profile"] / f"{stage}.txt").read_text(encoding="utf-8") for stage in ("discover", "merge", "extract")}}

    def stage_inputs(self, stage, w):
        r = self.roots(w)
        snap, run = r.get("snapshot"), r.get("run")
        inputs = {"策略 YAML": w["config"], "模型环境文件（内容不预览）": w["env_file"]}
        if stage in {"scan", "parse", "experiment"}:
            inputs["输入仓库"] = w["repo"]
        if snap and stage not in {"doctor", "connection", "scan"}:
            inputs["固定源码清单"] = str(snap / "manifest.json")
        if snap and stage == "prepare":
            inputs["规范图索引"] = str(snap / "normalized" / w["view"] / "index.json")
        if snap and stage in {"discover", "extract", "validate"}:
            inputs["模型证据包"] = str(snap / "prepared" / w["view"] / "packs.jsonl")
        if run and stage == "extract":
            inputs["固定 Schema（执行时复制）"] = str(run / "schema/schema.json")
        if run and stage in {"validate", "evaluate", "sample"}:
            inputs["本组最终产物"] = str(run / "final")
        if snap and stage == "report":
            inputs["各组指标与执行状态"] = str(snap / "runs" / w["experiment"])
        if stage == "evaluate":
            inputs["人工参考概念"] = w["gold"]
        if stage == "summarize":
            inputs["人工标签 CSV"] = str(r["reviews"] / f"{w['view']}-{w['profile']}.csv")
        if stage in {"discover", "extract", "experiment"}:
            inputs["实际提示词目录"] = str(Path(self.settings(w)[0].get("prompt_dir") or RESOURCE / "prompts"))
        return inputs

    def snapshots(self, root):
        root = Path(root).resolve()
        if not root.is_dir():
            return []
        result = []
        # Existing layout is root/snapshot or root/repo-id/snapshot. Avoid traversing raw graph/source trees.
        candidates = list(root.glob("manifest.json")) + list(root.glob("*/manifest.json")) + list(root.glob("*/*/manifest.json"))
        for p in candidates[:500]:
            if not within(p.resolve(), root):
                continue
            try:
                m = read_json(p)
                result.append({"path": str(p.parent), "id": m["snapshot_id"], "repo": m["repo"], "files": len(m["files"])})
            except (ValueError, KeyError, OSError):
                continue
        return result

    def browse(self, path):
        p = Path(path).resolve() if path else self.project
        allowed = list(self.browse_roots)
        for w in self.workspaces():
            allowed += [Path(w["repo"]), Path(w["out"]), Path(w["config"]).parent, Path(w["env_file"]).parent]
        if not any(within(p, r) for r in allowed):
            raise ValueError("目录不在浏览范围；可直接填写输入/输出绝对路径保存，或启动时追加 --allow-root")
        data = listing(p)
        # The picker may show the environment filename, never its contents.
        for file in p.glob(".env*"):
            if file.is_file() and not file.is_symlink() and file.name != ".env.example":
                data["entries"].append({"name": file.name, "path": file.name, "directory": False,
                                        "bytes": file.stat().st_size})
                data["total"] += 1
        for row in data["entries"]:
            row["absolute"] = str(p / row["name"])
        # Environment files may be selected by path, but their content is never browsed.
        data.update(absolute=str(p), parent_absolute=str(p.parent) if any(within(p.parent, r) for r in allowed) else "")
        return data

    def attempt_file(self, jid, relative):
        job = self.job(jid)
        directory = Path(job["log"]).parent
        allowed = {"task.json", "policy.yaml", "console.log", "input-schema.json", "metrics.json"}
        if relative not in allowed:
            raise ValueError("未知任务附件")
        env = self.environments.get(jid, {})
        secret = env.get("OPENAI_API_KEY") or self.settings(job["workspace_config"])[1].api_key
        return self.redact_data(self.preview.read(directory / relative), secret)

    def artifact(self, wid, root, relative, **kw):
        roots = self.roots(self.workspace(wid))
        if root not in roots:
            raise ValueError("未知产物根目录")
        path = checked(roots[root], relative)
        if path.is_dir():
            return {"format": "directory", **listing(roots[root], relative, kw.get("offset", 0))}
        # The configured environment file is blocked even if it has an unconventional name.
        if path == Path(self.workspace(wid)["env_file"]).resolve():
            raise ValueError("环境文件不能预览")
        data = self.preview.read(path, **kw)
        return self.redact_data(data, self.settings(self.workspace(wid))[1].api_key)

    @staticmethod
    def redact_data(data, secret):
        text = canonical(data)
        if secret and secret != "unused":
            text = text.replace(secret, "<redacted>")
        text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "<redacted>", text)
        return json.loads(text)

    def _command(self, stage, w, config, envfile, options):
        args = [sys.executable, "-u", "-m", "code_ontology"]
        common = ["--config", str(config), "--env-file", str(envfile)]
        snap = w["snapshot"]
        run = self.roots(w).get("run")
        if stage in {"doctor", "connection"}:
            return args + ["doctor", "--out", str(self.roots(w)["doctor"])] + common + (["--no-llm"] if stage == "doctor" else [])
        if stage in {"scan", "parse", "experiment"}:
            args += [stage, "--repo", w["repo"], "--out", w["out"]] + common
            if stage == "parse":
                args += ["--views", "ast,cfg,cpg"]
                args[3] = "code_ontology.workbench_runner"
                args += ["--expected-snapshot", w["snapshot"]]
            if stage == "experiment":
                args += ["--views", "ast,cfg,cpg", "--profile", "both", "--experiment", w["experiment"]]
            return args
        if stage == "prepare":
            return args + [stage, "--snapshot", snap, "--view", w["view"]] + common
        if stage in {"discover", "extract"}:
            args += [stage, "--snapshot", snap, "--view", w["view"], "--profile", w["profile"], "--experiment", w["experiment"]] + common
            if stage == "extract":
                args += ["--schema", str(run / "schema" / "schema.json")]
            elif options.get("update_schema"):
                args += ["--update-schema"]
            return args
        if stage == "validate":
            return args + [stage, "--snapshot", snap, "--view", w["view"], "--run-dir", str(run)] + common
        if stage == "evaluate":
            return args + [stage, "--run-dir", str(run), "--gold", w["gold"]] + common
        if stage == "report":
            return args + [stage, "--snapshot", snap, "--experiment", w["experiment"]] + common
        review = self.roots(w)["reviews"] / f"{w['view']}-{w['profile']}.csv"
        if stage == "sample":
            return [sys.executable, "-u", "-m", "code_ontology.review", "sample", "--run-dir", str(run), "--out", str(review), "--per-kind", "10", "--seed", "42"]
        if stage == "summarize":
            return [sys.executable, "-u", "-m", "code_ontology.review", "summarize", "--reviews", str(review), "--out", str(review.with_suffix(".summary.json"))]
        raise ValueError("不支持的阶段")

    def plan(self, wid, stage, options=None):
        if stage not in {x[0] for x in STAGES}:
            raise ValueError("未知阶段")
        d = self.describe(wid)
        s = next(x for x in d["stages"] if x["id"] == stage)
        return {"stage": stage, "label": s["label"], "model": s["model"], "blocked": s["blocked"],
                "command": self._command(stage, d["workspace"], d["workspace"]["config"], d["workspace"]["env_file"], options or {}),
                "inputs": s["inputs"], "fingerprint": self.fingerprint(d["workspace"], d["policy"], load_settings(d["workspace"]["env_file"]), stage),
                "note": "仅执行当前阶段；discover/extract 会按原 CLI 核对/复用 prepare，extract 显式指定固定 schema。"}

    def submit(self, wid, stage, options=None):
        options = options or {}
        with self.lock:
            if any(j["state"] in ACTIVE for j in self.jobs()):
                existing = next(j for j in self.jobs() if j["state"] in ACTIVE)
                if existing["workspace"] == wid and existing["stage"] == stage:
                    return existing
                raise Conflict("已有任务执行/等待中，请完成或取消后继续")
            if any(j["state"] == "interrupted" and not j.get("acknowledged") for j in self.jobs()):
                raise Conflict("有异常中断任务。请检查残留进程后点击“确认已检查残留进程”解除阻塞")
            plan = self.plan(wid, stage, options)
            if plan["blocked"]:
                raise ValueError("；".join(plan["blocked"]))
            w = self.workspace(wid)
            policy, settings = self.settings(w)
            if stage == "experiment" and policy["discovery_max_packs"] != 24:
                raise ValueError("六组基线请选择 baseline.yaml 或明确使用 discovery_max_packs: 24 的范围配置")
            # Existing read-only snapshot is never silently rebound to changed parser settings.
            jid = uuid.uuid4().hex[:16]
            directory = self.state / "jobs" / jid
            directory.mkdir(parents=True)
            frozen_prompts = directory / "prompts"
            shutil.copytree(Path(policy.get("prompt_dir") or RESOURCE / "prompts"), frozen_prompts)
            policy["prompt_dir"] = str(frozen_prompts)
            config = directory / "policy.yaml"
            config.write_text(yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
            command = self._command(stage, w, config, w["env_file"], options)
            if stage == "extract":
                fixed = directory / "input-schema.json"
                shutil.copyfile(self.roots(w)["run"] / "schema" / "schema.json", fixed)
                command[command.index("--schema") + 1] = str(fixed)
            environment = dict(os.environ)
            for field, key in MODEL_ENV.items():
                environment[key] = str(getattr(settings, field))
            environment["PYTHONIOENCODING"] = "utf-8"
            j = {"id": jid, "workspace": wid, "stage": stage, "state": "queued", "created_at": now(),
                 "workspace_config": w, "settings": settings.public(), "fingerprint": plan["fingerprint"],
                 "command": command, "options": options, "log": str(directory / "console.log"),
                 "progress": {}, "snapshot": w["snapshot"], "artifact_status": "pending"}
            write_json(directory / "task.json", j)
            self.environments[jid] = environment
            with self.db() as db:
                db.execute("INSERT INTO jobs VALUES(?,?,?,?)", (jid, wid, "queued", canonical(j)))
            self.wake.set()
            return j

    def acknowledge(self, jid):
        if self.job(jid)["state"] != "interrupted":
            raise ValueError("仅异常中断任务需要确认")
        return self.update_job(jid, acknowledged=True)

    def cancel(self, jid):
        with self.lock:
            j = self.job(jid)
            if j["state"] == "queued":
                self.environments.pop(jid, None)
                return self.update_job(jid, state="cancelled", ended_at=now(), artifact_status="incomplete")
            if j["state"] not in {"running", "cancelling"}:
                return j
            self.update_job(jid, state="cancelling")
            p = self.processes.get(jid)
            if p and p.poll() is None:
                if os.name == "nt":
                    result = subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                                            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                    if result.returncode and p.poll() is None:
                        self.update_job(jid, cancellation_error=result.stderr.decode(errors="replace")[:1500])
                        raise RuntimeError("停止进程树失败，请检查任务记录中的 cancellation_error；任务仍保留锁")
                else:
                    os.killpg(p.pid, signal.SIGKILL)
            return self.job(jid)

    def _loop(self):
        while not self.stop.is_set():
            self.wake.wait(.5); self.wake.clear()
            for j in reversed(self.jobs()):
                if j["state"] == "queued" and not self.stop.is_set():
                    self._run(j)

    def _check_outputs(self, stage, w):
        """A successful child must leave the stage's documented artifact contract."""
        r = self.roots(w)
        snap, run = r.get("snapshot"), r.get("run")
        files = []
        if stage in {"doctor", "connection"}:
            files = [r["doctor"] / "doctor.json"]
        elif stage == "scan":
            files = [snap / "manifest.json"] if snap else []
        elif stage == "parse" and snap:
            files = [snap / "raw/cpg.bin", snap / "raw/cpg/export.json"]
            files += [snap / "normalized" / v / "index.json" for v in ("ast", "cfg", "cpg")]
        elif stage == "prepare" and snap:
            files = [snap / "prepared" / w["view"] / n for n in ("packs.jsonl", "index.json")]
        elif stage == "discover" and run:
            files = [run / "schema" / n for n in ("schema.json", "provenance.json", "selection.json")]
        elif stage == "extract" and run:
            files = [run / "metrics.json", run / "final/schema.json", run / "extraction/selection.json"]
            files += [run / "final" / f"{n}.jsonl" for n in ("entities", "relations", "attributes", "rules", "candidates")]
        elif stage in {"validate", "evaluate"} and run:
            files = [run / ("validation.json" if stage == "validate" else "evaluation.json")]
        elif stage in {"report", "experiment"} and snap:
            files = [snap / "runs" / w["experiment"] / n for n in ("comparison.json", "comparison.md")]
            if stage == "experiment":
                files += [snap / "runs" / w["experiment"] / v / p / "metrics.json"
                          for v in ("ast", "cfg", "cpg") for p in ("general", "maintenance")]
        elif stage in {"sample", "summarize"}:
            suffix = ".csv" if stage == "sample" else ".summary.json"
            files = [r["reviews"] / f"{w['view']}-{w['profile']}{suffix}"]
        if not files or any(not p.is_file() for p in files):
            raise ValueError("命令结束但必要产物缺失：" + "; ".join(str(p) for p in files if not p.is_file()))
        if stage in {"doctor", "connection", "validate"} and not read_json(files[0]).get("ok"):
            raise ValueError("产物报告检查未通过")
        if stage == "extract" and read_json(run / "metrics.json").get("status") != "success":
            raise ValueError("实例产物未全部成功，请检查 metrics 和失败记录")

    def _run(self, job):
        jid = job["id"]
        environment = self.environments.get(jid)
        if not environment:
            self.update_job(jid, state="interrupted", error="运行环境丢失，请重新提交")
            return
        secret = environment.get("OPENAI_API_KEY", "")
        discovered = ""
        output_lock = None
        p = None
        try:
            frozen_policy = load_policy(job["command"][job["command"].index("--config") + 1]) if "--config" in job["command"] else None
            frozen_settings = Settings(**{**job["settings"], "base_url": environment["OPENAI_BASE_URL"], "api_key": secret})
            with self.lock:
                if self.job(jid)["state"] != "queued":
                    return
                out = Path(job["workspace_config"]["out"])
                out.mkdir(parents=True, exist_ok=True)
                output_lock = (out / "writer.workbench.lock").open("a+b")
                if os.name == "nt":
                    import msvcrt
                    output_lock.seek(0); output_lock.write(b"0"); output_lock.flush(); output_lock.seek(0)
                    msvcrt.locking(output_lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(output_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                p = subprocess.Popen(job["command"], cwd=self.project, env=environment, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", shell=False, **kwargs)
                self.processes[jid] = p
                self.update_job(jid, state="running", pid=p.pid, started_at=now())
            with Path(job["log"]).open("w", encoding="utf-8", buffering=1) as log:
                for line in p.stdout:
                    if secret and secret != "unused":
                        line = line.replace(secret, "<redacted>")
                    line = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "<redacted>", line)
                    log.write(line)
                    if line.startswith("SNAPSHOT="):
                        discovered = line.strip().split("=", 1)[1]
                    match = re.search(r"TASK (discover|extract) (\d+)/(\d+) \((\d+) successful\)", line)
                    if match:
                        self.update_job(jid, progress={"phase": match[1], "completed": int(match[2]),
                                                      "total": int(match[3]), "successful": int(match[4])})
                    if line.startswith("RUN "):
                        self.update_job(jid, combination=line.strip()[4:])
            code = p.wait()
            w = job["workspace_config"]
            if discovered:
                target = Path(discovered).resolve()
                if not within(target, Path(w["out"])) or not (target / "manifest.json").is_file():
                    raise ValueError("任务返回的快照不在输出目录或缺少 manifest")
                m = read_json(target / "manifest.json")
                if Path(m["repo"]).resolve() != Path(w["repo"]):
                    raise ValueError("快照来源仓库不一致")
                w["snapshot"] = str(target)
                self._save_workspace(w)
                self.update_job(jid, snapshot=str(target))
            state = "succeeded" if code == 0 else "failed"
            if self.job(jid)["state"] == "cancelling":
                state = "cancelled"
            elif code and w["snapshot"] and job["stage"] in {"discover", "extract", "experiment"}:
                run = self.roots(w)["run"]
                if job["stage"] == "experiment":
                    summary = Path(w["snapshot"]) / "runs" / w["experiment"] / "comparison.json"
                    if summary.is_file() and any(r.get("status") in {"success", "partial"} for r in read_json(summary).get("runs", [])):
                        state = "partial"
                else:
                    manifest = run / "run_manifest.json"
                    if manifest.is_file() and read_json(manifest).get("stages", {}).get("execution", {}).get("state") == "partial":
                        state = "partial"
            if state == "succeeded":
                self._check_outputs(job["stage"], w)
            self.update_job(jid, state=state, exit_code=code, ended_at=now(),
                            artifact_status="current" if state == "succeeded" else "incomplete",
                            fingerprint=self.fingerprint(w, frozen_policy, frozen_settings, job["stage"])
                            if state == "succeeded" and frozen_policy else job["fingerprint"])
            if w["snapshot"]:
                run = self.roots(w)["run"]
                metrics = run / "metrics.json"
                if job["stage"] in {"extract", "experiment"} and metrics.is_file():
                    write_json(Path(job["log"]).parent / "metrics.json", read_json(metrics))
                if job["stage"] == "discover" and (run / "schema" / "provenance.json").is_file():
                    actual = read_json(run / "schema" / "provenance.json")
                    expected_prompt = digest(prompt(load_policy(job["command"][job["command"].index("--config") + 1]), w["profile"], "discover") + prompt(load_policy(job["command"][job["command"].index("--config") + 1]), w["profile"], "merge"))
                    self.update_job(jid, schema_prompt_matches=actual.get("prompt_hash") == expected_prompt,
                                    schema_note="固定 Schema 默认复用；提示词变动需要勾选更新 Schema 或新建实验")
        except Exception as e:
            # Keep the writer lock until an unexpectedly abandoned child has really exited.
            if p and p.poll() is None:
                try:
                    self.cancel(jid)
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    self.update_job(jid, state="cancelling", error="异常清理尚未结束，请重试停止任务")
                for _ in p.stdout:
                    pass
                p.wait()
            message = str(e).replace(secret, "<redacted>") if secret else str(e)
            self.update_job(jid, state="failed", error=message[:1500], ended_at=now(), artifact_status="incomplete",
                            exit_code=p.returncode if p else None)
        finally:
            process = self.processes.get(jid)
            if process and process.stdout:
                process.stdout.close()
            if output_lock:
                output_lock.close()
            with self.lock:
                self.processes.pop(jid, None); self.environments.pop(jid, None)

    def logs(self, jid, offset=0):
        j = self.job(jid)
        path = Path(j["log"])
        return self.preview.read(path, offset=offset) if path.is_file() else {"text": "", "next_offset": 0}

    def prompt_copy(self, wid, stage, text):
        if stage not in {"discover", "merge", "extract"} or len(text) > 100000:
            raise ValueError("提示词阶段或长度无效")
        with self.lock:
            if any(j["state"] in ACTIVE for j in self.jobs(wid)):
                raise Conflict("任务执行中不能切换提示词")
            w = self.workspace(wid); policy, _ = self.settings(w)
            target = self.state / "prompt-edits" / uuid.uuid4().hex[:12]
            shutil.copytree(Path(policy.get("prompt_dir") or RESOURCE / "prompts"), target / "prompts")
            (target / "prompts" / w["profile"] / f"{stage}.txt").write_text(text, encoding="utf-8")
            policy["prompt_dir"] = str(target / "prompts")
            (target / "policy.yaml").write_text(yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
            w["config"] = str(target / "policy.yaml"); self._save_workspace(w)
            return self.describe(wid)
