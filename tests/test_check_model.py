import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.config import Settings
from scripts.check_model import main
from tests.fake_api import fake_server


class CheckModelTests(unittest.TestCase):
    def run_probe(self, endpoint, output):
        settings = Settings(base_url=endpoint, model="fixture-only", retries=0)
        with patch("scripts.check_model.load_settings", return_value=settings), contextlib.redirect_stdout(io.StringIO()):
            return main(["--out", str(output)])

    def test_each_invocation_sends_a_fresh_request(self):
        with tempfile.TemporaryDirectory() as directory, fake_server() as (endpoint, state):
            output = Path(directory)
            self.assertEqual(self.run_probe(endpoint, output), 0)
            self.assertEqual(self.run_probe(endpoint, output), 0)
            self.assertEqual(state["requests"], 2)
            reports = list(output.glob("*/result.json"))
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["ok"] for path in reports))

    def test_auth_failure_returns_nonzero_without_retry(self):
        with tempfile.TemporaryDirectory() as directory, fake_server("auth") as (endpoint, state):
            self.assertEqual(self.run_probe(endpoint, directory), 1)
            self.assertEqual(state["requests"], 1)

    def test_false_probe_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory, fake_server() as (endpoint, state):
            state["response_factory"] = lambda payload: {"ok": False}
            self.assertEqual(self.run_probe(endpoint, directory), 1)


if __name__ == "__main__":
    unittest.main()
