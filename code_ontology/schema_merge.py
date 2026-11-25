"""Typed merge protocol and deterministic provenance; names are never identity keys."""
from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from typing import Literal

from pydantic import Field

from .models import Ontology, StrictModel, supported_ids
from .util import canonical, digest

CATEGORIES = ("classes", "relations", "properties")
MERGE_FORMAT = 3


class SourceMapping(StrictModel):
    source_id: str = Field(min_length=1)
    target_id: str | None
    action: Literal["preserve", "merge", "unresolved"]
    reason: str = Field(min_length=1)


class MergeResponse(StrictModel):
    ontology: Ontology
    source_mapping: list[SourceMapping]


def retain_missing_sources(result, payload):
    """An omitted mapping stays explicit and pending, never guessed from a name."""
    present = {m.source_id for m in result.source_mapping}
    for candidate in payload["candidates"]:
        for category in CATEGORIES:
            for row in candidate[category]:
                if row["id"] not in present:
                    result.source_mapping.append(SourceMapping(source_id=row["id"], target_id=None,
                        action="unresolved", reason="Model omitted this source mapping; retained for independent AI review"))
    return result


def merge_payload(schemas, profile):
    """Namespace every input ID, including references, without exposing lineage to the model."""
    candidates, sources = [], {}
    for i, schema in enumerate(schemas):
        data = Ontology.model_validate(schema).model_dump()
        scope = digest(data, 24)
        remap = {row["id"]: "s_" + digest([scope, category, row["id"]], 24)
                 for category in CATEGORIES for row in data[category]}
        for category in CATEGORIES:
            for row in data[category]:
                old_id = row["id"]
                row["id"] = remap[old_id]
                for field in ("parents", "domain", "range"):
                    if field in row:
                        row[field] = [remap[x] for x in row[field]]
                if row["id"] in sources:
                    raise ValueError("Duplicate merge candidates; coalesce lineage before batching")
                sources[row["id"]] = {"candidate": i, "category": category, "item_id": old_id, "row": row}
        candidates.append(data)
    return {"stage": "merge", "profile": profile, "candidates": candidates}, sources


def validate_merge(result: MergeResponse, payload, *, compatibility=True):
    sources = {r["id"]: (cat, r) for s in payload["candidates"] for cat in CATEGORIES for r in s[cat]}
    targets = {r.id: (cat, r) for cat in CATEGORIES for r in getattr(result.ontology, cat)}
    mappings = {m.source_id: m for m in result.source_mapping}
    if len(mappings) != len(result.source_mapping):
        raise ValueError("Duplicate source_mapping source_id")
    missing, unknown = sources.keys() - mappings.keys(), mappings.keys() - sources.keys()
    if missing or unknown:
        raise ValueError(f"source_mapping must cover every input ID exactly once; missing={sorted(missing)[:12]}, unknown={sorted(unknown)[:12]}")
    allowed = {e for _, row in sources.values() for e in row["evidence_ids"]}
    if supported_ids(result.ontology) - allowed:
        raise ValueError("Merged ontology has unknown evidence IDs")
    used = set()
    for sid, mapping in mappings.items():
        if mapping.action == "unresolved":
            if mapping.target_id is not None:
                raise ValueError("Unresolved mapping must have null target_id")
            continue
        if mapping.target_id not in targets or targets[mapping.target_id][0] != sources[sid][0]:
            raise ValueError(f"Mapping {sid} must target an existing item of the same category")
        used.add(mapping.target_id)
    if targets.keys() - used:
        raise ValueError("Every output item needs at least one mapped input source")
    if compatibility:
        findings = alignment_findings(result, payload)
        if findings:
            raise ValueError("Source alignment issues: " + canonical(findings[:6]) +
                             ". Preserve incompatible meanings separately; integer may widen to number, never narrow.")


