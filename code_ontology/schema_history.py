"""Readable, append-only schema snapshots, independent from reusable checkpoints."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .util import now, write_json


class SchemaHistory:
    def __init__(self, schema_root: Path, stage: str, key: str):
        stamp = now().split(".")[0].replace("-", "").replace(":", "")
        while True:
            self.root = schema_root / "history" / f"{stage}-{stamp}-{uuid4().hex[:8]}"
            try:
                self.root.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        self.manifest = {"stage": stage, "key": key, "state": "running", "started_at": now(), "snapshots": []}
        self._save()

    def _save(self):
        write_json(self.root / "manifest.json", self.manifest)

    def snapshot(self, location: str, schemas: list[dict], *, state="accepted", mapping=None, **metadata):
        directory = self.root / location
        # Replaying cached tasks creates a new attempt, never overwrites old snapshots.
        directory.mkdir(parents=True, exist_ok=False)
        files = []
        for i, schema in enumerate(schemas, 1):
            name = "schema.json" if len(schemas) == 1 else f"schema-{i:03d}.json"
            write_json(directory / name, schema)
            files.append((directory / name).relative_to(self.root).as_posix())
        if mapping is not None:
            write_json(directory / "mapping.json", mapping)
        entry = {"location": location, "state": state, "created_at": now(), "schemas": files, **metadata}
        write_json(directory / "metadata.json", entry)
        self.manifest["snapshots"].append(entry)
        self._save()
        return directory

    def finish(self, state: str, **metadata):
        self.manifest.update(state=state, ended_at=now(), **metadata)
        self._save()


def split_location(location: str, child: int):
    """Keep recursive splits readable without exhausting Windows path lengths."""
    path = Path(location)
    return path.with_name(path.name + str(child)).as_posix() if path.name.startswith("part-") else f"{location}/part-{child}"
