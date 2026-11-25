from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from code_ontology.config import Settings, load_policy, load_settings, configuration_sources
from code_ontology.config_bundle import read_bundle
from code_ontology.discovery import payload, batches
from code_ontology.events import execution, emit, read_events, redact
from code_ontology.llm import LLM, trace_path
from code_ontology.models import Ontology
from code_ontology.pipeline import group_batches
from code_ontology.review_ledger import update_ledger
from code_ontology.schema_review import ReviewIssue
from code_ontology.util import canonical, read_json, write_json
from tests.fake_api import fake_server
from scripts.check_model_limits import main as check_limits


class RecentImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = self.root / "empty.env"
        self.env.write_text("", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_optional_review_connection_is_isolated_and_secret_free(self):
        self.env.write_text("OPENAI_BASE_URL=http://shared/v1\nOPENAI_API_KEY=shared-secret\nOPENAI_MODEL=shared\n"
                            "OPENAI_REVIEW_BASE_URL=http://review/v1\nOPENAI_REVIEW_API_KEY=review-secret\n"
                            "OPENAI_REVIEW_MODEL=reviewer\n", encoding="utf-8")
        settings = load_settings(str(self.env))
        discover = settings.for_stage("discover")
        review = settings.for_stage("review")
        self.assertEqual((discover.base_url, discover.model, discover.api_key),
                         ("http://shared/v1", "shared", "shared-secret"))
        self.assertEqual((review.base_url, review.model, review.api_key),
                         ("http://review/v1", "reviewer", "review-secret"))
        self.assertNotIn("secret", json.dumps(settings.public()))
        before = discover.request_identity()
        settings.review_model = "changed-reviewer"
        self.assertEqual(settings.for_stage("discover").request_identity(), before)

    def test_bundle_paths_precedence_and_secret_rejection(self):
        (self.root / "steps").mkdir()
        (self.root / "steps/discover.yaml").write_text("policy:\n  batch_packs: 8\nllm:\n  concurrency: 4\n  temperature: 0.8\n", encoding="utf-8")
        (self.root / "run.yaml").write_text("config_version: 1\nrun:\n  repo: repo\ndefaults:\n  policy:\n    prompt_dir: prompts\n  llm:\n    concurrency: 2\nsteps:\n  discover: steps/discover.yaml\n", encoding="utf-8")
        self.env.write_text("LLM_CONCURRENCY=3\nLLM_MERGE_CONCURRENCY=5\nLLM_DISCOVER_TEMPERATURE=0.2\nCO_DISCOVERY_BATCH_PACKS=6\n", encoding="utf-8")
        settings = load_settings(str(self.env), str(self.root))
        self.assertEqual(settings.concurrency, 3)
        self.assertEqual(settings.for_stage("discover").concurrency, 4)
        self.assertEqual(settings.for_stage("merge").concurrency, 5)
        self.assertEqual(settings.for_stage("discover").temperature, 0.2)
        policy = load_policy(str(self.root), str(self.env))
        self.assertEqual(policy["batch_packs"], 6)
        self.assertEqual(policy["prompt_dir"], str(self.root / "prompts"))
        self.assertIn("batch_packs", configuration_sources(str(self.env), str(self.root))["overrides"])
        self.assertEqual(read_bundle(self.root)["run"]["repo"], str(self.root / "repo"))
        (self.root / "steps/discover.yaml").write_text("llm:\n  api_key: forbidden\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_bundle(self.root)

    def test_event_order_paging_filters_and_redaction(self):
        with redirect_stdout(io.StringIO()), execution(self.root, "test", "discover", "secret-value") as record:
            threads = [threading.Thread(target=lambda: emit("progress", message="secret-value", completed=1, total=2)) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            emit("failure", level="error", message="authorization: Bearer token https://u:p@example.test/v1?key=x")
            record["state"] = "partial"
        path = Path(record["events"])
        rows = read_events(path)["rows"]
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows)+1)))
        self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))
        self.assertNotIn("u:p", path.read_text(encoding="utf-8"))
        self.assertEqual(len(read_events(path, level="error")["rows"]), 1)
        offset = path.stat().st_size
        with path.open("ab") as handle:
            handle.write(b'{"partial":')
        self.assertEqual(read_events(path, offset)["next_offset"], offset)
        self.assertNotIn("\x1b", redact("\x1b[2Jtext\r"))

    def test_lossless_sharing_preserves_pack_evidence_and_edges(self):
        packs = [{"pack_id": str(i), "code": "int large() { return 1; }"*100,
                  "source_refs": [{"file": "a.cpp"}], "evidence_ids": [str(i)],
                  "context": {"nodes": [{"id": "1", "code": "x"*300}], "edges": [{"src": "1", "dst": "1"}]}}
                 for i in range(3)]
        compact = payload(packs, "general", True)
        self.assertLess(len(canonical(compact)), len(canonical(payload(packs, "general"))))
        for original, row in zip(packs, compact["packs"]):
            self.assertEqual(compact["shared"][row["code"]["shared_ref"]], original["code"])
            self.assertEqual(compact["shared"][row["context"]["nodes"][0]["shared_ref"]], original["context"]["nodes"][0])
            self.assertEqual(original["evidence_ids"], row["evidence_ids"])
            self.assertEqual(original["context"]["edges"], row["context"]["edges"])
        class Budget:
            def fits(self, messages):
                return len(json.loads(messages[1]["content"])["packs"]) <= 2
        grouped = batches(packs, 8, "system", Budget(), "general", True)
        self.assertEqual([len(group) for group in grouped], [2, 1])
        self.assertEqual({p["pack_id"] for group in grouped for p in group}, {"0", "1", "2"})

    def test_merge_grouping_respects_estimated_output_budget(self):
        class Budget:
            def fits(self, messages):
                return True
        rows = [{"id": str(i), "size": 2} for i in range(5)]
        grouped = list(group_batches(rows, "system", Budget(), lambda items: {"rows": items}, 10,
                                     output_estimate=lambda items: sum(row["size"] for row in items),
                                     max_output_tokens=5))
        self.assertEqual([len(group) for group in grouped], [2, 2, 1])

    def test_disabled_wait_messages_do_not_start_monitor_thread(self):
        from types import SimpleNamespace
        llm = LLM(Settings(base_url="http://127.0.0.1:1/v1", model="fixture", retries=0), self.root / "cache")
        llm.wait_messages = False
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")], usage=None, model="fixture")
        try:
            with patch.object(llm.client.chat.completions, "create", return_value=response), \
                    patch("code_ontology.llm.threading.Thread") as thread:
                llm._request([{"role": "user", "content": "{}"}], self.root / "traces", "quiet")
                thread.assert_not_called()
        finally:
            llm.close()

    def test_changed_scheduling_reuses_response_but_output_budget_does_not(self):
        with fake_server() as (url, state):
            settings = Settings(base_url=url, model="fixture", retries=0)
            data = {"stage": "discover", "packs": [{"evidence_ids": ["ev"]}]}
            for effective in (settings, replace(settings, concurrency=4, timeout=300, retries=2), replace(settings, max_output_tokens=5000)):
                llm = LLM(effective, self.root / "cache")
                try:
                    llm.call("test", data, Ontology, self.root / "traces", namespace="same")
                finally:
                    llm.close()
            self.assertEqual(state["requests"], 2)
            records = [read_json(p) for p in (self.root / "traces").glob("*.json") if "parameters" in read_json(p)]
            self.assertTrue(all(r["elapsed_seconds"] >= 0 and r["ended_at"].endswith("+08:00") for r in records))

    def test_trace_names_sort_by_beijing_time_across_midnight(self):
        from datetime import datetime, timezone
        second = int(datetime(2026, 11, 25, 16, tzinfo=timezone.utc).timestamp())
        instants = [(second-1)*10**9+999999999, second*10**9, second*10**9+1]
        with patch("code_ontology.llm.time.time_ns", side_effect=instants):
            names = [trace_path(self.root, key, kind, attempt).name for key, kind, attempt in
                     [("z", "initial", 0), ("a", "repair", 1), ("a", "cache", None)]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "20261125-235959-999999999-UTC08-z-initial-0.json")
        self.assertEqual(names[1], "20261126-000000-000000000-UTC08-a-repair-1.json")
        self.assertNotIn(":", names[2])

    def test_request_repair_and_cache_use_timestamped_trace_names(self):
        with fake_server("invalid_once") as (url, state):
            llm = LLM(Settings(base_url=url, model="fixture", retries=0), self.root / "cache")
            traces = self.root / "traces"
            try:
                data = {"stage": "discover", "packs": [{"evidence_ids": ["ev"]}]}
                llm.call("test", data, Ontology, traces)
                llm.call("test", data, Ontology, traces)
                llm.call("test", data, Ontology, traces)
                records = sorted(traces.glob("*.json"))
                self.assertEqual(len(records), 4)
                self.assertEqual(state["requests"], 2)
                self.assertTrue(records[0].name.endswith("-initial-0.json"))
                self.assertTrue(records[1].name.endswith("-repair-0.json"))
                self.assertTrue(all(p.name.endswith("-cache.json") for p in records[2:]))
                for p in records:
                    self.assertRegex(p.name, r"^\d{8}-\d{6}-\d{9}-UTC08-")
                    self.assertTrue(read_json(p)["created_at"].endswith("+08:00"))
            finally:
                llm.close()

    def test_review_issues_remain_open_until_explicitly_closed(self):
        schema = Ontology(classes=[], relations=[], properties=[])
        issue = ReviewIssue(description="missing source support", severity="error", status="unresolved")
        ledger = {}
        update_ledger(ledger, [issue], schema, 1)
        key = next(iter(ledger))
        update_ledger(ledger, [], schema, 2)
        self.assertEqual(ledger[key]["status"], "unresolved")
        update_ledger(ledger, [issue.model_copy(update={"issue_id": key, "status": "fixed"})], schema, 3)
        self.assertEqual(ledger[key]["status"], "fixed")
        self.assertEqual(ledger[key]["verification"], "ai_not_human_verified")

    def test_review_budget_counts_repairs_and_unknown_usage(self):
        from types import SimpleNamespace
        from code_ontology.schema_review import SchemaReview
        from code_ontology.stages import IncompleteStage
        review = SchemaReview.__new__(SchemaReview)
        review.started = 0
        review.settings = Settings()
        review.p = SimpleNamespace(policy={"review_max_requests": 1})
        review.llm = SimpleNamespace(stats={"requests": 1, "usage_missing": 0, "prompt_tokens": 0, "completion_tokens": 0})
        with self.assertRaises(IncompleteStage):
            review.check_budget([])
        review.p.policy = {"review_max_tokens": 100000}
        review.llm.stats.update(requests=0, usage_missing=1)
        with self.assertRaises(IncompleteStage):
            review.check_budget([])
        review.llm.stats["usage_missing"] = 0
        review.check_budget([])

    def test_legacy_local_checkpoint_reused_after_concurrency_change(self):
        from types import SimpleNamespace
        from code_ontology.stages import SchemaStages
        from code_ontology.util import digest
        old = Settings(base_url="http://127.0.0.1:1/v1", model="fixture", concurrency=1)
        pipeline = SimpleNamespace(output=self.root / "run", dest=self.root,
            settings=replace(old, concurrency=4), policy=load_policy(env_file=str(self.env)),
            profile="general", stage_stats=[])
        # An old running manifest preserves the legacy batching strategy.
        write_json(pipeline.output / "schema/local-manifest.json", {"state": "running"})
        stages = SchemaStages(pipeline, "discover")
        try:
            self.assertEqual(stages.strategy, "legacy")
            data = {"stage": "discover", "profile": "general", "packs": [{"evidence_ids": ["ev"]}]}
            old_key = digest({"input": data, "prompt": stages.system, "settings": old.public(),
                "contract": Ontology.model_json_schema(), "format": 1}, 40)
            write_json(stages.root / "local" / f"{old_key}.json", {"key": old_key,
                "settings": old.public(), "schema": Ontology(classes=[], relations=[], properties=[]).model_dump()})
            stages.call(data)
            migrated = read_json(stages.root / "local" / f"{stages.signature(data)}.json")
            self.assertEqual(migrated["reused_from"], old_key+".json")
            self.assertEqual(stages.llm.stats["requests"], 0)
            self.assertEqual(stages.llm.stats["cache_hits"], 1)
        finally:
            stages.close()

    def test_workbench_execution_history_and_prompt_bundle_preservation(self):
        from code_ontology.workbench_jobs import Workbench
        from code_ontology.config import PROJECT
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "a.cpp").write_text("int value;", encoding="utf-8")
        (self.root / "discover.yaml").write_text("llm:\n  concurrency: 4\n", encoding="utf-8")
        (self.root / "run.yaml").write_text("config_version: 1\ndefaults:\n  policy:\n    parser_backend: tree-sitter\n  llm:\n    concurrency: 3\nsteps:\n  discover: discover.yaml\n", encoding="utf-8")
        service = Workbench(self.root / "control", project=PROJECT, browse_roots=[self.root])
        try:
            wid = service.save_workspace({"repo": str(repo), "out": str(self.root / "out"),
                "config": str(self.root / "run.yaml"), "env_file": str(self.env),
                "experiment": "test", "view": "ast", "profile": "general"})["workspace"]["id"]
            service.prompt_copy(wid, "discover", "Keep supported concepts")
            settings = service.settings(service.workspace(wid))[1]
            self.assertEqual(settings.concurrency, 3)
            self.assertEqual(settings.for_stage("discover").concurrency, 4)
            out = self.root / "out"
            with redirect_stdout(io.StringIO()), execution(out, "test", "discover") as record:
                emit("progress", completed=1, total=1)
                record["state"] = "success"
            rows = service.execution_events(wid, record["execution_id"])["rows"]
            self.assertEqual(rows[-1]["completed"], 1)
            with self.assertRaises(ValueError):
                service.execution_events(wid, "../escape")
            log = out / "sessions/test/discover.log"
            log.parent.mkdir(parents=True)
            log.write_text("old running task output\n", encoding="utf-8")
            write_json(out / "sessions/test.json", {"experiment": "test", "stages": {"discover-local": {"state": "running", "log": str(log)}}})
            legacy = next(row for row in service.executions(wid) if row.get("legacy"))
            self.assertIn("old running", service.execution_log(wid, legacy["execution_id"])["text"])
            self.assertEqual(service.execution_events(wid, legacy["execution_id"])["rows"], [])
        finally:
            service.close()

    def test_experiment_index_exposes_state_and_artifact_availability(self):
        from code_ontology.artifacts import index_experiment
        snapshot = self.root / "out" / "snapshot-1"
        run = snapshot / "runs/test/ast/general"
        (run / "schema").mkdir(parents=True)
        write_json(run / "schema/schema.json", {"classes": [], "relations": [], "properties": []})
        write_json(run / "schema-scope.json", {"review_state": "merged_unreviewed"})
        state = {"source_snapshot": "snapshot-1", "stages": {
            "execution": {"state": "running"}, "merge-schema": {"state": "success"}}}
        index_experiment(snapshot, "test", "ast", "general", state)
        index = read_json(self.root / "out/experiments/test/ast-general/index.json")
        self.assertEqual(index["layout_version"], 2)
        self.assertEqual(index["overall_state"], "running")
        self.assertEqual(index["current_stage"], "merge-schema")
        self.assertEqual(index["review_state"], "merged_unreviewed")
        self.assertTrue(index["availability"]["schema"])
        self.assertFalse(index["availability"]["report"])

    def test_limits_dry_run_and_length_are_distinct(self):
        with fake_server(mode="truncated") as (url, state), redirect_stdout(io.StringIO()):
            self.env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture\n", encoding="utf-8")
            args = ["--env-file", str(self.env), "--limits", "512,1024", "--out", str(self.root / "diagnostics")]
            self.assertEqual(check_limits(args+["--dry-run"]), 0)
            self.assertEqual(state["requests"], 0)
            # The existing fixture expects JSON user payload; substitute a minimal wire response.
            from unittest.mock import patch
            def response(llm, messages, traces, key, repair=False):
                write_json(traces / "response.json", {"finish_reason": "length", "usage": None,
                           "raw_content": "text", "elapsed_seconds": 0.1})
                from code_ontology.llm import BudgetError
                raise BudgetError("length")
            with patch.object(LLM, "_request", response):
                self.assertEqual(check_limits(args), 0)
            result = read_json(next((self.root / "diagnostics").glob("*/result.json")))
            self.assertEqual(len(result["tiers"]), 2)
            self.assertEqual(result["tiers"][0]["state"], "length")
            self.assertIsNone(result["tiers"][0]["reached_requested_usage"])


if __name__ == "__main__":
    unittest.main()
