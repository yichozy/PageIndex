"""Vision pre-pass: sparse detection, transcription assembly, failure
semantics, and the LocalAPI wiring around pageindex.vision.

All env thresholds are read at call time, so monkeypatch.setenv is
hermetic. VLM calls and page rendering are faked; the PDF itself is
real (conftest's build_pdf) so pdfium page walking runs for real.
"""
import pytest

import pageindex.vision as vision_mod
from pageindex.errors import PageIndexAPIError
from pageindex.local_api import LocalAPI

PAGE_TEXTS = ["Hello page one about apples", "Second page about bananas"]


@pytest.fixture(autouse=True)
def _pdfium_in_process(monkeypatch, request):
    """Run the pre-pass's pdfium work in-process.

    In production ``_detect_and_render`` executes in a spawn child; a
    child re-imports pageindex.vision fresh, so the monkeypatched
    primitives below would not apply there (and per-test spawning would
    be slow). Tests exercise ``_detect_and_render`` directly; the guarded
    wrapper gets its own dedicated tests, marked ``real_guard`` to keep
    the production wrapper in place.
    """
    if request.node.get_closest_marker("real_guard"):
        return
    monkeypatch.setattr(vision_mod, "_detect_and_render_guarded",
                        lambda fp, texts:
                        vision_mod._detect_and_render(fp, texts))


def _make_api(tmp_path):
    return LocalAPI(str(tmp_path / "storage"), model="test-model",
                    summary_model="test-model")


def _zero_tokens(text, model=None):
    return 0


# ── sparse detection ──


def test_sparse_page_thresholds(monkeypatch):
    monkeypatch.delenv("PAGEINDEX_VISION_PAGE_MIN_TOKENS", raising=False)
    monkeypatch.delenv("PAGEINDEX_VISION_IMAGE_AREA_RATIO", raising=False)
    monkeypatch.setattr(vision_mod, "count_tokens",
                        lambda text, model=None: len(text.split()))
    thin = "a few words"
    dense = " ".join(["word"] * 25)
    assert vision_mod.is_sparse_page(thin, 0.0) is True        # 3 < 25 tokens
    assert vision_mod.is_sparse_page(dense, 0.0) is False      # 25 not < 25
    assert vision_mod.is_sparse_page(dense, 0.199) is False    # small logo
    assert vision_mod.is_sparse_page(dense, 0.2) is True       # big chart
    assert vision_mod.is_sparse_page(dense, 0.9) is True


def test_env_thresholds_read_at_call_time(monkeypatch):
    dense = "word " * 200  # comfortably above any small token threshold
    monkeypatch.delenv("PAGEINDEX_VISION_PAGE_MIN_TOKENS", raising=False)
    monkeypatch.delenv("PAGEINDEX_VISION_IMAGE_AREA_RATIO", raising=False)
    assert vision_mod.is_sparse_page(dense, 0.0) is False
    monkeypatch.setenv("PAGEINDEX_VISION_PAGE_MIN_TOKENS", "1")
    assert vision_mod.is_sparse_page(dense, 0.0) is False   # >= 1 token
    monkeypatch.setenv("PAGEINDEX_VISION_IMAGE_AREA_RATIO", "0.3")
    assert vision_mod.is_sparse_page(dense, 0.25) is False
    assert vision_mod.is_sparse_page(dense, 0.35) is True


def test_page_image_area_ratio_no_images(sample_pdf):
    import pypdfium2
    doc = pypdfium2.PdfDocument(sample_pdf)
    try:
        assert vision_mod.page_image_area_ratio(doc, 0) == 0.0
        assert vision_mod.page_image_area_ratio(doc, 1) == 0.0
    finally:
        doc.close()


