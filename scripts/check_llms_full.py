#!/usr/bin/env python3
"""Verify every public symbol in `lauren.__all__` is referenced in `llms-full.txt`.

This is the local hook counterpart of `nox -s llms_check`. Keeping it as a
standalone script lets `prek` invoke it without spinning up a full nox
environment on every commit.

Exit codes:
    0  All public symbols are referenced.
    1  At least one symbol is missing (also prints the missing names).
    2  Something is structurally wrong (no `__all__`, file missing, …).
"""

from __future__ import annotations

import importlib
import pathlib
import re
import sys


def main(argv: list[str]) -> int:
    pkg = importlib.import_module("lauren")
    public = set(getattr(pkg, "__all__", ()) or ())
    if not public:
        print("ERROR: lauren.__all__ is empty or missing", file=sys.stderr)
        return 2

    llms_path = pathlib.Path("llms-full.txt")
    if not llms_path.exists():
        print("ERROR: llms-full.txt not found in repo root", file=sys.stderr)
        return 2

    text = llms_path.read_text(encoding="utf-8")
    missing = sorted(name for name in public if name not in text)
    if missing:
        print("ERROR: the following public symbols are missing from llms-full.txt:")
        for name in missing:
            print(f"  - {name}")
        print()
        print(
            "Either add them to llms-full.txt or remove them from `lauren.__all__`."
        )
        return 1

    print(
        f"OK: all {len(public)} public symbols of `lauren` "
        f"are referenced in llms-full.txt"
    )

    if "--list-extras" in argv:
        # Heuristic: top-level `lauren.<Name>` mentions that are no longer public.
        referenced = set(re.findall(r"\blauren\.([A-Za-z_][A-Za-z0-9_]*)", text))
        extras = sorted(referenced - public - set(dir(pkg)))
        if extras:
            print()
            print("llms-full.txt references symbols that are no longer in lauren:")
            for name in extras:
                print(f"  - {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
