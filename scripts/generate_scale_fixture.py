"""Generate a deterministic synthetic C++ maintenance repository; never real plant data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

DOMAINS = [
    ("thermal", "Temperature", "temperature_c", 85),
    ("hydraulic", "Pressure", "pressure_kpa", 240),
    ("vibration", "Vibration", "vibration_mm_s", 12),
    ("electrical", "Current", "current_amp", 30),
    ("lubrication", "OilLevel", "oil_level_pct", 20),
    ("cooling", "Coolant", "coolant_flow_lpm", 15),
    ("braking", "Brake", "brake_force_kn", 45),
    ("transmission", "Gearbox", "gearbox_speed_rpm", 1800),
    ("safety", "Interlock", "interlock_state", 1),
    ("maintenance", "Service", "service_hours", 500),
]


def generate(root: Path, total_lines=70000, code_lines=56000):
    root = root.resolve()
    if root.exists():
        raise ValueError(f"Refusing to overwrite fixture: {root}")
    files, methods, types = [], [], []
    for module, title, measurement, threshold in DOMAINS:
        for unit in range(5):
            cls = f"{title}Unit{unit:02d}"
            namespace = "synthetic_plant::" + module
            header = f"include/{module}/{cls}.h"
            h = ["#pragma once", "#include <cstdint>", f"namespace {namespace} {{",
                 f"enum class {cls}State {{ Idle, Running, Warning, Shutdown }};",
                 f"struct {cls}Reading {{", f"    double {measurement};", "    bool valid;", "    std::uint64_t timestamp_ms;", "};",
                 f"class {cls} {{", "public:", f"    explicit {cls}(int asset_id);",
                 f"    int evaluate(const {cls}Reading& reading);", "    bool service_due(int operating_hours) const;",
                 "    void acknowledge(int alarm_code);", "    int dispatch(int mode, int input);", "private:",
                 "    int asset_id_;", "    int alarm_code_ = 0;", f"    {cls}State state_ = {cls}State::Idle;", "};", "}"]
            files.append((header, h, None, module))
            types.extend([(header, cls + "State"), (header, cls + "Reading"), (header, cls)])
            methods.extend([(header, cls), (header, "evaluate"), (header, "service_due"), (header, "acknowledge"), (header, "dispatch")])
            implementation = f"src/{module}/{cls}.cpp"
            lines = [f'#include "{module}/{cls}.h"', f"namespace {namespace} {{",
                     f"{cls}::{cls}(int asset_id) : asset_id_(asset_id) {{}}",
                     f"int {cls}::evaluate(const {cls}Reading& reading) {{", "    if (!reading.valid) {",
                     "        alarm_code_ = 901;", "        return -1;", "    }",
                     f"    if (reading.{measurement} > {threshold + unit}) {{",
                     "        alarm_code_ = 101;", f"        state_ = {cls}State::Warning;", "        return 1;", "    }",
                     f"    state_ = {cls}State::Running;", "    return 0;", "}",
                     f"bool {cls}::service_due(int operating_hours) const {{", f"    return operating_hours >= {500 + unit * 100};", "}", "}"]
            files.append((implementation, lines, None, module))
            methods.extend([(implementation, cls + "::" + cls), (implementation, cls + "::evaluate"), (implementation, cls + "::service_due")])
            controller = f"src/{module}/{cls}Controller.cpp"
            lines = [f'#include "{module}/{cls}.h"', f"namespace {namespace} {{",
                     f"void {cls}::acknowledge(int alarm_code) {{", "    if (alarm_code == alarm_code_) {",
                     "        alarm_code_ = 0;", f"        state_ = {cls}State::Idle;", "    }", "}",
                     f"int {cls}::dispatch(int mode, int input) {{", "    int result = input;",
                     "    for (int attempt = 0; attempt < 3; ++attempt) {", "        result += attempt;", "    }",
                     "    switch (mode) {", "    case 0: result += asset_id_; break;", "    case 1: acknowledge(result); break;",
                     "    default: result = -1; break;", "    }", "    return result;", "}", "}"]
            # Extra executable statements are distributed across all controller functions.
            files.append((controller, lines, 18, module))
            methods.extend([(controller, cls + "::acknowledge"), (controller, cls + "::dispatch")])
    base = sum(len(lines) for _, lines, _, _ in files)
    if not base <= code_lines <= total_lines:
        raise ValueError(f"Require {base} <= code_lines <= total_lines; got {code_lines}, {total_lines}")
    expandable = list(range(len(files)))
    extra, rem = divmod(code_lines - base, len(expandable))
    for position, i in enumerate(expandable):
        name, lines, offset, module = files[i]
        remaining = extra + (position < rem)
        additions, helper = [], 0
        while remaining >= 6:
            count = min(remaining, 30)
            # Leave at least six lines for the final helper rather than a partial function.
            if 0 < remaining - count < 6:
                count -= 6 - (remaining - count)
            symbol = Path(name).stem + f"_policy_step_{helper:02d}"
            body = [f"static inline int {symbol}(int input) {{", "    int result = input;"]
            body += [f"    result += (input + {j + 1}) % {7 + (j + i) % 29};" for j in range(count - 4)]
            body += ["    return result;", "}"]
            additions.extend(body)
            methods.append((name, symbol))
            remaining -= count
            helper += 1
        # Small remainders are ordinary namespace constants, not malformed fragments.
        additions += [f"static constexpr int {Path(name).stem}_limit_{j} = {100 + j};" for j in range(remaining)]
        files[i] = (name, lines[:-1] + additions + lines[-1:], None, module)
    # Preserve exact physical count while varying file sizes deterministically.
    padding, rem = divmod(total_lines - code_lines, len(files))
    root.mkdir(parents=True)
    manifest = {"synthetic": True, "not_production_data": True,
                "line_count_definition": "physical lines; code = nonblank lines not starting //; inline code braces/directives included",
                "requested": {"cpp": 100, "h": 50, "physical_lines": total_lines, "code_lines": code_lines},
                "domains": [d[0] for d in DOMAINS], "expected_methods": methods, "expected_types": types, "files": []}
    for i, (name, lines, _, module) in enumerate(files):
        notes = []
        for j in range(padding + (i < rem)):
            if j % 6 == 0:
                notes.append("")
            else:
                notes.append(f"// SYNTHETIC {module} design note {i:03d}-{j:04d}: asset identity, measurement units, alarm threshold and service policy must be traceable.")
        # Place bulk documentation after namespace close; a concise business comment near code.
        # All padding stays outside functions, avoiding artificial function size from comments.
        full = lines + notes
        data = ("\n".join(full) + "\n").encode("utf-8")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest["files"].append({"file": name, "module": module, "physical_lines": len(full),
                                  "code_lines": len(lines), "comment_lines": sum(bool(s) for s in notes),
                                  "blank_lines": notes.count(""), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest["actual"] = {"cpp": sum(Path(f["file"]).suffix == ".cpp" for f in manifest["files"]), "h": 50,
                           **{key: sum(f[key] for f in manifest["files"]) for key in
                              ("physical_lines", "code_lines", "comment_lines", "blank_lines", "bytes")},
                           "method_locations": len(methods), "type_locations": len(types)}
    # Outside source root: the benchmark truth must never be sent to the extraction model.
    truth = root.parent / (root.name + "-truth.json")
    truth.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return truth


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--total-lines", type=int, default=70000)
    ap.add_argument("--code-lines", type=int, default=56000)
    args = ap.parse_args()
    truth = generate(args.out, args.total_lines, args.code_lines)
    print(truth)
    print(truth.read_text(encoding="utf-8").split('"actual": ')[-1][:500])
