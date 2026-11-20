import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
LIBRARY_DIR = DATA_DIR / "library"
DB_DIR = DATA_DIR / "db"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DB_DIR / "pdfmanager.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    filename      TEXT NOT NULL,
    folder        TEXT NOT NULL DEFAULT '',
    rel_path      TEXT NOT NULL UNIQUE,
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT,
    pages         INTEGER,
    file_date     TEXT,
    added_at      TEXT NOT NULL,
    text_status   TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
    text_error    TEXT,
    text_chars    INTEGER NOT NULL DEFAULT 0,
    ocr_status    TEXT NOT NULL DEFAULT 'none',      -- none | queued | running | done | error
    ocr_error     TEXT,
    ocr_at        TEXT,
    ocr_queued_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_ocr ON documents(ocr_status);
CREATE INDEX IF NOT EXISTS idx_documents_text ON documents(text_status);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag_id);

-- rowid = documents.id
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    filename, text, ocr_text,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def init_db():
    for d in (LIBRARY_DIR, DB_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def fold(s: str | None) -> str:
    """Lowercase and strip accents: 'Számla_ŐSZ' -> 'szamla_osz'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _new_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def connect():
    conn = _new_conn()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    conn = _new_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
