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

from .config import PROJECT, RESOURCE, Settings, load_policy, load_settings, configuration_sources
from .pipeline import prompt
from .parsing import select_backend, snapshot_parser
from .prepare import PREPARE_FORMAT, __file__ as prepare_implementation
from .util import canonical, digest, file_hash, now, read_json, within, write_json
from .workbench_files import Preview, checked, listing
from .events import read_events, redact
from .execution_plan import build_stage_command

STAGES = [
    ("doctor", "检查环境", False), ("connection", "测试模型连接", True),
    ("scan", "扫描仓库", False), ("parse", "解析并导出结构", False),
    ("prepare", "生成模型输入包", False), ("discover-local", "局部 Schema 提取", True),
    ("merge-schema", "合并 Schema（可直接抽取）", True), ("review-schema", "AI 审查并修正 Schema（可选）", True), ("discover", "发现并合并 Schema（兼容）", True),
    ("extract", "抽取实例与规则", True), ("finalize", "归并实例产物", False), ("export-rdf", "导出 RDF 三元组", False), ("validate", "校验结果", False),
    ("evaluate", "匹配参考概念", False), ("report", "生成对比报告", False),
    ("sample", "生成审核表", False), ("summarize", "汇总人工审核", False),
    ("experiment", "运行六组基线", True),
]
MODEL_ENV = {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY", "model": "OPENAI_MODEL",
             "timeout": "LLM_TIMEOUT_SECONDS", "retries": "LLM_MAX_RETRIES", "concurrency": "LLM_CONCURRENCY",
             "temperature": "LLM_TEMPERATURE", "context_tokens": "LLM_CONTEXT_TOKENS",
             "max_output_tokens": "LLM_MAX_OUTPUT_TOKENS", "send_max_tokens": "LLM_SEND_MAX_TOKENS", "json_mode": "LLM_JSON_MODE", "joern_home": "JOERN_HOME",
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
            config = Path(fields["config"]).resolve() if fields.get("config") else None
            envfile = Path(fields.get("env_file") or self.project / ".env").resolve()
            policy = load_policy(str(config) if config else None, str(envfile))
            settings = load_settings(str(envfile), str(config) if config else None)
            info = snapshot_parser(Path(snap)) if snap else select_backend(policy, settings)
            if view not in info["available_views"]:
                view = "ast"
            w = {"id": wid, "name": str(fields.get("name") or experiment)[:100], "repo": str(repo), "out": str(out),
                 "config": str(config) if config else "", "env_file": str(envfile), "snapshot": snap,
                 "view": view, "profile": profile, "experiment": experiment,
                 "gold": str(Path(fields["gold"]).resolve()) if fields.get("gold") else "",
                 "updated_at": now()}
            self._save_workspace(w)
            return self.describe(wid)

    def settings(self, w):
        policy = load_policy(w["config"] or None, w["env_file"])
        # Exclude control files when the input repository contains the workbench itself.
        if within(self.state, Path(w["repo"])):
            relative = self.state.relative_to(Path(w["repo"])).as_posix()
            if relative and relative not in policy["exclude"]:
                policy["exclude"] = [*policy["exclude"], relative]
        return policy, load_settings(w["env_file"], w["config"] or None)

    def fingerprint(self, w, policy, settings, stage="experiment"):
        directory = Path(policy.get("prompt_dir") or RESOURCE / "prompts")
        prompts = {str(p.relative_to(directory)): file_hash(p) for p in directory.rglob("*.txt")}
        scan_keys = ("include", "exclude", "includes", "defines", "encoding_fallback", "max_files", "max_file_bytes", "compile_commands", "parser_backend")
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
        draft = file_hash(run / "schema/draft-schema.json") if run and (run / "schema/draft-schema.json").is_file() else None
        draft_mapping = file_hash(run / "schema/draft-source-mapping.json") if run and (run / "schema/draft-source-mapping.json").is_file() else None
        base = {"snapshot": w["snapshot"], "view": w["view"], "profile": w["profile"], "experiment": w["experiment"]}
        if stage == "export-rdf":
            from .rdf_export import input_fingerprint
            return input_fingerprint(run, snap) if run else digest(base)
        if stage in {"doctor", "connection"}:
            return digest({"stage": stage, "settings": settings.public(), "parser": select_backend(policy, settings)})
        if stage in {"scan", "parse"}:
            return digest({"snapshot": w["snapshot"], "policy": {k: policy[k] for k in scan_keys}, "parser": select_backend(policy, settings), "joern_home": settings.joern_home})
        if stage == "prepare":
            return digest({**base, "graph": graph, "pack_policy": {k: policy[k] for k in pack_keys}})
        if stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract"}:
            selected_prompts = {k: v for k, v in prompts.items() if k.startswith("common/") or k.startswith(w["profile"] + "/")}
            effective = settings.public()
            if stage in {"extract", "finalize", "discover-local", "merge-schema", "review-schema"}:
                stage_name = {"discover-local": "discover", "merge-schema": "merge", "review-schema": "review", "finalize": "extract", "extract": "extract"}[stage]
                effective = settings.for_stage(stage_name).public()
                selected_prompts = {k: v for k, v in selected_prompts.items() if k.startswith("common/") or k.endswith(f"/{stage_name}.txt")}
            if stage == "extract":
                selected_prompts = {k:v for k,v in selected_prompts.items() if "/discover." not in k and "/merge." not in k}
            return digest({**base, "packs": packs, "schema": schema if stage in {"extract", "finalize"} else None,
                           "extract_batch_packs": policy.get("extract_batch_packs", 1) if stage in {"extract", "finalize"} else None,
                           "review_inputs": [draft, draft_mapping, policy["review_batch_items"], policy["review_max_rounds"]] if stage == "review-schema" else None,
                           "allow_unreviewed": policy["allow_unreviewed_schema"] if stage == "extract" else None,
                           "local": stamp(Path(w["snapshot"]) / "runs" / w["experiment"] / w["view"] / w["profile"] / "schema/local-manifest.json") if stage == "merge-schema" and w["snapshot"] else None,
                           "settings": effective, "output_language": policy["output_language"],
                           "prompts": selected_prompts, "pack_policy": {k:policy[k] for k in pack_keys},
                           "discovery": [policy["discovery_max_packs"],policy["batch_packs"],policy["merge_max_rounds"],policy["discovery_strategy"]]})
        if stage in {"validate", "evaluate", "report", "sample", "summarize"}:
            final = {p.name: stamp(p) for p in (run / "final").glob("*")} if run else {}
            reference = stamp(Path(w["gold"])) if stage == "evaluate" and w["gold"] else None
            review = stamp(self.roots(w)["reviews"] / f"{w['view']}-{w['profile']}.csv") if stage == "summarize" else None
            return digest({**base, "final": final, "gold": reference, "review": review, "packs": packs})
        return digest({**base, "policy": {**policy, "prompt_dir": "<content>"}, "settings": settings.public(), "prompts": prompts})

    def roots(self, w):
        config = Path(w["config"]) if w["config"] else None
        roots = {"repo": Path(w["repo"]), "config": (config if config.is_dir() else config.parent) if config else Path(w["env_file"]).parent,
                 "experiments": Path(w["out"]) / "experiments", "executions": Path(w["out"]) / "executions",
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
        if stage == "export-rdf":
            if not run:
                return ["请先选择快照并归并实例产物"]
            required = [run / "metrics.json", run / "final/schema.json"]
            required += [run / "final" / f"{k}.jsonl" for k in ("entities", "relations", "attributes", "rules", "candidates")]
            return [f"请先归并实例产物，缺少：{p}" for p in required if not p.is_file()]
        if stage in {"parse", "prepare", "discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "validate", "evaluate", "report", "sample", "summarize"} and not snap:
            return ["请先扫描/解析或选择已有快照"]
        if snap and stage in {"prepare", "discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "validate"} and w["view"] not in snapshot_parser(snap)["available_views"]:
            return ["当前快照仅支持 AST，请保存工作区以切换视图"]
        if stage in {"prepare", "discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract"}:
            required += [snap / "normalized" / w["view"] / "index.json"]
        if stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "validate"}:
            required += [snap / "prepared" / w["view"] / "packs.jsonl"]
        if stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract"} and snap:
            prepared = snap / "prepared" / w["view"] / "index.json"
            graph_index = snap / "normalized" / w["view"] / "index.json"
            if prepared.is_file() and graph_index.is_file():
                policy, _ = self.settings(w)
                key = digest({"source": read_json(snap / "manifest.json")["snapshot_id"], "view": w["view"],
                              "policy": {k:policy[k] for k in ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")},
                              "graph": read_json(graph_index).get("graph_fingerprint"),
                              "implementation": file_hash(Path(prepare_implementation)), "format": PREPARE_FORMAT})
                if read_json(prepared).get("key") != key:
                    return ["证据包与当前图/预算不一致，请先重新生成模型输入包"]
        if stage == "merge-schema":
            required += [run / "schema" / "local-manifest.json"]
        if stage == "review-schema":
            required += [run / "schema/draft-schema.json", run / "schema/draft-source-mapping.json"]
        if stage == "finalize":
            required += [run / "extraction" / "manifest.json"]
        if stage == "extract":
            required += [run / "schema" / "schema.json"]
            if (run / "schema/local-manifest.json").is_file():
                status = run / "schema/merge-status.json"
                if not status.is_file() or read_json(status).get("state") != "success":
                    return ["请先成功合并 Schema，再抽取实例；AI 审查为可选步骤"]
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
        info = snapshot_parser(Path(w["snapshot"])) if w["snapshot"] else select_backend(policy, settings)
        fingerprint = self.fingerprint(w, policy, settings)
        jobs = self.jobs(wid)
        stages = []
        for key, label, model in STAGES:
            latest = next((j for j in jobs if j["stage"] == key), None)
            problems = self._requirements(key, w)
            stages.append({"id": key, "label": "运行 AST 两组基线" if key == "experiment" and info["backend"] == "tree-sitter" else label, "model": model, "blocked": problems,
                           "inputs": self.stage_inputs(key, w),
                           "job": latest, "stale": bool(latest and (latest["fingerprint"] != self.fingerprint(w, policy, settings, key)
                                                                    or latest.get("schema_prompt_matches") is False))})
        roots = self.roots(w)
        return {"workspace": w, "settings": settings.public(), "policy": policy, "parser": info, "fingerprint": fingerprint,
                "stage_settings": {s: settings.for_stage(s).public() for s in ("discover", "merge", "review", "extract")},
                "configuration_sources": configuration_sources(w["env_file"], w["config"] or None),
                "stages": stages, "jobs": jobs, "roots": {k: str(v) for k, v in roots.items()},
                "prompts": {stage: prompt(policy, w["profile"], stage) for stage in ("discover", "merge", "review", "extract")},
                "prompt_sources": {stage: (Path(policy.get("prompt_dir") or RESOURCE / "prompts") / w["profile"] / f"{stage}.txt").read_text(encoding="utf-8") for stage in ("discover", "merge", "review", "extract")}}

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
        if snap and stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "validate"}:
            inputs["模型证据包"] = str(snap / "prepared" / w["view"] / "packs.jsonl")
        if run and stage == "extract":
            inputs["固定 Schema（执行时复制）"] = str(run / "schema/schema.json")
        if run and stage in {"validate", "evaluate", "sample", "export-rdf"}:
            inputs["本组最终产物"] = str(run / "final")
        if snap and stage == "report":
            inputs["各组指标与执行状态"] = str(snap / "runs" / w["experiment"])
        if stage == "evaluate":
            inputs["人工参考概念"] = w["gold"]
        if stage == "summarize":
            inputs["人工标签 CSV"] = str(r["reviews"] / f"{w['view']}-{w['profile']}.csv")
        if stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "experiment"}:
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
        return build_stage_command(stage, w, config, envfile, self.roots(w), options)

    def plan(self, wid, stage, options=None):
        if stage not in {x[0] for x in STAGES}:
            raise ValueError("未知阶段")
        d = self.describe(wid)
        s = next(x for x in d["stages"] if x["id"] == stage)
        return {"stage": stage, "label": s["label"], "model": s["model"], "blocked": s["blocked"],
                "command": self._command(stage, d["workspace"], d["workspace"]["config"], d["workspace"]["env_file"], options or {}),
                "inputs": s["inputs"], "fingerprint": self.fingerprint(d["workspace"], d["policy"], load_settings(d["workspace"]["env_file"], d["workspace"]["config"] or None), stage),
                "note": "局部提取与合并可独立运行；实例抽取后执行归并实例产物。discover 是保留的组合入口。"}

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
            for field, key in (("review_base_url", "OPENAI_REVIEW_BASE_URL"),
                               ("review_api_key", "OPENAI_REVIEW_API_KEY"),
                               ("review_model", "OPENAI_REVIEW_MODEL"),
                               ("review_ca_bundle", "LLM_REVIEW_CA_BUNDLE")):
                value = getattr(settings, field)
                if value:
                    environment[key] = str(value)
            for stage_name in ("discover", "merge", "review", "extract"):
                overrides = settings.stage_overrides.get(stage_name, {})
                for attr in ("context_tokens", "max_output_tokens", "send_max_tokens", "timeout", "retries", "concurrency", "temperature", "json_mode", "thinking"):
                    value = overrides.get(attr, "")
                    suffix = MODEL_ENV[attr].removeprefix("LLM_")
                    environment[f"LLM_{stage_name.upper()}_{suffix}"] = str(value)
            environment["CO_FROZEN_POLICY"] = str(config.resolve())
            environment["PYTHONIOENCODING"] = "utf-8"
            environment.update(CO_EXECUTION_ID=jid, CO_EVENT_EXPERIMENT=w["experiment"], CO_EVENT_STAGE=stage,
                               CO_EVENT_FILE=str(directory / "events.jsonl"))
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
            info = snapshot_parser(snap)
            files = ([snap / "raw/tree-sitter" / n for n in ("nodes.jsonl", "edges.jsonl", "export.json", "parse_report.json")]
                     if info["backend"] == "tree-sitter" else [snap / "raw/cpg.bin", snap / "raw/cpg/export.json"])
            files += [snap / "normalized" / v / "index.json" for v in info["available_views"]]
        elif stage == "prepare" and snap:
            files = [snap / "prepared" / w["view"] / n for n in ("packs.jsonl", "index.json")]
        elif stage == "discover" and run:
            files = [run / "schema" / n for n in ("schema.json", "provenance.json", "selection.json")]
        elif stage == "discover-local" and run:
            files = [run / "schema/local-manifest.json", run / "schema/selection.json"]
        elif stage == "merge-schema" and run:
            files = [run / "schema/schema.json", run / "schema/merge-status.json", run / "schema/provenance.json"]
        elif stage == "review-schema" and run:
            files = [run / "schema/schema.json", run / "schema/review-status.json", run / "schema/review-report.json"]
        elif stage == "extract" and run:
            files = [run / "extraction/manifest.json", run / "extraction/selection.json"]
        elif stage == "finalize" and run:
            files = [run / "metrics.json", run / "final/schema.json", run / "extraction/selection.json"]
            files += [run / "final" / f"{n}.jsonl" for n in ("entities", "relations", "attributes", "rules", "candidates")]
        elif stage == "export-rdf" and run:
            from .rdf_export import FILES
            files = [run / "rdf" / n for n in (*FILES, "export-manifest.json")]
        elif stage in {"validate", "evaluate"} and run:
            files = [run / ("validation.json" if stage == "validate" else "evaluation.json")]
        elif stage in {"report", "experiment"} and snap:
            files = [snap / "runs" / w["experiment"] / n for n in ("comparison.json", "comparison.md")]
            if stage == "experiment":
                files += [snap / "runs" / w["experiment"] / v / p / "metrics.json"
                          for v in snapshot_parser(snap)["available_views"] for p in ("general", "maintenance")]
        elif stage in {"sample", "summarize"}:
            suffix = ".csv" if stage == "sample" else ".summary.json"
            files = [r["reviews"] / f"{w['view']}-{w['profile']}{suffix}"]
        if not files or any(not p.is_file() for p in files):
            raise ValueError("命令结束但必要产物缺失：" + "; ".join(str(p) for p in files if not p.is_file()))
        if stage in {"doctor", "connection", "validate"} and not read_json(files[0]).get("ok"):
            raise ValueError("产物报告检查未通过")
        if stage == "finalize" and read_json(run / "metrics.json").get("status") != "success":
            raise ValueError("实例产物未全部成功，请检查 metrics 和失败记录")
        if stage == "export-rdf":
            from .rdf_export import input_fingerprint
            manifest = read_json(run / "rdf/export-manifest.json")
            if manifest.get("state") != "success" or manifest.get("input_fingerprint") != input_fingerprint(run, snap):
                raise ValueError("RDF 导出未完整通过或输入已变化，请查看 rdf/export-manifest.json")
        if stage in {"discover-local", "merge-schema", "review-schema", "extract"}:
            check = {"discover-local": "schema/local-manifest.json", "merge-schema": "schema/merge-status.json", "review-schema": "schema/review-status.json", "extract": "extraction/manifest.json"}[stage]
            if read_json(run / check).get("state") != "success":
                raise ValueError("阶段部分完成，请查看未完成任务")

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
            frozen_policy = load_policy(job["command"][job["command"].index("--config") + 1], frozen=True) if "--config" in job["command"] else None
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
                    line = redact(line, secret)
                    line = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "<redacted>", line)
                    marker = line.find("CO_EVENT ")
                    event = None
                    if marker >= 0:
                        try:
                            event, end = json.JSONDecoder().raw_decode(line[marker+len("CO_EVENT "):])
                            if not isinstance(event, dict) or not {"time", "level", "stage", "message", "kind"} <= event.keys():
                                event = None
                        except (ValueError, TypeError):
                            pass  # Text logs must never terminate an otherwise healthy child.
                    if event is not None:
                        if marker:
                            log.write(line[:marker] + "\n")
                        trailing = line[marker+len("CO_EVENT ")+end:].strip()
                        if trailing:
                            log.write(trailing + "\n")
                        log.write(f"{event['time']} [{event['level']}] {event['stage']} {event['message']}\n")
                        if event["kind"] == "progress":
                            self.update_job(jid, progress={"phase": event["stage"], **{
                                key: event.get(key, 0) for key in ("completed", "total", "successful", "failed", "unknown", "remaining", "elapsed_seconds")}})
                        else:
                            self.update_job(jid, last_event=event)
                        continue
                    log.write(line)
                    if line.startswith("SNAPSHOT="):
                        discovered = line.strip().split("=", 1)[1]
                    match = re.search(r"TASK (discover|extract|merge) (\d+)/(\d+) \((\d+) successful\)", line)
                    if match:
                        self.update_job(jid, progress={"phase": match[1], "completed": int(match[2]),
                                                      "total": int(match[3]), "successful": int(match[4]),
                                                      "failed": max(0,int(match[2])-int(match[4]))})
                    merge = re.search(r"MERGE round=(\d+) batch=(\d+)/(\d+)", line)
                    if merge:
                        self.update_job(jid, progress={"phase": f"merge round {merge[1]}", "completed": int(merge[2])-1,
                                                      "total": int(merge[3]), "successful": int(merge[2])-1})
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
                if w["view"] not in snapshot_parser(target)["available_views"]:
                    w["view"] = "ast"
                self._save_workspace(w)
                self.update_job(jid, snapshot=str(target))
            state = "succeeded" if code == 0 else "failed"
            if self.job(jid)["state"] == "cancelling":
                state = "cancelled"
            elif code and job["stage"] == "export-rdf" and w["snapshot"]:
                rdf_manifest = self.roots(w)["run"] / "rdf/export-manifest.json"
                if rdf_manifest.is_file() and read_json(rdf_manifest).get("state") == "partial":
                    state = "partial"
            elif code and w["snapshot"] and job["stage"] in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract", "experiment"}:
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
            if w["snapshot"]:
                run = self.roots(w)["run"]
                metrics = run / "metrics.json"
                if job["stage"] in {"extract", "finalize", "experiment"} and metrics.is_file():
                    write_json(Path(job["log"]).parent / "metrics.json", read_json(metrics))
                if job["stage"] == "discover" and (run / "schema" / "provenance.json").is_file():
                    actual = read_json(run / "schema" / "provenance.json")
                    expected_prompt = digest(prompt(frozen_policy, w["profile"], "discover") + prompt(frozen_policy, w["profile"], "merge"))
                    self.update_job(jid, schema_prompt_matches=actual.get("prompt_hash") == expected_prompt,
                                    schema_note="固定 Schema 默认复用；提示词变动需要勾选更新 Schema 或新建实验")
            self.update_job(jid, state=state, exit_code=code, ended_at=now(),
                            artifact_status="current" if state == "succeeded" else "incomplete",
                            fingerprint=self.fingerprint(w, frozen_policy, frozen_settings, job["stage"])
                            if state == "succeeded" and frozen_policy else job["fingerprint"])
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

    def events(self, jid, offset=0, level="", query=""):
        job = self.job(jid)
        return read_events(Path(job["log"]).parent / "events.jsonl", offset, level=level, query=query)

    def executions(self, wid):
        workspace = self.workspace(wid)
        root = Path(workspace["out"]) / "executions"
        rows = []
        # Explicit execution IDs link CLI history; never guess a latest snapshot.
        for path in sorted(root.glob("*/execution.json"), reverse=True)[:200]:
            if within(path.resolve(), root.resolve()):
                value = read_json(path)
                rows.append({key: value.get(key) for key in ("execution_id", "experiment", "stage", "state", "started_at", "ended_at", "elapsed_seconds")})
        for session_path in (Path(workspace["out"]) / "sessions").glob("*.json"):
            if not within(session_path.resolve(), Path(workspace["out"]).resolve()):
                continue
            session = read_json(session_path)
            for stage, record in session.get("stages", {}).items():
                if record.get("execution_id") or not record.get("log"):
                    continue
                rows.append({"execution_id": "legacy-"+digest(record["log"],24), "experiment": session.get("experiment"),
                             "stage": stage, "state": record.get("state"), "started_at": record.get("started_at"), "legacy": True})
        return rows

    def execution_events(self, wid, identifier, offset=0, level="", query=""):
        if identifier.startswith("legacy-") and any(row["execution_id"] == identifier for row in self.executions(wid)):
            return {"rows": [], "next_offset": 0, "has_more": False}
        if not re.fullmatch(r"[\w-]{1,100}", identifier):
            raise ValueError("Invalid execution ID")
        root = Path(self.workspace(wid)["out"]) / "executions"
        directory = root / identifier
        if not within(directory.resolve(), root.resolve()) or not within((directory / "events.jsonl").resolve(), root.resolve()) or not (directory / "execution.json").is_file():
            raise ValueError("Unknown execution")
        return read_events(directory / "events.jsonl", offset, level=level, query=query)

    def execution_log(self, wid, identifier, offset=0):
        workspace = self.workspace(wid)
        out = Path(workspace["out"]).resolve()
        log = None
        if identifier.startswith("legacy-"):
            for session_path in (out / "sessions").glob("*.json"):
                if not within(session_path.resolve(), out):
                    continue
                for record in read_json(session_path).get("stages", {}).values():
                    if record.get("log") and "legacy-"+digest(record["log"],24) == identifier:
                        log = Path(record["log"])
        elif re.fullmatch(r"[\w-]{1,100}", identifier):
            metadata = out / "executions" / identifier / "execution.json"
            if within(metadata.resolve(), out) and metadata.is_file():
                value = read_json(metadata)
                log = Path(value["log"]) if value.get("log") else None
        if log is None:
            return {"text": "", "next_offset": 0}
        if not within(log.resolve(), out) or log.suffix != ".log":
            raise ValueError("Execution log path rejected")
        return self.redact_data(self.preview.read(log, offset=offset), self.settings(workspace)[1].api_key)

    def prompt_copy(self, wid, stage, text):
        if stage not in {"discover", "merge", "review", "extract"} or len(text) > 100000:
            raise ValueError("提示词阶段或长度无效")
        with self.lock:
            if any(j["state"] in ACTIVE for j in self.jobs(wid)):
                raise Conflict("任务执行中不能切换提示词")
            w = self.workspace(wid); policy, _ = self.settings(w)
            from .config_bundle import read_bundle
            bundle = read_bundle(w.get("config"))
            target = self.state / "prompt-edits" / uuid.uuid4().hex[:12]
            shutil.copytree(Path(policy.get("prompt_dir") or RESOURCE / "prompts"), target / "prompts")
            (target / "prompts" / w["profile"] / f"{stage}.txt").write_text(text, encoding="utf-8")
            policy["prompt_dir"] = str(target / "prompts")
            # Preserve YAML stage defaults when editing only a prompt. Environment
            # overrides still resolve through the normal settings loader.
            frozen = {"config_version": 1, "defaults": {"policy": policy, "llm": bundle["llm"]}, "steps": {}}
            for name, overrides in bundle["stages"].items():
                filename = f"{name}.yaml"
                (target / filename).write_text(yaml.safe_dump({"llm": overrides}), encoding="utf-8")
                frozen["steps"][name] = filename
            (target / "policy.yaml").write_text(yaml.safe_dump(frozen, allow_unicode=True), encoding="utf-8")
            w["config"] = str(target / "policy.yaml"); self._save_workspace(w)
            return self.describe(wid)
