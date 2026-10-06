"""Vision pre-pass for standard-mode indexing.

Scanned and image-heavy PDFs build poor trees because the standard
pipeline reads only the text layer. Standard mode always runs this
pre-pass: the sparse pages (thin text or a large image footprint) are
rendered and shown once to the multimodal model, which returns only
what the text layer is missing — heading lines, one-line figure
captions, tables as markdown. The returned block is appended to the
page's build-time text so the unchanged standard pipeline (TOC
detection, no-TOC tree generation, verification, summaries) sees it,
while the stored page text stays the original extraction. Documents
without sparse pages pay only the local detection cost and index
exactly as before.

A second gate catches documents whose text layer is cipher garbage
(subset fonts without a ToUnicode CMap extract as glyph ids, e.g.
"#$%\\t&'()(*(+" — unreadable to any model yet dense enough to dodge
the sparse gate). The diagnosis is document-level: garbled fonts break
every page, so the median page's letter share cleanly separates them
from table-of-numbers pages in healthy documents. On a garbled
document each garbled page is fully transcribed from its image and the
transcript *replaces* the garbage in the build-time text (append
semantics stay for sparse pages).

The stage never fails a submission: LocalAPI catches everything from
``transcribe_sparse_pages`` and falls back to text-only indexing. The
pdfium work itself runs in a disposable child process (see
``_detect_and_render_guarded``).
"""
from __future__ import annotations

import base64
import contextvars
import io
import logging
import multiprocessing
import os
import re
import concurrent.futures
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from .utils import count_tokens, llm_completion

logger = logging.getLogger(__name__)

# Pages whose text layer holds fewer tokens than this are considered
# sparse (scanned pages, full-page charts). Roughly one or two lines.
MIN_TEXT_TOKENS_DEFAULT = 25
# A page also counts as sparse when image objects cover at least this
# fraction of the page area — catches "dense text around one big chart"
# pages that the token threshold alone would skip.
IMAGE_AREA_RATIO_DEFAULT = 0.2
# A page whose text layer is this short on letters (letters among
# non-whitespace characters; CJK and other scripts count as letters) is
# garbled: subset fonts without a ToUnicode CMap leak glyph ids, which
# extract as punctuation/digit soup no model can read. Only pages of at
# least GARBLED_MIN_CHARS non-whitespace characters are judged — thinner
# pages already ride the sparse gate.
GARBLED_LETTER_SHARE_DEFAULT = 0.45
GARBLED_MIN_CHARS_DEFAULT = 50

VISION_PROMPT = """You are given one page of a PDF: its rendered image and its extracted text layer.
The text layer may be empty (scanned page).

Return ONLY what the text layer is missing, as plain text:
1. Section heading/title lines visible on the page — exact strings, original
   language, keep hierarchy numbering like "2.3".
2. For each chart/diagram/figure: one line "[Figure: <one-sentence description>]".
3. For each table (including scanned table images): "[Table]" followed by a
   markdown table of its contents.
Do NOT transcribe body paragraphs already present in the text layer.
Do NOT include page headers, footers, or page numbers.
Wrap the entire output in <page-vision> ... </page-vision>.
If the page adds nothing beyond the text layer, return an empty
<page-vision></page-vision>."""

_VISION_BLOCK_RE = re.compile(r"<page-vision>\n?(.*?)\n?</page-vision>", re.DOTALL)

GARBLED_PROMPT = """You are given one page of a PDF as a rendered image.
The document's extracted text layer is unusable — its fonts carry broken
character encoding — so the page must be read from the image alone.

Transcribe ALL visible text on the page in reading order:
1. Section heading/title lines — exact strings, original language, keep
   hierarchy numbering like "2.3".
2. Body paragraphs, transcribed in full, in reading order.
3. Table of contents entries, line by line, keeping their page numbers.
4. For each chart/diagram/figure: one line "[Figure: <one-sentence description>]".
5. For each table: "[Table]" followed by a markdown table of its contents.
Do NOT include page headers, footers, or page numbers.
Wrap the entire output in <page-vision> ... </page-vision>."""