def test_page_image_area_ratio_with_image(tmp_path):
    # A real PDF with a half-page image: the ratio must actually be
    # computed (guards the pypdfium2 5.x get_objects(filter=) +
    # get_bounds() API usage against silent exception fallback).
    # The doc is saved and re-opened because unsaved in-memory documents
    # do not serve page objects back through doc[i].
    import pypdfium2 as pdfium
    from PIL import Image

    image_path = tmp_path / "dot.jpg"
    pdf_path = tmp_path / "with_image.pdf"
    Image.new("RGB", (60, 30), "red").save(image_path)

    doc = pdfium.PdfDocument.new()
    try:
        page = doc.new_page(width=612, height=792)
        image = pdfium.PdfImage.new(doc)
        image.load_jpeg(str(image_path))
        image.set_matrix(pdfium.PdfMatrix(306, 0, 0, 396, 153, 198))
        page.insert_obj(image)
        page.gen_content()
        doc.save(str(pdf_path))
    finally:
        doc.close()

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        ratio = vision_mod.page_image_area_ratio(doc, 0)
    finally:
        doc.close()
    assert 0.2 < ratio < 0.26  # 306*396 / (612*792) = 0.25


# ── transcribe call + parsing ──


def test_transcribe_page_parses_block(monkeypatch):
    captured = {}

    def fake_completion(model, prompt, timeout=None):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["timeout"] = timeout
        return "junk before\n<page-vision>\n[Table]\n| a | b |\n</page-vision>"

    monkeypatch.setattr(vision_mod, "llm_completion", fake_completion)
    out = vision_mod.transcribe_page("test-model", "text layer", "data:img")
    assert out == "[Table]\n| a | b |"
    assert captured["model"] == "test-model"
    assert captured["timeout"] == vision_mod.TRANSCRIBE_TIMEOUT_SECONDS
    text_part, image_part = captured["prompt"]
    assert "text layer" in text_part["text"]
    assert image_part == {"type": "image_url",
                          "image_url": {"url": "data:img"}}


def test_transcribe_page_empty_text_layer_placeholder(monkeypatch):
    captured = {}

    def fake_completion(model, prompt, timeout=None):
        captured["prompt"] = prompt
        return "<page-vision>\n1. Heading\n</page-vision>"

    monkeypatch.setattr(vision_mod, "llm_completion", fake_completion)
    out = vision_mod.transcribe_page("m", "   \n", "data:img")
    assert out == "1. Heading"
    assert "(empty — scanned page)" in captured["prompt"][0]["text"]


def test_transcribe_page_no_block_returns_empty(monkeypatch):
    monkeypatch.setattr(vision_mod, "llm_completion",
                        lambda model, prompt, timeout=None: "no tags")
    assert vision_mod.transcribe_page("m", "t", "d") == ""


def test_render_page_jpeg_data_url(sample_pdf):
    import pypdfium2
    doc = pypdfium2.PdfDocument(sample_pdf)
    try:
        url = vision_mod.render_page_jpeg(doc, 0)
    finally:
        doc.close()
    assert url.startswith("data:image/jpeg;base64,")


# ── transcribe: assembly + failure semantics ──


def test_transcribe_assembly_alignment(monkeypatch, sample_pdf):
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"data:img{index}")

    def fake_transcribe(model, page_text, image_data_url):
        if image_data_url == "data:img0":
            return "1. Heading Alpha\n[Figure: chart]"
        return ""  # page 2 adds nothing

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    out = vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)
    assert len(out) == len(PAGE_TEXTS)
    assert out[0] == (PAGE_TEXTS[0]
                      + "\n\n<page-vision>\n1. Heading Alpha\n"
                        "[Figure: chart]\n</page-vision>")
    assert out[1] == PAGE_TEXTS[1]
    assert "<page-vision>" not in out[1]


def test_first_failure_aborts_stage(monkeypatch, sample_pdf):
    # Page 0 fails and no page has succeeded yet: the stage is treated as
    # broken even though page 1 would have succeeded.
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"data:img{index}")

    def fake_transcribe(model, page_text, image_data_url):
        if image_data_url == "data:img0":
            raise RuntimeError("model does not support images")
        return "transcript one"

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    with pytest.raises(vision_mod.VisionStageError):
        vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)


