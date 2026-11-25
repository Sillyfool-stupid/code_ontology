from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Supported(StrictModel):
    evidence_ids: list[str] = Field(min_length=1)


class Concept(Supported):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    parents: list[str] = Field(default_factory=list)


class RelationType(Supported):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    domain: list[str] = Field(min_length=1)
    range: list[str] = Field(min_length=1)


class PropertyType(Supported):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    domain: list[str] = Field(min_length=1)
    datatype: Literal["string", "number", "integer", "boolean"]
    unit: str | None = None


class Ontology(StrictModel):
    version: str = "0.1"
    profile: str = ""
    classes: list[Concept]
    relations: list[RelationType]
    properties: list[PropertyType]
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        ids = [x.id for collection in (self.classes, self.relations, self.properties) for x in collection]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate schema IDs")
        classes = {x.id: x for x in self.classes}
        for concept in self.classes:
            if not set(concept.parents) <= classes.keys():
                raise ValueError(f"Unknown parent of {concept.id}")
        for collection in (self.relations, self.properties):
            for item in collection:
                referenced = item.domain + (item.range if isinstance(item, RelationType) else [])
                if not set(referenced) <= classes.keys():
                    raise ValueError(f"Unknown domain/range of {item.id}")
        visiting, done = set(), set()

        def visit(cid):
            if cid in visiting:
                raise ValueError("Class hierarchy cycle")
            if cid in done:
                return
            visiting.add(cid)
            for parent in classes[cid].parents:
                visit(parent)
            visiting.remove(cid); done.add(cid)
        for cid in classes:
            visit(cid)
        return self


Scope = Literal["code_definition", "configuration_record", "test", "example"]
Basis = Literal["direct", "inferred"]


class Entity(Supported):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type_id: str
    scope: Scope
    basis: Basis


class Relation(Supported):
    subject: str
    relation_id: str
    object: str
    basis: Basis


class Attribute(Supported):
    subject: str
    property_id: str
    value: str | float | int | bool
    unit: str | None = None
    basis: Basis


class Rule(Supported):
    name: str
    condition: str
    action: str
    scope: Scope
    basis: Basis


class Candidate(StrictModel):
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Extraction(StrictModel):
    entities: list[Entity]
    relations: list[Relation]
    attributes: list[Attribute]
    rules: list[Rule]
    candidates: list[Candidate] = Field(default_factory=list)


def supported_ids(ontology: Ontology) -> set[str]:
    return {eid for coll in (ontology.classes, ontology.relations, ontology.properties) for row in coll for eid in row.evidence_ids}