# Concurrent per-page transcribe calls; rendering stays in the caller's
# thread (the PDFium lock serializes it anyway), the network calls are
# what benefit from overlap.
TRANSCRIBE_CONCURRENCY = 4
TRANSCRIBE_TIMEOUT_SECONDS = 60

RENDER_SCALE = 1.5
JPEG_QUALITY = 80


class VisionStageError(Exception):
    """The whole vision stage failed before any page succeeded.

    LocalAPI treats this (like any exception) as "build the tree from
    the plain text layer" — the pre-pass is best-effort by contract.
    """


def _min_text_tokens() -> int:
    return int(os.environ.get("PAGEINDEX_VISION_PAGE_MIN_TOKENS",
                              MIN_TEXT_TOKENS_DEFAULT))


def _image_area_ratio_threshold() -> float:
    return float(os.environ.get("PAGEINDEX_VISION_IMAGE_AREA_RATIO",
                                IMAGE_AREA_RATIO_DEFAULT))


def _garbled_letter_share_threshold() -> float:
    return float(os.environ.get("PAGEINDEX_VISION_GARBLED_LETTER_SHARE",
                                GARBLED_LETTER_SHARE_DEFAULT))


def _garbled_min_chars() -> int:
    return int(os.environ.get("PAGEINDEX_VISION_GARBLED_MIN_CHARS",
                              GARBLED_MIN_CHARS_DEFAULT))


def is_sparse_page(page_text: str, image_area_ratio: float) -> bool:
    if count_tokens(page_text) < _min_text_tokens():
        return True
    return image_area_ratio >= _image_area_ratio_threshold()


def _page_letter_share(page_text: str) -> float | None:
    """Letters among non-whitespace characters; None for pages too short
    to judge (the sparse gate already covers those)."""
    nonspace = [c for c in page_text if not c.isspace()]
    if len(nonspace) < _garbled_min_chars():
        return None
    letters = sum(1 for c in nonspace if c.isalpha())
    return letters / len(nonspace)


def is_garbled_page(page_text: str) -> bool:
    share = _page_letter_share(page_text)
    return share is not None and share < _garbled_letter_share_threshold()


def document_is_garbled(page_texts: list[str]) -> bool:
    """The document-level diagnosis that arms the garbled gate.

    A broken font garbles every page it touches, so the MEDIAN page's
    letter share sits far below any healthy document's — including
    table-of-numbers-heavy ones, whose median page is still prose. A
    per-page test cannot make that call (a dense numeric table page is
    letter-poor too), so only a doc-level verdict engages the gate and
    spares healthy documents' odd symbol-dense pages the transcription
    cost.
    """
    shares = [share for share in map(_page_letter_share, page_texts)
              if share is not None]
    if not shares:
        return False
    shares.sort()
    mid = len(shares) // 2
    median = (shares[mid] if len(shares) % 2
              else (shares[mid - 1] + shares[mid]) / 2)
    return median < _garbled_letter_share_threshold()


def page_image_area_ratio(doc: Any, index: int) -> float:
    """Fraction of the page covered by image objects.

    Overlapping image objects double-count; fine for a threshold test.
    Zero-area objects (clips, invisible placeholders) are skipped.
    """
    import pypdfium2.raw as pdfium_c

    try:
        # doc[index] itself can raise for a damaged page object; keep it
        # inside the try so detection never aborts the whole pre-pass.
        page = doc[index]
        page_width, page_height = page.get_size()
        page_area = page_width * page_height
        if page_area <= 0:
            return 0.0
        images = page.get_objects(max_depth=2,
                                  filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE])
        covered = 0.0
        for image in images:
            x0, y0, x1, y1 = image.get_bounds()
            covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        return covered / page_area
    except Exception as exc:
        # Unreadable page objects must not block indexing; treat the
        # page as image-less (its text layer still feeds the pipeline).
        logger.warning("image-area detection failed on page %d (%s)",
                       index + 1, exc)
        return 0.0


