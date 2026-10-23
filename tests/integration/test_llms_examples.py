"""The curated ``run`` examples in the AI-facing docs execute end to end (D02).

Each documentation file's blocks marked ``run`` (``lauren/llms-full.txt`` holds
most of them) execute in order in one namespace — factory calls, decorators,
``TestClient`` round-trips and all — so an example that would crash when copied
fails here first. The same check runs as ``nox -s docs_contract``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _contract():
    name = "check_docs_contract"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


contract = _contract()
DOC_FILES = [
    path
    for path in contract.iter_source_files(ROOT)
    if any("run" in block.markers for block in contract.extract_blocks(path.read_text(encoding="utf-8")))
]


def test_llms_full_has_a_curated_set_of_runnable_examples() -> None:
    text = (ROOT / "lauren" / "llms-full.txt").read_text(encoding="utf-8")
    runnable = [b for b in contract.extract_blocks(text) if b.is_python and "run" in b.markers]

    assert len(runnable) >= 40


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_run_examples_execute(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    blocks = contract.extract_blocks(path.read_text(encoding="utf-8"), rel)

    violations = contract.run_blocks(blocks, f"_docs_example_{abs(hash(rel))}")

    assert violations == [], "\n".join(str(v) for v in violations)