def alignment_findings(result, payload):
    sources = {r["id"]: (cat, r) for s in payload["candidates"] for cat in CATEGORIES for r in s[cat]}
    targets = {r.id: (cat, r) for cat in CATEGORIES for r in getattr(result.ontology, cat)}
    mappings = {m.source_id: m for m in result.source_mapping}
    findings = []
    def issue(sid, reason):
        findings.append({"source_id": sid, "reason": reason})

    classes = {r.id: r for r in result.ontology.classes}
    def ancestors(cid):
        found, pending = {cid}, [cid]
        while pending:
            for parent in classes[pending.pop()].parents:
                if parent not in found:
                    found.add(parent); pending.append(parent)
        return found

    def mapped_refs(refs, target_refs, sid, field):
        for ref in refs:
            target = mappings[ref].target_id
            if target is None or not ancestors(target).intersection(target_refs):
                issue(sid, f"changes or loses {field}: source={ref}, mapped={target}, target references={target_refs}")

    for sid, mapping in mappings.items():
        if mapping.target_id is None:
            issue(sid, "unresolved: " + mapping.reason)
            continue
        category, row = sources[sid]
        target = targets[mapping.target_id][1]
        if category == "classes":
            for parent in row["parents"]:
                mapped = mappings[parent].target_id
                if mapped is None or mapped not in ancestors(target.id):
                    issue(sid, "loses its parent class")
        else:
            if (row.get("min_count"), row.get("max_count")) != (target.min_count, target.max_count):
                issue(sid, f"changes explicit cardinality: input={row.get('min_count')}/{row.get('max_count')}; "
                           f"target={target.min_count}/{target.max_count}")
            mapped_refs(row["domain"], target.domain, sid, "domain")
            if category == "relations":
                mapped_refs(row["range"], target.range, sid, "range/direction")
            else:
                # Widening integer to number preserves all source values; narrowing does not.
                compatible = target.datatype == row["datatype"] or (row["datatype"] == "integer" and target.datatype == "number")
                if not compatible or (row.get("unit") and row["unit"] != target.unit):
                    issue(sid, f"property narrows/changes datatype or unit: input={row['datatype']}/{row.get('unit')}; "
                               f"target={target.datatype}/{target.unit}")
    return findings


def retain_source_metadata(result, payload):
    """Keep mapped source provenance even when a model omits it from its response.

    These anchors record where a hypothesis came from, not semantic verification.
    Validation must run before this function so unknown IDs cannot be laundered.
    """
    targets = {r.id: r for cat in CATEGORIES for r in getattr(result.ontology, cat)}
    sources = {r["id"]: r for s in payload["candidates"] for cat in CATEGORIES for r in s[cat]}
    for mapping in result.source_mapping:
        if mapping.target_id is not None:
            source, target = sources[mapping.source_id], targets[mapping.target_id]
            target.aliases = sorted(set(target.aliases + source.get("aliases", []) + [source["name"]]))
            target.evidence_ids = sorted(set(target.evidence_ids + source["evidence_ids"]))
    return result


def canonical_merge(result, payload, profile, *, compatibility=True):
    """Preserve aliases/evidence mechanically, then update mapping to canonical output IDs."""
    from .pipeline import canonical_schema
    validate_merge(result, payload, compatibility=compatibility)
    result = retain_source_metadata(result.model_copy(deep=True), payload)
    # Preserve all upstream explicit uncertainty; provenance completeness is not semantic certainty.
    result.ontology.unresolved = sorted(set(result.ontology.unresolved +
        [u for s in payload["candidates"] for u in s.get("unresolved", [])]))
    schema, remap = canonical_schema(result.ontology, profile, return_id_map=True)
    for mapping in result.source_mapping:
        if mapping.target_id is not None:
            mapping.target_id = remap[mapping.target_id]
    result.ontology = schema
    validate_merge(result, payload, compatibility=compatibility)
    return result


