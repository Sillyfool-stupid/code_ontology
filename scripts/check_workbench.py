"""Verify real Joern stage orchestration with a synthetic local model API."""
from pathlib import Path
import argparse
import json
import os
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from code_ontology.config import load_settings
from code_ontology.console import configure_console
from code_ontology.util import read_json, write_json, write_text
from code_ontology.workbench_jobs import Workbench
from tests.fake_api import fake_server


def main():
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    backends = parser.add_mutually_exclusive_group(required=True)
    backends.add_argument("--real-joern", action="store_true")
    backends.add_argument("--tree-sitter", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/workbench-verification")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    joern_home = load_settings().joern_home
    results = []
    backend = "tree-sitter" if args.tree_sitter else "joern"
    combinations = 2 if args.tree_sitter else 6
    experiment = "fixture-workbench-" + uuid.uuid4().hex[:8]
    config = out / "verification-policy.yaml"
    config.write_text(f"parser_backend: {backend}\ndiscovery_max_packs: 24\n", encoding="utf-8")
    with fake_server() as (url, transport), patch.dict(os.environ):
        envfile = out / ".env.fixture"
        # Never copy the user's model endpoint or key into the verification task.
        envfile.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_MODEL=fixture-not-a-real-llm\n"
                           f"OPENAI_API_KEY=fixture-only\nJOERN_HOME={joern_home}\nLLM_MAX_RETRIES=0\n", encoding="utf-8")
        os.environ.update(OPENAI_BASE_URL=url, OPENAI_MODEL="fixture-not-a-real-llm",
                          OPENAI_API_KEY="fixture-only", JOERN_HOME=joern_home, LLM_MAX_RETRIES="0")
        service = Workbench(out / "control", project=ROOT, browse_roots=[ROOT, out])
        try:
            wid = service.save_workspace({"repo": str(ROOT / "examples/maintenance"), "out": str(out / "artifacts"),
                "config": str(config), "env_file": str(envfile),
                "experiment": experiment, "view": "cpg", "profile": "maintenance",
                "gold": str(ROOT / "examples/maintenance/gold.json")})["workspace"]["id"]
            def run(stage):
                print(f"WORKBENCH CHECK {stage}", flush=True)
                job = service.submit(wid, stage)
                start = time.monotonic()
                while time.monotonic() - start < 900:
                    current = service.job(job["id"])
                    if current["state"] not in {"queued", "running", "cancelling"}:
                        break
                    time.sleep(.1)
                else:
                    service.cancel(job["id"])
                    raise RuntimeError(f"Stage timed out: {stage}")
                if current["state"] != "succeeded":
                    raise RuntimeError(json.dumps(current, ensure_ascii=False) + "\n" + service.logs(job["id"])["text"])
                results.append({"stage": stage, "job": current["id"], "state": current["state"]})
                return current
            for stage in ("doctor", "scan", "parse", "prepare", "discover-local", "merge-schema", "extract", "finalize", "validate", "evaluate", "report", "sample", "summarize"):
                run(stage)
            before = transport["requests"]
            run("extract")
            if before != transport["requests"]:
                raise RuntimeError("Identical resume unexpectedly called the API")
            run("experiment")
            snapshot = Path(service.workspace(wid)["snapshot"])
            comparison = read_json(snapshot / "runs" / experiment / "comparison.json")
            if comparison["missing"] or comparison["failures"] or len(comparison["runs"]) != combinations:
                raise RuntimeError("Experiment incomplete")
            for group in comparison["runs"]:
                if group["status"] != "success":
                    raise RuntimeError("Non-successful experiment group")
            summary = {"mode": f"REAL {backend} + SYNTHETIC API; NOT MODEL QUALITY EVALUATION",
                       "python": sys.executable, "snapshot": str(snapshot), "experiment": experiment, "stages": results,
                       "combinations": combinations, "additional_requests_on_resume": 0,
                       "fixture_api_requests": transport["requests"]}
            write_json(out / "verification.json", summary)
            write_text(snapshot / "runs" / experiment / "FIXTURE_ONLY.txt",
                       f"真实 {backend} + 模拟模型，验证工作台编排，不用于评价真实模型的抽取质量。\n")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        finally:
            service.close()


if __name__ == "__main__":
    main()
