"""Independent manual evaluation helpers; extraction outputs are never modified."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import heapq
import json
from pathlib import Path

from .util import canonical, digest, iter_jsonl, now, read_json, write_json
from .viewer import record_id
from .console import configure_console

RATINGS = {"verdict": {"", "correct", "partial", "incorrect", "uncertain"},
           "evidence_support": {"", "supported", "partial", "unsupported", "uncertain"},
           "domain_relevant": {"", "yes", "no", "uncertain"},
           "scope_correct": {"", "yes", "no", "na", "uncertain"},
           "rule_condition_complete": {"", "yes", "no", "na", "uncertain"}}
FIELDS = ["record_id", "kind", "payload_json", "evidence_ids", *RATINGS, "notes"]
ASSERTIONS = {"entities", "relations", "attributes", "rules"}


def collections(run_dir):
    schema_path = run_dir / "final" / "schema.json"
    if not schema_path.is_file():
        raise ValueError("Run must have final/schema.json before sampling")
    schema = read_json(schema_path)
    for kind in ("classes", "relations", "properties"):
        yield "schema." + kind, iter(schema.get(kind, []))
    for kind in ("entities", "relations", "attributes", "rules", "candidates", "conflicts"):
        path = run_dir / "final" / f"{kind}.jsonl"
        yield kind, iter_jsonl(path) if path.is_file() else iter(())


def sample(run_dir: Path, out: Path, per_kind=30, seed=42):
    if per_kind <= 0:
        raise ValueError("per-kind must be positive")
    rows, population = [], {}
    for kind, items in collections(run_dir):
        total = 0
        def candidates():
            nonlocal total
            for payload in items:
                total += 1
                rid = record_id(kind, payload)
                yield {"record_id": rid, "kind": kind, "payload_json": canonical(payload),
                       "evidence_ids": ";".join(payload.get("evidence_ids", payload.get("payload", {}).get("evidence_ids", []))),
                       **{key: "" for key in RATINGS}, "notes": ""}
        selected = heapq.nsmallest(per_kind, candidates(), key=lambda r: digest([seed, r["record_id"]], 40))
        population[kind] = {"population": total, "sampled": len(selected)}
        rows.extend(selected)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() or out.with_suffix(out.suffix + ".manifest.json").exists():
        raise ValueError("Review output already exists; choose a new path to preserve existing judgments")
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader(); writer.writerows(rows)
    write_json(out.with_suffix(out.suffix + ".manifest.json"), {
        "format_version": 1, "created_at": now(), "run_dir": str(run_dir.resolve()),
        "sampling_method": "deterministic_stratified_hash", "seed": seed, "per_kind": per_kind,
        "population": population,
        "sample_fingerprints": {r["record_id"]: digest([r["kind"], r["payload_json"], r["evidence_ids"]], 40) for r in rows},
        "rating_values": {k: sorted(v) for k, v in RATINGS.items()},
        "note": "Annotate only rating columns and notes. Precision describes this sample; recall requires a separate gold set."})
    return {"csv": str(out.resolve()), "rows": len(rows), "population": population}


def stats(rows):
    verdict = Counter(r.get("verdict", "") for r in rows)
    denominator = sum(verdict[v] for v in ("correct", "partial", "incorrect"))
    support = Counter(r.get("evidence_support", "") for r in rows)
    support_n = sum(support[v] for v in ("supported", "partial", "unsupported"))
    result = {"sample_rows": len(rows), "verdict_counts": dict(verdict),
              "judged_rows": denominator, "strict_sample_precision": verdict["correct"] / denominator if denominator else None,
              "source_support_judged": support_n, "source_supported_rate": support["supported"] / support_n if support_n else None,
              "unresolved_or_unreviewed": verdict["uncertain"] + verdict[""]}
    for key in ("domain_relevant", "scope_correct", "rule_condition_complete"):
        values = Counter(r.get(key, "") for r in rows)
        n = values["yes"] + values["no"]
        result[key] = {"counts": dict(values), "judged": n, "yes_rate": values["yes"] / n if n else None}
    return result


def summarize(path: Path, out: Path):
    if out.resolve() in {path.resolve(), path.with_suffix(path.suffix + ".manifest.json").resolve()}:
        raise ValueError("Summary output must not overwrite review data or its manifest")
    metadata = None
    if path.suffix.lower() == ".json":
        data = read_json(path)
        if data.get("sampling_method") != "interactive_nonrandom":
            raise ValueError("Expected exported viewer review JSON with interactive_nonrandom sampling_method")
        rows = data["annotations"]
        method = "interactive_nonrandom"
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not set(FIELDS) <= set(reader.fieldnames or []):
                raise ValueError("Review CSV is missing required columns")
            rows = list(reader)
        meta = path.with_suffix(path.suffix + ".manifest.json")
        metadata = read_json(meta) if meta.is_file() else None
        method = metadata.get("sampling_method", "unknown_csv") if metadata else "unknown_csv"
    seen, grouped = set(), defaultdict(list)
    for i, row in enumerate(rows, 1):
        if not row.get("record_id") or not row.get("kind"):
            raise ValueError(f"Row {i} is missing record_id or kind")
        if row["record_id"] in seen:
            raise ValueError(f"Duplicate review record: {row['record_id']}")
        seen.add(row["record_id"])
        for field, allowed in RATINGS.items():
            row[field] = row.get(field, "").strip()
            if row[field] not in allowed:
                raise ValueError(f"Row {i}: invalid {field}: {row[field]}")
        if metadata:
            expected = metadata["sample_fingerprints"].get(row["record_id"])
            actual = digest([row["kind"], row["payload_json"], row["evidence_ids"]], 40)
            if expected != actual:
                raise ValueError(f"Row {i}: original sample content was changed; edit only ratings and notes")
        grouped[row["kind"]].append(row)
    result = {"created_at": now(), "sampling_method": method,
              "per_kind": {k: stats(v) for k, v in sorted(grouped.items())},
              "assertion_sample": stats([r for r in rows if r["kind"] in ASSERTIONS]),
              "schema_sample": stats([r for r in rows if r["kind"].startswith("schema.")]),
              "population": metadata.get("population") if metadata else None,
              "missing_sample_rows": len(set(metadata["sample_fingerprints"]) - seen) if metadata else None,
              "warning": "Sample statistics only. Stratified aggregate is not population-weighted precision. Interactive reviews are nonrandom. No semantic labels or recall are inferred automatically."}
    write_json(out, result)
    return result


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description="独立人工评审抽样与汇总；不修改现有抽取结果")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--per-kind", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("summarize")
    p.add_argument("--reviews", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = sample(args.run_dir, args.out, args.per_kind, args.seed) if args.command == "sample" else summarize(args.reviews, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
