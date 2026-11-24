import filecmp
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, workers
from .search import build_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pdfmanager")

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")  # PDF.js modules
mimetypes.add_type("application/wasm", ".wasm")

MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
PDFJS_DIR = Path(os.environ.get("PDFJS_DIR", "/srv/pdfjs"))  # PDF.js viewer, see Dockerfile
APP_NAME = os.environ.get("APP_NAME", "").strip() or "PDF Manager"

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    workers.start()
    yield


app = FastAPI(title=APP_NAME, lifespan=lifespan)


# --------------------------------------------------------------------------- helpers

def _clean_segment(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[\x00-\x1f/\\]", "", s).strip().strip(".").strip()
    return s[:150]


def clean_folder(folder: str | None) -> str:
    if not folder:
        return ""
    parts = [_clean_segment(p) for p in re.split(r"[/\\]+", folder)]
    return "/".join(p for p in parts if p and p != "..")


def clean_filename(name: str) -> str:
    name = _clean_segment(os.path.basename((name or "").replace("\\", "/"))) or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def unique_path(folder: str, filename: str) -> tuple[str, Path]:
    base, ext = os.path.splitext(filename)
    n = 0
    while True:
        name = filename if n == 0 else f"{base} ({n}){ext}"
        rel = f"{folder}/{name}" if folder else name
        full = db.LIBRARY_DIR / rel
        if not full.exists():
            with db.connect() as conn:
                if not conn.execute("SELECT 1 FROM documents WHERE rel_path=?", (rel,)).fetchone():
                    return rel, full
        n += 1


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def set_tags(conn: sqlite3.Connection, doc_id: int, tags: list[str]):
    conn.execute("DELETE FROM document_tags WHERE document_id=?", (doc_id,))
    add_tags(conn, [doc_id], tags)


def norm_tags(tags) -> list[str]:
    out, seen = [], set()
    for t in tags or []:
        t = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(t))).strip()[:60]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def add_tags(conn, doc_ids: list[int], tags: list[str]):
    for t in norm_tags(tags):
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (t,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO document_tags(document_id, tag_id) VALUES (?, ?)",
            [(d, tag_id) for d in doc_ids],
        )


def cleanup_tags(conn):
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM document_tags)")


def insert_document(conn, *, filename, folder, rel_path, size, sha256, file_date, tags=None) -> int:
    cur = conn.execute(
        "INSERT INTO documents(filename, folder, rel_path, size, sha256, file_date, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (filename, folder, rel_path, size, sha256, file_date, workers.now_iso()),
    )
    doc_id = cur.lastrowid
    conn.execute("INSERT INTO doc_fts(rowid, filename, text, ocr_text) VALUES (?, ?, '', '')",
                 (doc_id, filename))
    if tags:
        add_tags(conn, [doc_id], tags)
    return doc_id


def remove_empty_dirs(start: Path):
    p = start
    while p != db.LIBRARY_DIR and db.LIBRARY_DIR in p.parents:
        try:
            p.rmdir()
        except OSError:
            break
        p = p.parent


