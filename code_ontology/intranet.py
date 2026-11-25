"""One configuration, explicit stages, portable intranet entry point."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import json
import os
from pathlib import Path
import re
import sys
import time

from .config import PROJECT, env_values, load_policy, load_settings, configuration_sources
from .util import digest, now, read_json, write_json, file_hash
from .events import execution, emit, redact

STEPS = ("scan", "parse", "normalize", "prepare", "discover-local", "merge-schema", "review-schema", "extract", "finalize", "validate", "report")


@contextmanager
def writer_lock(root):
    """Same OS lock used by the workbench; no stale-PID guessing."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "writer.workbench.lock").open("a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Output is being written by another script/workbench task") from exc
        yield
    finally:
        handle.close()


class Tee:
    def __init__(self, stream, file, secret):
        self.stream, self.file, self.secret = stream, file, secret

    def write(self, value):
        value = redact(value, self.secret)
        self.stream.write(value); self.file.write(value); self.file.flush()
        return len(value)

    def flush(self):
        self.stream.flush(); self.file.flush()


def resolve(env, key, base, default=""):
    value = env.get(key) or default
    return str((base / value).resolve()) if value else ""


def session_config(args):
    envpath = Path(args.env_file).resolve()
    env = env_values(str(envpath))
    base = envpath.parent
    from .config_bundle import read_bundle
    run = read_bundle(args.config)["run"]
    name = args.experiment or env.get("CO_EXPERIMENT") or run.get("experiment") or "baseline-01"
    if not re.fullmatch(r"[\w-]{1,80}", name):
        raise ValueError("Invalid experiment name")
    out = Path(args.out).resolve() if args.out else Path(resolve(env, "CO_OUTPUT", base, run.get("out", "outputs/intranet")))
    path = out / "sessions" / f"{name}.json"
    return envpath, env, base, name, out, path


