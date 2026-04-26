"""Tests that the AI-facing documentation is shipped and discoverable."""

from __future__ import annotations


def test_llms_txt_available():
    from lauren import docs

    content = docs.llms_txt()
    assert "lauren" in content.lower()
    assert "llms-full.txt" in content.lower()


def test_llms_full_txt_available():
    from lauren import docs

    content = docs.llms_full_txt()
    # Sanity: the file is substantial and covers the main concepts.
    assert len(content) > 10_000
    for section in (
        "Extractors",
        "Dependency Injection",
        "Guards",
        "Middleware",
        "Auto-Serialization",
        "Error Catalog",
    ):
        assert section in content


def test_docs_exposed_on_package():
    import lauren

    # Both APIs should be reachable via the top-level package.
    assert callable(lauren.docs.llms_txt)
    assert callable(lauren.docs.llms_full_txt)
