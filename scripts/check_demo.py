"""Real Joern + explicitly synthetic API integration check, without external LLM calls."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from code_ontology.cli import main, validate
from code_ontology.console import configure_console
from code_ontology.config import Settings, load_settings, load_policy
from code_ontology.joern import parse
from code_ontology.pipeline import Pipeline, comparison
from code_ontology.prepare import prepare
from code_ontology.repository import snapshot
from code_ontology.util import read_json, write_json, write_text
from tests.fake_api import fake_server


def run():
    configure_console()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, help="Reuse a previously parsed snapshot")
    ap.add_argument("--real-joern", action="store_true", help="Run real Joern if no snapshot is supplied")
    args = ap.parse_args()
    if not args.snapshot and not args.real_joern:
        ap.error("Use --snapshot <parsed directory> or --real-joern")
    policy = load_policy()
    policy["parser_backend"] = "joern"  # This script explicitly validates Joern's three views.
    if args.snapshot:
        dest = args.snapshot.resolve()
    else:
        settings = load_settings()
        dest = snapshot(ROOT / "examples" / "maintenance", ROOT / "outputs" / "real-joern", policy, settings.joern_home)
        parse(dest, settings, policy, ["ast", "cfg", "cpg"])
    with fake_server() as (url, transport):
        settings = Settings(base_url=url, api_key="fixture-only", model="fixture-not-a-real-llm", retries=0)
        results = []
        for view in ("ast", "cfg", "cpg"):
            prepare(dest, view, policy)
            for profile in ("general", "maintenance"):
                print(f"FIXTURE TEST {view}/{profile}", flush=True)
                pipeline = Pipeline(dest, view, profile, "fixture-integration", settings, policy)
                try:
                    schema = pipeline.fixed_schema()
                    if not pipeline.extract(schema):
                        raise RuntimeError("Partial fixture run")
                    if validate(dest, view, pipeline.output):
                        raise RuntimeError("Evidence validation failed")
                    metrics = read_json(pipeline.output / "metrics.json")
                    metrics["test_mode"] = "synthetic_llm_real_joern_not_quality_evaluation"
                    write_json(pipeline.output / "metrics.json", metrics)
                    results.append(metrics)
                finally:
                    pipeline.close()
        before = transport["requests"]
        resumed = Pipeline(dest, "cpg", "maintenance", "fixture-integration", settings, policy)
        try:
            resumed.extract(resumed.fixed_schema())
        finally:
            resumed.close()
        resume_requests = transport["requests"] - before
        if resume_requests:
            raise RuntimeError("Unexpected API request during identical resume")
        report = comparison(dest, "fixture-integration")
        summary = {"test_mode": "REAL JOERN + SYNTHETIC API; NOT REAL LLM QUALITY", "snapshot": str(dest),
                   "graph": read_json(dest / "raw" / "cpg" / "export.json"), "combinations": len(results),
                   "additional_requests_on_resume": resume_requests, "api_requests": transport["requests"],
                   "views": {v: read_json(dest / "normalized" / v / "index.json") for v in ("ast", "cfg", "cpg")}}
        write_json(dest / "fixture_validation.json", summary)
        write_text(report.parent / "FIXTURE_ONLY.txt", "本目录使用真实 Joern 和测试 API 替身。模型知识为人工构造的测试响应，不能用于评估抽取质量。\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
