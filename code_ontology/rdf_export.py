"""Deterministic RDF delivery from finalized JSON; never invokes a model."""
from __future__ import annotations

from pathlib import Path
from decimal import Decimal
from urllib.parse import quote, urlsplit

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

from .models import Extraction, Ontology
from .util import canonical, digest, file_hash, iter_jsonl, now, read_json, write_json, write_text

FORMAT = "rdf-1"
BASE = "urn:code-ontology:"
CO = Namespace(BASE + "vocab:")
PROV = Namespace("http://www.w3.org/ns/prov#")
KINDS = ("entities", "relations", "attributes", "rules", "candidates")
FILES = ("ontology.ttl", "shapes.ttl", "facts.ttl", "assertions.ttl", "rules.ttl",
         "review.ttl", "facts.nt", "validation-report.ttl")


def input_paths(run: Path, snapshot: Path | None = None):
    paths = [run / "final/schema.json", run / "metrics.json"]
    paths += [run / "final" / f"{kind}.jsonl" for kind in (*KINDS, "conflicts")]
    paths += [run / name for name in ("schema/schema.json", "schema-scope.json", "extraction/manifest.json")]
    if snapshot:
        paths += [snapshot / "manifest.json"]
        metrics = read_json(run / "metrics.json") if (run / "metrics.json").is_file() else {}
        paths += [snapshot / "prepared" / metrics.get("view", "cpg") / "packs.jsonl"]
    return paths


def input_fingerprint(run: Path, snapshot: Path | None = None):
    return digest({"format": FORMAT, "implementation": file_hash(Path(__file__)),
                   "inputs": [file_hash(p) if p.is_file() else None for p in input_paths(run, snapshot)]})