def render_page_jpeg(doc: Any, index: int) -> str:
    """Render one page to a base64 JPEG data URL at indexing resolution."""
    image = doc[index].render(scale=RENDER_SCALE).to_pil().convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(
        buffer.getvalue()).decode("ascii")


def render_page_png(doc: Any, index: int, dpi: float) -> str:
    """Render one page to bare base64 PNG (no data: prefix — MCP image
    blocks carry the raw payload) at the given print resolution
    (scale = dpi / 72, pdfium's 1.0 being 72 DPI)."""
    image = doc[index].render(scale=dpi / 72).to_pil().convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def transcribe_page(model: str | None, page_text: str, image_data_url: str,
                    garbled: bool = False) -> str:
    """One VLM call: page image + text layer → the missing structure text.

    A garbled page is read from the image alone (full transcription) —
    its text layer is cipher garbage that would only mislead, so it is
    not sent at all. Returns "" when the model reports nothing beyond
    the text layer.
    """
    if garbled:
        prompt = (GARBLED_PROMPT
                  + "\n\nText layer of this page:\n(unusable — garbled "
                    "font encoding)")
    else:
        prompt = (VISION_PROMPT + "\n\nText layer of this page:\n"
                  + (page_text if page_text.strip()
                     else "(empty — scanned page)"))
    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]
    response = llm_completion(model=model, prompt=content,
                              timeout=TRANSCRIBE_TIMEOUT_SECONDS)
    match = _VISION_BLOCK_RE.search(response or "")
    if match is None:
        logger.debug("vision transcription returned no <page-vision> block")
        return ""
    return match.group(1).strip()


def _detect_and_render(
        file_path: str,
        page_texts: list[str],
) -> tuple[list[int], dict[int, str], set[int], bool]:
    """All pdfium work of the pre-pass: detection + page rendering.

    Returns ``(targets, renders, garbled, doc_garbled)``: the pages to
    transcribe (sparse ∪ garbled), their JPEG renders, which of them
    are garbled (full-transcription prompt, replacement semantics), and
    the document-level verdict that armed the garbled gate at all.

    This function is the subprocess target of
    :func:`_detect_and_render_guarded`: it is expected to die natively
    on documents pdfium cannot survive. Pages that fail cleanly
    (``PdfiumError`` on load/render) are skipped individually — they
    are permanently untranscribable but say nothing about the model.
    """
    import pypdfium2 as pdfium

    doc_garbled = document_is_garbled(page_texts)
    garbled: set[int] = set()
    doc = pdfium.PdfDocument(file_path)
    try:
        targets: list[int] = []
        for index in range(len(page_texts)):
            page_garbled = doc_garbled and is_garbled_page(page_texts[index])
            if page_garbled:
                garbled.add(index)
            if page_garbled or is_sparse_page(page_texts[index],
                                              page_image_area_ratio(doc, index)):
                targets.append(index)
        renders: dict[int, str] = {}
        for index in targets:
            try:
                renders[index] = render_page_jpeg(doc, index)
            except Exception as exc:
                logger.warning(
                    "vision pre-pass: page %d cannot be rendered (%s: %s); "
                    "skipping its transcription",
                    index + 1, type(exc).__name__, exc)
    finally:
        doc.close()
    return targets, renders, garbled, doc_garbled


def _detect_and_render_guarded(
        file_path: str,
        page_texts: list[str],
) -> tuple[list[int], dict[int, str], set[int], bool] | None:
    """Run :func:`_detect_and_render` in a disposable child process.

    Damaged documents can crash pdfium natively (SIGSEGV), which Python
    cannot catch: the work runs in a spawned single-worker pool whose
    death (``BrokenProcessPool``) the parent merely observes, returning
    ``None`` so the stage degrades to text-only indexing. The parent
    cannot intercept the segfault itself; it only sees the pool failure.
    """
    try:
        # Reuse flash's spawn guard: spawn re-executes the caller's
        # __main__ in the child, which for the service entrypoint would
        # re-run module-level server startup.
        from .flash.parser_pdfium_parallel import _anonymous_main

        with _anonymous_main(), ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn")) as executor:
            return executor.submit(_detect_and_render,
                                   file_path, page_texts).result()
    except Exception as exc:
        logger.warning(
            "vision pre-pass: pdfium child failed (%s: %s); building from "
            "the text layer only", type(exc).__name__, exc)
        return None


