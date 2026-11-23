import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from code_ontology.console import configure_console


class ConsoleTests(unittest.TestCase):
    def test_host_owned_string_streams_are_preserved(self):
        stream = io.StringIO()
        with patch("sys.stdout", stream), patch("sys.stderr", None):
            configure_console()
            print("中文输出")
        self.assertEqual(stream.getvalue(), "中文输出\n")

    def test_cli_outputs_utf8_when_interpreter_mode_is_disabled(self):
        root = Path(__file__).resolve().parent.parent
        env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii:strict"}
        for module in ("code_ontology", "code_ontology.viewer", "code_ontology.review"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-m", module, "--help"], cwd=root,
                                        env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
                self.assertTrue(any(ord(c) > 127 for c in result.stdout.decode("utf-8")))
