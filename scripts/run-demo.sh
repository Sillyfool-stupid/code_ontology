#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
repo="${1:-$project_root/examples/maintenance}"
mode="${2:-smoke}"
case "$mode" in smoke|baseline) ;; *) echo "mode must be smoke or baseline" >&2; exit 2;; esac
.venv/bin/python -m code_ontology doctor --env-file .env
.venv/bin/python -m code_ontology run --repo "$repo" --config "configs/$mode.yaml" --env-file .env

