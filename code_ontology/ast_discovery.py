"""AST-01: request-local, reversible sharing of overlapping source evidence."""
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import PurePosixPath

from .discovery import payload as budget_payload
from .util import canonical, digest

FORMAT = 1
GUIDANCE = (
    "AST-01: packs are evidence contexts, not separate ontologies. Resolve shared_ref from shared. "
    "For code with segments, concatenate its strings and resolved shared_ref strings in order, without separators. "
    "Repeated views of the same source location are one evidence occurrence; emit a supported concept, relation or property "
    "once per meaning in this response and union all applicable original evidence_ids. Inspect every pack's scope, "
    "signature and graph context before identifying meanings. Same text or name alone does not prove equivalence. "
    "Keep distinct roles, conditions, units and meanings separate; report uncertain matches in unresolved. "
    "Shared IDs are transport references only, never evidence IDs. Only original evidence_ids may be cited."
)


def source_order(pack):
    ref = pack["source_refs"][0]
    file = ref["file"].replace("\\", "/")
    return (str(PurePosixPath(file).parent), file, ref.get("start_line", 0), -ref.get("end_line", 0), pack["pack_id"])


def _source_row(pack):
    """Fragments and ambiguous ranges keep their full text rather than guessing offsets."""
    refs = pack.get("source_refs", [])
    code = pack.get("code", "")
    if len(refs) != 1 or not isinstance(code, str) or pack.get("fragment_of"):
        return None
    ref = refs[0]
    start, end = ref.get("start_line"), ref.get("end_line")
    if (not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start
            or not ref.get("source_sha256") or ref.get("snippet_hash") != digest(code, 64)):
        return None
    lines = code.splitlines(keepends=True)
    if len(lines) != end - start + 1:
        return None
    return (ref["file"], ref["source_sha256"]), start, end + 1, lines


def payload(packs, profile):
    # Keep the existing exact code/node sharing. Overlap sharing is selected only
    # when it improves on that representation, including all reference overhead.
    baseline = deepcopy(budget_payload(packs, profile, True))
    candidate = deepcopy(baseline)
    groups = defaultdict(list)
    for index, pack in enumerate(packs):
        row = _source_row(pack)
        if row is not None:
            identity, start, stop, lines = row
            groups[identity].append((index, start, stop, lines))
    pool, parts = {}, {}
    for identity, rows in groups.items():
        boundaries = sorted({point for _, start, stop, _ in rows for point in (start, stop)})
        for index, start, stop, lines in rows:
            cuts = [point for point in boundaries if start <= point <= stop]
            pieces = []
            for a, b in zip(cuts, cuts[1:]):
                text = "".join(lines[a - start:b - start])
                key = "span_" + digest([identity, a, b, text], 40)
                pool[key] = text
                pieces.append(key)
            parts[index] = pieces
    counts = Counter(key for pieces in parts.values() for key in pieces)
    shared = candidate.setdefault("shared", {})
    for index, pieces in parts.items():
        if not any(counts[key] > 1 for key in pieces):
            continue
        segments = []
        for key in pieces:
            if counts[key] > 1:
                shared[key] = pool[key]
                segments.append({"shared_ref": key})
            elif segments and isinstance(segments[-1], str):
                segments[-1] += pool[key]
            else:
                segments.append(pool[key])
        candidate["packs"][index]["code"] = {"segments": segments}
    # Remove exact-code entries superseded by segment sharing, retaining shared nodes.
    used = set()
    for pack in candidate["packs"]:
        code = pack.get("code")
        if isinstance(code, dict):
            if "shared_ref" in code:
                used.add(code["shared_ref"])
            for segment in code.get("segments", []):
                if isinstance(segment, dict):
                    used.add(segment["shared_ref"])
        for node in pack.get("context", {}).get("nodes", []):
            if "shared_ref" in node:
                used.add(node["shared_ref"])
    candidate["shared"] = {key: value for key, value in shared.items() if key in used}
    if not candidate["shared"]:
        candidate.pop("shared")
    for result in (baseline, candidate):
        result["input_format"] = GUIDANCE
        result["ast_01_format"] = FORMAT
    result = candidate if len(canonical(candidate)) < len(canonical(baseline)) else baseline
    # Capabilities and warnings are often larger than source text. Share only
    # exact repeated values and only when their dictionary entry pays for itself.
    metadata = defaultdict(list)
    for pack in result["packs"]:
        for owner, field in ((pack, "parse_warnings"),
                             (pack.get("evidence_layers", {}), "capabilities"),
                             (pack.get("context", {}), "edges"),
                             (pack.get("context", {}), "neighbors")):
            if field in owner:
                value = owner[field]
                key = "meta_" + digest(value, 40)
                metadata[key].append((owner, field, value))
    for key, occurrences in metadata.items():
        if len(occurrences) < 2:
            continue
        value = occurrences[0][2]
        reference = {"shared_ref": key}
        if len(canonical({key: value})) + len(occurrences) * len(canonical(reference)) >= len(occurrences) * len(canonical(value)):
            continue
        result.setdefault("shared", {})[key] = value
        for owner, field, _ in occurrences:
            owner[field] = reference
    return result


def expand(value, shared):
    """Reconstruct every original pack field for offline auditing."""
    if isinstance(value, list):
        return [expand(item, shared) for item in value]
    if isinstance(value, dict):
        if set(value) == {"shared_ref"}:
            return expand(shared[value["shared_ref"]], shared)
        if set(value) == {"segments"}:
            return "".join(expand(piece, shared) for piece in value["segments"])
        return {key: expand(item, shared) for key, item in value.items()}
    return value


def batches(packs, size, system, llm, profile):
    """Keep related ranges adjacent; never drop a pack or exceed a batch budget."""
    groups, current, module = [], [], None
    for pack in sorted(packs, key=source_order):
        next_module = source_order(pack)[0]
        proposed = current + [pack]
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": canonical(payload(proposed, profile))}]
        if current and (module != next_module or len(proposed) > size or not llm.fits(messages)):
            groups.append(current)
            current = []
        current.append(pack)
        module = next_module
    if current:
        groups.append(current)
    return groups


def input_report(groups, profile):
    rows = []
    for group in groups:
        encoded = payload(group, profile)
        original = budget_payload(group, profile)
        rows.append({"pack_ids": [p["pack_id"] for p in group],
                     "source_bytes": sum(len(p.get("code", "").encode("utf-8")) for p in group),
                     "original_json_bytes": len(canonical(original).encode("utf-8")),
                     "request_json_bytes": len(canonical(encoded).encode("utf-8")),
                     "shared_entries": len(encoded.get("shared", {})),
                     "segmented_packs": sum(isinstance(p.get("code"), dict) and "segments" in p["code"]
                                            for p in encoded["packs"])})
    return {"strategy": "ast-01", "format": FORMAT, "batches": rows,
            "selected_packs": sum(len(row["pack_ids"]) for row in rows),
            "original_json_bytes": sum(row["original_json_bytes"] for row in rows),
            "request_json_bytes": sum(row["request_json_bytes"] for row in rows),
            "note": "Initial planned inputs only. Bytes are not model tokens or semantic quality; recovery requests may differ."}
