"""Measure real AST/prepare stages and audit LLM planning without a remote model."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from code_ontology.cli import main as cli_main
from code_ontology.config import Settings, load_policy
from code_ontology.llm import LLM, BudgetError
from code_ontology.models import Ontology
from code_ontology.pipeline import Pipeline, selected_packs, group_batches, prompt
from code_ontology.util import canonical, iter_jsonl, read_json, token_bound, write_json


def peak_memory_mib():
    if os.name == "nt":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                 "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize / 1024 ** 2
    import resource
    amount = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return amount / (1024 ** 2 if sys.platform == "darwin" else 1024)


def worker(args):
    started = time.perf_counter()
    code, error = 1, None
    try:
        code = cli_main(args)
    except Exception as exc:
        error = str(exc)
    return {"seconds": round(time.perf_counter() - started, 3), "peak_working_set_mib": round(peak_memory_mib(), 2),
            "exit_code": code, "error": error, "command": [sys.executable, "-m", "code_ontology", *args]}


def stage(name, args, output):
    report = output / (name + ".json")
    with (output / (name + ".log")).open("w", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(report), *args],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    info = read_json(report)
    print(name + ": " + canonical(info), flush=True)
    if result.returncode or info["exit_code"]:
        raise RuntimeError(f"Stage {name} failed; see {output / (name + '.log')}")
    return info


def percentiles(values):
    values = sorted(values)
    return {"min": values[0], "median": statistics.median(values), "p95": values[min(len(values) - 1, int(len(values) * .95))],
            "max": values[-1], "total": sum(values)} if values else {}


def module(file):
    return Path(file).parts[1]


def audit(dest, truth, output):
    policy = load_policy(str(ROOT / "configs/tree-sitter.yaml"))
    packs = list(iter_jsonl(dest / "prepared/ast/packs.jsonl"))
    symbols = list(iter_jsonl(dest / "normalized/symbols.jsonl"))
    exported = read_json(dest / "raw/tree-sitter/export.json")
    report = read_json(dest / "raw/tree-sitter/parse_report.json")
    actual_methods = {(s["FILENAME"], s["NAME"]) for s in symbols if s["kind"] == "METHOD"}
    actual_types = {(s["FILENAME"], s["NAME"]) for s in symbols if s["kind"] == "TYPE_DECL"}
    expected_methods = {tuple(s) for s in truth["expected_methods"]}
    expected_types = {tuple(s) for s in truth["expected_types"]}
    covered = defaultdict(set)
    for pack in packs:
        for ref in pack["source_refs"]:
            covered[ref["file"]].update(range(ref["start_line"], ref["end_line"] + 1))
    missing_code, all_code = [], 0
    for record in truth["files"]:
        lines = (dest / "source" / record["file"]).read_text(encoding="utf-8").splitlines()
        for line, text in enumerate(lines, 1):
            if text.strip() and not text.lstrip().startswith("//"):
                all_code += 1
                if line not in covered[record["file"]]:
                    missing_code.append([record["file"], line])
    comment_only, comment_share = [], []
    for pack in packs:
        lines = pack["code"].splitlines()
        comments = sum(bool(line.strip()) and line.lstrip().startswith("//") for line in lines)
        nonblank = sum(bool(line.strip()) for line in lines)
        comment_share.append(comments / nonblank if nonblank else 0)
        if nonblank and comments == nonblank:
            comment_only.append(pack)
    selection = []
    for limit in (8, 24, 100, 200, 0):
        chosen = selected_packs(packs, limit)
        files = {p["source_refs"][0]["file"] for p in chosen}
        modules = sorted({module(f) for f in files})
        selected_ids = {p["pack_id"] for p in chosen}
        selection.append({"limit": limit, "packs": len(chosen), "files": len(files), "modules": modules,
                          "comment_only_packs": sum(p["pack_id"] in selected_ids for p in comment_only),
                          "fraction_of_all_packs": round(len(chosen) / len(packs), 5)})
    # Budget inspection uses the production budget functions, with no network request.
    settings = Settings(base_url="http://127.0.0.1:1/v1", api_key="fixture-only", model="dry-plan-no-network", retries=0)
    llm = LLM(settings, output / "planning-cache")
    try:
        system = prompt(policy, "maintenance", "discover")
        chosen = selected_packs(packs, policy["discovery_max_packs"])
        make = lambda batch: {"stage": "discover", "profile": "maintenance", "packs": batch}
        batches = list(group_batches(chosen, system, llm, make, policy["batch_packs"]))
        discover_sizes = [token_bound([{"role": "system", "content": system}, {"role": "user", "content": canonical(make(batch))}]) for batch in batches]
        extract_system = prompt(policy, "maintenance", "extract")
        class_names = sorted({s["NAME"] for s in symbols if s["kind"] == "TYPE_DECL" and "State" not in s["NAME"] and "Reading" not in s["NAME"]})
        schema = Ontology.model_validate({"version": "0.1", "profile": "maintenance", "classes": [
            {"id": f"c{i}", "name": name, "definition": "Synthetic equipment class for budget measurement; not a model-generated business concept.",
             "aliases": [name], "parents": [], "evidence_ids": packs[0]["evidence_ids"]}
            for i, name in enumerate(class_names)], "relations": [], "properties": [], "unresolved": []})
        sizes = [token_bound([{"role": "system", "content": extract_system}, {"role": "user", "content": canonical({"stage": "extract", "schema": schema.model_dump(), "packs": [pack]})}]) for pack in packs]
        nofit = sum(s + settings.max_output_tokens + 512 > settings.context_tokens for s in sizes)
        sample = selected_packs(packs, min(100, len(packs)))
        schema_scenarios = []
        for count in (50, 200, 500):
            data = schema.model_dump()
            data["classes"] = [{"id": f"c{i}", "name": class_names[i] if i < len(class_names) else f"synthetic_additional_concept_{i}",
                "definition": "Synthetic equipment class for budget measurement; not a model-generated business concept.",
                "aliases": [class_names[i]] if i < len(class_names) else [], "parents": [], "evidence_ids": packs[0]["evidence_ids"]} for i in range(count)]
            proposed = Ontology.model_validate(data)
            errors, subset, successful_sizes = 0, 0, []
            for pack in sample:
                try:
                    sub, omitted = Pipeline.extraction_schema(SimpleNamespace(llm=llm), proposed, [pack], extract_system)
                    subset += bool(omitted)
                    successful_sizes.append(token_bound([{"role": "system", "content": extract_system}, {"role": "user", "content": canonical({"stage":"extract", "schema":sub.model_dump(), "packs":[pack]})}]))
                except BudgetError:
                    errors += 1
            schema_scenarios.append({"classes": count, "sample_packs": len(sample), "lexical_subset_tasks": subset,
                                     "budget_errors": errors, "input_utf8_byte_bound": percentiles(successful_sizes)})
        planning = {"network_requests": llm.stats["requests"], "profile": "maintenance", "context_tokens": settings.context_tokens,
                    "reserved_output_tokens": settings.max_output_tokens, "discovery_tasks": len(batches),
                    "discovery_input_utf8_byte_bound": percentiles(discover_sizes), "extract_tasks_one_pack_each": len(packs),
                    "base_requests_one_profile_minimum_one_merge": len(batches) + 1 + len(packs),
                    "base_requests_two_profiles_minimum_one_merge_each": 2 * (len(batches) + 1 + len(packs)),
                    "full_schema_50_classes_requests_not_fitting": nofit, "full_schema_50_classes_input_utf8_byte_bound": percentiles(sizes),
                    "schema_budget_scenarios": schema_scenarios,
                    "note": "UTF-8 byte bounds are conservative project budgeting units, NOT tokenizer counts. Schemas are synthetic, not extracted. Merge calls/retries/repairs may add requests."}
    finally:
        llm.close()
    result = {"input": truth["actual"], "snapshot": str(dest), "graph": exported,
              "syntax_errors": report["syntax_errors"], "symbols": {"total": len(symbols),
                  "expected_method_locations": len(expected_methods), "found_expected_methods": len(actual_methods & expected_methods),
                  "expected_type_locations": len(expected_types), "found_expected_types": len(actual_types & expected_types),
                  "missing_methods": sorted(expected_methods - actual_methods), "missing_types": sorted(expected_types - actual_types)},
              "packs": {"total": len(packs), "by_kind": dict(Counter(p["kind"] for p in packs)),
                  "source_files": len(covered), "covered_code_lines": all_code - len(missing_code), "all_code_lines": all_code,
                  "missing_code_lines": missing_code[:50], "missing_code_line_count": len(missing_code),
                  "comment_only": len(comment_only), "context_empty": sum(not p["context"] for p in packs),
                  "utf8_byte_bound": percentiles([token_bound(p) for p in packs]),
                  "artifact_bytes": (dest / "prepared/ast/packs.jsonl").stat().st_size,
                  "prepare_index": read_json(dest / "prepared/ast/index.json")},
              "discovery_selection": selection, "llm_plan": planning,
              "artifact_sizes_bytes": {name: sum(p.stat().st_size for p in (dest / name).rglob("*") if p.is_file()) for name in ("source", "raw", "normalized", "prepared")}}
    write_json(output / "audit.json", result)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fake-llm", action="store_true", help="Run full maintenance pipeline against a local synthetic model")
    args = ap.parse_args()
    args.out = args.out.resolve()
    if (args.out / "benchmark.json").exists():
        ap.error("Choose a new --out to preserve benchmark evidence")
    args.out.mkdir(parents=True, exist_ok=True)
    policy = str(ROOT / "configs/tree-sitter.yaml")
    parse_args = ["parse", "--repo", str(args.repo.resolve()), "--config", policy, "--out", str(args.out / "snapshots")]
    stages = {"parse": stage("parse", parse_args, args.out)}
    snapshots = list((args.out / "snapshots").glob("*/manifest.json"))
    if len(snapshots) != 1:
        raise RuntimeError("Expected exactly one snapshot")
    dest = snapshots[0].parent
    stages["parse_cached"] = stage("parse_cached", parse_args, args.out)
    prepare_args = ["prepare", "--snapshot", str(dest), "--view", "ast", "--config", policy]
    stages["prepare"] = stage("prepare", prepare_args, args.out)
    stages["prepare_cached"] = stage("prepare_cached", prepare_args, args.out)
    stages["validate"] = stage("validate", ["validate", "--snapshot", str(dest), "--view", "ast"], args.out)
    result = audit(dest, read_json(args.truth), args.out)
    result["stages"] = stages
    write_json(args.out / "benchmark.json", result)
    print("BENCHMARK=" + str(args.out / "benchmark.json"), flush=True)
    if args.fake_llm:
        from tests.fake_api import fake_server
        import uuid
        with fake_server() as (url, transport):
            env = args.out / ".env.fixture"
            env.write_text(f"OPENAI_BASE_URL={url}\nOPENAI_API_KEY=fixture-only\nOPENAI_MODEL=synthetic-not-quality\n", encoding="utf-8")
            # The production config loader prioritizes environment variables; isolate them for worker children.
            old = dict(os.environ)
            try:
                os.environ.update(OPENAI_BASE_URL=url, OPENAI_API_KEY="fixture-only", OPENAI_MODEL="synthetic-not-quality", LLM_MAX_RETRIES="0")
                experiment = "scale-fixture-" + uuid.uuid4().hex[:8]
                common = ["--snapshot", str(dest), "--view", "ast", "--profile", "maintenance", "--experiment", experiment, "--config", policy, "--env-file", str(env)]
                stages["discover_fixture"] = stage("discover_fixture", ["discover", *common], args.out)
                schema = dest / "runs" / experiment / "ast/maintenance/schema/schema.json"
                extract = ["extract", *common, "--schema", str(schema)]
                stages["extract_fixture"] = stage("extract_fixture", extract, args.out)
                first_metrics = read_json(schema.parent.parent / "metrics.json")
                write_json(args.out / "first-extraction-metrics.json", first_metrics)
                before = transport["requests"]
                stages["extract_fixture_cached"] = stage("extract_fixture_cached", extract, args.out)
                run = schema.parent.parent
                stages["validate_fixture"] = stage("validate_fixture", ["validate", "--snapshot", str(dest), "--view", "ast", "--run-dir", str(run)], args.out)
                result["fixture_pipeline"] = {"mode": "LOCAL SYNTHETIC API; NO REAL MODEL; NOT SEMANTIC QUALITY",
                    "run": str(run), "requests": transport["requests"], "additional_requests_on_resume": transport["requests"] - before,
                    "first_metrics": first_metrics, "resumed_metrics": read_json(run / "metrics.json")}
            finally:
                os.environ.clear()
                os.environ.update(old)
        write_json(args.out / "benchmark.json", result)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        result = worker(sys.argv[3:])
        write_json(Path(sys.argv[2]), result)
        raise SystemExit(result["exit_code"])
    main()
