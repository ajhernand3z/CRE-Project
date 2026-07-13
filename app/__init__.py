import sys

# Fail fast with a clear message instead of a cryptic TypeError from the
# `X | None` annotations used throughout (PEP 604 syntax, Python 3.10+).
if sys.version_info < (3, 10):
    raise SystemExit(
        f"This app requires Python 3.10 or newer; you are running "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        "On macOS, install the current version from https://python.org/downloads, "
        "reopen your terminal, and re-run with `python3`."
    )
