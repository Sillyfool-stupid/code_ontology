"""Offline alternative planning; makes no model requests and changes no pipeline outputs."""
from collections import defaultdict, deque, Counter
from pathlib import Path
import argparse
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from code_ontology.config import Settings, load_policy
from code_ontology.llm import LLM, BudgetError
from code_ontology.models import Ontology
from code_ontology.pipeline import Pipeline, prompt
from code_ontology.util import canonical, iter_jsonl, read_json, token_bound, write_json


def has_code(pack):
    return any(line.strip() and not line.lstrip().startswith("//") for line in pack["code"].splitlines())


def diverse_selection(packs, limit):
    files = defaultdict(list)
    for pack in packs:
        if has_code(pack):
            files[pack["source_refs"][0]["file"]].append(pack)
    modules = defaultdict(deque)
    for file in sorted(files):
        candidates = sorted(files[file], key=lambda p: (p["kind"] != "METHOD", p["kind"] != "TYPE_DECL", p["pack_id"]))
        modules[Path(file).parts[1]].append(candidates[0])
    chosen = []
    while modules and len(chosen) < limit:
        for name in list(sorted(modules)):
            chosen.append(modules[name].popleft())
            if not modules[name]:
                del modules[name]
            if len(chosen) == limit:
                break
    return chosen


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark", type=Path, required=True)
    args = ap.parse_args()
    benchmark = read_json(args.benchmark)
    dest = Path(benchmark["snapshot"])
    packs = list(iter_jsonl(dest / "prepared/ast/packs.jsonl"))
    active = [p for p in packs if has_code(p)]
    symbols = list(iter_jsonl(dest / "normalized/symbols.jsonl"))
    names = sorted({s["NAME"] for s in symbols if s["kind"] == "TYPE_DECL" and not s["NAME"].endswith(("Reading", "State"))})
    policy = load_policy(str(ROOT / "configs/tree-sitter.yaml"))
    system = prompt(policy, "maintenance", "extract")
    # Sample all files, then deterministic positions throughout the corpus, including documentation.
    chosen = diverse_selection(packs, 150)
    positions = [packs[int(i * (len(packs) - 1) / 149)] for i in range(150)]
    sample = list({p["pack_id"]:p for p in chosen + positions}.values())
    results = {"note": "OFFLINE PLAN, NOT AN IMPLEMENTED PIPELINE CHANGE OR MODEL QUALITY RESULT", "diverse_selection": [],
               "code_bearing_packs": len(active), "comment_only_packs": len(packs) - len(active),
               "schema_retrieval": [], "batching": [], "network_requests": 0}
    for limit in (24, 80, 150):
        selected = diverse_selection(packs, limit)
        files = {p["source_refs"][0]["file"] for p in selected}
        results["diverse_selection"].append({"limit":limit,"packs":len(selected),"files":len(files),
            "modules":sorted({Path(f).parts[1] for f in files})})
    for context, output in ((16384,4096),(65536,8192)):
        llm = LLM(Settings(base_url="http://127.0.0.1:1/v1", model="no-network-plan", api_key="fixture-only",
                           context_tokens=context, max_output_tokens=output), args.benchmark.parent / "strategy-cache")
        planner = SimpleNamespace(llm=llm)
        try:
            def schema(count, aliases):
                return Ontology.model_validate({"version":"0.1","profile":"maintenance","classes":[
                    {"id":f"c{i}","name":f"模拟业务设备概念{i:03d}",
                     "definition":"Synthetic concept used to exercise request-budget logic; never a measured ontology-quality result.",
                     "aliases":[names[i]] if aliases and i<len(names) else [],"parents":[],"evidence_ids":packs[0]["evidence_ids"]}
                    for i in range(count)],"relations":[],"properties":[],"unresolved":[]})
            for count in (50,200,500):
                for aliases in (True,False):
                    proposed = schema(count,aliases)
                    failures, subsets, comment_failures = 0,0,0
                    for pack in sample:
                        try:
                            _, omitted = Pipeline.extraction_schema(planner,proposed,[pack],system)
                            subsets += bool(omitted)
                        except BudgetError:
                            failures += 1
                            comment_failures += not has_code(pack)
                    results["schema_retrieval"].append({"context":context,"schema_classes":count,"english_aliases":aliases,
                        "sample":len(sample),"subset_tasks":subsets,"budget_errors":failures,"comment_only_errors":comment_failures})
            # Proposed adaptive batches from the same source file; use current subset logic for every candidate batch.
            proposed = schema(50,True)
            groups = defaultdict(list)
            for pack in active:
                groups[pack["source_refs"][0]["file"]].append(pack)
            tasks, failed_singles, sizes, item_counts = 0,0,[],[]
            for file in sorted(groups):
                batch = []
                def fits(candidate):
                    try:
                        subset, _ = Pipeline.extraction_schema(planner,proposed,candidate,system)
                        messages=[{"role":"system","content":system},{"role":"user","content":canonical({"stage":"extract","schema":subset.model_dump(),"packs":candidate})}]
                        return llm.fits(messages), token_bound(messages)
                    except BudgetError:
                        return False,0
                for pack in groups[file]:
                    ok,size = fits(batch + [pack]) if len(batch)<8 else (False,0)
                    if not ok and batch:
                        tasks += 1
                        sizes.append(fits(batch)[1]); item_counts.append(len(batch))
                        batch=[]
                    ok,size=fits(batch+[pack])
                    if ok:
                        batch.append(pack)
                    else:
                        failed_singles += 1
                if batch:
                    tasks+=1; sizes.append(fits(batch)[1]); item_counts.append(len(batch))
            results["batching"].append({"context":context,"output_reserve":output,"max_packs_per_request":8,
                "input_packs":len(active),"requests":tasks,"failed_single_packs":failed_singles,
                "packs_per_request":dict(Counter(item_counts)),"total_input_byte_bound":sum(sizes),
                "note":"Capacity-only estimate; combined output size and real extraction quality still require model tests."})
            results["network_requests"] += llm.stats["requests"]
        finally:
            llm.close()
    results["graph_context"]={"packs":len(packs),"zero_graph_nodes":sum(not p["context"].get("nodes") for p in packs),
        "average_nodes_per_pack":sum(len(p["context"].get("nodes",[])) for p in packs)/len(packs),
        "average_edges_per_pack":sum(len(p["context"].get("edges",[])) for p in packs)/len(packs),
        "code_packs_missing_graph_edges":sum(not p["context"].get("edges") for p in active)}
    path=args.benchmark.parent / "strategy.json"
    write_json(path,results)
    print(canonical(results),flush=True)


if __name__=="__main__":
    main()