def test_abort_cancels_pending_calls(monkeypatch, tmp_path):
    # With one worker the pages run sequentially; after page 0 fails the
    # queued pages must be cancelled, so at most the in-flight page can
    # still fire — never all of them.
    from conftest import build_pdf
    path = tmp_path / "five.pdf"
    path.write_bytes(build_pdf([f"page {i}" for i in range(5)]))
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "TRANSCRIBE_CONCURRENCY", 1)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"data:img{index}")
    calls = []

    def fake_transcribe(model, page_text, image_data_url):
        calls.append(image_data_url)
        if image_data_url == "data:img0":
            raise RuntimeError("boom")
        import time
        time.sleep(0.01)
        return "cap"

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    with pytest.raises(vision_mod.VisionStageError):
        vision_mod.transcribe_sparse_pages(str(path),
                                     [f"page {i}" for i in range(5)])
    assert len(calls) <= 2


def test_transcribe_threads_inherit_llm_backend(monkeypatch, sample_pdf):
    # The indexing lane's overrides ride a contextvar; transcribe calls
    # run in pool threads, which must each see the captured context. The
    # barrier forces both pages in-flight at once: a single shared
    # Context would raise "cannot enter context: already entered".
    import threading
    from pageindex.utils import _llm_backend
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"img{index}")
    seen = {}
    barrier = threading.Barrier(2, timeout=5)

    def fake_transcribe(model, page_text, image_data_url):
        seen.setdefault("backend", _llm_backend.get())
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return "cap"

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    token = _llm_backend.set({"base_url": "http://x", "api_key": "k"})
    try:
        out = vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)
    finally:
        _llm_backend.reset(token)
    assert seen["backend"] == {"base_url": "http://x", "api_key": "k"}
    assert all("<page-vision>" in text for text in out)


def test_single_page_failure_skips_that_page(monkeypatch, sample_pdf):
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"data:img{index}")

    def fake_transcribe(model, page_text, image_data_url):
        if image_data_url == "data:img0":
            return "transcript zero"
        raise RuntimeError("transient")

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    out = vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)
    assert out[0].endswith("<page-vision>\ntranscript zero\n</page-vision>")
    assert out[1] == PAGE_TEXTS[1]


def test_no_page_cap_all_sparse_processed(monkeypatch, tmp_path):
    from conftest import build_pdf
    n = 51
    path = tmp_path / "many.pdf"
    path.write_bytes(build_pdf([f"page {i}" for i in range(n)]))
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)
    monkeypatch.setattr(vision_mod, "render_page_jpeg",
                        lambda doc, index: f"data:img{index}")
    monkeypatch.setattr(vision_mod, "transcribe_page",
                        lambda model, text, image: f"cap {image}")
    out = vision_mod.transcribe_sparse_pages(str(path),
                                       [f"page {i}" for i in range(n)])
    assert len(out) == n
    assert all(f"cap data:img{i}" in out[i] for i in range(n))


def test_no_sparse_pages_passthrough(monkeypatch, sample_pdf):
    # Dense text, no images: nothing is transcribed, texts unchanged.
    monkeypatch.setattr(vision_mod, "count_tokens",
                        lambda text, model=None: 1000)

    def fail(*args, **kwargs):
        raise AssertionError("no VLM call expected")

    monkeypatch.setattr(vision_mod, "transcribe_page", fail)
    out = vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)
    assert out == PAGE_TEXTS


# ── LocalAPI wiring ──


def test_index_standard_maybe_vision_passes_build_texts(monkeypatch, tmp_path,
                                                        sample_pdf):
    api = _make_api(tmp_path)
    monkeypatch.setattr(
        vision_mod, "transcribe_sparse_pages",
        lambda fp, texts, model=None: [t + " VIS" for t in texts])
    seen = {}

    def fake_index(self, file_path, texts):
        seen["texts"] = texts
        seen["model"] = model = api._model
        return [{"title": "t", "start_index": 1, "end_index": 2}], "d"

    monkeypatch.setattr(LocalAPI, "_index_standard", fake_index)
    structure, description = api._index_standard_maybe_vision(
        sample_pdf, ["a", "b"])
    assert seen["texts"] == ["a VIS", "b VIS"]
    assert seen["model"] == "test-model"
    assert description == "d"
    assert structure[0]["title"] == "t"


