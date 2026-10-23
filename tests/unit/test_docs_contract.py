"""Unit tests for the docs-as-tests guard-rail (D02).

``scripts/check_docs_contract.py`` compiles and runs documentation code blocks,
checks attribute claims and lints ``llms-full.txt``; ``scripts/sync_doc_facts.py``
regenerates the fact regions. Both are plain scripts, loaded here by path.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contract = _load("check_docs_contract")
facts = _load("sync_doc_facts")


def _md(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


# ---------------------------------------------------------------------------
# Extraction and markers
# ---------------------------------------------------------------------------


def test_extracts_python_blocks_with_their_opening_line() -> None:
    blocks = contract.extract_blocks(
        _md("""
        Intro.

        ```python
        x = 1
        ```

        ```bash
        echo hi
        ```
    """)
    )

    assert [(b.line, b.language, b.source) for b in blocks] == [
        (3, "python", "x = 1\n"),
        (7, "bash", "echo hi\n"),
    ]


@pytest.mark.parametrize(
    "fence,markers",
    [
        ("```python run", {"run"}),
        ("```python fragment", {"fragment"}),
        ('```python title="app.py"', set()),
        ("```{.python .fragment}", {"fragment"}),
    ],
)
def test_info_string_markers(fence: str, markers: set[str]) -> None:
    (block,) = contract.extract_blocks(f"{fence}\nx = 1\n```\n")

    assert block.language == "python"
    assert set(block.markers) == markers


def test_comment_marker_applies_to_the_next_fence_only() -> None:
    blocks = contract.extract_blocks(
        _md("""
        <!-- docs-contract: fragment -->

        ```python
        return 1
        ```

        ```python
        y = 2
        ```
    """)
    )

    assert [set(b.markers) for b in blocks] == [{"fragment"}, set()]


def test_comment_marker_is_dropped_by_intervening_text() -> None:
    (block,) = contract.extract_blocks(
        _md("""
        <!-- docs-contract: run -->
        Some prose in between.

        ```python
        z = 3
        ```
    """)
    )

    assert block.markers == frozenset()


def test_longer_outer_fence_keeps_nested_fences_as_content() -> None:
    blocks = contract.extract_blocks(
        _md("""
        ````markdown
        ```python
        def broken(
        ```
        ````
    """)
    )

    assert [b.language for b in blocks] == ["markdown"]


def test_indented_block_inside_a_list_is_dedented() -> None:
    (block,) = contract.extract_blocks("1. Item\n\n    ```python\n    if True:\n        pass\n    ```\n")

    assert block.source == "if True:\n    pass\n"


# ---------------------------------------------------------------------------
# Compile and run
# ---------------------------------------------------------------------------


def test_compile_reports_the_failing_line() -> None:
    blocks = contract.extract_blocks(
        _md("""
        Title

        ```python
        ok = 1
        def broken(:
        ```
    """),
        "doc.md",
    )

    (violation,) = contract.check_compile(blocks)

    assert violation.kind == "compile"
    assert violation.location == "doc.md:5"


@pytest.mark.parametrize(
    "source",
    [
        "<!-- docs-contract: fragment -->\n```python\nreturn 1\n```\n",
        "```python fragment\nreturn 1\n```\n",
        '```python\n--8<-- "docs/tutorial/app.py"\n```\n',
        "```python\n>>> 1 + 1\n2\n```\n",
    ],
)
def test_intentional_fragments_are_not_compiled(source: str) -> None:
    assert contract.check_compile(contract.extract_blocks(source)) == []


def test_top_level_await_compiles() -> None:
    blocks = contract.extract_blocks("```python\nimport asyncio\nawait asyncio.sleep(0)\n```\n")

    assert contract.check_compile(blocks) == []


def test_run_blocks_share_one_namespace_per_file() -> None:
    blocks = contract.extract_blocks(
        _md("""
        ```python run
        value = 20
        ```

        ```python
        this_block_is_not_run = 1 / 0
        ```

        ```python run
        import asyncio
        await asyncio.sleep(0)
        assert value + 1 == 21
        ```
    """),
        "doc.md",
    )

    assert contract.run_blocks(blocks, "_docs_contract_test_ns") == []


def test_failing_run_block_is_reported_with_its_output() -> None:
    blocks = contract.extract_blocks(
        _md("""
        ```python run
        print("about to fail")
        raise ValueError("boom")
        ```
    """),
        "doc.md",
    )

    (violation,) = contract.run_blocks(blocks, "_docs_contract_test_fail")

    assert violation.kind == "run"
    assert violation.location == "doc.md:1"
    assert "ValueError: boom" in violation.message
    assert "about to fail" in violation.message


def test_run_blocks_resolve_postponed_annotations() -> None:
    # Classes defined in a run block must be resolvable by Lauren's
    # type-hint resolver: the block runs in a module registered in sys.modules.
    blocks = contract.extract_blocks(
        _md("""
        ```python run
        from dataclasses import dataclass
        from lauren import Json, LaurenFactory, controller, module, post
        from lauren.testing import TestClient

        @dataclass
        class Item:
            name: str

        @controller("/items")
        class Items:
            @post("/")
            async def create(self, body: Json[Item]) -> dict:
                return {"name": body.name}

        @module(controllers=[Items])
        class ItemsModule: ...

        assert TestClient(LaurenFactory.create(ItemsModule)).post("/items/", json={"name": "x"}).json() == {"name": "x"}
        ```
    """),
        "doc.md",
    )

    assert contract.run_blocks(blocks, "_docs_contract_test_hints") == []


# ---------------------------------------------------------------------------
# Attribute claims
# ---------------------------------------------------------------------------


def test_has_attribute_sees_dataclass_fields_and_slots() -> None:
    from dataclasses import dataclass

    @dataclass
    class WithField:
        required: int

    class WithSlots:
        __slots__ = ("slot",)

    assert contract.has_attribute(WithField, "required")
    assert contract.has_attribute(WithSlots, "slot")
    assert not contract.has_attribute(WithField, "missing")


def test_missing_attribute_claim_is_reported() -> None:
    violations = contract.check_attributes({"lauren.background.TaskHandle": ["task_id", "no_such_member"]})

    assert [v.message for v in violations] == [
        "lauren.background.TaskHandle has no attribute 'no_such_member'"
    ]


def test_every_documented_attribute_exists() -> None:
    # Regression lock for documented-but-missing attributes (TaskHandle,
    # TestResponse, UploadFile, ...): the claims table must resolve.
    claims = contract.load_config()["attributes"]

    assert claims
    assert contract.check_attributes(claims) == []


# ---------------------------------------------------------------------------
# llms-full.txt structure and links
# ---------------------------------------------------------------------------

_GOOD = _md("""
    # Title

    ## Table of Contents

    1. [Alpha Beta](#alpha-beta)
    2. [Gamma (`x`)](#gamma-x)

    ---

    ## Alpha Beta

    See [Gamma](#gamma-x).

    ```python
    ## not a heading inside a fence
    ```

    ## Gamma (`x`)

    Text.

    ---

    _End of reference._
""")


def test_well_formed_reference_passes() -> None:
    assert contract.check_llms_structure(_GOOD) == []
    assert contract.check_reference_links("[a](llms-full.txt#alpha-beta)", _GOOD) == []


def test_end_marker_must_be_last() -> None:
    text = _GOOD.replace("_End of reference._", "_End of reference._\n\n## Late\n\nMore.")

    messages = [v.message for v in contract.check_llms_structure(text)]

    assert any("must be the last line" in m for m in messages)
    assert any("'Late' is missing from the table of contents" in m for m in messages)


def test_missing_or_duplicated_end_marker_is_reported() -> None:
    without = _GOOD.replace("_End of reference._", "")
    doubled = _GOOD.replace("Text.", "_End of reference._")

    assert any("found 0" in v.message for v in contract.check_llms_structure(without))
    assert any("found 2" in v.message for v in contract.check_llms_structure(doubled))


def test_wrong_toc_anchor_is_reported() -> None:
    text = _GOOD.replace("(#gamma-x)\n\n---", "(#gamma)\n\n---", 1)

    assert any("should be #gamma-x" in v.message for v in contract.check_llms_structure(text))


def test_duplicated_tail_is_reported() -> None:
    tail = "line one\nline two\nline three\n"
    text = _GOOD.replace("_End of reference._", tail + tail + "_End of reference._")
    text = text.replace(tail + tail + "_End of reference._", "_End of reference._\n" + tail + tail)

    assert any("duplicated" in v.message for v in contract.check_llms_structure(text))


def test_dangling_reference_links_are_reported() -> None:
    violations = contract.check_reference_links("[x](llms-full.txt#nowhere)", _GOOD + "\n[y](#gone)\n")

    assert sorted(v.message for v in violations) == [
        "link to #gone has no matching heading",
        "link to llms-full.txt#nowhere has no matching heading",
    ]


def test_github_slug_matches_github_anchors() -> None:
    assert contract.github_slug("Guards & Middleware") == "guards--middleware"
    assert contract.github_slug("Server-Sent Events (`EventStream`)") == "server-sent-events-eventstream"


def test_repository_reference_is_well_formed() -> None:
    text = (ROOT / "lauren" / "llms-full.txt").read_text(encoding="utf-8")

    assert contract.check_llms_structure(text) == []


# ---------------------------------------------------------------------------
# Generated facts
# ---------------------------------------------------------------------------


def test_error_catalog_lists_every_exception_class() -> None:
    import lauren.exceptions as exceptions

    table = facts.error_catalog()

    for name in exceptions.__all__:
        assert f"| `{name}` |" in table
    assert "| `ExtractorFieldError` | `ExtractorError` | 422 | `extractor_field_error` |" in table


def test_render_replaces_only_marked_regions() -> None:
    text = "before\n<!-- lauren:facts:error-catalog -->\nstale\n<!-- /lauren:facts -->\nafter\n"

    rendered = facts.render(text)

    assert rendered.startswith("before\n<!-- lauren:facts:error-catalog -->\n| Class |")
    assert rendered.endswith("<!-- /lauren:facts -->\nafter\n")
    assert "stale" not in rendered


def test_unknown_fact_region_is_an_error() -> None:
    with pytest.raises(KeyError, match="unknown fact region 'nope'"):
        facts.render("<!-- lauren:facts:nope -->\n<!-- /lauren:facts -->\n")


def test_stale_regions_are_detected(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("<!-- lauren:facts:error-hierarchy -->\nold\n<!-- /lauren:facts -->\n", encoding="utf-8")

    assert facts.stale_regions(tmp_path, ("doc.md",)) == ["doc.md"]
    doc.write_text(facts.render(doc.read_text(encoding="utf-8")), encoding="utf-8")
    assert facts.stale_regions(tmp_path, ("doc.md",)) == []


def test_repository_fact_regions_are_current() -> None:
    assert facts.stale_regions() == []
