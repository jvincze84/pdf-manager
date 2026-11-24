"""Background workers: text extraction (automatic) and OCR (on demand)."""
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

import pymupdf

from . import dates, db

log = logging.getLogger("pdfmanager.workers")

OCR_LANG = os.environ.get("OCR_LANG", "eng+hun")
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))
OCR_PARALLEL = max(1, int(os.environ.get("OCR_PARALLEL", "2")))

_text_event = threading.Event()
_ocr_event = threading.Event()
_cancel_lock = threading.Lock()
_cancelled: set[int] = set()

# id -> {"done": int, "total": int}
ocr_progress: dict[int, dict] = {}

# Change log for the UI poller: (seq, doc_id) entries, doc_id None = "many rows changed, reload".
_changes_lock = threading.Lock()
_changes: deque = deque(maxlen=5000)
_seq = 0


def mark_changed(*doc_ids: int, reload: bool = False):
    global _seq
    with _changes_lock:
        for d in (doc_ids if not reload else (None,)):
            _seq += 1
            _changes.append((_seq, d))


def changes_since(since: int) -> dict:
    """What changed after `since`: {"seq", "ids", "reload"}."""
    with _changes_lock:
        seq = _seq
        if since > seq or (_changes and since < _changes[0][0] - 1):
            return {"seq": seq, "ids": [], "reload": True}  # server restarted / log overflow
        ids, reload = set(), False
        for s, d in reversed(_changes):
            if s <= since:
                break
            if d is None:
                reload = True
            else:
                ids.add(d)
    return {"seq": seq, "ids": sorted(ids), "reload": reload}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wake_text():
    _text_event.set()


def wake_ocr():
    _ocr_event.set()


def cancel_ocr(doc_id: int):
    with _cancel_lock:
        _cancelled.add(doc_id)


def _is_cancelled(doc_id: int) -> bool:
    with _cancel_lock:
        return doc_id in _cancelled


def _clear_cancel(doc_id: int):
    with _cancel_lock:
        _cancelled.discard(doc_id)


def _open_pdf(path):
    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise RuntimeError("PDF is password protected")
    return doc


# --------------------------------------------------------------------------- text extraction

def _set_date(conn, row, pdf):
    doc_date, source = dates.resolve(pdf, row["filename"], row["file_date"], row["added_at"])
    conn.execute("UPDATE documents SET doc_date=?, date_source=? WHERE id=?", (doc_date, source, row["id"]))


def _extract_one(row):
    doc_id, rel_path = row["id"], row["rel_path"]
    path = db.LIBRARY_DIR / rel_path
    try:
        with _open_pdf(path) as pdf:
            pages = pdf.page_count
            text = "\n".join(page.get_text("text") for page in pdf)
            with db.transaction() as conn:
                _set_date(conn, row, pdf)
        text = text.replace("\x00", "")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET text_status='done', text_error=NULL, pages=?, text_chars=? WHERE id=?",
                (pages, len(text.strip()), doc_id),
            )
            conn.execute("UPDATE doc_fts SET text=? WHERE rowid=?", (text, doc_id))
    except Exception as e:  # noqa: BLE001
        log.warning("Text extraction failed for %s: %s", rel_path, e)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET text_status='error', text_error=? WHERE id=?",
                (str(e)[:500], doc_id),
            )
            if row["date_source"] is None:
                _set_date(conn, row, None)


def _date_one(row):
    """(Re)compute the document date only (existing documents, rename, 'Refresh dates')."""
    pdf = None
    try:
        pdf = _open_pdf(db.LIBRARY_DIR / row["rel_path"])
    except Exception as e:  # noqa: BLE001
        log.info("Date: cannot open %s (%s) - using file name / file date", row["rel_path"], e)
    try:
        with db.transaction() as conn:
            _set_date(conn, row, pdf)
    finally:
        if pdf is not None:
            pdf.close()


def _text_loop():
    while True:
        try:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM documents WHERE text_status='pending' ORDER BY id LIMIT 1"
                ).fetchone()
                date_row = None if row else conn.execute(
                    "SELECT * FROM documents WHERE date_source IS NULL ORDER BY id LIMIT 1"
                ).fetchone()
            if row is not None:
                _extract_one(row)
                mark_changed(row["id"])
            elif date_row is not None:
                _date_one(date_row)
                mark_changed(date_row["id"])
            else:
                _text_event.wait(30)
                _text_event.clear()
        except Exception:  # noqa: BLE001
            log.exception("Text worker error")
            time.sleep(5)


# --------------------------------------------------------------------------- OCR

def _tesseract(img_path: str) -> str:
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        res = subprocess.run(
            ["tesseract", img_path, "stdout", "-l", OCR_LANG, "--psm", "3"],
            capture_output=True, env=env, timeout=600,
        )
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace").strip()[-300:] or "tesseract failed")
    return res.stdout.decode("utf-8", errors="replace")


class _Cancelled(Exception):
    pass