def delete_documents(ids: list[int]) -> int:
    count = 0
    with db.transaction() as conn:
        for doc_id in ids:
            row = conn.execute("SELECT rel_path FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not row:
                continue
            workers.cancel_ocr(doc_id)
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute("DELETE FROM doc_fts WHERE rowid=?", (doc_id,))
            full = db.LIBRARY_DIR / row["rel_path"]
            try:
                full.unlink(missing_ok=True)
                remove_empty_dirs(full.parent)
            except OSError as e:
                log.warning("Could not delete %s: %s", full, e)
            count += 1
        cleanup_tags(conn)
    workers.mark_changed(reload=True)
    return count


def row_to_doc(r: sqlite3.Row) -> dict:
    d = {
        "id": r["id"],
        "filename": r["filename"],
        "folder": r["folder"],
        "size": r["size"],
        "pages": r["pages"],
        "file_date": r["file_date"],
        "added_at": r["added_at"],
        "doc_date": r["doc_date"] or r["file_date"] or r["added_at"],
        "date_source": r["date_source"],
        "text_status": r["text_status"],
        "text_error": r["text_error"],
        "has_text": r["text_chars"] > 0,
        "ocr_status": r["ocr_status"],
        "ocr_error": r["ocr_error"],
        "ocr_at": r["ocr_at"],
        "tags": sorted(r["tags"].split("\x1f"), key=str.lower) if r["tags"] else [],
    }
    if d["ocr_status"] == "running" and r["id"] in workers.ocr_progress:
        d["ocr_progress"] = workers.ocr_progress[r["id"]]
    if "snippet" in r.keys():
        d["snippet"] = r["snippet"]
    return d


def folder_clause(folder: str, column: str = "folder") -> tuple[str, list]:
    """SQL condition selecting a folder and all its subfolders ("/" = library root only)."""
    if folder == "/":
        return f"{column} = ''", []
    f = clean_folder(folder)
    if not f:
        return "1", []
    esc = f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"({column} = ? OR {column} LIKE ? ESCAPE '\\')", [f, esc + "/%"]


def get_doc(doc_id: int) -> sqlite3.Row:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    return row


# --------------------------------------------------------------------------- documents

DATE_SQL = "COALESCE(d.doc_date, d.file_date, d.added_at)"

SORTS = {
    "name": "d.filename COLLATE NOCASE",
    "folder": "d.folder COLLATE NOCASE",
    "size": "d.size",
    "date": DATE_SQL,
    "added": "d.added_at",
    "pages": "d.pages",
}

SNIP_OPEN, SNIP_CLOSE = "\x02", "\x03"


@app.get("/api/documents")
def list_documents(
    q: str = "",
    name: str = "",
    tag: list[str] = Query(default=[]),
    folder: str | None = None,
    ocr: str | None = None,
    sort: str = "",
    order: str = "",
    page: int = 1,
    page_size: int = 50,
    offset: int | None = None,
    limit: int | None = None,
):
    """Paging: either page/page_size, or offset/limit (used by the UI's infinite scroll)."""
    if limit is not None:
        page_size = min(max(1, limit), 1000)
        start = max(0, offset or 0)
    else:
        page_size = min(max(1, page_size), 500)
        start = (max(1, page) - 1) * page_size
    page = start // page_size + 1
    where, params, joins = [], [], ""
    select_extra = ""
    match = build_match(q) if q.strip() else None

    if q.strip() and not match:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "query_error": None}

    if match:
        joins = "JOIN doc_fts f ON f.rowid = d.id"
        where.append("doc_fts MATCH ?")
        params.append(match)
        select_extra = (f", snippet(doc_fts, -1, '{SNIP_OPEN}', '{SNIP_CLOSE}', ' … ', 64) AS snippet"
                        ", bm25(doc_fts, 5.0, 1.0, 1.0) AS rank")

    if folder:
        fsql, fparams = folder_clause(folder, "d.folder")
        where.append(fsql)
        params += fparams

    # file name search: every word must occur somewhere in the name (case and accent insensitive)
    for word in name.split():
        w = db.fold(word).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("fold(d.filename) LIKE ? ESCAPE '\\'")
        params.append(f"%{w}%")

    for t in tag:
        where.append(
            "EXISTS (SELECT 1 FROM document_tags dt JOIN tags tg ON tg.id = dt.tag_id "
            "WHERE dt.document_id = d.id AND tg.name = ?)"
        )
        params.append(t)

    if ocr == "done":
        where.append("d.ocr_status = 'done'")
    elif ocr == "not_done":
        where.append("d.ocr_status IN ('none', 'error')")
    elif ocr == "active":
        where.append("d.ocr_status IN ('queued', 'running')")
    elif ocr == "no_text":
        where.append("d.text_chars = 0 AND d.ocr_status != 'done'")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if sort in SORTS:
        direction = "ASC" if order == "asc" else "DESC"
        order_sql = f"{SORTS[sort]} {direction}, d.id {direction}"
    elif match:
        order_sql = "rank"
    else:
        order_sql = f"{DATE_SQL} DESC, d.id DESC"

    sql = f"""
        SELECT d.*,
               (SELECT group_concat(tg.name, char(31)) FROM document_tags dt
                  JOIN tags tg ON tg.id = dt.tag_id WHERE dt.document_id = d.id) AS tags
               {select_extra}
        FROM documents d {joins}
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    count_sql = f"SELECT COUNT(*) FROM documents d {joins} {where_sql}"
    try:
        with db.connect() as conn:
            total = conn.execute(count_sql, params).fetchone()[0]
            rows = conn.execute(sql, params + [page_size, start]).fetchall()
    except sqlite3.OperationalError as e:
        log.info("Search query failed (%r -> %r): %s", q, match, e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "query_error": "Invalid search expression"}

    return {
        "items": [row_to_doc(r) for r in rows],
        "total": total,
        "offset": start,
        "page": page,
        "page_size": page_size,
        "query_error": None,
    }


@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    folder: str = Form(""),
    relpath: str = Form(""),
    tags: str = Form(""),
    last_modified: int | None = Form(None),
    skip_duplicates: bool = Form(True),
):
    target_folder = clean_folder(folder)
    if relpath and "/" in relpath.replace("\\", "/"):
        sub = clean_folder(os.path.dirname(relpath.replace("\\", "/")))
        target_folder = "/".join(p for p in (target_folder, sub) if p)

    filename = clean_filename(file.filename or relpath)
    tmp_path = db.TMP_DIR / f"upload-{os.getpid()}-{id(file)}-{datetime.now().timestamp()}.part"
    sha = hashlib.sha256()
    size = 0
    head = b""
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                if not head:
                    head = chunk[:1024]
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, f"File too large (max {MAX_UPLOAD // 1024 // 1024} MB)")
                sha.update(chunk)
                out.write(chunk)
        if b"%PDF" not in head:
            raise HTTPException(415, "Not a PDF file")

        digest = sha.hexdigest()
        if skip_duplicates:
            with db.connect() as conn:
                dup = conn.execute("SELECT id, rel_path FROM documents WHERE sha256=?", (digest,)).fetchone()
            if dup:
                return {"status": "duplicate", "id": dup["id"], "existing": dup["rel_path"],
                        "filename": filename}

        rel, full = unique_path(target_folder, filename)
        full.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), full)
        file_date = None
        if last_modified:
            ts = last_modified / 1000.0
            try:
                os.utime(full, (ts, ts))
            except OSError:
                pass
            file_date = iso_from_ts(ts)
        else:
            file_date = iso_from_ts(full.stat().st_mtime)

        tag_list = [t for t in (s.strip() for s in tags.split(",")) if t]
        with db.transaction() as conn:
            doc_id = insert_document(conn, filename=os.path.basename(rel), folder=target_folder,
                                     rel_path=rel, size=size, sha256=digest, file_date=file_date,
                                     tags=tag_list)
        workers.wake_text()
        workers.mark_changed(reload=True)
        return {"status": "ok", "id": doc_id, "rel_path": rel, "filename": os.path.basename(rel)}
    finally:
        tmp_path.unlink(missing_ok=True)
        await file.close()


class IdList(BaseModel):
    ids: list[int]


@app.post("/api/documents/batch")
def get_documents_batch(body: IdList):
    """Current state of the given documents (missing ids are omitted). Used to refresh rows in place."""
    ids = list(dict.fromkeys(body.ids))[:2000]
    if not ids:
        return {"items": []}
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT d.*, (SELECT group_concat(tg.name, char(31)) FROM document_tags dt
                  JOIN tags tg ON tg.id = dt.tag_id WHERE dt.document_id = d.id) AS tags
                FROM documents d WHERE d.id IN ({','.join('?' * len(ids))})""", ids).fetchall()
    return {"items": [row_to_doc(r) for r in rows]}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    with db.connect() as conn:
        r = conn.execute(
            """SELECT d.*, (SELECT group_concat(tg.name, char(31)) FROM document_tags dt
                 JOIN tags tg ON tg.id = dt.tag_id WHERE dt.document_id = d.id) AS tags
               FROM documents d WHERE d.id=?""", (doc_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Document not found")
    return row_to_doc(r)


@app.get("/api/documents/{doc_id}/file")
def get_file(doc_id: int, download: bool = False):
    row = get_doc(doc_id)
    full = db.LIBRARY_DIR / row["rel_path"]
    if not full.is_file():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(full, media_type="application/pdf", filename=row["filename"],
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/documents/{doc_id}/text")
def get_text(doc_id: int):
    get_doc(doc_id)
    with db.connect() as conn:
        r = conn.execute("SELECT text, ocr_text FROM doc_fts WHERE rowid=?", (doc_id,)).fetchone()
    return {"text": r["text"] if r else "", "ocr_text": r["ocr_text"] if r else ""}


class DocPatch(BaseModel):
    tags: list[str] | None = None
    filename: str | None = None
    folder: str | None = None


@app.patch("/api/documents/{doc_id}")
def patch_document(doc_id: int, body: DocPatch):
    row = get_doc(doc_id)
    renamed = False
    with db.transaction() as conn:
        if body.tags is not None:
            set_tags(conn, doc_id, body.tags)
            cleanup_tags(conn)
        if body.filename is not None or body.folder is not None:
            new_folder = clean_folder(body.folder) if body.folder is not None else row["folder"]
            new_name = clean_filename(body.filename) if body.filename is not None else row["filename"]
            if new_folder != row["folder"] or new_name != row["filename"]:
                rel, full = unique_path(new_folder, new_name)
                old = db.LIBRARY_DIR / row["rel_path"]
                full.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), full)
                remove_empty_dirs(old.parent)
                conn.execute("UPDATE documents SET filename=?, folder=?, rel_path=? WHERE id=?",
                             (os.path.basename(rel), new_folder, rel, doc_id))
                conn.execute("UPDATE doc_fts SET filename=? WHERE rowid=?", (os.path.basename(rel), doc_id))
                if os.path.basename(rel) != row["filename"]:
                    # the date may come from the file name -> recompute in the background
                    conn.execute("UPDATE documents SET date_source=NULL WHERE id=?", (doc_id,))
                    renamed = True
    if renamed:
        workers.wake_text()
    workers.mark_changed(doc_id)
    return get_document(doc_id)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    get_doc(doc_id)
    delete_documents([doc_id])
    return {"deleted": 1}


# --------------------------------------------------------------------------- bulk

class BulkAction(BaseModel):
    ids: list[int]
    action: str  # ocr | delete | add_tags | remove_tags | move
    tags: list[str] = []
    folder: str | None = None


@app.post("/api/bulk")
def bulk(body: BulkAction):
    ids = list(dict.fromkeys(body.ids))
    if not ids:
        return {"affected": 0}
    if body.action == "ocr":
        return {"affected": queue_ocr(ids=ids)}
    if body.action == "delete":
        return {"affected": delete_documents(ids)}
    if body.action == "add_tags":
        with db.transaction() as conn:
            existing = [r[0] for r in conn.execute(
                f"SELECT id FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids)]
            add_tags(conn, existing, body.tags)
        workers.mark_changed(*existing)
        return {"affected": len(existing)}
    if body.action == "remove_tags":
        names = norm_tags(body.tags)
        with db.transaction() as conn:
            for name in names:
                conn.execute(
                    f"DELETE FROM document_tags WHERE tag_id=(SELECT id FROM tags WHERE name=?) "
                    f"AND document_id IN ({','.join('?' * len(ids))})", [name] + ids)
            cleanup_tags(conn)
        workers.mark_changed(*ids)
        return {"affected": len(ids)}
    if body.action == "move":
        n = 0
        for doc_id in ids:
            try:
                patch_document(doc_id, DocPatch(folder=body.folder or ""))
                n += 1
            except HTTPException:
                pass
        return {"affected": n}
    raise HTTPException(400, "Unknown action")


# --------------------------------------------------------------------------- duplicates

def _keeper_rank(d: dict):
    """Which copy to keep by default: OCR'd first, then the one with most tags, then the oldest."""
    return (d["ocr_status"] != "done", -len(d["tags"]), d["added_at"], d["id"])


@app.get("/api/duplicates")
def list_duplicates():
    """Groups of documents with identical content (same SHA-256). The first item of each group
    is the suggested copy to keep."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT d.*, (SELECT group_concat(tg.name, char(31)) FROM document_tags dt
                         JOIN tags tg ON tg.id = dt.tag_id WHERE dt.document_id = d.id) AS tags
            FROM documents d
            WHERE d.sha256 IN (SELECT sha256 FROM documents WHERE sha256 IS NOT NULL
                               GROUP BY sha256 HAVING COUNT(*) > 1)""").fetchall()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["sha256"], []).append(row_to_doc(r))
    out = []
    for sha, items in groups.items():
        items.sort(key=_keeper_rank)
        out.append({"sha256": sha, "size": items[0]["size"], "count": len(items), "items": items})
    out.sort(key=lambda g: (-g["size"] * (g["count"] - 1), g["items"][0]["filename"].lower()))
    return {
        "groups": out,
        "duplicates": sum(g["count"] - 1 for g in out),
        "reclaimable": sum(g["size"] * (g["count"] - 1) for g in out),
    }


class DuplicateDelete(BaseModel):
    keep: list[int]  # one id per group: every other copy with the same content is deleted


@app.post("/api/duplicates/delete")
def delete_duplicates(body: DuplicateDelete):
    """For each kept document delete the other documents with identical content. Files are
    compared byte by byte first; tags and OCR text of the deleted copies go to the kept one."""
    keep = list(dict.fromkeys(body.keep))
    victims: list[int] = []
    skipped = 0
    changed: list[int] = []
    with db.transaction() as conn:
        for keep_id in keep:
            k = conn.execute("SELECT * FROM documents WHERE id=?", (keep_id,)).fetchone()
            if not k or not k["sha256"]:
                continue
            k_path = db.LIBRARY_DIR / k["rel_path"]
            if not k_path.is_file():
                skipped += 1
                continue
            others = conn.execute(
                "SELECT * FROM documents WHERE sha256=? AND id<>? ORDER BY id", (k["sha256"], keep_id)).fetchall()
            for o in others:
                if o["id"] in keep or o["id"] in victims:
                    continue
                o_path = db.LIBRARY_DIR / o["rel_path"]
                try:
                    same = o_path.is_file() and filecmp.cmp(k_path, o_path, shallow=False)
                except OSError:
                    same = False
                if not same:
                    skipped += 1
                    continue
                # keep the metadata of the copy being removed
                conn.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id) "
                             "SELECT ?, tag_id FROM document_tags WHERE document_id=?", (keep_id, o["id"]))
                cur = conn.execute("SELECT ocr_status FROM documents WHERE id=?", (keep_id,)).fetchone()
                if o["ocr_at"] and cur["ocr_status"] in ("none", "error"):
                    conn.execute("UPDATE documents SET ocr_status='done', ocr_error=NULL, ocr_at=? WHERE id=?",
                                 (o["ocr_at"], keep_id))
                    conn.execute("UPDATE doc_fts SET ocr_text=(SELECT ocr_text FROM doc_fts WHERE rowid=?) "
                                 "WHERE rowid=?", (o["id"], keep_id))
                victims.append(o["id"])
                if keep_id not in changed:
                    changed.append(keep_id)
    deleted = delete_documents(victims) if victims else 0
    workers.mark_changed(*changed)
    return {"deleted": deleted, "skipped": skipped}


