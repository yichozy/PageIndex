"""Serialize all in-process pypdfium2 document usage behind one lock.

PDFium's FFI is not thread-safe — not even for concurrent opens of
*different* documents (pypdfium2's own documentation and issue tracker
are explicit about this; the SDK's parallel parser sidesteps it with a
process pool). Any embedding that calls ``pdfium.PdfDocument()`` from
multiple threads — e.g. a queue worker with concurrency > 1 — races on
PDFium's shared internal state, and concurrent loads fail with
"Data format error" even though the files are valid.

The lock is held for the whole document lifetime (acquired on open,
released on close), so page-level calls are serialized too, not just
load/close. Construction-only locking is not sufficient (pypdfium2
issue #309). Release has two paths, whichever fires first:

  - ``close()``: the normal path for successful extraction;
  - ``weakref.finalize``: safety net for exception paths that skip
    ``close()``, releasing the lock when the abandoned object is
    garbage-collected.

The lock is re-entrant for the owning thread, so same-thread nesting
(e.g. validation opening a second document) cannot self-deadlock.

It cannot be a ``threading.RLock``: finalizers run on whatever thread
drops the last reference, which is not necessarily the opening thread.
If an exception raised inside a pipeline on a worker thread is re-raised
on the caller's event-loop thread, releasing that exception releases the
abandoned document's frame — the finalizer then fires off the owning
thread, where ``RLock.release()`` raises ``RuntimeError`` and the lock
would be held forever. So the lock below is owner-agnostic: a condition
variable plus per-thread hold counts, re-entrant for the owner,
releasable from any thread.

Applied once at package import (see ``pageindex/__init__.py``), which is
guaranteed to run before any submodule — including the lazily imported
flash pipeline — touches ``pypdfium2``. Set
``PAGEINDEX_DISABLE_PDFIUM_LOCK=1`` to opt out (single-threaded
embeddings that want the last bit of parallelism): the swap below is
skipped entirely and the stock ``PdfDocument`` stays in place.
"""

import os
import threading
import weakref

import pypdfium2 as pdfium

cond = threading.Condition()
holds: dict[int, int] = {}  # thread ident -> documents it holds open


def _acquire() -> int:
    me = threading.get_ident()
    with cond:
        while holds and me not in holds:
            cond.wait()
        holds[me] = holds.get(me, 0) + 1
        return me


def _release(cell: list | None) -> None:
    # Check-and-clear the cell so exactly one of the two release paths
    # (close vs finalizer) lets go of the lock. The owner ident rides
    # in the cell because release may run on a different thread.
    if cell is None or not cell[0]:
        return
    cell[0] = False
    owner = cell[1]
    with cond:
        remaining = holds.get(owner, 0) - 1
        if remaining > 0:
            holds[owner] = remaining
        else:
            holds.pop(owner, None)
        if not holds:
            cond.notify_all()


class _ThreadSafePdfDocument(pdfium.PdfDocument):
    def __init__(self, *args, **kwargs):
        owner = _acquire()
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            _release([True, owner])
            raise
        cell = [True, owner]
        self._pdfium_lock_cell = cell
        weakref.finalize(self, _release, cell)

    def close(self, *args, **kwargs):
        try:
            return super().close(*args, **kwargs)
        finally:
            cell = getattr(self, "_pdfium_lock_cell", None)
            if cell is not None:
                self._pdfium_lock_cell = None
                _release(cell)


def apply() -> None:
    """Swap ``pypdfium2.PdfDocument`` for the lock-wrapping subclass."""
    if os.getenv("PAGEINDEX_DISABLE_PDFIUM_LOCK") == "1":
        return
    pdfium.PdfDocument = _ThreadSafePdfDocument