# One shared pool of OCR_PARALLEL tesseract workers for all documents. Pages are rendered
# by the per-document threads and submitted here; `_inflight` counts pages rendered but not
# finished yet (bounded to OCR_PARALLEL * 2 so rendering stays only a little ahead).
# A new document is started whenever fewer than OCR_PARALLEL pages are in flight, so
# short documents run side by side while a long one keeps every worker busy on its own.
_pool: ThreadPoolExecutor | None = None
_cv = threading.Condition()
_inflight = 0
_active: set[int] = set()  # documents currently being OCR'd


def _acquire_page_slot(doc_id: int):
    global _inflight
    with _cv:
        while _inflight >= OCR_PARALLEL * 2:
            if _is_cancelled(doc_id):
                raise _Cancelled()
            _cv.wait(0.5)
        _inflight += 1


def _release_page_slot(_future=None):
    global _inflight
    with _cv:
        _inflight -= 1
        _cv.notify_all()


def _ocr_one(doc_id: int, rel_path: str, started: threading.Event):
    path = db.LIBRARY_DIR / rel_path
    futures = []
    try:
        with _open_pdf(path) as pdf, tempfile.TemporaryDirectory(dir=db.TMP_DIR) as tmp:
            total = pdf.page_count
            progress = ocr_progress[doc_id] = {"done": 0, "total": total}

            def page_done(f):
                _release_page_slot()
                if not f.cancelled():
                    progress["done"] += 1

            try:
                for i, page in enumerate(pdf):
                    if _is_cancelled(doc_id):
                        raise _Cancelled()
                    _acquire_page_slot(doc_id)
                    try:
                        img = os.path.join(tmp, f"p{i}.png")
                        page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY).save(img)
                        f = _pool.submit(_tesseract, img)
                    except BaseException:
                        _release_page_slot()
                        raise
                    f.add_done_callback(page_done)
                    futures.append(f)
                    started.set()
                started.set()
                while not all(f.done() for f in futures):
                    if _is_cancelled(doc_id):
                        raise _Cancelled()
                    wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
            finally:
                # don't remove the temp dir under running tesseract processes
                for f in futures:
                    f.cancel()
                wait(futures)
            results = [f.result() for f in futures]

        text = "\n\f\n".join(results).replace("\x00", "")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET ocr_status='done', ocr_error=NULL, ocr_at=?, pages=COALESCE(pages, ?) "
                "WHERE id=?",
                (now_iso(), total, doc_id),
            )
            conn.execute("UPDATE doc_fts SET ocr_text=? WHERE rowid=?", (text, doc_id))
        log.info("OCR done: %s (%d pages)", rel_path, total)
    except _Cancelled:
        log.info("OCR cancelled: %s", rel_path)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET ocr_status=CASE WHEN ocr_at IS NULL THEN 'none' ELSE 'done' END "
                "WHERE id=?",
                (doc_id,),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("OCR failed for %s: %s", rel_path, e)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET ocr_status='error', ocr_error=? WHERE id=?",
                (str(e)[:500], doc_id),
            )
    finally:
        ocr_progress.pop(doc_id, None)
        _clear_cancel(doc_id)
        started.set()


def _run_doc(doc_id: int, rel_path: str, started: threading.Event):
    try:
        _ocr_one(doc_id, rel_path, started)
    except Exception:  # noqa: BLE001
        log.exception("OCR worker error")
    finally:
        with _cv:
            _active.discard(doc_id)
            _cv.notify_all()
        mark_changed(doc_id)


def _ocr_loop():
    while True:
        try:
            # wait for a free tesseract worker (and cap the number of documents in progress)
            with _cv:
                while len(_active) >= OCR_PARALLEL or _inflight >= OCR_PARALLEL:
                    _cv.wait()
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT id, rel_path FROM documents WHERE ocr_status='queued' "
                    "ORDER BY ocr_queued_at, id LIMIT 1"
                ).fetchone()
                if row is not None:
                    conn.execute("UPDATE documents SET ocr_status='running' WHERE id=?", (row["id"],))
            if row is None:
                _ocr_event.wait(30)
                _ocr_event.clear()
                continue
            doc_id = row["id"]
            _clear_cancel(doc_id)
            with _cv:
                _active.add(doc_id)
            mark_changed(doc_id)  # now running
            started = threading.Event()
            threading.Thread(target=_run_doc, args=(doc_id, row["rel_path"], started),
                             name=f"ocr-doc-{doc_id}", daemon=True).start()
            # let it put its first page in flight before deciding whether to start another one
            started.wait(60)
        except Exception:  # noqa: BLE001
            log.exception("OCR worker error")
            time.sleep(5)


def start():
    global _pool
    # Anything that was running when the app stopped goes back into the queue.
    with db.transaction() as conn:
        conn.execute("UPDATE documents SET ocr_status='queued' WHERE ocr_status='running'")
    _pool = ThreadPoolExecutor(max_workers=OCR_PARALLEL, thread_name_prefix="tesseract")
    threading.Thread(target=_text_loop, name="text-worker", daemon=True).start()
    threading.Thread(target=_ocr_loop, name="ocr-worker", daemon=True).start()
