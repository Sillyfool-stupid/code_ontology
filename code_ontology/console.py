"""Explicit UTF-8 CLI output, including CMD pipes and redirected logs."""
import sys


def configure_console():
    # Do not replace streams owned by notebooks, tests, or host applications.
    # File readers/writers specify their encoding separately; no interpreter restart needed.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