def transcribe_sparse_pages(file_path: str, page_texts: list[str],
                            model: str | None = None) -> list[str]:
    """Visually transcribe the document's sparse — or garbled — pages.

    Pages whose text layer is thin (or that are mostly image) are
    rendered and shown to the multimodal model, which returns the
    structure the text layer is missing; that transcription is appended
    to the page's build-time text. On a document whose text layer is
    cipher garbage (see :func:`document_is_garbled`), the garbled pages
    are fully transcribed from their images and each transcript
    *replaces* that page's text. Either way the result is a list
    parallel to ``page_texts`` with a ``<page-vision>`` block on
    transcribed pages — one entry per input page.

    Failure semantics: all pdfium work runs in a disposable child
    process; if that child fails or dies natively (a damaged document
    can segfault pdfium), the stage degrades to text-only indexing.
    Pages that fail cleanly (pdfium cannot load/render them) are
    skipped individually. For the model calls: if they fail and not a
    single one has succeeded, the stage is treated as broken
    (unsupported model, unreachable endpoint) and raises
    :class:`VisionStageError` without launching further calls. After
    any success, an individual page failure just leaves that page
    without a transcription.
    """
    detected = _detect_and_render_guarded(file_path, page_texts)
    if detected is None:
        return list(page_texts)
    targets, renders, garbled, doc_garbled = detected
    if not targets:
        logger.info("vision pre-pass: no sparse pages")
        return list(page_texts)
    if not renders:
        logger.warning("vision pre-pass: no sparse page could be rendered")
        return list(page_texts)
    renderable = sorted(renders)
    total = len(page_texts)
    if doc_garbled:
        logger.info("vision pre-pass: garbled text layer (%d pages); "
                    "transcribing %d of %d pages",
                    len(garbled), len(renderable), total)
    else:
        logger.info("vision pre-pass: transcribing %d of %d pages",
                    len(renderable), total)

    transcripts: dict[int, str] = {}
    # The indexing lane's connection overrides ride a contextvar set in
    # this thread (LocalAPI._with_backend); pool threads start with an
    # empty context. A Context can only be entered by one thread at a
    # time, so every transcribe call gets its own shallow copy.
    ctx = contextvars.copy_context()
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=TRANSCRIBE_CONCURRENCY,
        thread_name_prefix="pageindex-vision")
    try:
        futures = {index: pool.submit(ctx.copy().run, transcribe_page, model,
                                      page_texts[index], renders[index],
                                      index in garbled)
                   for index in renderable}
        any_success = False
        for index in renderable:
            try:
                transcripts[index] = futures[index].result()
                any_success = True
            except Exception as exc:
                if not any_success:
                    for future in futures.values():
                        future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise VisionStageError(
                        f"vision transcription failed on page {index + 1} "
                        f"with no prior success: {exc}") from exc
                logger.warning(
                    "vision transcription failed on page %d (%s: %s); that "
                    "page gets no transcription", index + 1,
                    type(exc).__name__, exc)
                transcripts[index] = ""
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    build_texts: list[str] = []
    for index, text in enumerate(page_texts):
        transcript = transcripts.get(index, "").strip()
        if transcript and index in garbled:
            # The text layer here is cipher garbage; the transcript
            # replaces it instead of appending — residue would only
            # mislead TOC detection and bloat every later prompt.
            build_texts.append(
                f"<page-vision>\n{transcript}\n</page-vision>")
        elif transcript:
            build_texts.append(
                f"{text}\n\n<page-vision>\n{transcript}\n</page-vision>")
        else:
            build_texts.append(text)
    return build_texts