def test_vision_failure_falls_back_to_text_only(monkeypatch, tmp_path,
                                                sample_pdf):
    api = _make_api(tmp_path)

    def boom(fp, texts, model=None):
        raise vision_mod.VisionStageError("broken")

    monkeypatch.setattr(vision_mod, "transcribe_sparse_pages", boom)
    seen = {}

    def fake_index(self, fp, texts):
        seen["texts"] = texts
        return [], None

    monkeypatch.setattr(LocalAPI, "_index_standard", fake_index)
    api._index_standard_maybe_vision(sample_pdf, ["a"])
    assert seen["texts"] == ["a"]


def test_scan_auto_triggers_without_any_flag(monkeypatch, tmp_path,
                                             sample_pdf):
    api = _make_api(tmp_path)
    called = {}
    monkeypatch.setattr(
        vision_mod, "transcribe_sparse_pages",
        lambda fp, texts, model=None: called.setdefault("texts", texts))
    monkeypatch.setattr(
        LocalAPI, "_index_standard",
        lambda self, fp, texts: ([], None))

    api._index_standard_maybe_vision(sample_pdf, ["", "  "])
    assert "texts" in called


def test_dense_text_still_passes_through_transcribe(monkeypatch, tmp_path,
                                                    sample_pdf):
    # No flag exists anymore: every standard index runs
    # transcribe_sparse_pages; sparse detection inside it decides
    # nothing needs transcribing.
    api = _make_api(tmp_path)
    seen = {}
    monkeypatch.setattr(
        vision_mod, "transcribe_sparse_pages",
        lambda fp, texts, model=None: seen.setdefault("texts", texts))
    monkeypatch.setattr(
        LocalAPI, "_index_standard",
        lambda self, fp, texts: ([], None))

    api._index_standard_maybe_vision(sample_pdf, ["plenty of text"])
    assert seen["texts"] == ["plenty of text"]


def test_submit_dense_text_makes_no_transcribe_calls(monkeypatch, tmp_path,
                                                     sample_pdf):
    # Full submit path with dense text: the pre-pass must not fire any
    # VLM call (local detection cost only).
    api = _make_api(tmp_path)
    monkeypatch.setattr(vision_mod, "count_tokens",
                        lambda text, model=None: 1000)

    def fail(*args, **kwargs):
        raise AssertionError("no VLM call expected for dense text")

    monkeypatch.setattr(vision_mod, "transcribe_page", fail)
    monkeypatch.setattr(
        LocalAPI, "_index_standard",
        lambda self, fp, texts: (
            [{"title": "t", "start_index": 1, "end_index": 2}], "d"))
    result = api.submit_document(sample_pdf, mode="standard")
    assert api._store.get_meta(result["doc_id"])["mode"] == "standard"


def test_submit_storage_purity(monkeypatch, tmp_path, sample_pdf):
    api = _make_api(tmp_path)
    monkeypatch.setattr(
        vision_mod, "transcribe_sparse_pages",
        lambda fp, texts, model=None: [
            t + "\n\n<page-vision>\nTRANSCRIPT\n</page-vision>" for t in texts])
    monkeypatch.setattr(
        LocalAPI, "_index_standard",
        lambda self, fp, texts: (
            [{"title": "t", "start_index": 1, "end_index": 2}], "d"))
    result = api.submit_document(sample_pdf, mode="standard")
    pages = api._store.get_pages(result["doc_id"])
    assert [p["markdown"] for p in pages] == PAGE_TEXTS
    assert all("<page-vision>" not in p["markdown"] for p in pages)
    assert api._store.get_meta(result["doc_id"])["mode"] == "standard"