# --------------------------------------------------------------------------- OCR

def queue_ocr(ids: list[int] | None = None, only_unprocessed: bool = False, folder: str | None = None) -> int:
    with db.transaction() as conn:
        sql = "UPDATE documents SET ocr_status='queued', ocr_queued_at=? WHERE ocr_status NOT IN ('queued', 'running')"
        params: list = [workers.now_iso()]
        if only_unprocessed:
            sql += " AND ocr_status IN ('none', 'error')"
        if ids is not None:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += ids
        if folder:
            fsql, fparams = folder_clause(folder)
            sql += " AND " + fsql
            params += fparams
        n = conn.execute(sql, params).rowcount
    workers.wake_ocr()
    if ids is not None and len(ids) <= 100:
        workers.mark_changed(*ids)
    elif n:
        workers.mark_changed(reload=True)
    return n


@app.post("/api/documents/{doc_id}/ocr")
def ocr_document(doc_id: int):
    get_doc(doc_id)
    return {"queued": queue_ocr(ids=[doc_id])}


@app.post("/api/documents/{doc_id}/ocr/cancel")
def ocr_cancel(doc_id: int):
    row = get_doc(doc_id)
    if row["ocr_status"] == "running":
        workers.cancel_ocr(doc_id)
    elif row["ocr_status"] == "queued":
        with db.transaction() as conn:
            conn.execute("UPDATE documents SET ocr_status=CASE WHEN ocr_at IS NULL THEN 'none' ELSE 'done' END "
                         "WHERE id=? AND ocr_status='queued'", (doc_id,))
    workers.mark_changed(doc_id)
    return {"ok": True}


