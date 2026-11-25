"""Lossless batch-local sharing and deterministic request-budget packing."""
from copy import deepcopy
from pathlib import Path

from .util import canonical, digest


def payload(packs, profile, compact=False):
    result = {"stage": "discover", "profile": profile, "packs": packs}
    if not compact:
        return result
    rows = deepcopy(packs)
    pool, counts = {}, {}
    for pack in rows:
        values = [pack.get("code", ""), *pack.get("context", {}).get("nodes", [])]
        for value in values:
            key = digest(value, 40)
            counts[key] = counts.get(key, 0) + 1
            pool[key] = value
    shared = {key: value for key, value in pool.items() if counts[key] > 1}
    if not shared:
        return result
    for pack in rows:
        key = digest(pack.get("code", ""), 40)
        if key in shared:
            pack["code"] = {"shared_ref": key}
        context = pack.get("context", {})
        if "nodes" in context:
            context["nodes"] = [{"shared_ref": digest(node, 40)} if digest(node, 40) in shared else node
                                for node in context["nodes"]]
    candidate = {**result, "packs": rows, "shared": shared,
        "input_format": "Resolve every {shared_ref: ID} from shared before reading code or graph nodes. "
                        "All original pack IDs, source ranges, evidence and graph edges remain authoritative."}
    return candidate if len(canonical(candidate)) < len(canonical(result)) else result


def batches(packs, size, system, llm, profile, compact):
    groups = []
    current = []
    module = None
    for pack in sorted(packs, key=lambda p: (str(Path(p["source_refs"][0]["file"]).parent), p["pack_id"])):
        parent = str(Path(pack["source_refs"][0]["file"]).parent)
        proposed = current + [pack]
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": canonical(payload(proposed, profile, compact))}]
        if current and (parent != module or len(proposed) > size or compact and not llm.fits(messages)):
            groups.append(current)
            current = []
        current.append(pack)
        module = parent
    if current:
        groups.append(current)
    return groups
