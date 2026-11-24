"""Workbench parse guard: snapshot first, parse only the source range already reviewed."""
from __future__ import annotations

from pathlib import Path
import sys

from .cli import parser
from .config import load_policy, load_settings
from .console import configure_console
from .joern import parse
from .repository import snapshot


def main(argv=None):
    configure_console()
    values = list(sys.argv[1:] if argv is None else argv)
    position = values.index("--expected-snapshot")
    expected = Path(values[position + 1]).resolve()
    del values[position:position + 2]
    args = parser().parse_args(values)
    if args.command != "parse":
        raise ValueError("此入口仅支持工作台 parse")
    settings, policy = load_settings(args.env_file), load_policy(args.config)
    dest = snapshot(args.repo, args.out, policy, settings.joern_home)
    print(f"SNAPSHOT={dest}", flush=True)
    if dest != expected:
        print("源码或解析配置已变化：已生成新快照，请检查文件清单后再次执行解析。本次未运行 Joern。", file=sys.stderr)
        return 2
    parse(dest, settings, policy, args.views.split(","), args.force_parse)
    print("Joern parse and view exports complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