@app.post("/api/ocr/all")
def ocr_all(folder: str | None = None):
    """Queue OCR for every document (optionally only in `folder` and its subfolders)."""
    return {"queued": queue_ocr(folder=folder)}


@app.post("/api/ocr/unprocessed")
def ocr_unprocessed(folder: str | None = None):
    """Queue OCR for documents not OCR'd yet (optionally only in `folder` and its subfolders)."""
    return {"queued": queue_ocr(only_unprocessed=True, folder=folder)}


@app.post("/api/ocr/cancel")
def ocr_cancel_all():
    with db.transaction() as conn:
        n = conn.execute("UPDATE documents SET ocr_status=CASE WHEN ocr_at IS NULL THEN 'none' ELSE 'done' END "
                         "WHERE ocr_status='queued'").rowcount
        running = [r[0] for r in conn.execute("SELECT id FROM documents WHERE ocr_status='running'")]
    for doc_id in running:
        workers.cancel_ocr(doc_id)
    workers.mark_changed(reload=True)
    return {"cancelled": n + len(running)}


# --------------------------------------------------------------------------- meta

@app.get("/api/status")
def status(since: int | None = None):
    with db.connect() as conn:
        r = conn.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(size), 0) AS total_size,
                   SUM(ocr_status='done') AS ocr_done,
                   SUM(ocr_status IN ('none', 'error')) AS ocr_not_done,
                   SUM(ocr_status='queued') AS ocr_queued,
                   SUM(ocr_status='running') AS ocr_running,
                   SUM(ocr_status='error') AS ocr_error,
                   SUM(text_status='pending') AS text_pending
            FROM documents""").fetchone()
        running = conn.execute(
            "SELECT id, filename FROM documents WHERE ocr_status='running'").fetchall()
    out = {k: (r[k] or 0) for k in r.keys()}
    out["running"] = [
        {"id": x["id"], "filename": x["filename"], "progress": workers.ocr_progress.get(x["id"])}
        for x in running
    ]
    out["ocr_lang"] = workers.OCR_LANG
    out["ocr_parallel"] = workers.OCR_PARALLEL
    if since is not None:
        out["changes"] = workers.changes_since(since)
    return out


@app.get("/api/tags")
def list_tags():
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT t.name, COUNT(dt.document_id) AS count FROM tags t
            LEFT JOIN document_tags dt ON dt.tag_id = t.id
            GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()
    return [{"name": r["name"], "count": r["count"]} for r in rows]


@app.get("/api/folders")
def list_folders():
    with db.connect() as conn:
        rows = conn.execute("SELECT folder, COUNT(*) AS c FROM documents GROUP BY folder").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        f = r["folder"]
        if not f:
            continue
        parts = f.split("/")
        for i in range(1, len(parts) + 1):
            key = "/".join(parts[:i])
            counts[key] = counts.get(key, 0) + r["c"]
    root = sum(r["c"] for r in rows if not r["folder"])
    return {"root": root, "folders": [{"path": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: kv[0].lower())]}


@app.post("/api/rescan")
def rescan():
    """Sync the database with the library folder: add new PDFs, drop entries whose file vanished."""
    added = removed = 0
    with db.connect() as conn:
        known = {r["rel_path"]: r["id"] for r in conn.execute("SELECT id, rel_path FROM documents")}
    on_disk = set()
    for path in db.LIBRARY_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        rel = path.relative_to(db.LIBRARY_DIR).as_posix()
        on_disk.add(rel)
        if rel in known:
            continue
        try:
            sha = hashlib.sha256()
            with open(path, "rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    sha.update(chunk)
            st = path.stat()
            folder = path.parent.relative_to(db.LIBRARY_DIR).as_posix()
            with db.transaction() as conn:
                insert_document(conn, filename=path.name, folder="" if folder == "." else folder,
                                rel_path=rel, size=st.st_size, sha256=sha.hexdigest(),
                                file_date=iso_from_ts(st.st_mtime))
            added += 1
        except OSError as e:
            log.warning("Rescan: cannot read %s: %s", path, e)
    missing = [doc_id for rel, doc_id in known.items() if rel not in on_disk]
    if missing:
        with db.transaction() as conn:
            for doc_id in missing:
                conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
                conn.execute("DELETE FROM doc_fts WHERE rowid=?", (doc_id,))
            cleanup_tags(conn)
        removed = len(missing)
    workers.wake_text()
    if added or removed:
        workers.mark_changed(reload=True)
    return {"added": added, "removed": removed}


@app.post("/api/dates/refresh")
def refresh_dates(folder: str | None = None):
    """Recompute document dates (PDF metadata -> file name -> file date -> upload date) in the
    background, e.g. after replacing files on disk or changing the rules."""
    where, params = "1", []
    if folder:
        where, params = folder_clause(folder)
    with db.transaction() as conn:
        n = conn.execute(f"UPDATE documents SET date_source=NULL WHERE {where}", params).rowcount
    workers.wake_text()
    return {"queued": n}


# --------------------------------------------------------------------------- app shell (APP_NAME)

@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("{{APP_NAME}}", html.escape(APP_NAME)),
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    data["name"] = data["short_name"] = APP_NAME
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


if PDFJS_DIR.is_dir():
    app.mount("/pdfjs", StaticFiles(directory=PDFJS_DIR), name="pdfjs")
else:
    log.warning("PDF.js not found in %s - the built-in PDF viewer will not work", PDFJS_DIR)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
