from collections import Counter
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.batch_passes import run_passes
from code_ontology.config import Settings, load_policy
from code_ontology.llm import BudgetError, ModelFailure, NonRetryableModelFailure, RequestUnknown
from code_ontology.parsing import parse_repository
from code_ontology.pipeline import Pipeline
from code_ontology.prepare import prepare
from code_ontology.repository import snapshot
from code_ontology.stages import SchemaStages
from code_ontology.util import read_json
from tests.fake_api import fake_server, fixture_response


class BatchPassTests(unittest.TestCase):
    def run_batches(self, tasks, process, path, passes=2, concurrency=2):
        return run_passes(tasks, process, passes=passes, concurrency=concurrency,
                          stage="discover", path=path, safe_error=str)

    def test_only_failed_batches_repeat_and_preserve_order(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            path = Path(directory)/"passes.json"
            calls = Counter()
            def process(i, task, attempt):
                calls[i] += 1
                if i == 1 and calls[i] == 1:
                    raise ModelFailure("temporary service error")
                return task
            results, errors = self.run_batches(["a", "b", "c"], process, path)
            self.assertFalse(errors)
            self.assertEqual(results, {0:"a", 1:"b", 2:"c"})
            self.assertEqual(calls, {0:1, 1:2, 2:1})
            self.assertEqual(read_json(path)["state"], "success")
            self.assertEqual(read_json(path)["pass_number"], 2)

    def test_unknown_budget_and_exhaustion_are_not_success(self):
        for error, attempts, state in [(RequestUnknown("timeout"),1,"unknown"),
                                       (BudgetError("unsplittable"),1,"failed"),
                                       (ValueError("invalid response"),2,"failed")]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                calls = []
                def process(i, task, attempt):
                    calls.append(i)
                    if i == 0:
                        raise error
                    return task
                path = Path(directory)/"passes.json"
                results, errors = self.run_batches(["bad", "good"], process, path)
                self.assertEqual(calls.count(0), attempts)
                self.assertEqual(calls.count(1), 1)
                self.assertEqual(errors[0]["state"], state)
                self.assertEqual(results, {1:"good"})
                self.assertEqual(read_json(path)["state"], "partial")

    def test_auth_and_cancellation_never_enter_next_pass(self):
        for error in (NonRetryableModelFailure("401"), KeyboardInterrupt()):
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                calls=[]
                def process(i, task, attempt):
                    calls.append(i)
                    raise error
                with self.assertRaises(type(error)):
                    self.run_batches([1,2], process, Path(directory)/"passes.json", concurrency=1)
                self.assertEqual(calls,[0])

    def test_parameters_are_positive_and_environment_overrides_yaml(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root=Path(directory)
            config=root/"config.yaml";config.write_text("discovery_pass_count: 3\nmerge_pass_count: 4\n")
            env=root/"empty.env";env.write_text("CO_DISCOVERY_PASS_COUNT=2\n")
            policy=load_policy(str(config),str(env))
            self.assertEqual((policy["discovery_pass_count"],policy["merge_pass_count"]),(2,4))
            env.write_text("CO_MERGE_PASS_COUNT=0\n")
            with self.assertRaises(ValueError):
                load_policy(str(config),str(env))

    def test_local_and_merge_recovery_reuse_successful_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory, fake_server() as (url,state), redirect_stdout(io.StringIO()):
            root=Path(directory);repo=root/"repo";repo.mkdir()
            (repo/"device.cpp").write_text("struct Device {\n int run(int t) {\n if(t>85) return 1;\n return 0;\n }\n};\n")
            env=root/"empty.env";env.write_text("")
            policy=load_policy(env_file=str(env))
            policy.update(parser_backend="tree-sitter",discovery_max_packs=0,batch_packs=1,
                          discovery_pass_count=2,merge_pass_count=2,merge_batch_items=2)
            settings=Settings(base_url=url,model="fixture-only",retries=0,context_tokens=32768)
            dest=snapshot(repo,root/"out",policy)
            parse_repository(dest,settings,policy,["ast"]);prepare(dest,"ast",policy)
            pipeline=Pipeline(dest,"ast","general","passes",settings,policy)
            original=SchemaStages.call
            failures=set()
            def fail_once(runner,payload):
                key=runner.stage
                if key == "discover" and key not in failures:
                    failures.add(key)
                    raise ModelFailure("temporary service error")
                return original(runner,payload)
            def responses(payload):
                value = fixture_response(payload)
                if payload.get("stage") == "merge" and state["stage_counts"]["merge"] <= 2:
                    value["source_mapping"][0]["target_id"] = "nonexistent"
                return value
            state["response_factory"] = responses
            with patch.object(SchemaStages,"call",fail_once):
                pipeline.discover_local()
                pipeline.merge_schema()
            schema_root=pipeline.output/"schema"
            self.assertEqual(read_json(schema_root/"local-passes.json")["pass_number"],2)
            self.assertTrue(any(read_json(p)["pass_number"]==2 for p in (schema_root/"merge-passes").glob("*.json")))
            count=state["requests"]
            pipeline.policy.update(discovery_pass_count=3,merge_pass_count=3)
            pipeline.discover_local();pipeline.merge_schema()
            self.assertEqual(state["requests"],count)
            self.assertEqual(read_json(schema_root/"local-passes.json")["pass_number"],1)
            self.assertEqual(read_json(schema_root/"merge-status.json")["state"],"success")


if __name__ == "__main__":
    unittest.main()