def initial_candidates(schemas):
    registry, candidates = {}, []
    for raw in schemas:
        schema = Ontology.model_validate(raw).model_dump()
        scope = digest(schema, 24)
        lineage = {}
        for category in CATEGORIES:
            for row in schema[category]:
                sid = "s_" + digest([scope, category, row["id"]], 24)
                registry[sid] = {"source_id": sid, "category": category, "source_schema": scope, "source": deepcopy(row)}
                lineage[row["id"]] = [sid]
        candidates.append({"schema": schema, "lineage": lineage, "pending": []})
    return coalesce(candidates), registry


def coalesce(candidates):
    """Deduplicate identical schemas without dropping any original source lineage."""
    groups = {}
    for candidate in candidates:
        key = canonical(candidate["schema"])
        if key not in groups:
            groups[key] = deepcopy(candidate)
        else:
            target = groups[key]
            for item_id, roots in candidate["lineage"].items():
                target["lineage"][item_id] = sorted(set(target["lineage"].get(item_id, []) + roots))
            target["pending"] += candidate["pending"]
    return [groups[k] for k in sorted(groups)]


def compose(batch, response, sources):
    lineage, pending = {}, [p for c in batch for p in c["pending"]]
    for mapping in response.source_mapping:
        source = sources[mapping.source_id]
        roots = batch[source["candidate"]]["lineage"][source["item_id"]]
        if mapping.target_id is None:
            pending += [{"source_id": root, "reason": mapping.reason} for root in roots]
        else:
            lineage.setdefault(mapping.target_id, []).extend(roots)
    return {"schema": response.ontology.model_dump(),
            "lineage": {k: sorted(set(v)) for k, v in lineage.items()}, "pending": pending}


def final_mapping(candidate, registry):
    targets = {r["id"]: r for cat in CATEGORIES for r in candidate["schema"][cat]}
    resolved = {}
    for target_id, roots in candidate["lineage"].items():
        for root in roots:
            if root in resolved and resolved[root]["target_id"] != target_id:
                raise ValueError("Conflicting cross-round source mapping")
            resolved[root] = {"target_id": target_id, "target_name": targets[target_id]["name"], "state": "mapped"}
    for row in candidate["pending"]:
        if row["source_id"] in resolved:
            raise ValueError("Source is both mapped and unresolved")
        resolved[row["source_id"]] = {"target_id": None, "state": "unresolved", "reason": row["reason"]}
    if resolved.keys() != registry.keys():
        raise ValueError("Cross-round provenance lost or invented original sources")
    return [{**registry[sid], **resolved[sid]} for sid in sorted(registry)]


def semantic_signatures(schema):
    """Create ID-independent structural signatures for conservative registry lookup."""
    data = Ontology.model_validate(schema).model_dump()
    class_keys = {row["id"]: digest([row["name"].casefold(), row["definition"].strip(), sorted(a.casefold() for a in row["aliases"])], 24)
                  for row in data["classes"]}
    result = {}
    for category in CATEGORIES:
        for row in data[category]:
            value = {k: v for k, v in row.items() if k not in {"id", "evidence_ids"}}
            for field in ("parents", "domain", "range"):
                if field in value:
                    value[field] = sorted(class_keys.get(x, "missing:" + x) for x in value[field])
            result[(category, row["id"])] = digest([category, value], 40)
    return result


