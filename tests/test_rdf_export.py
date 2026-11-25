from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from rdflib import Graph, Literal
from rdflib.compare import isomorphic
from rdflib.namespace import RDF, SH, XSD

from code_ontology.cli import main
from code_ontology.rdf_export import CO, FILES, PROV, export_rdf, input_fingerprint
from code_ontology.util import read_json, write_json, write_jsonl


class RDFExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = Path(self.temp.name) / "snapshot"
        self.run = self.snapshot / "runs/demo/ast/general"
        write_json(self.snapshot / "manifest.json", {"snapshot_id": "stable-snapshot"})
        self.schema = {"classes": [
            {"id": "a", "name": "服务", "definition": "Service", "evidence_ids": ["ev"]},
            {"id": "b", "name": "订单", "definition": "Order", "evidence_ids": ["ev"]}],
            "relations": [{"id": "r", "name": "提交", "definition": "submits", "domain": ["a", "b"],
                           "range": ["b"], "evidence_ids": ["ev"]}],
            "properties": [{"id": "p", "name": "金额", "definition": "amount", "domain": ["b"],
                            "datatype": "number", "evidence_ids": ["ev"]}]}
        write_json(self.run / "final/schema.json", self.schema)
        write_json(self.run / "metrics.json", {"status": "success", "view": "ast", "profile": "general",
                                               "schema_scope": {"review_state": "unreviewed"}})
        self.entities = [dict(id=eid, name=name, type_id=tid, scope="code_definition", basis="direct", evidence_ids=["ev"])
                         for eid, name, tid in (("e1", "服务", "a"), ("e2", "订单", "b"))]
        write_jsonl(self.run / "final/entities.jsonl", self.entities)
        write_jsonl(self.run / "final/relations.jsonl", [dict(subject="e1", relation_id="r", object="e2", basis="direct", evidence_ids=["ev"])])
        write_jsonl(self.run / "final/attributes.jsonl", [dict(subject="e2", property_id="p", value=1.5, basis="inferred", evidence_ids=["ev"], condition="当启用时")])
        write_jsonl(self.run / "final/rules.jsonl", [dict(name="规则", condition="x > 1", action="submit", scope="code_definition", basis="direct", evidence_ids=["ev"])])
        write_jsonl(self.run / "final/candidates.jsonl", [dict(reason="待确认", payload={"x": 1})])
        write_jsonl(self.run / "final/conflicts.jsonl", [{"status": "preserved_for_review"}])
        write_jsonl(self.snapshot / "prepared/ast/packs.jsonl", [{"source_refs": [dict(evidence_id="ev", file="订单.cpp", start_line=1, end_line=3, source_sha256="hash")]}])

    def load(self, name):
        return Graph().parse(self.run / "rdf" / name, format="turtle")

    def test_roundtrip_provenance_conditions_and_repeat(self):
        with patch("code_ontology.llm.LLM", side_effect=AssertionError("No model calls")):
            manifest = export_rdf(self.run, self.snapshot)
        self.assertEqual(manifest["state"], "success")
        self.assertFalse(manifest["human_verified"])
        self.assertEqual(manifest["missing_evidence_locations"], [])
        self.assertEqual(manifest["schema_scope"]["review_state"], "unreviewed")
        for name in FILES:
            Graph().parse(self.run / "rdf" / name, format="nt" if name.endswith(".nt") else "turtle")
        facts = self.load("facts.ttl")
        assertions = self.load("assertions.ttl")
        self.assertFalse(any(facts.triples((None, None, Literal("1.5", datatype=XSD.decimal)))))
        self.assertTrue(any(assertions.triples((None, CO.condition, Literal("当启用时")))))
        self.assertTrue(any(assertions.triples((None, CO.file, Literal("订单.cpp")))))
        self.assertTrue(any(assertions.triples((None, PROV.wasDerivedFrom, None))))
        self.assertEqual(len(list(assertions.subjects(RDF.type, RDF.Statement))), 4)
        self.assertTrue(isomorphic(facts, Graph().parse(self.run / "rdf/facts.nt", format="nt")))
        export_rdf(self.run, self.snapshot)
        self.assertTrue(isomorphic(facts, self.load("facts.ttl")))
        self.assertTrue(isomorphic(assertions, self.load("assertions.ttl")))

    def test_shacl_range_error_is_partial(self):
        self.entities[1]["type_id"] = "a"
        write_jsonl(self.run / "final/entities.jsonl", self.entities)
        manifest = export_rdf(self.run, self.snapshot)
        self.assertEqual(manifest["state"], "partial")
        self.assertFalse(manifest["shacl_conforms"])
        self.assertTrue(any(self.load("validation-report.ttl").subjects(RDF.type, SH.ValidationResult)))

    def test_same_identity_preserves_assertion_variants(self):
        self.entities[0]["name"] = "Service"
        variant = {**self.entities[0], "name": "service", "basis": "inferred", "evidence_ids": ["ev2"]}
        write_jsonl(self.run / "final/entities.jsonl", [*self.entities, variant])
        with patch("code_ontology.llm.LLM", side_effect=AssertionError("No model calls")):
            manifest = export_rdf(self.run, self.snapshot)
        self.assertEqual(manifest["state"], "success")
        self.assertEqual(manifest["counts"]["entities"], 3)
        self.assertEqual(manifest["counts"]["entity_identities"], 2)
        facts, assertions = self.load("facts.ttl"), self.load("assertions.ttl")
        self.assertEqual(len(list(facts.subject_objects(RDF.type))), 2)
        self.assertEqual(len(list(assertions.subjects(RDF.type, RDF.Statement))), 5)
        records = [row for value in assertions.objects(None, CO.sourceRecord)
                   if (row := json.loads(str(value))).get("id") == "e1"]
        self.assertEqual({r["basis"] for r in records}, {"direct", "inferred"})
        self.assertEqual({e for r in records for e in r["evidence_ids"]}, {"ev", "ev2"})

    def test_conflicting_entity_identity_fails(self):
        for changes in ({"type_id": "b"}, {"scope": "example"}, {"name": "Other"}):
            with self.subTest(changes=changes):
                write_jsonl(self.run / "final/entities.jsonl", [*self.entities, {**self.entities[0], **changes}])
                with self.assertRaisesRegex(ValueError, "Conflicting final entity identity: e1"):
                    export_rdf(self.run, self.snapshot)
                self.assertEqual(read_json(self.run / "rdf/export-manifest.json")["state"], "failed")

    def test_upstream_partial_never_success(self):
        write_json(self.run / "metrics.json", {"status": "partial", "view": "ast"})
        self.assertEqual(export_rdf(self.run, self.snapshot)["state"], "partial")

    def test_escaping_snapshot_inference_and_missing_evidence(self):
        self.entities[0]["name"] = '中文 "quoted"\n\\name'
        write_jsonl(self.run / "final/entities.jsonl", self.entities)
        explicit = export_rdf(self.run, self.snapshot)
        facts = self.load("facts.ttl")
        implicit = export_rdf(self.run)
        self.assertEqual(explicit["input_fingerprint"], implicit["input_fingerprint"])
        self.assertTrue(isomorphic(facts, self.load("facts.ttl")))
        (self.snapshot / "prepared/ast/packs.jsonl").unlink()
        manifest = export_rdf(self.run, self.snapshot)
        self.assertEqual(manifest["missing_evidence_locations"], ["ev"])

    def test_changed_inputs_during_export_fail(self):
        from pyshacl import validate as real_validate
        def change_input(*args, **kwargs):
            report = real_validate(*args, **kwargs)
            write_json(self.run / "metrics.json", {"status": "partial"})
            return report
        with patch("pyshacl.validate", side_effect=change_input):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                export_rdf(self.run, self.snapshot)
        self.assertEqual(read_json(self.run / "rdf/export-manifest.json")["state"], "failed")

    def test_unknown_reference_and_stale_schema_fail(self):
        export_rdf(self.run, self.snapshot)
        write_jsonl(self.run / "final/relations.jsonl", [dict(subject="missing", relation_id="r", object="e2", basis="direct", evidence_ids=["ev"])])
        with self.assertRaisesRegex(ValueError, "endpoint"):
            export_rdf(self.run, self.snapshot)
        self.assertEqual(read_json(self.run / "rdf/export-manifest.json")["state"], "failed")
        self.schema["classes"][0]["name"] = "变更"
        write_json(self.run / "schema/schema.json", self.schema)
        with self.assertRaisesRegex(ValueError, "schema changed"):
            export_rdf(self.run, self.snapshot)

    def test_min_count_qualified_fact_is_not_unconditional(self):
        self.schema["properties"][0]["min_count"] = 1
        write_json(self.run / "final/schema.json", self.schema)
        self.assertFalse(export_rdf(self.run, self.snapshot)["shacl_conforms"])

    def test_inheritance_numeric_literals_and_fingerprint(self):
        self.schema["classes"].append(dict(id="child", name="子类", definition="child", parents=["b"], evidence_ids=["ev"]))
        write_json(self.run / "final/schema.json", self.schema)
        self.entities[1]["type_id"] = "child"
        write_jsonl(self.run / "final/entities.jsonl", self.entities)
        for value in (3, 1e-20, 1e20):
            write_jsonl(self.run / "final/attributes.jsonl", [dict(subject="e2", property_id="p", value=value, basis="direct", evidence_ids=["ev"])])
            key = input_fingerprint(self.run, self.snapshot)
            self.assertTrue(export_rdf(self.run, self.snapshot)["shacl_conforms"])
            self.assertEqual(key, input_fingerprint(self.run, self.snapshot))

    def test_cli_and_intranet_step(self):
        with patch("code_ontology.cli.load_settings", side_effect=AssertionError("Export needs no model settings")):
            self.assertEqual(main(["export-rdf", "--run-dir", str(self.run), "--snapshot", str(self.snapshot)]), 0)
        from code_ontology.intranet import STEPS, execute, stage_fingerprint
        from code_ontology.config import Settings
        self.assertIn("export-rdf", STEPS)
        session = dict(snapshot=str(self.snapshot), out=str(self.snapshot.parent), view="ast", profile="general", experiment="demo", repo="unused")
        with patch("code_ontology.repository.verify_sources"):
            self.assertEqual(execute("export-rdf", session, Settings(), {}), 0)
        self.assertEqual(stage_fingerprint("export-rdf", session, Settings(), {}), input_fingerprint(self.run, self.snapshot))

    def test_workbench_subprocess_and_stale_inputs(self):
        from code_ontology.workbench_jobs import Workbench
        root = self.snapshot.parent
        repo = root / "repo"
        repo.mkdir()
        env = root / ".env"
        env.write_text("OPENAI_API_KEY=unused\nOPENAI_BASE_URL=http://127.0.0.1:1/v1\n", encoding="utf-8")
        write_json(self.snapshot / "manifest.json", {"snapshot_id": "stable-snapshot", "repo": str(repo),
                   "files": [], "parser": {"backend": "tree-sitter", "available_views": ["ast"], "limitations": []}})
        service = Workbench(root / "control")
        try:
            wid = service.save_workspace(dict(repo=str(repo), out=str(self.snapshot), snapshot=str(self.snapshot),
                   env_file=str(env), view="ast", profile="general", experiment="demo"))["workspace"]["id"]
            plan = service.plan(wid, "export-rdf")
            self.assertFalse(plan["model"])
            self.assertFalse(plan["blocked"])
            job = service.submit(wid, "export-rdf")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                current = service.job(job["id"])
                if current["state"] not in {"running", "queued", "cancelling"}:
                    break
                time.sleep(.05)
            self.assertEqual(current["state"], "succeeded", service.logs(job["id"]))
            service._check_outputs("export-rdf", service.workspace(wid))
            original = plan["fingerprint"]
            write_jsonl(self.run / "final/conflicts.jsonl", [{"status": "new conflict"}])
            self.assertNotEqual(original, service.plan(wid, "export-rdf")["fingerprint"])
            with self.assertRaisesRegex(ValueError, "输入已变化"):
                service._check_outputs("export-rdf", service.workspace(wid))
            write_json(self.run / "metrics.json", {"status": "partial", "view": "ast"})
            partial_job = service.submit(wid, "export-rdf")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                current = service.job(partial_job["id"])
                if current["state"] not in {"running", "queued", "cancelling"}:
                    break
                time.sleep(.05)
            self.assertEqual(current["state"], "partial", service.logs(partial_job["id"]))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
