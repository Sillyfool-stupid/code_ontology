"""Compare AST input plans offline; never creates an LLM client or edits a snapshot."""
import argparse
from pathlib import Path

from code_ontology import ast_discovery
from code_ontology.discovery import batches, payload
from code_ontology.util import canonical, iter_jsonl, token_bound, write_json


class Budget:
    def __init__(self, context_tokens, output_tokens):
        self.input_limit = context_tokens - output_tokens - 512
        if self.input_limit <= 0:
            raise ValueError("context-tokens must exceed max-output-tokens plus 512 reserved tokens")

    def fits(self, messages):
        return token_bound(messages) <= self.input_limit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Independent output JSON outside the snapshot")
    parser.add_argument("--batch-packs", type=int, default=8)
    parser.add_argument("--context-tokens", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--profile", default="general", choices=["general", "maintenance"])
    args = parser.parse_args()
    if args.batch_packs < 1:
        parser.error("batch-packs must be positive")
    try:
        args.out.resolve().relative_to(args.snapshot.resolve())
    except ValueError:
        pass
    else:
        parser.error("out must be outside the snapshot")
    packs = list(iter_jsonl(args.snapshot / "prepared/ast/packs.jsonl"))
    budget = Budget(args.context_tokens, args.max_output_tokens)
    # The same system prompt is included in both planners' budget checks.
    import yaml
    from code_ontology.config import RESOURCE
    from code_ontology.pipeline import prompt
    system = prompt(yaml.safe_load((RESOURCE / "default.yaml").read_text(encoding="utf-8")), args.profile, "discover")
    original = batches(packs, args.batch_packs, system, budget, args.profile, True)
    improved = ast_discovery.batches(packs, args.batch_packs, system, budget, args.profile)
    originals = {p["pack_id"]: p for p in packs}
    covered = []
    for group in improved:
        encoded = ast_discovery.payload(group, args.profile)
        for row in encoded["packs"]:
            decoded = ast_discovery.expand(row, encoded.get("shared", {}))
            if decoded != originals[row["pack_id"]]:
                raise ValueError(f"Round-trip mismatch: {row['pack_id']}")
            covered.append(row["pack_id"])
    if sorted(covered) != sorted(p["pack_id"] for p in packs):
        raise ValueError("Pack coverage mismatch")
    old_bytes = sum(len(canonical(payload(g, args.profile, True)).encode("utf-8")) for g in original)
    report = ast_discovery.input_report(improved, args.profile)
    new_bytes = report["request_json_bytes"]
    report.update(snapshot=str(args.snapshot.resolve()), batch_limit=args.batch_packs,
                  context_tokens=args.context_tokens, max_output_tokens=args.max_output_tokens,
                  budget_baseline={"batches": len(original), "json_bytes": old_bytes},
                  ast_01_batches=len(improved), round_trip="all original pack fields restored exactly",
                  byte_reduction_vs_budget=round(1 - new_bytes / old_bytes, 6) if old_bytes else None,
                  real_model_requests=0)
    write_json(args.out, report)
    print(canonical({k: v for k, v in report.items() if k != "batches"}))


if __name__ == "__main__":
    main()