def registry_candidates(schemas, previous=None):
    """Retrieve exact and lexical candidates without deciding semantic equivalence."""
    previous = previous or {"entries": []}
    exact = defaultdict(list)
    lexical = defaultdict(list)
    for entry in previous.get("entries", []):
        exact[(entry["category"], entry["semantic_signature"])].append(entry["registry_id"])
        for name in entry.get("names", []):
            lexical[(entry["category"], name.casefold())].append(entry["registry_id"])
    rows = []
    for schema_index, schema in enumerate(schemas):
        data = Ontology.model_validate(schema).model_dump()
        signatures = semantic_signatures(data)
        for category in CATEGORIES:
            for row in data[category]:
                signature = signatures[(category, row["id"])]
                names = [row["name"], *row.get("aliases", [])]
                exact_ids = sorted(set(exact[(category, signature)]))
                lexical_ids = sorted({rid for name in names for rid in lexical[(category, name.casefold())]})
                rows.append({"schema_index": schema_index, "category": category, "source_id": row["id"],
                             "semantic_signature": signature, "exact_registry_ids": exact_ids,
                             "candidate_registry_ids": sorted(set(exact_ids + lexical_ids)),
                             "decision": "exact_reuse_candidate" if exact_ids else "model_or_unresolved"})
    return {"format": MERGE_FORMAT, "entries": rows,
            "exact_candidates": sum(bool(row["exact_registry_ids"]) for row in rows),
            "note": "Names only retrieve candidates; they never prove identity. Exact signatures may be reused only with current-source mapping and validation."}


def build_concept_registry(schema, source_mapping, snapshot_id, previous=None):
    """Materialize stable registry IDs while retaining the current schema and lineage."""
    data = Ontology.model_validate(schema).model_dump()
    signatures = semantic_signatures(data)
    previous_by_key = {(e["category"], e["semantic_signature"]): e for e in (previous or {}).get("entries", [])}
    roots = defaultdict(list)
    for row in source_mapping:
        if row.get("target_id"):
            roots[row["target_id"]].append(row["source_id"])
    entries = []
    for category in CATEGORIES:
        for row in data[category]:
            signature = signatures[(category, row["id"])]
            old = previous_by_key.get((category, signature), {})
            registry_id = old.get("registry_id") or "reg_" + digest([category, signature], 32)
            entries.append({"registry_id": registry_id, "category": category, "semantic_signature": signature,
                            "current_id": row["id"], "names": sorted(set([row["name"], *row.get("aliases", [])])),
                            "evidence_ids": sorted(row["evidence_ids"]), "source_ids": sorted(roots[row["id"]]),
                            "first_snapshot": old.get("first_snapshot", snapshot_id), "latest_snapshot": snapshot_id})
    return {"format": MERGE_FORMAT, "snapshot_id": snapshot_id, "schema_hash": digest(data),
            "entries": sorted(entries, key=lambda e: (e["category"], e["registry_id"]))}


def split_candidate_components(candidate, max_items):
    """Split only disconnected schema components; never duplicate dependency anchors."""
    schema = Ontology.model_validate(candidate["schema"]).model_dump()
    total = sum(len(schema[c]) for c in CATEGORIES)
    if total <= max_items:
        return [candidate]
    adjacency = defaultdict(set)
    category_by_id = {}
    rows_by_id = {}
    for category in CATEGORIES:
        for row in schema[category]:
            category_by_id[row["id"]] = category
            rows_by_id[row["id"]] = row
            adjacency[row["id"]]
    for row in schema["classes"]:
        for ref in row["parents"]:
            adjacency[row["id"]].add(ref); adjacency[ref].add(row["id"])
    for category in ("relations", "properties"):
        for row in schema[category]:
            for ref in row["domain"] + (row["range"] if category == "relations" else []):
                adjacency[row["id"]].add(ref); adjacency[ref].add(row["id"])
    components, remaining = [], set(rows_by_id)
    while remaining:
        pending = [min(remaining)]; found = set()
        while pending:
            item = pending.pop()
            if item in found:
                continue
            found.add(item); pending.extend(adjacency[item] - found)
        remaining -= found; components.append(found)
    if len(components) == 1:
        return [candidate]
    result = []
    for number, ids in enumerate(sorted(components, key=lambda c: min(c))):
        part = {**schema, **{category: [row for row in schema[category] if row["id"] in ids] for category in CATEGORIES}}
        Ontology.model_validate(part)
        result.append({"schema": part, "lineage": {item_id: roots for item_id, roots in candidate["lineage"].items() if item_id in ids},
                       "pending": list(candidate["pending"]) if number == 0 else []})
    return result
