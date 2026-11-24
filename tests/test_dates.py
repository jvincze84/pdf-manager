"""Document date: PDF metadata -> file name -> file date -> upload date; resync."""
import sqlite3
from datetime import datetime, timezone

import pymupdf

from app import dates, db
from conftest import doc, meta_pdf, search, upload, wait_for

XMP = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
<xmp:CreateDate>2021-03-04T05:06:07+02:00</xmp:CreateDate>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""


def local_iso(*a):
    """Naive date in the server's local time zone -> stored UTC ISO string."""
    return datetime(*a).astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- parsing

def test_parse_pdf_date():
    p = dates.parse_pdf_date
    assert p("D:20260202093229+01'00'") == datetime(2026, 2, 2, 8, 32, 29, tzinfo=timezone.utc)
    assert p("D:20260202093229+01'00") == datetime(2026, 2, 2, 8, 32, 29, tzinfo=timezone.utc)
    assert p("D:20260202093229Z") == datetime(2026, 2, 2, 9, 32, 29, tzinfo=timezone.utc)
    assert p("D:20260202093229-05'30'") == datetime(2026, 2, 2, 15, 2, 29, tzinfo=timezone.utc)
    assert p("D:2026") == datetime(2026, 1, 1).astimezone()  # partial, no zone -> local
    assert p("20260202") == datetime(2026, 2, 2).astimezone()  # without the D: prefix
    for bad in ("", None, "garbage", "D:19700101000000Z", "D:00000101", "D:20261399000000Z", "D:29990101000000Z"):
        assert p(bad) is None, bad


def test_parse_xmp_date():
    assert dates.parse_xmp_date("2021-03-04T05:06:07+02:00") == datetime(2021, 3, 4, 3, 6, 7, tzinfo=timezone.utc)
    assert dates.parse_xmp_date("2021-03-04T05:06:07.123Z") == datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    assert dates.parse_xmp_date("2021-03-04") == datetime(2021, 3, 4).astimezone()


def test_date_from_filename():
    f = dates.date_from_filename
    assert f("2026-10-21 _17-04-23-Golf.pdf") == datetime(2026, 10, 21, 17, 4, 23).astimezone()
    assert f("2026-05-07-13-43-Lajos.pdf") == datetime(2026, 5, 7, 13, 43).astimezone()
    assert f("20 Az öröklési illeték 20260202.pdf") == datetime(2026, 2, 2).astimezone()
    assert f("scan_2024_12_31.pdf") == datetime(2024, 12, 31).astimezone()
    assert f("szamla 2024.03.15.pdf") == datetime(2024, 3, 15).astimezone()
    for none in ("14034_N_244_2026_11-14034_N_244_2026_11.pdf",  # year_month only
                 "invoice 123456789.pdf",                         # longer number
                 "2024-13-01.pdf", "2024-02-30.pdf",              # impossible dates
                 "2024-03_15.pdf",                                # mixed separators
                 "plain.pdf"):
        assert f(none) is None, none


# --------------------------------------------------------------------------- API

def test_date_priority(client):
    a = upload(client, meta_pdf("a", created="D:20200102030405Z", modified="D:20230101000000Z"), "2019-01-01 a.pdf")["id"]
    b = upload(client, meta_pdf("b", modified="D:20210607080910Z"), "2019-01-01 b.pdf")["id"]
    c = upload(client, meta_pdf("c", xmp=XMP), "c.pdf")["id"]
    d = upload(client, meta_pdf("d"), "scan 2018-11-12.pdf")["id"]
    e = upload(client, meta_pdf("e"), "e.pdf", last_modified=1600000000000)["id"]

    assert (doc(client, a)["doc_date"], doc(client, a)["date_source"]) == ("2020-01-02T03:04:05+00:00", "pdf_created")
    assert (doc(client, b)["doc_date"], doc(client, b)["date_source"]) == ("2021-06-07T08:09:10+00:00", "pdf_modified")
    assert (doc(client, c)["doc_date"], doc(client, c)["date_source"]) == ("2021-03-04T03:06:07+00:00", "pdf_created")
    assert (doc(client, d)["doc_date"], doc(client, d)["date_source"]) == (local_iso(2018, 11, 12), "filename")
    de = doc(client, e)
    assert (de["doc_date"], de["date_source"]) == ("2020-09-13T12:26:40+00:00", "file")
    # upload date is kept separately
    assert de["added_at"] > "2026" and de["file_date"] == "2020-09-13T12:26:40+00:00"

    ids = lambda **p: [x["id"] for x in client.get("/api/documents", params=p).json()["items"]]
    assert ids(sort="date", order="asc") == [d, a, e, c, b]
    assert ids() == [b, c, e, a, d]                       # default: newest document date first
    assert ids(sort="added", order="asc") == [a, b, c, d, e]


