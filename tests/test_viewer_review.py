from pathlib import Path
import csv
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from code_ontology.review import sample, summarize
from code_ontology.util import file_hash, write_json, write_jsonl
from code_ontology.viewer import ArtifactStore, make_server
from code_ontology.workbench import WorkspaceArtifacts


class ViewerReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snap = self.root / "snapshot"
        self.run = self.snap / "runs" / "demo" / "cpg" / "maintenance"
        self.source = self.snap / "source" / "设备.cpp"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("// 设备\nint threshold = 85;\n", encoding="utf-8")
        self.schema = {"classes": [{"id": "device", "name": "设备", "definition": "设备定义", "parents": [], "aliases": ["Device"], "evidence_ids": ["ev1"]}],
                       "relations": [], "properties": [], "unresolved": []}
        write_json(self.snap / "manifest.json", {"snapshot_id": "snapshot", "repo": "demo", "files": [{"file": "设备.cpp"}]})
        write_json(self.run / "run_manifest.json", {"settings": {"model": "test-real-name"}, "stages": {"execution": {"state": "success"}}})
        write_json(self.run / "metrics.json", {"status": "success", "entities": 1})
        write_json(self.run / "final" / "schema.json", self.schema)
        write_jsonl(self.run / "final" / "entities.jsonl", [{"id": "e", "name": "设备A", "type_id": "device", "scope": "example", "basis": "direct", "evidence_ids": ["ev1"]}])
        write_jsonl(self.run / "evidence.jsonl", [{"evidence_id": "ev1", "file": "设备.cpp", "start_line": 2, "end_line": 2, "source_sha256": file_hash(self.source)}])
        self.store = ArtifactStore(self.root)
        self.rid = self.store.listing()["runs"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def fingerprint(self):
        return {str(p.relative_to(self.snap)): file_hash(p) for p in self.snap.rglob("*") if p.is_file()}

    def test_read_only_records_evidence_and_partial_runs(self):
        before = self.fingerprint()
        detail = self.store.detail(self.rid)
        self.assertEqual(detail["schema"]["classes"][0]["name"], "设备")
        self.assertIn("device", detail["schema_record_ids"]["classes"])
        self.assertEqual(self.store.records(self.rid, "entities", query="不存在")["total"], 0)
        self.assertEqual(self.store.records(self.rid, "entities", query="设备A")["total"], 1)
        self.assertEqual(self.store.records(self.rid, "entities", offset=1)["rows"], [])
        evidence = self.store.evidence(self.rid, "ev1")
        self.assertTrue(evidence["source_hash_matches"])
        self.assertTrue(evidence["lines"][1]["highlight"])
        self.assertEqual(before, self.fingerprint())
        self.source.write_text("// changed\nint threshold=90;\n", encoding="utf-8")
        self.assertFalse(self.store.evidence(self.rid, "ev1")["source_hash_matches"])
        write_json(self.run / "run_manifest.json", {"stages": {"execution": {"state": "failed"}}})
        self.assertEqual(self.store.listing()["runs"][0]["status"], "failed")

    def test_path_and_query_boundaries(self):
        with self.assertRaises(KeyError):
            self.store.evidence(self.rid, "../../.env")
        with self.assertRaises(ValueError):
            self.store.records(self.rid, "../.env")
        with self.assertRaises(ValueError):
            self.store.records(self.rid, "entities", limit=1001)
        with self.assertRaises(ValueError):
            self.store.artifact(self.rid, "../.env")
        write_jsonl(self.run / "evidence.jsonl", [{"evidence_id": "bad", "file": "../../.env", "start_line": 1, "end_line": 1}])
        with self.assertRaises(ValueError):
            self.store.evidence(self.rid, "bad")

    def test_schema_history_rounds_are_readable_and_remain_read_only(self):
        from code_ontology.schema_history import SchemaHistory
        history = SchemaHistory(self.run / "schema", "merge", "fixture-key")
        history.snapshot("round-001/result", [self.schema, self.schema])
        history.snapshot("round-002/result", [self.schema])
        history.finish("success")
        before = self.fingerprint()
        rows = self.store.detail(self.rid)["schema_history"]
        self.assertEqual(len(rows), 3)
        result = self.store.schema_snapshot(self.rid, rows[0]["path"])
        self.assertEqual(result["schema"]["classes"][0]["name"], "设备")
        self.assertIn("device", result["schema_record_ids"]["classes"])
        with self.assertRaises(KeyError):
            self.store.schema_snapshot(self.rid, "../../.env")
        self.assertEqual(before, self.fingerprint())
        # Even a corrupt history manifest cannot expose another output artifact.
        manifest = history.manifest
        manifest["snapshots"].append({"schemas": ["../../../../run_manifest.json"]})
        write_json(history.root / "manifest.json", manifest)
        self.assertEqual(len(self.store.schema_history(self.rid)), 3)

    def workspace_store(self):
        return WorkspaceArtifacts({"out": str(self.root), "snapshot": str(self.snap),
                                   "experiment": "demo", "view": "cpg", "profile": "maintenance"}, True)

    def test_local_results_visible_before_manifest_merge_or_extraction(self):
        (self.run / "run_manifest.json").unlink()
        (self.run / "metrics.json").unlink()
        (self.run / "final/schema.json").unlink()
        write_json(self.run / "schema/local/first.json", {"key": "first", "schema": self.schema})
        # A running task and a truncated checkpoint must not hide successful work.
        write_json(self.run / "schema/local/second.task.json", {"state": "running"})
        (self.run / "schema/local/broken.json").write_text('{"schema":', encoding="utf-8")
        before = self.fingerprint()
        store = self.workspace_store()
        rows = store.listing()["runs"]
        self.assertEqual(len(rows), 1)
        detail = store.detail(rows[0]["id"])
        self.assertEqual(detail["schema"]["classes"][0]["name"], "设备")
        self.assertEqual(detail["schema_source"]["stage"], "local")
        self.assertEqual(len(detail["schema_history"]), 1)
        self.assertEqual(store.records(self.rid, "entities")["total"], 0)
        self.assertTrue(store.evidence(self.rid, "ev1")["source_hash_matches"])
        self.assertEqual(before, self.fingerprint())

    def test_intermediate_sources_stay_separate_and_final_schema_is_preserved(self):
        local = json.loads(json.dumps(self.schema))
        local["classes"][0]["name"] = "局部设备"
        write_json(self.run / "schema/local/first.json", {"schema": local})
        write_json(self.run / "schema/module-schemas.json", {"schemas": [self.schema, local]})
        write_json(self.run / "schema/merge/first.json", {"schema": self.schema, "source_mapping": []})
        write_json(self.run / "schema/merge/second.candidate.json", local)
        write_json(self.run / "schema/merge/second.task.json", {"state": "failed"})
        store = self.workspace_store()
        rows = store.listing()["runs"]
        before = self.fingerprint()
        detail = store.detail(rows[0]["id"])
        sources = detail["schema_history"]
        self.assertEqual(len(sources), 5)
        self.assertEqual(detail["schema_source"]["stage"], "merge")
        for source in sources:
            snapshot = store.schema_snapshot(self.rid, source["path"])
            self.assertEqual(len(snapshot["schema"]["classes"]), 1)
            self.assertIn("device", snapshot["schema_record_ids"]["classes"])
        fragments = [s for s in sources if s["state"] == "unaligned"]
        self.assertEqual(len(fragments), 2)
        self.assertNotEqual(store.schema_snapshot(self.rid, fragments[0]["path"])["schema"],
                            store.schema_snapshot(self.rid, fragments[1]["path"])["schema"])
        self.assertEqual(self.store.detail(self.rid)["schema"]["classes"][0]["name"], "设备")
        for path in ("artifact:../evidence.jsonl", "artifact:local/../schema.json", "artifact:module-schemas.json#99"):
            with self.assertRaises(KeyError):
                store.schema_snapshot(self.rid, path)
        self.assertEqual(before, self.fingerprint())

    def test_history_only_failed_merge_and_legacy_local_manifest(self):
        from code_ontology.schema_history import SchemaHistory
        (self.run / "final/schema.json").unlink()
        history = SchemaHistory(self.run / "schema", "merge", "partial")
        history.snapshot("round-001/batch-001", [self.schema])
        history.finish("partial", error="remaining batch failed")
        store = self.workspace_store()
        self.assertEqual(len(store.listing()["runs"]), 1)
        detail = store.detail(self.rid)
        self.assertEqual(detail["schema_source"]["attempt_state"], "partial")
        write_json(self.run / "schema/local-manifest.json", {"state": "partial", "schemas": [self.schema]})
        rows = store.schema_history(self.rid)
        local = next(row for row in rows if row["stage"] == "local")
        self.assertEqual(store.schema_snapshot(self.rid, local["path"])["schema"]["classes"][0]["id"], "device")

    def test_http_static_api_and_no_write_routes(self):
        write_json(self.run / "schema/local/http-checkpoint.json", {"schema": self.schema})
        server = make_server(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        before = self.fingerprint()
        try:
            with urlopen(base + "/") as r:
                self.assertIn("CODE ATLAS", r.read().decode())
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
            with urlopen(base + "/app.js") as r:
                self.assertIn("function showEvidence", r.read().decode())
            with urlopen(base + "/api/runs") as r:
                self.assertEqual(len(json.load(r)["runs"]), 1)
            with urlopen(base + "/api/records?id=" + self.rid + "&kind=entities&limit=1") as r:
                self.assertEqual(json.load(r)["total"], 1)
            with urlopen(base + "/api/schema-snapshot?" + urlencode({"id": self.rid, "path": "artifact:local/http-checkpoint.json"})) as r:
                snapshot = json.load(r)
                self.assertEqual(snapshot["schema_source"]["stage"], "local")
                self.assertEqual(snapshot["schema"]["classes"][0]["name"], "设备")
            for path in ("/.env", "/api/run?id=../../", "/unknown"):
                with self.assertRaises(HTTPError):
                    urlopen(base + path)
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base + "/api/run", data=b"{}", method="POST"))
            self.assertEqual(e.exception.code, 501)
            self.assertEqual(before, self.fingerprint())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_sampling_and_human_summary(self):
        csv_path = self.root / "reviews" / "labels.csv"
        result = sample(self.run, csv_path, per_kind=1, seed=42)
        self.assertEqual(result["rows"], 2)
        with self.assertRaises(ValueError):
            sample(self.run, csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f); fields = reader.fieldnames; rows = list(reader)
        for row in rows:
            row.update(verdict="correct", evidence_support="supported", domain_relevant="yes")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        summary = summarize(csv_path, self.root / "summary.json")
        self.assertEqual(summary["assertion_sample"]["strict_sample_precision"], 1.0)
        self.assertEqual(summary["missing_sample_rows"], 0)
        with self.assertRaises(ValueError):
            summarize(csv_path, csv_path)
        rows[0]["payload_json"] = "{}"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        with self.assertRaises(ValueError):
            summarize(csv_path, self.root / "bad.json")

    def test_interactive_reviews_are_marked_nonrandom(self):
        path = self.root / "interactive.json"
        write_json(path, {"sampling_method": "interactive_nonrandom", "annotations": [
            {"record_id": "entity:1", "kind": "entities", "verdict": "uncertain", "evidence_support": "unsupported"},
            {"record_id": "entity:2", "kind": "entities", "verdict": "partial", "evidence_support": "supported"}]})
        result = summarize(path, self.root / "interactive-summary.json")
        self.assertEqual(result["sampling_method"], "interactive_nonrandom")
        self.assertEqual(result["assertion_sample"]["judged_rows"], 1)
        self.assertEqual(result["assertion_sample"]["strict_sample_precision"], 0)
        self.assertEqual(result["assertion_sample"]["unresolved_or_unreviewed"], 1)