def stage_fingerprint(step, session, settings, policy):
    """Only relevant stage dependencies, for status diagnostics; execution also validates contracts."""
    from .pipeline import prompt
    snap = Path(session["snapshot"]) if session.get("snapshot") else None
    view, profile = session["view"], session["profile"]
    run = snap / "runs" / session["experiment"] / view / profile if snap else None
    data = {"step": step, "repo": session["repo"], "snapshot": str(snap)}
    def stamp(path):
        return file_hash(path) if path and path.is_file() else None
    if step in {"scan", "parse"}:
        keys = ("parser_backend", "include", "exclude", "max_files", "max_file_bytes", "includes", "defines", "compile_commands", "joern_timeout")
        data["policy"] = {k: policy[k] for k in keys}
    elif step in {"normalize", "prepare"}:
        data["graph"] = stamp(snap / "raw" / ("tree-sitter" if (snap / "raw/tree-sitter/export.json").is_file() else "cpg") / "export.json") if snap else None
        if step == "prepare":
            data["policy"] = {k: policy[k] for k in ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")}
            data["graph_index"] = stamp(snap / "normalized" / view / "index.json") if snap else None
    else:
        data["packs"] = stamp(snap / "prepared" / view / "index.json") if snap else None
        if step in {"discover-local", "merge-schema", "review-schema", "extract", "finalize"}:
            stage = {"discover-local": "discover", "merge-schema": "merge", "review-schema": "review", "finalize": "extract", "extract": "extract"}[step]
            data["settings"] = settings.for_stage(stage).public()
            data["prompt"] = prompt(policy, profile, stage)
            data["recovery"] = policy["recovery_max_depth"]
            if stage == "discover":
                data["selection"] = [policy["discovery_max_packs"], policy["batch_packs"]]
            if stage == "merge":
                data["local"] = stamp(run / "schema/local-manifest.json") if run else None
                data["batch"] = policy["merge_batch_items"]
            if stage == "review":
                data["draft"] = stamp(run / "schema/draft-schema.json") if run else None
                data["mapping"] = stamp(run / "schema/draft-source-mapping.json") if run else None
                data["review"] = [policy["review_batch_items"], policy["review_max_rounds"]]
            if step in {"extract", "finalize"}:
                data["schema"] = stamp(run / "schema/schema.json") if run else None
                data["allow_unreviewed_schema"] = policy["allow_unreviewed_schema"]
        if step in {"finalize", "validate", "report"}:
            data["extraction"] = stamp(run / "extraction/manifest.json") if run else None
    return digest(data)


def execute(step, session, settings, policy):
    """Stage API shared with the existing parser, prepare and pipeline implementations."""
    from .repository import snapshot, verify_sources
    from .parsing import parse_repository, parser_views, snapshot_parser, normalize_repository
    from .prepare import prepare
    from .pipeline import Pipeline, report_run
    from .cli import validate
    out, view = Path(session["out"]), session["view"]
    if step == "scan":
        dest = snapshot(Path(session["repo"]), out, policy, settings.joern_home)
        session["snapshot"] = str(dest)
        print(f"SNAPSHOT={dest}", flush=True)
        return 0
    if not session.get("snapshot"):
        raise ValueError("Run scan first")
    dest = Path(session["snapshot"])
    verify_sources(dest)
    run = dest / "runs" / session["experiment"] / view / session["profile"]
    if step == "parse":
        current = snapshot(Path(session["repo"]), out, policy, settings.joern_home)
        if current != dest:
            raise ValueError("Source/parser configuration changed; run scan and review new snapshot first")
        effective = parser_views(snapshot_parser(dest), [view])
        if effective != [view]:
            raise ValueError("Tree-sitter supports AST only; configure CO_VIEW=ast and init")
        parse_repository(dest, settings, policy, [view], raw_only=True)
    elif step == "normalize":
        normalize_repository(dest, [view])
    elif step == "prepare":
        prepare(dest, view, policy)
    elif step == "validate":
        return validate(dest, view, run)
    elif step == "report":
        if not (run / "metrics.json").is_file():
            raise ValueError("Run finalize first")
        report_run(run)
        print(run / "report.md")
    else:
        graph = dest / "normalized" / view / "index.json"
        index = dest / "prepared" / view / "index.json"
        if not index.is_file() or not graph.is_file():
            raise ValueError("Run normalize and prepare first")
        from .util import file_hash
        from .prepare import PREPARE_FORMAT, __file__ as implementation
        expected = digest({"source": read_json(dest / "manifest.json")["snapshot_id"], "view": view,
                           "policy": {k: policy[k] for k in ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack")},
                           "graph": read_json(graph).get("graph_fingerprint"), "implementation": file_hash(Path(implementation)), "format": PREPARE_FORMAT})
        if read_json(index).get("key") != expected:
            raise ValueError("Evidence configuration changed; run prepare first")
        pipeline = Pipeline(dest, view, session["profile"], session["experiment"], settings, policy)
        try:
            if step == "discover-local":
                pipeline.discover_local()
            elif step == "merge-schema":
                pipeline.merge_schema()
            elif step == "review-schema":
                pipeline.review_schema()
            elif step == "extract":
                state = run / "schema" / "merge-status.json"
                supplied = session.get("schema_file")
                if not supplied and (not state.is_file() or read_json(state).get("state") != "success"):
                    raise ValueError("Run merge-schema successfully first")
                if not supplied:
                    from .schema_review import check_schema_use
                    review_state = check_schema_use(pipeline)
                schema = pipeline.fixed_schema(Path(supplied) if supplied else run / "schema" / "schema.json")
                write_json(run / "schema-scope.json", {"source": supplied or "merged",
                           "review_state": "explicit_schema_selection" if supplied else review_state,
                           "note": "Explicit schema selection; not proof of complete global alignment" if supplied else "Merged schema for selected discovery scope"})
                if not pipeline.extract(schema, finalize=False):
                    return 1
            elif step == "finalize":
                pipeline.finalize_saved()
            pipeline.save_state("execution", "success")
        except Exception as exc:
            pipeline.save_state("execution", "partial", error=str(exc))
            raise
        finally:
            pipeline.close()
    return 0


def main(argv=None):
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="Intranet step runner: edit .env once; steps never run upstream implicitly")
    parser.add_argument("step", choices=("init", "status", "config", "doctor", "check", "smoke", "run", *STEPS))
    parser.add_argument("--env-file", default=str(PROJECT / ".env"))
    parser.add_argument("--config", help="Policy YAML or directory containing run.yaml")
    parser.add_argument("--repo")
    parser.add_argument("--out")
    parser.add_argument("--experiment")
    parser.add_argument("--schema", help="Explicit schema for extract; use a new experiment for a module schema")
    parser.add_argument("--with-llm", action="store_true", help="Explicitly test configured real model")
    parser.add_argument("--to", choices=STEPS, default="report")
    args = parser.parse_args(argv)
    settings = None
    try:
        if args.step == "smoke":
            from .intranet_smoke import run
            return run(Path(args.out).resolve() / "smoke" if args.out else PROJECT / "outputs/intranet-smoke")
        envpath, env, base, name, out, path = session_config(args)
        policy, settings = load_policy(args.config, str(envpath)), load_settings(str(envpath), args.config)
        from .config_bundle import read_bundle
        run_config = read_bundle(args.config)["run"]
        for field, env_key in (("view", "CO_VIEW"), ("profile", "CO_PROFILE")):
            if run_config.get(field) and not env.get(env_key):
                policy[field + "s"] = [run_config[field]]
        if args.step in {"doctor", "check"}:
            from .cli import doctor
            return doctor(settings, out / "doctor", not args.with_llm, policy)
        if args.step == "config":
            # Resolved layers are explicit. Only non-secret values are persisted/displayed.
            print(json.dumps({"env_file": str(envpath), "priority": "CLI > system env > .env > YAML > defaults",
                              "policy": policy, "settings": settings.public(),
                              "sources": configuration_sources(str(envpath), args.config),
                              "stages": {s: settings.for_stage(s).public() for s in ("discover", "merge", "review", "extract")}}, ensure_ascii=False, indent=2))
            return 0
        if args.step == "status":
            if not path.is_file():
                raise ValueError("Run init first")
            session = read_json(path)
            for step, record in session["stages"].items():
                record["configuration_changed"] = record.get("fingerprint") != stage_fingerprint(step, session, settings, policy)
                if record["configuration_changed"] and record["state"] == "success":
                    record["display_state"] = "stale"
            print(json.dumps(session, ensure_ascii=False, indent=2))
            if session.get("snapshot"):
                schema_root = Path(session["snapshot"]) / "runs" / name / session["view"] / session["profile"] / "schema"
                for label, filename in (("MERGE", "merge-status.json"), ("REVIEW", "review-status.json")):
                    status = schema_root / filename
                    if status.is_file():
                        print(label + ":", status.read_text(encoding="utf-8"))
            return 0
        with writer_lock(out):
            if args.step == "init":
                repo = str(Path(args.repo).resolve()) if args.repo else resolve(env, "CO_REPO", base, run_config.get("repo", ""))
                if not repo or not Path(repo).is_dir():
                    raise ValueError("Set CO_REPO in .env or use init --repo PATH")
                old = read_json(path) if path.is_file() else {}
                session = {"repo": repo, "out": str(out), "experiment": name, "view": policy["views"][0],
                           "profile": policy["profiles"][0], "env_file": str(envpath), "stages": {}, "created_at": now()}
                if old and any(old[k] != session[k] for k in ("repo", "out", "view", "profile")):
                    raise ValueError("Session identity changed; choose a new CO_EXPERIMENT")
                session.update({k: old[k] for k in ("snapshot", "stages") if k in old})
                write_json(path, session)
                print(f"SESSION={path}\nNext: intranet scan")
                return 0
            if not path.is_file():
                raise ValueError("Run init first")
            session = read_json(path)
            if policy["views"][0] != session["view"] or policy["profiles"][0] != session["profile"] or (env.get("CO_REPO") and resolve(env, "CO_REPO", base) != session["repo"]):
                raise ValueError("Session identity differs from .env; choose a new experiment and init")
            if args.schema:
                if args.step != "extract":
                    raise ValueError("--schema is only accepted by extract")
                selected_schema = str(Path(args.schema).resolve())
                if not Path(selected_schema).is_file():
                    raise ValueError("Explicit schema file does not exist")
                session["schema_file"] = selected_schema
            steps = STEPS[:STEPS.index(args.to)+1] if args.step == "run" else (args.step,)
            if args.step == "run" and args.to != "review-schema":
                steps = tuple(step for step in steps if step != "review-schema")
            for step in steps:
                log = out / "sessions" / name / f"{step}-{time.time_ns()}.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                record = {"state": "running", "started_at": now(), "log": str(log),
                          "resume_command": f"intranet {step}"}
                session["stages"][step] = record
                write_json(path, session)
                write_json(log.with_suffix(".config.json"), {"settings": settings.public(), "policy": policy})
                with log.open("w", encoding="utf-8") as handle, redirect_stdout(Tee(sys.stdout, handle, settings.api_key)), redirect_stderr(Tee(sys.stderr, handle, settings.api_key)), execution(out, name, step, settings.api_key) as activity:
                    record["execution_id"] = activity["execution_id"]
                    record["events"] = activity["events"]
                    activity["log"] = str(log)
                    # Publish the association before a slow request starts so
                    # the web reader can follow the live CLI log immediately.
                    write_json(path, session)
                    write_json(Path(activity["events"]).parent / "execution.json", activity)
                    print(f"START {step} {now()} log={log}", flush=True)
                    try:
                        code = execute(step, session, settings, policy)
                        record.update(state="success" if code == 0 else "partial", ended_at=now())
                    except KeyboardInterrupt:
                        record.update(state="interrupted", ended_at=now(), error="Interrupted; inspect server state before retry")
                        code = 130
                    except Exception as exc:
                        from .llm import RequestUnknown
                        error = str(exc).replace(settings.api_key, "<redacted>") if settings.api_key else str(exc)
                        record.update(state="unknown" if isinstance(exc, RequestUnknown) else "failed", error=error, ended_at=now())
                        print(f"FAILED {step}: {error}\nResume: intranet {step}", flush=True)
                        code = 1
                    finally:
                        record["fingerprint"] = stage_fingerprint(step, session, settings, policy)
                        write_json(path, session)
                        activity["state"] = record["state"]
                        emit("stage_ended", state=record["state"], message="阶段执行结束", artifact=record["log"])
                if code:
                    return code
        return 0
    except Exception as exc:
        message = str(exc)
        if settings and settings.api_key:
            message = message.replace(settings.api_key, "<redacted>")
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
