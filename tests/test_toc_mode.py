"""mode='toc': build the tree purely from the PDF bookmark outline.

Real fixture first: examples/documents/attention-residuals.pdf carries 22
genuine bookmarks (pinned in test_flash_extraction) and exercises the
whole path — read, validate, classify, nest, store. Custom outlines
(generic titles, blank scans) are written with PyPDF2, a fork dependency,
because pypdfium2 cannot create bookmarks.
"""
import io
from pathlib import Path

import pytest

from pageindex import PageIndexClient
from pageindex.errors import PageIndexAPIError
from pageindex.local_api import LocalAPI

ATTENTION = (Path(__file__).parent.parent / "examples" / "documents"
             / "attention-residuals.pdf")


def _make_api(tmp_path):
    return LocalAPI(str(tmp_path / "storage"), model="test-model",
                    summary_model="test-model")


def _pdf_with_outline(tmp_path, page_texts, outline):
    """build_pdf bytes plus a flat bookmark outline: [(title, 0-based page)]."""
    from PyPDF2 import PdfReader, PdfWriter
    from conftest import build_pdf

    reader = PdfReader(io.BytesIO(build_pdf(page_texts)))
    writer = PdfWriter()
    writer.append(reader)
    for title, page_index in outline:
        writer.add_outline_item(title, page_index)
    path = tmp_path / "outlined.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


def _flatten(nodes):
    out = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.get("nodes") or []))
    return out


# ── happy path: real bookmarks ──


def test_toc_builds_tree_from_bookmarks(tmp_path):
    api = _make_api(tmp_path)
    result = api.submit_document(str(ATTENTION), mode="toc")
    doc_id = result["doc_id"]

    tree = api._store.get_tree(doc_id)
    flat = _flatten(tree)
    # 22 genuine bookmarks survive validation, in document order.
    assert [node["node_id"] for node in flat] == [
        str(i).zfill(4) for i in range(22)]
    assert flat[0]["title"] == "Introduction"
    assert flat[0]["start_index"] == 2
    # Boundaries: starts walk forward, no node ends before it starts, and
    # the last section runs to the end of the document (a parent's end is
    # promoted to its subtree max, so only siblings share exactly).
    starts = [node["start_index"] for node in flat]
    assert starts == sorted(starts)
    assert all(node["end_index"] >= node["start_index"] for node in flat)
    meta = api._store.get_meta(doc_id)
    meta = api._store.get_meta(doc_id)
    assert flat[-1]["end_index"] == meta["pageNum"]
    # No summaries, no document description: the outline is all there is.
    assert all("summary" not in node for node in flat)
    assert meta["mode"] == "toc"
    assert meta["description"] is None
    # Formatted tree keeps the chapter start pages.
    formatted = api.get_tree(doc_id)["result"]
    assert formatted[0]["page_index"] == 2


def test_toc_storage_purity(tmp_path):
    api = _make_api(tmp_path)
    doc_id = api.submit_document(str(ATTENTION), mode="toc")["doc_id"]
    expected = api._extract_page_texts(str(ATTENTION))
    pages = api._store.get_pages(doc_id)
    assert [page["markdown"] for page in pages] == expected


# ── gate: absent / untrustworthy outlines ──


def test_toc_no_bookmarks_rejected(tmp_path, sample_pdf):
    api = _make_api(tmp_path)
    with pytest.raises(PageIndexAPIError,
                       match=r"no usable PDF bookmark outline"):
        api.submit_document(sample_pdf, mode="toc")


def test_toc_ignore_tier_rejected(tmp_path):
    # A "Page N" enumeration is filing noise, not a table of contents.
    path = _pdf_with_outline(
        tmp_path, [f"body text page {i}" for i in range(4)],
        [("Page 1", 0), ("Page 2", 1), ("Page 3", 2), ("Page 4", 3)])
    api = _make_api(tmp_path)
    with pytest.raises(PageIndexAPIError,
                       match=r"no usable PDF bookmark outline"):
        api.submit_document(path, mode="toc")


# ── blank scans with bookmarks are indexable ──


def test_toc_blank_scan_with_bookmarks_ok(tmp_path):
    # A pure scan has no text at all; with a real outline the tree comes
    # from the bookmarks, so the blank-page rejection must not fire.
    path = _pdf_with_outline(
        tmp_path, ["", ""],
        [("Introduction", 0), ("Results", 1), ("Conclusion", 1)])
    api = _make_api(tmp_path)
    result = api.submit_document(path, mode="toc")
    meta = api._store.get_meta(result["doc_id"])
    assert meta["pageNum"] == 2
    assert meta["mode"] == "toc"


# ── API surface ──


def test_unknown_mode_message_lists_toc(tmp_path, sample_pdf):
    api = _make_api(tmp_path)
    with pytest.raises(PageIndexAPIError, match=r"'toc'"):
        api.submit_document(sample_pdf, mode="bogus")


def test_cloud_rejects_toc(monkeypatch, sample_pdf):
    import types

    calls = []

    def handler(method, url):
        calls.append((method, url))
        raise AssertionError("no network call expected for toc mode")

    fake = types.SimpleNamespace(
        post=lambda url, **kw: handler("POST", url),
        get=lambda url, **kw: handler("GET", url),
        delete=lambda url, **kw: handler("DELETE", url),
    )
    monkeypatch.setattr("pageindex.cloud_api.requests", fake)
    client = PageIndexClient(api_key="secret")
    with pytest.raises(PageIndexAPIError, match=r"local-only"):
        client.submit_document(sample_pdf, mode="toc")
    assert not calls