def test_blank_pdf_allowed_in_standard_mode(monkeypatch, tmp_path):
    # A pure scan has no text at all; standard mode must let it through
    # to the vision pre-pass instead of the blank-page rejection.
    from conftest import build_pdf
    path = tmp_path / "scan.pdf"
    path.write_bytes(build_pdf(["", ""]))
    api = _make_api(tmp_path)
    monkeypatch.setattr(
        vision_mod, "transcribe_sparse_pages",
        lambda fp, texts, model=None: [
            "<page-vision>\nscanned heading\n</page-vision>",
            "<page-vision>\n2nd\n</page-vision>"])
    monkeypatch.setattr(
        LocalAPI, "_index_standard",
        lambda self, fp, texts: (
            [{"title": "t", "start_index": 1, "end_index": 2}], "d"))
    result = api.submit_document(str(path), mode="standard")
    assert api._store.get_meta(result["doc_id"])["pageNum"] == 2


def test_blank_pdf_still_rejected_in_flash_mode(tmp_path):
    from conftest import build_pdf
    path = tmp_path / "scan.pdf"
    path.write_bytes(build_pdf(["", ""]))
    api = _make_api(tmp_path)
    with pytest.raises(PageIndexAPIError,
                       match=r"all pages are blank.*mode='standard'"):
        api.submit_document(str(path), mode="flash")


def test_render_failure_skips_only_that_page(monkeypatch, sample_pdf):
    # A damaged page (pdfium cannot load/render it) is page-local damage:
    # it must be skipped without aborting transcription for the document's
    # other sparse pages (regression: one broken page poisoned the whole
    # stage at render time, before any model call).
    monkeypatch.setattr(vision_mod, "count_tokens", _zero_tokens)

    def fake_render(doc, index):
        if index == 0:
            raise RuntimeError("Failed to load page.")
        return f"data:img{index}"

    monkeypatch.setattr(vision_mod, "render_page_jpeg", fake_render)

    def fake_transcribe(model, page_text, image_data_url):
        return "recovered transcript"

    monkeypatch.setattr(vision_mod, "transcribe_page", fake_transcribe)
    out = vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS)
    assert len(out) == len(PAGE_TEXTS)
    assert "<page-vision>" not in out[0]
    assert "<page-vision>\nrecovered transcript\n</page-vision>" in out[1]


# ── subprocess isolation ──


def test_child_death_degrades_to_text_only(monkeypatch, sample_pdf):
    # A native pdfium crash kills the child (the guard returns None);
    # the pre-pass must then return the texts unchanged — no VLM call,
    # no exception to the caller.
    monkeypatch.setattr(vision_mod, "_detect_and_render_guarded",
                        lambda fp, texts: None)

    def fail(*args, **kwargs):
        raise AssertionError("no VLM call expected after child death")

    monkeypatch.setattr(vision_mod, "transcribe_page", fail)
    assert vision_mod.transcribe_sparse_pages(sample_pdf, PAGE_TEXTS) == PAGE_TEXTS


@pytest.mark.real_guard
def test_guard_returns_none_when_pool_breaks(monkeypatch, sample_pdf):
    # A segfaulting child surfaces as BrokenProcessPool; the guard must
    # swallow it and report None (not raise) so the stage degrades.
    from concurrent.futures.process import BrokenProcessPool

    class _BrokenPool:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def submit(self, *args, **kwargs):
            raise BrokenProcessPool("child died")

    monkeypatch.setattr(vision_mod, "ProcessPoolExecutor", _BrokenPool)
    assert vision_mod._detect_and_render_guarded(sample_pdf, PAGE_TEXTS) is None


@pytest.mark.real_guard
def test_guard_runs_detection_in_real_child(sample_pdf):
    # End-to-end spawn check: the guard's child must import the package,
    # open the document, and return (sparse, renders) through the pool.
    dense = " ".join(["word"] * 40)
    sparse_list, renders = vision_mod._detect_and_render_guarded(
        sample_pdf, [dense, dense])
    # conftest's sample_pdf is two text-only pages: nothing sparse.
    assert sparse_list == []
    assert renders == {}
