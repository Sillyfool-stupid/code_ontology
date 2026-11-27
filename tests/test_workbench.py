from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from code_ontology.config import PROJECT, load_policy
from code_ontology.joern import normalize
from code_ontology.util import read_json, write_json, write_jsonl
from code_ontology.workbench import WorkspaceArtifacts, make_server
from code_ontology.workbench_files import MAX_BYTES, Preview
from code_ontology.workbench_jobs import Conflict, Workbench
from tests.fake_api import fake_server


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench 中文 ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "代码 仓"
        self.repo.mkdir()
        (self.repo / "device.cpp").write_text("// 设备\nint monitor(int t) {\n if (t > 85) return 1;\n return 0;\n}\n", encoding="utf-8")
        self.env = self.root / ".env"
        self.key = "fixture-sensitive-key-never-persist"
        self.env.write_text(f"OPENAI_BASE_URL=http://127.0.0.1:1/v1\nOPENAI_MODEL=fixture-model\nOPENAI_API_KEY={self.key}\nLLM_MAX_RETRIES=0\n", encoding="utf-8")
        self.config = self.root / "policy.yaml"
        self.config.write_text("discovery_max_packs: 24\nparser_backend: joern\n", encoding="utf-8")
        self.service = Workbench(self.root / "control", project=PROJECT, browse_roots=[self.root])
        self.fields = {"repo": str(self.repo), "out": str(self.root / "outputs"), "config": str(self.config),
                       "env_file": str(self.env), "view": "cpg", "profile": "maintenance", "experiment": "test-01"}
        self.wid = self.service.save_workspace(self.fields)["workspace"]["id"]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def finish(self, job, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            j = self.service.job(job["id"])
            if j["state"] not in {"running", "queued", "cancelling"}:
                return j
            time.sleep(.05)
        self.fail("Task timed out: " + str(self.service.job(job["id"])))

    def run_stage(self, stage):
        j = self.finish(self.service.submit(self.wid, stage))
        self.assertEqual(j["state"], "succeeded", self.service.logs(j["id"]))
        return j

    def graph(self):
        snap = Path(self.service.workspace(self.wid)["snapshot"])
        props = {"FILENAME": str(snap / "source" / "device.cpp"), "NAME": "monitor", "FULL_NAME": "monitor:int(int)",
                 "SIGNATURE": "int(int)", "LINE_NUMBER": 2, "LINE_NUMBER_END": 5, "IS_EXTERNAL": False}
        nodes = [{"id":"1", "label":"METHOD", "properties":props},
                 {"id":"2", "label":"RETURN", "properties":{"FILENAME":"device.cpp","LINE_NUMBER":3,"CODE":"return 1"}}]
        write_jsonl(snap / "raw/cpg/nodes.jsonl", nodes)
        write_jsonl(snap / "raw/cpg/edges.jsonl", [{"src":"1","dst":"2","label":"AST"}, {"src":"1","dst":"2","label":"CFG"}])
        normalize(snap, ["ast","cfg","cpg"])
        return snap

    def test_empty_workspace_scan_and_guarded_parse(self):
        d = self.service.describe(self.wid)
        self.assertTrue(next(s for s in d["stages"] if s["id"] == "parse")["blocked"])
        j = self.run_stage("scan")
        snap = Path(j["snapshot"])
        self.assertTrue((snap / "manifest.json").is_file())
        self.assertNotIn(self.key, json.dumps(self.service.describe(self.wid)))
        self.assertFalse(next(s for s in self.service.describe(self.wid)["stages"] if s["id"] == "scan")["stale"])
        self.assertEqual(len(self.service.snapshots(self.root / "outputs")), 1)
        (self.repo / "device.cpp").write_text("int changed;", encoding="utf-8")
        j = self.finish(self.service.submit(self.wid, "parse"))
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["exit_code"], 2)
        self.assertNotEqual(j["snapshot"], str(snap))
        self.assertIn("本次未运行解析器", self.service.logs(j["id"])["text"])

    def save_step(self, stage, values):
        revision = self.service.describe(self.wid)["step_configurations"][stage]["revision"]
        return self.service.save_step_configuration(self.wid, stage, values, revision)

    def test_web_step_overrides_environment_and_restores_inheritance(self):
        with self.env.open("a", encoding="utf-8") as f:
            f.write("LLM_MERGE_TIMEOUT_SECONDS=555\nCO_MERGE_BATCH_ITEMS=9\n")
        before = self.env.read_bytes(), self.config.read_bytes()
        original = self.service.describe(self.wid)
        w = self.service.workspace(self.wid)
        local_before = self.service.fingerprint(w, *self.service.settings(w), stage="discover-local")
        merge_before = self.service.fingerprint(w, *self.service.settings(w), stage="merge-schema")
        merged = self.save_step("merge-schema", {"policy": {"merge_batch_items": 3}, "llm": {"timeout": 777}})
        self.assertEqual(merged["stage_settings"]["merge"]["timeout"], 777)
        self.assertEqual(merged["policy"]["merge_batch_items"], 3)
        self.assertEqual(merged["stage_settings"]["discover"], original["stage_settings"]["discover"])
        self.assertEqual(self.service.fingerprint(w, *self.service.settings(w), stage="discover-local"), local_before)
        self.assertNotEqual(self.service.fingerprint(w, *self.service.settings(w), stage="merge-schema"), merge_before)
        config = merged["step_configurations"]["merge-schema"]
        field = next(f for f in config["fields"] if f["key"] == "timeout")
        self.assertEqual(field["inherited"], 555)
        self.assertEqual(field["source"], config["file"])
        self.assertEqual(yaml.safe_load(Path(config["file"]).read_text(encoding="utf-8")), config["overrides"])
        restored = self.save_step("merge-schema", {})
        self.assertEqual(restored["stage_settings"]["merge"]["timeout"], 555)
        self.assertEqual(restored["policy"]["merge_batch_items"], 9)
        self.assertEqual(before, (self.env.read_bytes(), self.config.read_bytes()))
        other = self.service.save_workspace(self.fields)["workspace"]["id"]
        self.assertNotEqual(other, self.wid)
        self.assertFalse(Path(self.service.describe(other)["step_configurations"]["merge-schema"]["file"]).exists())

    def test_web_step_validation_conflicts_and_no_credentials(self):
        original = self.service.describe(self.wid)["step_configurations"]["merge-schema"]
        for values in ({"llm": {"api_key": "forbidden"}}, {"llm": {"timeout": -1}},
                       {"llm": {"concurrency": True}}, {"llm": {"context_tokens": 100}},
                       {"policy": {"extract_batch_packs": 2}}, {"llm": {"timeout": float("nan")}}):
            with self.assertRaises(ValueError):
                self.service.save_step_configuration(self.wid, "merge-schema", values, original["revision"])
        self.assertFalse(Path(original["file"]).exists())
        self.save_step("merge-schema", {"llm": {"timeout": 800}})
        with self.assertRaises(Conflict):
            self.service.save_step_configuration(self.wid, "merge-schema", {}, original["revision"])
        with self.assertRaises(ValueError):
            self.service.save_step_configuration(self.wid, "../../outside", {}, "x")
        self.assertNotIn(self.key, json.dumps(self.service.describe(self.wid)))
        plan = self.service.plan(self.wid, "scan")
        self.save_step("scan", {"policy": {"max_files": 1}})
        with self.assertRaisesRegex(Conflict, "计划预览后变化"):
            self.service.submit(self.wid, "scan", {"configuration_revision": plan["configuration_revision"]})
        revision = self.service.describe(self.wid)["step_configurations"]["merge-schema"]["revision"]
        with self.env.open("a", encoding="utf-8") as f:
            f.write("LLM_MERGE_TIMEOUT_SECONDS=333\n")
        with self.assertRaises(Conflict):
            self.service.save_step_configuration(self.wid, "merge-schema", {}, revision)

    def test_web_parser_override_used_when_resaving_workspace(self):
        self.save_step("parse", {"policy": {"parser_backend": "tree-sitter"}})
        changed = self.service.save_workspace({**self.fields, "id": self.wid, "view": "cpg"})
        self.assertEqual(changed["workspace"]["view"], "ast")
        self.assertEqual(changed["policy"]["parser_backend"], "tree-sitter")

    def test_web_step_settings_reach_subprocess_and_do_not_change_running_job(self):
        (self.repo / "second.cpp").write_text("int second;", encoding="utf-8")
        self.save_step("scan", {"policy": {"max_files": 1}})
        # Hold the worker at the existing queue lock while editing next-run settings.
        with self.service.lock:
            job = self.service.submit(self.wid, "scan")
            frozen = read_json(Path(job["log"]).parent / "effective-config.json")
            self.save_step("scan", {"policy": {"max_files": 2}})
            self.assertEqual(frozen["policy"]["max_files"], 1)
            self.assertEqual(read_json(Path(job["log"]).parent / "effective-config.json"), frozen)
            self.assertEqual(self.service.describe(self.wid)["policy"]["max_files"], 2)
        self.assertEqual(self.finish(job)["state"], "succeeded")
        snap = Path(self.service.workspace(self.wid)["snapshot"])
        self.assertEqual(len(read_json(snap / "manifest.json")["files"]), 1)

    def test_web_model_step_settings_reach_requests_and_preserve_local_cache(self):
        with fake_server() as (url, state):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-only\nOPENAI_API_KEY={self.key}\n", encoding="utf-8")
            self.run_stage("scan"); self.graph(); self.run_stage("prepare")
            self.save_step("discover-local", {"llm": {"max_output_tokens": 512, "thinking": "disabled"}})
            job = self.run_stage("discover-local")
            self.assertEqual(state["payloads"][0]["max_tokens"], 512)
            local_count = state["stage_counts"]["discover"]
            self.save_step("merge-schema", {"llm": {"max_output_tokens": 1024}})
            self.run_stage("merge-schema")
            self.run_stage("discover-local")
            self.assertEqual(state["stage_counts"]["discover"], local_count)
            text = self.service.attempt_file(job["id"], "effective-config.json")["text"]
            self.assertNotIn(self.key, text)

    def test_real_subprocess_stages_with_synthetic_api_and_resume(self):
        with fake_server() as (url, transport):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-only\nOPENAI_API_KEY={self.key}\n", encoding="utf-8")
            self.run_stage("scan")
            snap = self.graph()
            self.run_stage("prepare")
            self.run_stage("discover")
            self.assertEqual(transport["stage_counts"].get("review", 0), 0)
            extracted = self.run_stage("extract")
            self.assertTrue((Path(extracted["log"]).parent / "input-schema.json").is_file())
            extracted = self.run_stage("finalize")
            self.assertTrue((Path(extracted["log"]).parent / "metrics.json").is_file())
            self.run_stage("validate")
            self.run_stage("report")
            self.run_stage("sample")
            self.run_stage("summarize")
            run = snap / "runs/test-01/cpg/maintenance"
            self.assertTrue(read_json(run / "validation.json")["ok"])
            before = transport["requests"]
            self.run_stage("extract"); self.run_stage("finalize")
            self.assertEqual(before, transport["requests"])
            for path in (self.root / "control").rglob("*"):
                if path.is_file() and path.suffix in {".json", ".log", ".yaml"}:
                    self.assertNotIn(self.key, path.read_text(encoding="utf-8"))
            self.assertIn("entities", self.service.artifact(self.wid, "run", "metrics.json")["text"])

    def test_stale_pack_is_blocked_and_prompt_copy_is_independent(self):
        self.run_stage("scan"); self.graph(); self.run_stage("prepare")
        self.config.write_text("pack_tokens: 5000\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "证据包"):
            self.service.submit(self.wid, "discover")
        changed = self.service.prompt_copy(self.wid, "discover", "仅测试设备")
        self.assertEqual(changed["prompt_sources"]["discover"], "仅测试设备")
        self.assertNotEqual(changed["workspace"]["config"], str(self.config))
        self.assertEqual(self.config.read_text(encoding="utf-8"), "pack_tokens: 5000\n")

    def test_workbench_extracts_merged_schema_without_optional_review(self):
        with fake_server() as (url, state):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-only\nCO_ALLOW_UNREVIEWED_SCHEMA=false\n", encoding="utf-8")
            self.run_stage("scan"); self.graph(); self.run_stage("prepare")
            self.run_stage("discover-local")
            with self.assertRaisesRegex(ValueError, "合并 Schema"):
                self.service.submit(self.wid, "extract")
            self.run_stage("merge-schema")
            merged = self.run_stage("extract")
            self.assertNotIn("--require-schema-review", merged["command"])
            self.assertIn("--require-current-schema", merged["command"])
            run = self.service.roots(self.service.workspace(self.wid))["run"]
            self.assertEqual(read_json(run / "schema-scope.json")["review_state"], "merged_unreviewed")
            self.assertEqual(state["stage_counts"].get("review", 0), 0)
            strict_command = list(merged["command"]) + ["--require-schema-review"]
            rejected = subprocess.run(strict_command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("review-schema", rejected.stderr)
            self.run_stage("review-schema")
            reviewed = self.run_stage("extract")
            self.assertEqual(read_json(run / "schema-scope.json")["review_state"], "ai_reviewed_not_human_verified")
            run = self.service.roots(self.service.workspace(self.wid))["run"]
            current = read_json(run / "schema/schema.json")
            altered = json.loads(json.dumps(current))
            altered["classes"][0]["name"] = "Different frozen schema"
            alternate = self.root / "alternate-schema.json"
            write_json(alternate, altered)
            command = list(reviewed["command"])
            command[command.index("--schema") + 1] = str(alternate)
            rejected = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Frozen schema differs", rejected.stderr)
            self.assertEqual(read_json(run / "schema/schema.json"), current)
            self.run_stage("merge-schema")
            self.run_stage("extract")
            run = self.service.roots(self.service.workspace(self.wid))["run"]
            self.assertEqual(read_json(run / "schema-scope.json")["review_state"], "merged_unreviewed")

    def test_frozen_config_and_reused_schema_are_marked_stale(self):
        with fake_server() as (url, _):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-only\n", encoding="utf-8")
            self.run_stage("scan"); self.graph(); self.run_stage("prepare"); self.run_stage("discover")
            self.service.prompt_copy(self.wid, "discover", "修改后的发现指导")
            reused = self.run_stage("discover")
            self.assertFalse(reused["schema_prompt_matches"])
            self.assertTrue(next(s for s in self.service.describe(self.wid)["stages"] if s["id"] == "discover")["stale"])
            changed = threading.Event()
            original = self.service._check_outputs
            def check(stage, w):
                original(stage, w)
                if stage == "prepare":
                    config = Path(w["config"])
                    policy = load_policy(str(config)); policy["pack_tokens"] += 100
                    config.write_text(yaml.safe_dump(policy), encoding="utf-8")
                    changed.set()
            with patch.object(self.service, "_check_outputs", side_effect=check):
                self.run_stage("prepare")
            self.assertTrue(changed.is_set())
            self.assertTrue(next(s for s in self.service.describe(self.wid)["stages"] if s["id"] == "prepare")["stale"])

    def test_schema_visualization_before_instances_and_version_separation(self):
        with fake_server() as (url, _):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-only\n", encoding="utf-8")
            self.run_stage("scan"); self.graph(); self.run_stage("prepare"); self.run_stage("discover")
            w = self.service.workspace(self.wid)
            store = WorkspaceArtifacts(w, schema_only=True)
            rid = store.listing()["runs"][0]["id"]
            detail = store.detail(rid)
            self.assertEqual(len(detail["schema"]["classes"]), 1)
            self.assertIn("无需先抽取实例", detail["workbench_note"])
            evidence = detail["schema"]["classes"][0]["evidence_ids"][0]
            self.assertTrue(store.evidence(rid, evidence)["source_hash_matches"])
            self.run_stage("extract"); self.run_stage("finalize")
            run = Path(w["snapshot"]) / "runs/test-01/cpg/maintenance"
            new_schema = read_json(run / "schema/schema.json")
            new_schema["classes"][0]["name"] = "新发现概念"
            write_json(run / "schema/schema.json", new_schema)
            self.assertEqual(store.detail(rid)["schema"]["classes"][0]["name"], "新发现概念")
            self.assertEqual(store.records(rid, "entities")["total"], 0)
            final = WorkspaceArtifacts(w)
            final.listing()
            self.assertNotEqual(final.detail(rid)["schema"]["classes"][0]["name"], "新发现概念")
            self.assertIn("不一致", final.detail(rid)["workbench_note"])
            self.assertGreater(final.records(rid, "entities")["total"], 0)

    def test_zero_exit_without_artifacts_is_failed_and_live_child_is_cleaned(self):
        with patch.object(self.service, "_command", return_value=[sys.executable, "-c", "pass"]):
            j = self.finish(self.service.submit(self.wid, "scan"))
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["exit_code"], 0)
        self.assertEqual(j["artifact_status"], "incomplete")
        self.assertIn("产物缺失", j["error"])
        command = [sys.executable, "-u", "-c", "import time;print('ready',flush=True);time.sleep(60)"]
        original = self.service.update_job
        def update(jid, **values):
            result = original(jid, **values)
            if values.get("state") == "running":
                # Force failure after the child starts and before log consumption.
                raise OSError("forced execution error")
            return result
        with patch.object(self.service, "_command", return_value=command), patch.object(self.service, "update_job", side_effect=update):
            j = self.finish(self.service.submit(self.wid, "scan"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("forced execution error", j["error"])
        self.assertNotIn(j["id"], self.service.processes)

    def test_duplicate_start_cancel_and_single_server(self):
        with self.assertRaises(Conflict):
            Workbench(self.root / "control", project=PROJECT)
        command = [sys.executable, "-u", "-c", "import time;print('waiting',flush=True);time.sleep(60)"]
        with patch.object(self.service, "_command", return_value=command):
            j = self.service.submit(self.wid, "scan")
            duplicate = self.service.submit(self.wid, "scan")
            self.assertEqual(j["id"], duplicate["id"])
            with self.assertRaises(Conflict):
                self.service.save_workspace({**self.fields,"id":self.wid})
            with self.assertRaises(Conflict):
                self.service.submit(self.wid, "doctor")
        deadline = time.monotonic()+5
        while self.service.job(j["id"])["state"] == "queued" and time.monotonic()<deadline:
            time.sleep(.02)
        self.service.cancel(j["id"])
        self.assertEqual(self.finish(j)["state"], "cancelled")

    def test_cancel_terminates_descendant(self):
        pidfile = self.root / "child.pid"
        child = "import time; time.sleep(60)"
        code = "import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid));print('spawned',flush=True);time.sleep(60)"
        with patch.object(self.service, "_command", return_value=[sys.executable,"-u","-c",code]):
            j = self.service.submit(self.wid,"scan")
        deadline = time.monotonic()+8
        while not pidfile.exists() and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertTrue(pidfile.exists())
        pid = int(pidfile.read_text())
        self.service.cancel(j["id"])
        self.assertEqual(self.finish(j)["state"],"cancelled")
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                exitcode = ctypes.c_ulong()
                kernel.GetExitCodeProcess(handle, ctypes.byref(exitcode)); kernel.CloseHandle(handle)
                self.assertNotEqual(exitcode.value,259)
        else:
            try:
                os.kill(pid,0)
            except ProcessLookupError:
                pass
            else:
                self.assertEqual(Path(f"/proc/{pid}/stat").read_text().split()[2],"Z")

    def test_preview_paging_private_paths_and_record_limit(self):
        self.run_stage("scan")
        snap = Path(self.service.workspace(self.wid)["snapshot"])
        target = snap / "records.jsonl"
        write_jsonl(target, ({"i":i,"name":"设备"} for i in range(3000)))
        first = self.service.artifact(self.wid,"snapshot","records.jsonl",offset=2500,limit=10)
        self.assertEqual(first["total"],3000)
        self.assertEqual(first["rows"][0]["payload"]["i"],2500)
        second = self.service.artifact(self.wid,"snapshot","records.jsonl",offset=first["next_offset"],limit=10)
        self.assertEqual(second["rows"][0]["payload"]["i"],2510)
        write_jsonl(target,[{"huge":"x"*(MAX_BYTES*2)}])
        self.assertTrue(self.service.artifact(self.wid,"snapshot","records.jsonl")["rows"][0]["truncated"])
        for root,path in [("repo","../.env"),("config",".env"),("snapshot","cache/llm/responses/key.json")]:
            with self.assertRaises(ValueError):
                self.service.artifact(self.wid,root,path)
        secret = self.repo / "unusual.txt"; secret.write_text(self.key)
        result = self.service.artifact(self.wid,"repo","unusual.txt")
        self.assertNotIn(self.key, json.dumps(result))

    def test_restart_requires_manual_orphan_acknowledgement(self):
        fake = {"id":"orphan", "workspace":self.wid,"stage":"scan","state":"running","fingerprint":"old","log":str(self.root/"missing.log")}
        with self.service.db() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?)",("orphan",self.wid,"running",json.dumps(fake)))
        self.service.close()
        self.service = Workbench(self.root/"control",project=PROJECT,browse_roots=[self.root])
        self.assertEqual(self.service.job("orphan")["state"],"interrupted")
        with self.assertRaises(Conflict):
            self.service.submit(self.wid,"scan")
        self.service.acknowledge("orphan")
        self.run_stage("scan")

    def test_http_session_origin_artifacts_and_native_ontology(self):
        server = make_server(self.root/"http-control",0,PROJECT,[self.root])
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def get(route):
            with urlopen(base+route) as r:
                return r.read().decode("utf-8")
        token = json.loads(get('/api/bootstrap'))['token']
        try:
            index = get('/')
            self.assertIn("步骤工作台", index)
            self.assertIn('id="ontology-list"', index)
            self.assertIn('id="ontology-list-view"', index)
            self.assertIn('id="ontology-graph-view"', index)
            self.assertIn('<iframe id="ontology-graph"', index)
            self.assertIn("function renderDetail",get('/app.js'))
            self.assertIn("function loadOntology",get('/app.js'))
            payload=json.dumps(self.fields).encode()
            for headers in [{}, {"X-Workbench-Token":token,"Origin":"https://evil.invalid"}]:
                with self.assertRaises(HTTPError) as e:
                    urlopen(Request(base+'/api/workspace',data=payload,headers=headers))
                self.assertEqual(e.exception.code,403)
            with urlopen(Request(base+'/api/workspace',data=payload,headers={"X-Workbench-Token":token})) as r:
                result=json.load(r)
            wid=result['workspace']['id']
            self.assertNotIn(self.key,json.dumps(result))
            step_payload = json.dumps({"workspace": wid, "stage": "merge-schema", "values": {"llm": {"timeout": 900}},
                                       "revision": result["step_configurations"]["merge-schema"]["revision"]}).encode()
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base+'/api/step-configuration', data=step_payload))
            self.assertEqual(e.exception.code, 403)
            with urlopen(Request(base+'/api/step-configuration', data=step_payload, headers={"X-Workbench-Token": token})) as r:
                saved = json.load(r)
            self.assertEqual(saved["stage_settings"]["merge"]["timeout"], 900)
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base+'/api/step-configuration', data=step_payload, headers={"X-Workbench-Token": token}))
            self.assertEqual(e.exception.code, 409)
            with patch.object(server.service, "cancel", side_effect=RuntimeError("process tree remains locked")):
                with self.assertRaises(HTTPError) as e:
                    urlopen(Request(base+'/api/cancel',data=b'{"id":"fixture"}',headers={"X-Workbench-Token":token}))
                self.assertEqual(e.exception.code,500)
                self.assertIn("process tree remains locked",e.exception.read().decode())
            runs = json.loads(get('/api/ontology-runs?workspace='+wid+'&mode=schema'))
            self.assertEqual(runs['runs'], [])
            # The independent viewer remains available for old bookmarks.
            self.assertIn('/viewer-assets/app.js?workspace=',get('/viewer/?workspace='+wid))
            self.assertIn('/viewer-api/',get('/viewer-assets/app.js?workspace='+wid))
            schema_js = get('/viewer-assets/app.js?workspace='+wid+'&mode=schema')
            self.assertIn('mode:"schema"',schema_js)
            self.assertIn('d.workbench_note',schema_js)
            with self.assertRaises(HTTPError):
                get('/.env')
        finally:
            server.shutdown();server.server_close();server.service.close();thread.join()


if __name__ == "__main__":
    unittest.main()
