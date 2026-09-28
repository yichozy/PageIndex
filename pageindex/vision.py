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

The stage never fails a submission: LocalAPI catches everything from
``transcribe_sparse_pages`` and falls back to text-only indexing.
"""
from __future__ import annotations

import base64
import contextvars
import io
import logging
import os
import re
import concurrent.futures
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


def is_sparse_page(page_text: str, image_area_ratio: float) -> bool:
    if count_tokens(page_text) < _min_text_tokens():
        return True
    return image_area_ratio >= _image_area_ratio_threshold()


def page_image_area_ratio(doc: Any, index: int) -> float:
    """Fraction of the page covered by image objects.

    Overlapping image objects double-count; fine for a threshold test.
    Zero-area objects (clips, invisible placeholders) are skipped.
    """
    import pypdfium2.raw as pdfium_c

    page = doc[index]
    try:
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


def transcribe_page(model: str | None, page_text: str,
                    image_data_url: str) -> str:
    """One VLM call: page image + text layer → the missing structure text.

    Returns "" when the model reports nothing beyond the text layer.
    """
    content: list[dict] = [
        {"type": "text",
         "text": VISION_PROMPT + "\n\nText layer of this page:\n"
                 + (page_text if page_text.strip()
                    else "(empty — scanned page)")},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]
    response = llm_completion(model=model, prompt=content,
                              timeout=TRANSCRIBE_TIMEOUT_SECONDS)
    match = _VISION_BLOCK_RE.search(response or "")
    if match is None:
        logger.debug("vision transcription returned no <page-vision> block")
        return ""
    return match.group(1).strip()


def transcribe_sparse_pages(file_path: str, page_texts: list[str],
                            model: str | None = None) -> list[str]:
    """Visually transcribe the document's sparse pages.

    Pages whose text layer is thin (or that are mostly image) are
    rendered and shown to the multimodal model, which returns the
    structure the text layer is missing. The transcription is appended
    to the page's build-time text: the result is a list parallel to
    ``page_texts`` with a trailing ``<page-vision>`` block on
    transcribed pages — one entry per input page, transcriptions only
    ever appended.

    Failure semantics: if page calls fail and not a single one has
    succeeded, the stage is treated as broken (unsupported model,
    unreachable endpoint) and raises :class:`VisionStageError` without
    launching further calls. After any success, an individual page
    failure just leaves that page without a transcription.
    """
    import pypdfium2 as pdfium

    total = len(page_texts)
    doc = pdfium.PdfDocument(file_path)
    try:
        sparse: list[int] = []
        for index in range(total):
            if is_sparse_page(page_texts[index],
                              page_image_area_ratio(doc, index)):
                sparse.append(index)
        renders = {index: render_page_jpeg(doc, index)
                   for index in sparse}
    finally:
        doc.close()
    if not sparse:
        logger.info("vision pre-pass: no sparse pages")
        return list(page_texts)
    logger.info("vision pre-pass: transcribing %d of %d pages",
                len(sparse), total)

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
                                      page_texts[index], renders[index])
                   for index in sparse}
        any_success = False
        for index in sparse:
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
        if transcript:
            build_texts.append(
                f"{text}\n\n<page-vision>\n{transcript}\n</page-vision>")
        else:
            build_texts.append(text)
    return build_texts
