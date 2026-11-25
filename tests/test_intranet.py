"""Recovery, stage isolation and configuration behavior, using only local fake models."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.config import Settings, load_policy, load_settings
from code_ontology.intranet import main, writer_lock, execute
from code_ontology.llm import RequestUnknown
from code_ontology.pipeline import Pipeline, prompt
from code_ontology.prepare import prepare
from code_ontology.parsing import parse_repository, normalize_repository
from code_ontology.repository import snapshot
from code_ontology.util import read_json, write_json, now
from tests.fake_api import fake_server, fixture_response


class IntranetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intranet-stage-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for i in range(4):
            (self.repo / f"device{i}.cpp").write_text(f"int device{i}(int x) {{ if(x>80) return x+1; return 0; }}\n", encoding="utf-8")
        self.env = self.root / ".env"
        self.env.write_text("", encoding="utf-8")
        self.policy = load_policy(env_file=str(self.env))
        self.policy.update(parser_backend="tree-sitter", views=["ast"], profiles=["general"],
                           discovery_max_packs=0, batch_packs=1, merge_batch_items=2)
        self.dest = snapshot(self.repo, self.root / "out", self.policy)
        parse_repository(self.dest, Settings(), self.policy, ["ast"], raw_only=True)
        self.assertFalse((self.dest / "normalized").exists())
        normalize_repository(self.dest, ["ast"])
        prepare(self.dest, "ast", self.policy)

    def tearDown(self):
        self.temp.cleanup()

    def pipeline(self, settings, name="test"):
        return Pipeline(self.dest, "ast", "general", name, settings, self.policy)

    def test_env_stage_inheritance_and_paths(self):
        self.env.write_text('LLM_MAX_OUTPUT_TOKENS=2048\nLLM_MERGE_MAX_OUTPUT_TOKENS=6000\n'
                            'CO_PARSER=tree-sitter\nCO_DISCOVERY_MODE=full\nCO_INCLUDE_DIRS=["include"]\n'
                            'CO_PROMPT_DIR=prompts\nCO_OUTPUT_LANGUAGE=en\n', encoding="utf-8")
        settings = load_settings(str(self.env))
        self.assertEqual(settings.for_stage("extract").max_output_tokens, 2048)
        self.assertEqual(settings.for_stage("merge").max_output_tokens, 6000)
        policy = load_policy(env_file=str(self.env))
        self.assertEqual(policy["discovery_max_packs"], 0)
        self.assertEqual(policy["prompt_dir"], str(self.root / "prompts"))
        with patch.dict(os.environ, {"CO_OUTPUT_LANGUAGE": "mixed"}):
            self.assertEqual(load_policy(env_file=str(self.env))["output_language"], "mixed")
        self.assertTrue(now().endswith("+08:00"))

    def test_optional_output_cap_transport_and_stage_override(self):
        from code_ontology.llm import LLM, BudgetError
        self.env.write_text('LLM_SEND_MAX_TOKENS=false\nLLM_MERGE_SEND_MAX_TOKENS=true\n', encoding="utf-8")
        settings = load_settings(str(self.env))
        self.assertFalse(settings.for_stage("discover").send_max_tokens)
        self.assertTrue(settings.for_stage("merge").send_max_tokens)
        with fake_server() as (url, state):
            for send in (False, True):
                llm = LLM(replace(settings, base_url=url, model="fixture", send_max_tokens=send), self.root / "cap-cache")
                try:
                    llm._request([{"role": "user", "content": "{}"}], self.root / "traces", str(send))
                    self.assertEqual("max_tokens" in state["payloads"][-1], send)
                    self.assertNotIn("max_completion_tokens", state["payloads"][-1])
                    self.assertFalse(llm.fits([{"role": "user", "content": "x" * 20000}]))
                finally:
                    llm.close()
        with fake_server(mode="truncated") as (url, state):
            llm = LLM(replace(settings, base_url=url, model="fixture", send_max_tokens=False), self.root / "length-cache")
            try:
                with self.assertRaises(BudgetError):
                    llm._request([{"role": "user", "content": "{}"}], self.root / "traces", "length")
            finally:
                llm.close()
        self.env.write_text('LLM_SEND_MAX_TOKENS=invalid\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_settings(str(self.env))

    def test_merge_timeout_resume_does_not_rediscover(self):
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=0)
            pipeline = self.pipeline(settings)
            try:
                pipeline.discover_local()
                discovers = state["stage_counts"]["discover"]
                state["fail_merge_on"] = 2
                with self.assertRaises(RequestUnknown):
                    pipeline.merge_schema()
                self.assertEqual(read_json(pipeline.output / "schema/merge-status.json")["state"], "partial")
                first_history = Path(read_json(pipeline.output / "schema/merge-status.json")["history"])
                self.assertTrue((first_history / "round-001/batch-001/result/schema.json").is_file())
                self.assertEqual(read_json(first_history / "manifest.json")["state"], "partial")
                first_schema = read_json(first_history / "round-001/batch-001/result/schema.json")
                calls = state["stage_counts"]["merge"]
                pipeline.merge_schema()
                # First successful merge batch is a checkpoint, only failed/later batches request again.
                self.assertEqual(state["stage_counts"]["merge"] - calls, 2)
                self.assertEqual(state["stage_counts"]["discover"], discovers)
                second_history = Path(read_json(pipeline.output / "schema/merge-status.json")["history"])
                self.assertNotEqual(first_history, second_history)
                self.assertEqual(read_json(first_history / "round-001/batch-001/result/schema.json"), first_schema)
                round_files = list(second_history.glob("round-*/result/schema*.json"))
                self.assertGreaterEqual(len(round_files), 2)
                for saved in round_files:
                    from code_ontology.models import Ontology
                    Ontology.model_validate(read_json(saved))
                before = state["requests"]
                pipeline.merge_schema()
                self.assertEqual(state["requests"], before)
            finally:
                pipeline.close()

    def test_merge_parameter_change_preserves_local_results(self):
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=0)
            pipeline = self.pipeline(settings)
            pipeline.discover_local(); pipeline.merge_schema(); pipeline.close()
            before = state["stage_counts"]["discover"]
            changed = replace(settings, stage_overrides={"merge": {"max_output_tokens": 5000}})
            pipeline = self.pipeline(changed)
            try:
                pipeline.merge_schema()
                self.assertEqual(state["stage_counts"]["discover"], before)
                pipeline.review_schema()
                schema = pipeline.fixed_schema()
                self.assertTrue(pipeline.extract(schema, finalize=False))
                self.assertFalse((pipeline.output / "final").exists())
                pipeline.close()
                offline = self.pipeline(Settings(stage_overrides={"merge": {"max_output_tokens": 5000}}))
                # Finalization does not need model connection. Fingerprint checks use original effective settings.
                offline.settings = replace(changed, base_url=url)
                offline.finalize_saved()
                self.assertIsNone(offline._llm)
                self.assertTrue((offline.output / "final/entities.jsonl").is_file())
                offline.close()
            finally:
                pipeline.close()

    def test_local_truncation_split_and_checkpoint(self):
        self.policy["batch_packs"] = 4
        with fake_server() as (url, state):
            state["truncate_next"] = True
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0, context_tokens=65536))
            try:
                pipeline.discover_local()
                before = state["requests"]
                pipeline.discover_local()
                self.assertEqual(state["requests"], before)
                manifest = read_json(pipeline.output / "schema/local-manifest.json")
                self.assertEqual(manifest["state"], "success")
                self.assertEqual(len(manifest["selected"]), len(pipeline.packs))
                self.assertGreaterEqual(len(manifest["schemas"]), 2)
            finally:
                pipeline.close()

    def test_incomplete_local_cannot_merge_or_hide_old_schema(self):
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0))
            try:
                pipeline.discover_local(); pipeline.merge_schema()
                state["truncate_next"] = True
                self.policy["recovery_max_depth"] = 1
                # A deliberately changed prompt makes new local requests, while retained schema must be stale.
                from code_ontology.stages import SchemaStages
                runner = SchemaStages(pipeline, "discover")
                try:
                    runner.system += " changed"
                    with self.assertRaisesRegex(RuntimeError, "incomplete"):
                        runner.local()
                finally:
                    runner.close()
                with self.assertRaisesRegex(RuntimeError, "changed|incomplete"):
                    pipeline.merge_schema()
                self.assertNotEqual(read_json(pipeline.output / "schema/merge-status.json")["state"], "success")
            finally:
                pipeline.close()

    def test_client_timeout_is_unknown_without_automatic_resubmission(self):
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=3)
            pipeline = self.pipeline(settings)
            try:
                pipeline.discover_local()
                state["merge_delay"] = 0.25
                pipeline.settings = replace(settings, stage_overrides={"merge": {"timeout": 0.05}})
                with self.assertRaises(RequestUnknown):
                    pipeline.merge_schema()
                self.assertEqual(state["stage_counts"]["merge"], 1)
                tasks = list((pipeline.output / "schema/merge").glob("*.task.json"))
                self.assertTrue(any(read_json(p)["state"] == "unknown" for p in tasks))
            finally:
                pipeline.close()

    def test_session_short_commands_and_writer_conflict(self):
        with fake_server() as (url, state):
            self.env.write_text(f"CO_REPO={self.repo.as_posix()}\nCO_OUTPUT={(self.root/'session-out').as_posix()}\n"
                                f"CO_EXPERIMENT=short\nCO_PARSER=tree-sitter\nCO_VIEW=ast\nCO_PROFILE=general\n"
                                f"CO_DISCOVERY_MODE=full\nOPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["init", "--env-file", str(self.env)]), 0)
                self.assertEqual(main(["extract", "--env-file", str(self.env)]), 1)
                self.assertEqual(state["requests"], 0)
                self.assertEqual(main(["run", "--env-file", str(self.env)]), 0)
                before = state["requests"]
                self.assertEqual(main(["merge-schema", "--env-file", str(self.env)]), 0)
                self.assertEqual(state["requests"], before)
                with writer_lock(self.root / "session-out"):
                    self.assertEqual(main(["prepare", "--env-file", str(self.env)]), 1)

    def test_mixed_language_preserves_names_and_prompt_versions(self):
        mixed = prompt(self.policy, "general", "extract")
        self.assertIn("English and Chinese may coexist", mixed)
        self.assertIn("Comments may be absent", mixed)
        self.assertNotIn("Use Chinese readable labels", mixed)
        self.policy["output_language"] = "en"
        self.assertNotEqual(mixed, prompt(self.policy, "general", "extract"))

    def test_merge_truncation_recovery_preserves_children(self):
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0))
            try:
                pipeline.discover_local()
                state["truncate_next"] = True
                pipeline.merge_schema()
                before = state["requests"]
                pipeline.merge_schema()
                self.assertEqual(state["requests"], before)
                self.assertEqual(read_json(pipeline.output / "schema/merge-status.json")["state"], "success")
            finally:
                pipeline.close()

    def test_extraction_single_function_truncation_and_offline_finalize(self):
        long_code = "int large(int x) {\n" + "  x += 1;\n" * 30 + "  return x;\n}\n"
        (self.repo / "large.cpp").write_text(long_code, encoding="utf-8")
        self.dest = snapshot(self.repo, self.root / "out", self.policy)
        parse_repository(self.dest, Settings(), self.policy, ["ast"])
        prepare(self.dest, "ast", self.policy)
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0, concurrency=1))
            try:
                pipeline.discover_local(); schema = pipeline.merge_schema()
                # Ensure the first extraction task has a splittable function.
                pipeline.packs.sort(key=lambda p: -len(p["code"]))
                state["truncate_next"] = True
                self.assertTrue(pipeline.extract(schema, finalize=False))
                before = state["requests"]
                self.assertTrue(pipeline.extract(schema, finalize=False))
                self.assertEqual(state["requests"], before)
                pipeline.close()
                pipeline._llm = None
                pipeline.finalize_saved()
                self.assertIsNone(pipeline._llm)
            finally:
                pipeline.close()

    def test_changed_merge_config_blocks_stale_schema_without_model_calls(self):
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0))
            try:
                pipeline.discover_local(); pipeline.merge_schema()
                before = state["requests"]
                from code_ontology.stages import SchemaStages
                pipeline.settings = replace(pipeline.settings, stage_overrides={"merge": {"timeout": 300}})
                runner = SchemaStages(pipeline, "merge")
                try:
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        runner.verify_merged()
                    self.assertEqual(state["requests"], before)
                finally:
                    runner.close()
            finally:
                pipeline.close()

    def test_draft_is_saved_with_alignment_issues_and_ai_review_repairs_it(self):
        self.policy["review_batch_items"] = 1
        def factory(payload):
            value = fixture_response(payload)
            if payload["stage"] == "discover":
                value["properties"] = [{"id": "hours", "name": "hours", "definition": "Operating hours", "domain": ["device"],
                                        "datatype": "number", "unit": "hours", "evidence_ids": value["classes"][0]["evidence_ids"]}]
            elif payload["stage"] == "merge":
                evidence = value["ontology"]["classes"][0]["evidence_ids"]
                value["ontology"]["properties"] = [{"id": "hours", "name": "hours", "definition": "Operating hours", "domain": ["device"],
                                                     "datatype": "integer", "unit": "hours", "evidence_ids": evidence}]
                value["source_mapping"] += [{"source_id": r["id"], "target_id": "hours", "action": "merge", "reason": "Fixture narrowing error"}
                                            for s in payload["candidates"] for r in s["properties"]]
            elif payload["stage"] == "review":
                for row in value["ontology"]["properties"]:
                    row["datatype"] = "number"
                context_sources = [payload["class_context"][0]["item"]["id"]] if payload["class_context"] else []
                value["issues"] = [{"source_ids": context_sources, "target_ids": [], "severity": "warning", "status": "fixed", "description": "Widened number without losing values; checked source class context"}]
                present = {m["source_id"] for m in value["source_mapping"]}
                value["source_mapping"] += [c["current_mapping"] for c in payload["class_context"] if c["item"]["id"] not in present]
            return value
        with fake_server() as (url, state):
            state["response_factory"] = factory
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0, context_tokens=65536))
            try:
                pipeline.discover_local(); pipeline.merge_schema()
                root = pipeline.output / "schema"
                self.assertTrue((root / "schema.json").is_file())
                self.assertGreater(read_json(root / "merge-status.json")["alignment_issues"], 0)
                from code_ontology.schema_review import check_schema_use
                with self.assertRaisesRegex(ValueError, "review-schema"):
                    check_schema_use(pipeline)
                pipeline.policy["allow_unreviewed_schema"] = True
                self.assertEqual(check_schema_use(pipeline), "explicit_unreviewed_override")
                pipeline.policy["allow_unreviewed_schema"] = False
                pipeline.review_schema()
                self.assertEqual(read_json(root / "schema.json")["properties"][0]["datatype"], "number")
                self.assertEqual(read_json(root / "draft-schema.json")["properties"][0]["datatype"], "integer")
                self.assertEqual(check_schema_use(pipeline), "ai_reviewed_not_human_verified")
                self.assertEqual(read_json(root / "review-status.json")["state"], "success")
                report = read_json(root / "review-report.json")
                published = read_json(root / "schema.json")
                target_ids = {r["id"] for cat in ("classes", "relations", "properties") for r in published[cat]}
                self.assertEqual(set(report["publication_id_mapping"].values()), target_ids)
            finally:
                pipeline.close()

    def test_review_timeout_resume_and_parameter_change_do_not_repeat_discovery(self):
        self.policy["review_batch_items"] = 1
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0))
            try:
                pipeline.discover_local(); pipeline.merge_schema()
                discovers = state["stage_counts"]["discover"]
                state["fail_review_on"] = 2
                with self.assertRaises(RequestUnknown):
                    pipeline.review_schema()
                previous_history = Path(read_json(pipeline.output / "schema/review-status.json")["history"])
                self.assertTrue((previous_history / "round-001/batch-001/result/schema.json").is_file())
                before = state["stage_counts"]["review"]
                pipeline.review_schema()
                current_history = Path(read_json(pipeline.output / "schema/review-status.json")["history"])
                self.assertNotEqual(previous_history, current_history)
                self.assertTrue((current_history / "round-001/result/schema.json").is_file())
                self.assertTrue((current_history / "final/schema.json").is_file())
                self.assertEqual(state["stage_counts"]["review"] - before, 3)
                self.assertEqual(state["stage_counts"]["discover"], discovers)
                before = state["requests"]
                pipeline.review_schema()
                self.assertEqual(state["requests"], before)
                from code_ontology.schema_review import check_schema_use
                pipeline.settings = replace(pipeline.settings, stage_overrides={"review": {"timeout": 300}})
                with self.assertRaisesRegex(ValueError, "configuration changed"):
                    check_schema_use(pipeline)
            finally:
                pipeline.close()

    def test_extraction_repair_explains_pack_evidence_boundary(self):
        from code_ontology.models import Ontology
        with fake_server() as (url, state):
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0))
            try:
                first_pack = pipeline.packs[0]
                wrong_evidence = pipeline.packs[1]["evidence_ids"][0]
                def factory(payload):
                    value = fixture_response(payload)
                    if payload.get("stage") == "extract" and payload["packs"][0]["pack_id"] == first_pack["pack_id"] and not state.get("bad_sent"):
                        value["entities"][0]["evidence_ids"] = [wrong_evidence]
                        state["bad_sent"] = True
                    return value
                state["response_factory"] = factory
                schema = Ontology.model_validate(fixture_response({"stage": "discover", "packs": pipeline.packs}))
                self.assertTrue(pipeline.extract(schema, finalize=False))
                repairs = [r["messages"][-1]["content"] for r in state["payloads"] if len(r["messages"]) > 2]
                self.assertEqual(len(repairs), 1)
                self.assertIn(first_pack["evidence_ids"][0], repairs[0])
                self.assertIn("type provenance", repairs[0])
                self.assertIn("otherwise remove", repairs[0])
            finally:
                pipeline.close()

    def test_review_second_round_only_revisits_pending_sources(self):
        self.policy["review_batch_items"] = 3
        def factory(payload):
            value = fixture_response(payload)
            if payload["stage"] == "discover":
                value["properties"] = [{"id": "hours", "name": "hours", "definition": "Operating hours", "domain": ["device"],
                    "datatype": "number", "evidence_ids": value["classes"][0]["evidence_ids"]}]
            elif payload["stage"] == "merge":
                value["ontology"]["properties"] = [{"id": "hours", "name": "hours", "definition": "Operating hours", "domain": ["device"],
                    "datatype": "integer", "evidence_ids": value["ontology"]["classes"][0]["evidence_ids"]}]
                value["source_mapping"] += [{"source_id": r["id"], "target_id": "hours", "action": "merge", "reason": "Synthetic narrowing"}
                    for s in payload["candidates"] for r in s["properties"]]
            elif payload["stage"] == "review" and "review_focus" in payload:
                value["ontology"]["properties"][0]["datatype"] = "number"
            return value
        with fake_server() as (url, state):
            state["response_factory"] = factory
            pipeline = self.pipeline(Settings(base_url=url, model="fixture", retries=0, context_tokens=65536))
            try:
                pipeline.discover_local(); pipeline.merge_schema(); pipeline.review_schema()
                self.assertEqual(state["stage_counts"]["review"], 5)  # 8 sources, then only 4 pending
                self.assertEqual(read_json(pipeline.output / "schema/review-status.json")["state"], "success")
            finally:
                pipeline.close()

    def test_portable_smoke_runs_all_stages_and_resumes_without_model_cost(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["smoke", "--out", str(self.root / "portable")]), 0)
        report = read_json(self.root / "portable/smoke/verification.json")
        self.assertTrue(report["ok"])
        self.assertEqual(report["resume_extra_requests"], 0)


if __name__ == "__main__":
    unittest.main()