def test_unreadable_pdf_falls_back_to_filename(client):
    r = upload(client, b"%PDF-1.4 broken", "broken 2022-02-03.pdf")
    d = doc(client, r["id"])
    assert d["text_status"] == "error"
    assert (d["doc_date"], d["date_source"]) == (local_iso(2022, 2, 3), "filename")


def test_rename_recomputes_filename_date(client):
    r = upload(client, meta_pdf("x"), "2020-01-01 x.pdf", last_modified=1600000000000)["id"]
    assert doc(client, r)["date_source"] == "filename"
    client.patch(f"/api/documents/{r}", json={"filename": "renamed"})
    wait_for(lambda: doc(client, r)["date_source"] == "file")
    client.patch(f"/api/documents/{r}", json={"filename": "renamed 2015-05-06"})
    wait_for(lambda: doc(client, r)["doc_date"] == local_iso(2015, 5, 6))


def test_refresh_dates(client):
    a = upload(client, meta_pdf("a", created="D:20200102030405Z"), "a.pdf", folder="F")["id"]
    b = upload(client, meta_pdf("b", created="D:20200102030405Z"), "b.pdf", folder="G")["id"]
    # file replaced on disk with one that has a different date
    (db.LIBRARY_DIR / "F/a.pdf").write_bytes(meta_pdf("a", created="D:20110102030405Z"))
    (db.LIBRARY_DIR / "G/b.pdf").write_bytes(meta_pdf("b", created="D:20110102030405Z"))

    assert client.post("/api/dates/refresh", params={"folder": "F"}).json() == {"queued": 1}
    wait_for(lambda: doc(client, a)["doc_date"] == "2011-01-02T03:04:05+00:00")
    assert doc(client, b)["doc_date"] == "2020-01-02T03:04:05+00:00"  # other folder untouched

    assert client.post("/api/dates/refresh").json() == {"queued": 2}
    wait_for(lambda: doc(client, b)["doc_date"] == "2011-01-02T03:04:05+00:00")


def test_rescan_import_gets_pdf_date(client):
    (db.LIBRARY_DIR / "dropped.pdf").write_bytes(meta_pdf("dropped", created="D:20190304050607Z"))
    client.post("/api/rescan")
    wait_for(lambda: search(client) == ["dropped.pdf"])
    i = client.get("/api/documents").json()["items"][0]["id"]
    wait_for(lambda: doc(client, i)["date_source"] == "pdf_created")
    assert doc(client, i)["doc_date"] == "2019-03-04T05:06:07+00:00"


def test_migration_adds_columns(tmp_path, monkeypatch):
    """A database from before this feature gets the new columns; old rows are queued (NULL)."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',"
                 " rel_path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, pages INTEGER,"
                 " file_date TEXT, added_at TEXT NOT NULL, text_status TEXT NOT NULL DEFAULT 'pending',"
                 " text_error TEXT, text_chars INTEGER NOT NULL DEFAULT 0, ocr_status TEXT NOT NULL DEFAULT 'none',"
                 " ocr_error TEXT, ocr_at TEXT, ocr_queued_at TEXT)")
    conn.execute("INSERT INTO documents(filename, rel_path, added_at, text_status) VALUES ('a.pdf', 'a.pdf', 'x', 'done')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()  # idempotent
    with db.connect() as c:
        row = c.execute("SELECT doc_date, date_source FROM documents").fetchone()
    assert tuple(row) == (None, None)


def test_meta_pdf_helper_has_no_dates():
    with pymupdf.open(stream=meta_pdf("x"), filetype="pdf") as p:
        assert dates.pdf_dates(p) == (None, None)