def export_rdf(run: Path, snapshot: Path | None = None, base_iri: str = BASE):
    """Publish files atomically, with the completion manifest written last.

    SHACL validates unconditional facts. Qualified claims are preserved separately;
    neither conditions nor natural-language rules become executable OWL axioms.
    """
    run = Path(run).resolve()
    snapshot = Path(snapshot).resolve() if snapshot else None
    if snapshot is None and len(run.parents) > 3 and (run.parents[3] / "manifest.json").is_file():
        snapshot = run.parents[3]
    if not urlsplit(base_iri).scheme or any(c.isspace() or c in '<>"{}|\\^`' for c in base_iri):
        raise ValueError("base-iri must be an absolute IRI without whitespace or forbidden characters")
    base_iri = base_iri if base_iri.endswith(("/", "#", ":")) else base_iri + "/"
    required = [run / "final/schema.json", run / "metrics.json"]
    required += [run / "final" / f"{kind}.jsonl" for kind in KINDS]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise ValueError("Run finalize first; missing: " + ", ".join(missing))
    out = run / "rdf"
    manifest = {"format": FORMAT, "state": "running", "created_at": now(), "human_verified": False}
    write_json(out / "export-manifest.json", manifest)
    try:
        key = input_fingerprint(run, snapshot)
        schema = Ontology.model_validate(read_json(run / "final/schema.json"))
        metrics = read_json(run / "metrics.json")
        current = run / "schema/schema.json"
        if current.is_file() and Ontology.model_validate(read_json(current)) != schema:
            raise ValueError("Fixed schema changed since finalize; rerun extract/finalize first")
        rows = {kind: list(iter_jsonl(run / "final" / f"{kind}.jsonl")) for kind in KINDS}
        result = Extraction.model_validate(rows)
        conflicts_path = run / "final/conflicts.jsonl"
        conflicts = list(iter_jsonl(conflicts_path)) if conflicts_path.is_file() else []
        snap_manifest = read_json(snapshot / "manifest.json") if snapshot and (snapshot / "manifest.json").is_file() else {}
        identity = {"snapshot": snap_manifest.get("snapshot_id"),
                    "experiment": run.relative_to(snapshot).as_posix() if snapshot and run.is_relative_to(snapshot) else run.name,
                    "view": metrics.get("view"), "profile": metrics.get("profile")}
        schema_ns = Namespace(base_iri + "schema/" + digest(schema.model_dump()) + "/")
        data_ns = Namespace(base_iri + "data/" + digest(identity) + "/")

        def term(identifier):
            return schema_ns["term/" + quote(identifier, safe="")]

        def entity(identifier):
            return data_ns["entity/" + quote(identifier, safe="")]

        def graph():
            g = Graph()
            for prefix, ns in (("co", CO), ("biz", schema_ns), ("inst", data_ns), ("prov", PROV),
                               ("owl", OWL), ("sh", SH), ("xsd", XSD)):
                g.bind(prefix, ns)
            return g

        ontology, shapes, facts, assertions, rules, review = [graph() for _ in range(6)]
        activity = data_ns["export/" + key]
        assertions.add((activity, RDF.type, PROV.Activity))
        assertions.add((activity, CO.humanVerified, Literal(False)))
        assertions.add((activity, CO.sourceStatus, Literal(metrics.get("status", "unknown"))))
        assertions.add((activity, CO.sourceMetadata, Literal(canonical({"identity": identity, "schema_scope": metrics.get("schema_scope")}))))
        evidence = {}
        packs = snapshot / "prepared" / metrics.get("view", "cpg") / "packs.jsonl" if snapshot else None
        if packs and packs.is_file():
            for pack in iter_jsonl(packs):
                for ref in pack.get("source_refs", []):
                    evidence.setdefault(ref["evidence_id"], []).append(ref)
        missing_evidence = set()

        def provenance(g, node, row):
            g.add((node, PROV.wasGeneratedBy, activity))
            g.add((node, CO.sourceRecord, Literal(canonical(row))))
            for field in ("basis", "scope", "condition", "unit", "effective_time", "valid_from", "valid_to"):
                if row.get(field) is not None:
                    g.add((node, CO[field], Literal(row[field])))
            for eid in row.get("evidence_ids", []):
                ev = data_ns["evidence/" + quote(eid, safe="")]
                g.add((node, PROV.wasDerivedFrom, ev))
                assertions.add((ev, RDF.type, PROV.Entity))
                assertions.add((ev, CO.sourceId, Literal(eid)))
                if eid not in evidence:
                    missing_evidence.add(eid)
                for ref in evidence.get(eid, []):
                    loc = data_ns["location/" + digest(ref)]
                    assertions.add((ev, CO.sourceLocation, loc))
                    for name, value in ref.items():
                        assertions.add((loc, CO[name], Literal(value)))

        def union_class(g, ids):
            if len(ids) == 1:
                return term(ids[0])
            node, head = BNode(), BNode()
            g.add((node, RDF.type, OWL.Class))
            g.add((node, OWL.unionOf, head))
            Collection(g, head, [term(cid) for cid in ids])
            return node

        ontology.add((schema_ns["ontology"], RDF.type, OWL.Ontology))
        for items, kind in ((schema.classes, OWL.Class), (schema.relations, OWL.ObjectProperty),
                            (schema.properties, OWL.DatatypeProperty)):
            for item in items:
                node = term(item.id)
                ontology.add((node, RDF.type, kind))
                ontology.add((node, RDFS.label, Literal(item.name)))
                ontology.add((node, RDFS.comment, Literal(item.definition)))
                for alias in item.aliases:
                    ontology.add((node, CO.alias, Literal(alias)))
                provenance(ontology, node, item.model_dump())
                if kind == OWL.Class:
                    for parent in item.parents:
                        ontology.add((node, RDFS.subClassOf, term(parent)))
                else:
                    ontology.add((node, RDFS.domain, union_class(ontology, item.domain)))
                    if kind == OWL.ObjectProperty:
                        ontology.add((node, RDFS.range, union_class(ontology, item.range)))
                    else:
                        datatype = {"string": XSD.string, "integer": XSD.integer,
                                    "boolean": XSD.boolean, "number": XSD.decimal}[item.datatype]
                        ontology.add((node, RDFS.range, datatype))

        def class_constraint(g, node, ids):
            if len(ids) == 1:
                g.add((node, SH["class"], term(ids[0])))
            else:
                alternatives = []
                for cid in ids:
                    alt = BNode(); g.add((alt, SH["class"], term(cid))); alternatives.append(alt)
                head = BNode(); Collection(g, head, alternatives); g.add((node, SH["or"], head))

        # A global subject shape checks disjunctive domain; per-class shapes add counts.
        for spec in [*schema.relations, *schema.properties]:
            predicate = term(spec.id)
            domain_shape = schema_ns["shape/domain/" + quote(spec.id, safe="")]
            shapes.add((domain_shape, RDF.type, SH.NodeShape))
            shapes.add((domain_shape, SH.targetSubjectsOf, predicate))
            class_constraint(shapes, domain_shape, spec.domain)
            for cid in spec.domain:
                target = schema_ns["shape/" + quote(cid, safe="")]
                prop = BNode()
                shapes.add((target, RDF.type, SH.NodeShape))
                shapes.add((target, SH.targetClass, term(cid)))
                shapes.add((target, SH.property, prop))
                shapes.add((prop, SH.path, predicate))
                for field, constraint in (("min_count", SH.minCount), ("max_count", SH.maxCount)):
                    value = getattr(spec, field)
                    if value is not None:
                        shapes.add((prop, constraint, Literal(value)))
                if hasattr(spec, "range"):
                    shapes.add((prop, SH.nodeKind, SH.IRI))
                    class_constraint(shapes, prop, spec.range)
                else:
                    datatype = {"string": XSD.string, "integer": XSD.integer,
                                "boolean": XSD.boolean, "number": XSD.decimal}[spec.datatype]
                    if spec.datatype == "number":
                        alternatives = []
                        for dt in (XSD.integer, XSD.decimal):
                            alt = BNode(); shapes.add((alt, SH.datatype, dt)); alternatives.append(alt)
                        head = BNode(); Collection(shapes, head, alternatives); shapes.add((prop, SH["or"], head))
                    else:
                        shapes.add((prop, SH.datatype, datatype))

        # Finalize preserves assertion variants (basis, evidence and name casing)
        # for a single entity identity. Keep every assertion below, while using
        # this index only to validate endpoint existence and identity consistency.
        entities = {}
        for item in result.entities:
            previous = entities.setdefault(item.id, item)
            if (previous.name.casefold(), previous.type_id, previous.scope) != (
                    item.name.casefold(), item.type_id, item.scope):
                raise ValueError("Conflicting final entity identity: " + item.id)
        classes = {c.id for c in schema.classes}
        relations = {r.id: r for r in schema.relations}
        properties = {p.id: p for p in schema.properties}

        def statement(row, triple, qualified=False):
            node = data_ns["assertion/" + digest(row)]
            assertions.add((node, RDF.type, RDF.Statement))
            for predicate, value in zip((RDF.subject, RDF.predicate, RDF.object), triple):
                assertions.add((node, predicate, value))
            assertions.add((node, CO.assertedDirectly, Literal(not qualified)))
            provenance(assertions, node, row)
            if not qualified:
                facts.add(triple)

        for item in result.entities:
            if item.type_id not in classes:
                raise ValueError("Unknown entity class: " + item.type_id)
            statement(item.model_dump(), (entity(item.id), RDF.type, term(item.type_id)))
            facts.add((entity(item.id), RDFS.label, Literal(item.name)))
        for item in result.relations:
            if item.subject not in entities or item.object not in entities or item.relation_id not in relations:
                raise ValueError("Unknown relation or endpoint")
            statement(item.model_dump(), (entity(item.subject), term(item.relation_id), entity(item.object)))
        for item in result.attributes:
            if item.subject not in entities or item.property_id not in properties:
                raise ValueError("Unknown property or endpoint")
            spec = properties[item.property_id]
            expected = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}[spec.datatype]
            if type(item.value) not in expected:
                raise ValueError("Property datatype mismatch: " + item.property_id)
            if isinstance(item.value, float):
                import math
                if not math.isfinite(item.value):
                    raise ValueError("Non-finite numeric attribute")
            dt = {str: XSD.string, bool: XSD.boolean, int: XSD.integer, float: XSD.decimal}[type(item.value)]
            qualified = any(getattr(item, name) is not None for name in ("condition", "effective_time", "valid_from", "valid_to"))
            lexical = format(Decimal(str(item.value)), "f") if isinstance(item.value, float) else item.value
            statement(item.model_dump(), (entity(item.subject), term(item.property_id), Literal(lexical, datatype=dt)), qualified)
        for item in result.rules:
            row = item.model_dump(); node = data_ns["rule/" + digest(row)]
            rules.add((node, RDF.type, CO.RuleDescription))
            rules.add((node, RDFS.label, Literal(item.name)))
            rules.add((node, CO.action, Literal(item.action)))
            provenance(rules, node, row)
        for category, records in (("candidate", rows["candidates"]), ("conflict", conflicts), ("unresolved", schema.unresolved)):
            for row in records:
                node = data_ns[category + "/" + digest(row)]
                review.add((node, RDF.type, CO[category.title()]))
                review.add((node, CO.sourceRecord, Literal(canonical(row))))

        from pyshacl import validate
        conforms, report, report_text = validate(facts + ontology, shacl_graph=shapes, inference="none", advanced=False)
        graphs = dict(zip(("ontology.ttl", "shapes.ttl", "facts.ttl", "assertions.ttl", "rules.ttl", "review.ttl"),
                          (ontology, shapes, facts, assertions, rules, review)))
        graphs["validation-report.ttl"] = report
        for filename, g in graphs.items():
            text = g.serialize(format="turtle")
            # Parse the actual delivery bytes as part of export validation.
            Graph().parse(data=text, format="turtle")
            write_text(out / filename, text)
        nt = facts.serialize(format="nt")
        Graph().parse(data=nt, format="nt")
        write_text(out / "facts.nt", nt)
        write_text(out / "validation-report.txt", str(report_text))
        if key != input_fingerprint(run, snapshot):
            raise ValueError("RDF inputs changed during export; rerun export-rdf")
        upstream = metrics.get("status", "unknown")
        extraction = run / "extraction/manifest.json"
        if extraction.is_file() and read_json(extraction).get("state") != "success":
            upstream = "partial"
        state = "success" if conforms and upstream == "success" else "partial"
        manifest.update(state=state, ended_at=now(), input_fingerprint=key, base_iri=base_iri,
                        upstream_status=upstream, shacl_conforms=bool(conforms),
                        schema_scope=metrics.get("schema_scope"),
                        counts={**{k: len(v) for k, v in rows.items()}, "conflicts": len(conflicts),
                                "unresolved": len(schema.unresolved), "entity_identities": len(entities)},
                        triples={name: len(g) for name, g in graphs.items()},
                        missing_evidence_locations=sorted(missing_evidence),
                        inputs={str(p): file_hash(p) if p.is_file() else None for p in input_paths(run, snapshot)},
                        files={name: file_hash(out / name) for name in (*FILES, "validation-report.txt")},
                        validation_scope="Unconditional facts only; qualified statements and rule text are preserved, not executed. SHACL is not semantic/human validation.")
        write_json(out / "export-manifest.json", manifest)
        print(f"RDF={out} state={state} SHACL={bool(conforms)}", flush=True)
        return manifest
    except Exception as exc:
        manifest.update(state="failed", ended_at=now(), error=str(exc))
        write_json(out / "export-manifest.json", manifest)
        raise
