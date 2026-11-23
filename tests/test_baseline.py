from __future__ import annotations

import json
import os
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.cli import main, validate
from code_ontology.config import Settings, load_policy, load_settings
from code_ontology.joern import normalize
from code_ontology.llm import LLM, BudgetError, ModelFailure
from code_ontology.models import Ontology, Extraction
from code_ontology.pipeline import Pipeline, canonical_schema, evidence_check, comparison
from code_ontology.prepare import chunks, prepare, evidence_valid, type_end
from code_ontology.repository import snapshot, verify_sources
from code_ontology.util import digest, iter_jsonl, read_json, token_bound, write_json, write_jsonl
from tests.fake_api import fake_server


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ontology 中文 ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "代码 仓"
        self.repo.mkdir()
        self.policy = load_policy()
        self.policy["discovery_max_packs"] = 0
        (self.repo / "device.cpp").write_text("// 设备温度规则\nint monitor(int temperature) {\n  if (temperature > 85) return 1;\n  return 0;\n}\n", encoding="utf-8")
        self.dest = snapshot(self.repo, self.root / "out", self.policy)

    def tearDown(self):
        self.temp.cleanup()

    def graph(self):
        props = lambda **kw: {"FILENAME": str(self.dest / "source" / "device.cpp"), **kw}
        nodes = [
            {"id": "1", "label": "METHOD", "properties": props(NAME="monitor", FULL_NAME="monitor:int(int)", SIGNATURE="int(int)", LINE_NUMBER=2, LINE_NUMBER_END=5, IS_EXTERNAL=False)},
            {"id": "2", "label": "CONTROL_STRUCTURE", "properties": props(CODE="if (temperature > 85)", LINE_NUMBER=3)},
            {"id": "3", "label": "RETURN", "properties": props(CODE="return 1", LINE_NUMBER=3)},
            {"id": "4", "label": "RETURN", "properties": props(CODE="return 0", LINE_NUMBER=4)},
            {"id": "5", "label": "METHOD_RETURN", "properties": props(CODE="RET", LINE_NUMBER=5)},
        ]
        edges = [{"src": "1", "dst": str(i), "label": "AST"} for i in (2, 3, 4, 5)] + [
            {"src": "1", "dst": "2", "label": "CFG"}, {"src": "2", "dst": "3", "label": "CFG"},
            {"src": "2", "dst": "4", "label": "CFG"}, {"src": "3", "dst": "5", "label": "CFG"},
            {"src": "4", "dst": "5", "label": "CFG"}]
        write_jsonl(self.dest / "raw" / "cpg" / "nodes.jsonl", nodes)
        write_jsonl(self.dest / "raw" / "cpg" / "edges.jsonl", edges)
        normalize(self.dest, ["ast", "cfg", "cpg"])
        for view in ("ast", "cfg", "cpg"):
            prepare(self.dest, view, self.policy)

    def test_snapshot_exclusion_and_identity(self):
        (self.repo / "gold.json").write_text('{"secret_answer":true}')
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "bad.cpp").write_text("int simulated_failure;")
        new = snapshot(self.repo, self.root / "out", self.policy)
        self.assertEqual(self.dest, new)
        files = read_json(new / "manifest.json")["files"]
        self.assertEqual([r["file"] for r in files], ["device.cpp"])
        (self.repo / "device.cpp").write_text("int changed;")
        self.assertNotEqual(snapshot(self.repo, self.root / "out", self.policy), new)
        self.assertIn("monitor", (new / "source" / "device.cpp").read_text(encoding="utf-8"))

    def test_source_tamper_is_detected(self):
        (self.dest / "source" / "device.cpp").write_text("tampered")
        with self.assertRaises(ValueError):
            verify_sources(self.dest)

    def test_encoding_and_output_inside_repo(self):
        (self.repo / "legacy.h").write_bytes("// 设备\nint legacy;".encode("gb18030"))
        dest = snapshot(self.repo, self.repo / "generated", self.policy)
        again = snapshot(self.repo, self.repo / "generated", self.policy)
        self.assertEqual(dest, again)
        self.assertIn("设备", (dest / "source" / "legacy.h").read_text(encoding="utf-8"))

    def test_large_unicode_line_chunks(self):
        text = "//" + "温度" * 500 + "\nint x;\n"
        parts = list(chunks(text, 1, 100))
        self.assertEqual("".join(p[2] for p in parts), text)
        self.assertTrue(all(token_bound(p[2]) <= 100 for p in parts))

    def test_three_views_and_provenance(self):
        self.graph()
        selected = []
        for view in ("ast", "cfg", "cpg"):
            edges = list(iter_jsonl(self.dest / "normalized" / view / "edges.jsonl"))
            if view == "cfg":
                self.assertEqual({e["label"] for e in edges}, {"CFG"})
            packs = list(iter_jsonl(self.dest / "prepared" / view / "packs.jsonl"))
            self.assertTrue(all(evidence_valid(self.dest, p) for p in packs))
            self.assertTrue(all(token_bound(p) <= self.policy["pack_tokens"] for p in packs))
            selected.append({p["symbol_id"] for p in packs})
        self.assertEqual(selected[0], selected[1]); self.assertEqual(selected[1], selected[2])

    def test_graph_dangling_edges_rejected(self):
        self.graph()
        write_jsonl(self.dest / "raw" / "cpg" / "edges.jsonl", [{"src": "1", "dst": "missing", "label": "CFG"}])
        with self.assertRaises(ValueError):
            normalize(self.dest, ["cfg"])

    def test_schema_cycles_and_unknown_domain(self):
        concept = {"id": "c", "name": "设备", "definition": "设备", "parents": ["c"], "evidence_ids": ["ev"]}
        with self.assertRaises(ValueError):
            Ontology(classes=[concept], relations=[], properties=[])
        concept["parents"] = []
        with self.assertRaises(ValueError):
            Ontology(classes=[concept], relations=[{"id": "r", "name": "有", "definition": "有", "domain": ["unknown"], "range": ["c"], "evidence_ids": ["ev"]}], properties=[])

    def test_env_precedence(self):
        env = self.root / ".env"
        env.write_text("OPENAI_MODEL=file-model\nOPENAI_API_KEY=file-secret\n", encoding="utf-8")
        with patch.dict(os.environ, {"OPENAI_MODEL": "system-model"}):
            settings = load_settings(str(env))
        self.assertEqual(settings.model, "system-model")
        self.assertNotIn("api_key", settings.public())

    def test_llm_json_repair_and_cache(self):
        for mode in ("invalid_once", "empty_once", "bad_evidence_once", "unsupported_json"):
            with self.subTest(mode=mode), fake_server(mode) as (url, state):
                settings = Settings(base_url=url, model="fixture", api_key="secret", retries=0)
                llm = LLM(settings, self.root / mode)
                payload = {"stage": "discover", "packs": [{"evidence_ids": ["ev"]}]}
                schema = llm.call("Return JSON", payload, Ontology, self.root / "traces", lambda r: evidence_check(r, {"ev"}))
                self.assertEqual(len(schema.classes), 1)
                before = state["requests"]
                llm.call("Return JSON", payload, Ontology, self.root / "traces", lambda r: evidence_check(r, {"ev"}))
                self.assertEqual(state["requests"], before)
                self.assertEqual(llm.stats["cache_hits"], 1)
                llm.call("Changed prompt. Return JSON", payload, Ontology, self.root / "traces")
                self.assertGreater(state["requests"], before)
                llm.close()

    def test_thinking_compatibility_and_secret_free_traces(self):
        for thinking in ("auto", "disabled", "enabled"):
            with self.subTest(thinking=thinking), fake_server() as (url, state):
                traces = self.root / ("thinking-" + thinking)
                llm = LLM(Settings(base_url=url, model="fixture", api_key="secret-for-test", thinking=thinking),
                          self.root / ("cache-" + thinking))
                try:
                    llm.call("Return JSON", {"stage": "discover", "packs": [{"evidence_ids": ["ev"]}]}, Ontology, traces)
                    request = state["payloads"][0]
                    if thinking == "auto":
                        self.assertNotIn("thinking", request)
                    else:
                        self.assertEqual(request["thinking"], {"type": thinking})
                    self.assertEqual("temperature" in request, thinking != "enabled")
                    for trace in traces.glob("*.json"):
                        self.assertNotIn("secret-for-test", trace.read_text(encoding="utf-8"))
                finally:
                    llm.close()

    def test_auth_and_truncation_fail_explicitly(self):
        for mode, error in (("auth", ModelFailure), ("truncated", BudgetError)):
            with fake_server(mode) as (url, state):
                llm = LLM(Settings(base_url=url, model="fixture", retries=0), self.root / mode)
                with self.assertRaises(error):
                    llm.call("JSON", {"stage": "discover", "packs": []}, Ontology, self.root / "traces")
                self.assertEqual(state["requests"], 1)
                llm.close()

    def test_rate_limit_and_context_budget(self):
        with fake_server("rate_limit_once") as (url, state):
            llm = LLM(Settings(base_url=url, model="fixture", retries=1), self.root / "limited")
            with patch("code_ontology.llm.time.sleep"):
                llm.call("Return JSON", {"stage": "discover", "packs": [{"evidence_ids": ["ev"]}]}, Ontology, self.root / "traces")
            self.assertEqual(state["requests"], 2)
            with self.assertRaises(BudgetError):
                llm.call("x" * 30000, {}, Ontology, self.root / "traces")
            self.assertEqual(state["requests"], 2)
            llm.close()

    def test_empty_and_partial_tasks_are_visible_and_resumable(self):
        self.graph()
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=0)
            pipeline = Pipeline(self.dest, "cpg", "maintenance", "partial", settings, self.policy)
            schema = pipeline.fixed_schema()
            original_call = pipeline.llm.call
            def fail_extract(system, payload, *args, **kwargs):
                if payload.get("stage") == "extract":
                    raise ValueError("temporary malformed response")
                return original_call(system, payload, *args, **kwargs)
            with patch.object(pipeline.llm, "call", side_effect=fail_extract):
                self.assertFalse(pipeline.extract(schema))
            self.assertEqual(read_json(pipeline.output / "metrics.json")["status"], "partial")
            self.assertTrue(list((pipeline.output / "extraction").glob("*.failed.json")))
            pipeline.close()
            resumed = Pipeline(self.dest, "cpg", "maintenance", "partial", settings, self.policy)
            self.assertTrue(resumed.extract(resumed.fixed_schema()))
            self.assertFalse(list((resumed.output / "extraction").glob("*.failed.json")))
            self.assertEqual(read_json(resumed.output / "metrics.json")["status"], "success")
            resumed.close()

    def test_graph_update_invalidates_prepared_cache(self):
        self.graph()
        before = read_json(self.dest / "prepared" / "cpg" / "index.json")["key"]
        edges = list(iter_jsonl(self.dest / "raw" / "cpg" / "edges.jsonl"))
        edges.append({"src": "2", "dst": "4", "label": "REF"})
        write_jsonl(self.dest / "raw" / "cpg" / "edges.jsonl", edges)
        normalize(self.dest, ["cpg"])
        prepare(self.dest, "cpg", self.policy)
        self.assertNotEqual(before, read_json(self.dest / "prepared" / "cpg" / "index.json")["key"])

    def test_type_extent_ignores_literal_and_comment_braces(self):
        lines = ['class Device {\n', '  const char* x = "}"; // }\n', '  const char* y = R"tag({})tag";\n', '};\n', 'class Other {};\n']
        self.assertEqual(type_end(lines, 1), 4)
        self.assertEqual(type_end(['class Forward;\n', 'class Later {};\n'], 1), 1)

    def test_typed_assertions_rules_and_conflicts(self):
        self.graph()
        with fake_server() as (url, state):
            pipeline = Pipeline(self.dest, "cpg", "maintenance", "typed", Settings(base_url=url, model="fixture"), self.policy)
            ev = next(iter(pipeline.evidence))
            schema = Ontology(classes=[
                {"id": "device", "name": "设备", "definition": "设备", "evidence_ids": [ev]},
                {"id": "pump", "name": "泵", "definition": "泵设备", "parents": ["device"], "evidence_ids": [ev]}],
                relations=[{"id": "r", "name": "关联", "definition": "关联", "domain": ["device"], "range": ["device"], "evidence_ids": [ev]}],
                properties=[{"id": "p", "name": "温度", "definition": "温度", "domain": ["device"], "datatype": "number", "unit": "C", "evidence_ids": [ev]}])
            entity = {"id": "e", "name": "示例泵", "type_id": "pump", "scope": "example", "basis": "direct", "evidence_ids": [ev]}
            result = Extraction(entities=[entity],
                relations=[{"subject": "e", "relation_id": "r", "object": "e", "basis": "direct", "evidence_ids": [ev]}],
                attributes=[{"subject": "e", "property_id": "p", "value": value, "unit": "C", "basis": "direct", "evidence_ids": [ev]} for value in (85, 90, "not_a_number")],
                rules=[{"name": "超温规则", "condition": "temperature > 85", "action": "return 1", "scope": "code_definition", "basis": "direct", "evidence_ids": [ev]}])
            pipeline.finalize(schema, [result]); pipeline.close()
            metrics = read_json(pipeline.output / "metrics.json")
            self.assertEqual(metrics["relations"], 1)
            self.assertEqual(metrics["attributes"], 2)
            self.assertEqual(metrics["rules"], 1)
            self.assertEqual(metrics["conflicts"], 1)
            self.assertEqual(metrics["candidates"], 1)

    def test_cli_stage_commands_and_streaming_array_import(self):
        self.graph()
        for kind in ("nodes", "edges"):
            write_json(self.root / f"{kind}.json", list(iter_jsonl(self.dest / "raw" / "cpg" / f"{kind}.jsonl")))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["import-graph", "--snapshot", str(self.dest), "--nodes", str(self.root / "nodes.json"), "--edges", str(self.root / "edges.json")]), 0)
        env = self.root / "test.env"
        with fake_server() as (url, state):
            env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture\nOPENAI_API_KEY=fixture\n", encoding="utf-8")
            base = ["--snapshot", str(self.dest), "--view", "cpg", "--profile", "maintenance", "--env-file", str(env)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["discover"] + base), 0)
                self.assertEqual(main(["extract"] + base), 0)
                self.assertEqual(main(["report", "--snapshot", str(self.dest)]), 0)
            self.assertTrue((self.dest / "runs" / "baseline" / "cpg" / "maintenance" / "final" / "entities.jsonl").is_file())

    def test_prepared_changes_refresh_fixed_schema(self):
        self.graph()
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture")
            first = Pipeline(self.dest, "cpg", "maintenance", "refresh", settings, self.policy)
            first.fixed_schema(); first.close()
            packs_path = self.dest / "prepared" / "cpg" / "packs.jsonl"
            packs = list(iter_jsonl(packs_path))
            packs[0]["context"]["change_marker"] = "new view context"
            write_jsonl(packs_path, packs)
            before = state["requests"]
            second = Pipeline(self.dest, "cpg", "maintenance", "refresh", settings, self.policy)
            second.fixed_schema(); second.close()
            self.assertGreater(state["requests"], before)

    def test_six_combinations_and_resume(self):
        self.graph()
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=0)
            for view in ("ast", "cfg", "cpg"):
                for profile in ("general", "maintenance"):
                    pipeline = Pipeline(self.dest, view, profile, "fixture-baseline", settings, self.policy)
                    schema = pipeline.fixed_schema()
                    self.assertTrue(pipeline.extract(schema))
                    self.assertEqual(validate(self.dest, view, pipeline.output), 0)
                    pipeline.close()
            comparison(self.dest, "fixture-baseline")
            before = state["requests"]
            pipeline = Pipeline(self.dest, "cpg", "maintenance", "fixture-baseline", settings, self.policy)
            pipeline.extract(pipeline.fixed_schema()); pipeline.close()
            self.assertEqual(state["requests"], before)
            report = read_json(self.dest / "runs" / "fixture-baseline" / "comparison.json")
            self.assertEqual(len(report["runs"]), 6); self.assertFalse(report["missing"])

    def test_invalid_knowledge_goes_to_candidates(self):
        self.graph()
        with fake_server() as (url, state):
            pipeline = Pipeline(self.dest, "cpg", "maintenance", "validation", Settings(base_url=url, model="fixture"), self.policy)
            evidence = next(iter(pipeline.evidence))
            schema = Ontology(classes=[{"id": "c", "name": "设备", "definition": "设备", "evidence_ids": [evidence]}], relations=[], properties=[])
            extraction = Extraction(entities=[{"id": "e", "name": "无效", "type_id": "unknown", "scope": "test", "basis": "direct", "evidence_ids": [evidence]}], relations=[], attributes=[], rules=[])
            pipeline.finalize(schema, [extraction]); pipeline.close()
            self.assertEqual(len(list(iter_jsonl(pipeline.output / "final" / "entities.jsonl"))), 0)
            self.assertEqual(len(list(iter_jsonl(pipeline.output / "final" / "candidates.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()
