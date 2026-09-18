import asyncio
import sys
import threading
import types

import pypdfium2

import pageindex._pdfium_lock as pdfium_lock
from pageindex.local_api import LocalAPI
from pageindex.page_index_classic import SHORT_DOC_PAGE_THRESHOLD, verify_toc


def test_verify_toc_bypassed_for_short_docs():
    async def run():
        return await asyncio.gather(
            verify_toc(["p1"], [{"x": 1}]),
            verify_toc(["p1", "p2"], [{"x": 1}, {"x": 2}]),
            # Above the threshold the real verify runs; a TOC with no
            # physical_index takes verify_toc's own early return, so this
            # needs no LLM — it must NOT come back as the bypass value.
            verify_toc(["p1", "p2", "p3"], [{"x": 1}]),
        )

    short_one, short_two, longer = asyncio.run(run())
    assert SHORT_DOC_PAGE_THRESHOLD == 2
    assert short_one == (1.0, [])
    assert short_two == (1.0, [])
    assert longer == (0, [])


def test_pdfium_opens_are_serialized(sample_pdf):
    # Package import swaps pypdfium2.PdfDocument for the locking subclass.
    assert pypdfium2.PdfDocument is pdfium_lock._ThreadSafePdfDocument

    acquired = threading.Event()

    def second_thread_open():
        # Blocks until the first thread closes its document.
        doc = pypdfium2.PdfDocument(sample_pdf)
        acquired.set()
        doc.close()

    # First open holds the lock.
    held = pypdfium2.PdfDocument(sample_pdf)

    worker = threading.Thread(target=second_thread_open)
    worker.start()
    try:
        assert not acquired.wait(0.2), (
            "second open must block while a document is open")
    finally:
        held.close()
    assert acquired.wait(2), "second open must proceed after close"
    worker.join(2)
    assert not worker.is_alive()


def test_extract_page_texts_falls_back_per_page(monkeypatch, sample_pdf):
    # PyPDF2 dies on page 2 (malformed ToUnicode CMap shape); that page is
    # re-read with PDFium while page 1 keeps its PyPDF2 text.
    fake_pypdf2 = types.ModuleType("PyPDF2")

    class FakePage:
        def __init__(self, index):
            self.index = index

        def extract_text(self):
            if self.index == 1:
                raise UnboundLocalError("cannot access local variable 'cm'")
            return "pypdf2-p0"

    class FakeReader:
        def __init__(self, f):
            self.pages = [FakePage(0), FakePage(1)]

    fake_pypdf2.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "PyPDF2", fake_pypdf2)
    monkeypatch.setitem(sys.modules, "pypdfium2",
                        _fake_pdfium(["ignored-p0", "pdfium-p1"]))

    assert LocalAPI._extract_page_texts(sample_pdf) == ["pypdf2-p0", "pdfium-p1"]


def test_extract_page_texts_falls_back_document_level(monkeypatch, sample_pdf):
    # PyPDF2 cannot even walk the page tree; every page comes from PDFium.
    fake_pypdf2 = types.ModuleType("PyPDF2")

    class FakeReader:
        def __init__(self, f):
            raise TypeError("argument of type 'FloatObject' is not iterable")

    fake_pypdf2.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "PyPDF2", fake_pypdf2)
    monkeypatch.setitem(sys.modules, "pypdfium2",
                        _fake_pdfium(["pdfium-text", "pdfium-text"]))

    assert LocalAPI._extract_page_texts(sample_pdf) == [
        "pdfium-text", "pdfium-text"]


def _fake_pdfium(page_texts):
    """A fake pypdfium2 module whose documents carry one text per page."""
    fake = types.ModuleType("pypdfium2")

    class FakeTextPage:
        def __init__(self, text):
            self.text = text

        def get_text_range(self):
            return self.text

        def close(self):
            return None

    class FakePageObj:
        def __init__(self, text):
            self.text = text

        def get_textpage(self):
            return FakeTextPage(self.text)

    class FakePdfDocument:
        def __init__(self, *args, **kwargs):
            self.pages = [FakePageObj(text) for text in page_texts]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            return None

    fake.PdfDocument = FakePdfDocument
    return fake
