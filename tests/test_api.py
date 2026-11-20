"""API tests: upload, search, file-name search, folders, tags, edit, delete, rescan, files, duplicates."""
import shutil

from app import db
from conftest import doc, search, text_pdf, upload, wait_for


# --------------------------------------------------------------------------- upload

def test_upload_basic(client):
    r = upload(client, text_pdf("hello world"), "hello.pdf", folder="Invoices/2024", tags="invoice, Test")
    assert r["status"] == "ok"
    d = doc(client, r["id"])
    assert d["folder"] == "Invoices/2024"
    assert d["filename"] == "hello.pdf"
    assert d["tags"] == ["invoice", "Test"]
    assert d["pages"] == 1 and d["has_text"] and d["text_status"] == "done"
    assert d["ocr_status"] == "none"
    assert (db.LIBRARY_DIR / "Invoices/2024/hello.pdf").is_file()


def test_upload_duplicate_skipped_or_allowed(client):
    data = text_pdf("same content")
    assert upload(client, data, "a.pdf")["status"] == "ok"
    dup = upload(client, data, "b.pdf")
    assert dup["status"] == "duplicate" and dup["existing"] == "a.pdf"
    assert upload(client, data, "b.pdf", skip_duplicates="false")["status"] == "ok"


def test_upload_same_name_gets_suffix(client):
    upload(client, text_pdf("one"), "x.pdf")
    r = upload(client, text_pdf("two"), "x.pdf")
    assert r["filename"] == "x (1).pdf"


def test_upload_rejects_non_pdf(client):
    r = client.post("/api/documents/upload", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 415


def test_upload_relpath_keeps_folder_structure(client):
    r = upload(client, text_pdf("nested"), "n.pdf", folder="Base", relpath="Scans/sub/n.pdf")
    assert doc(client, r["id"])["folder"] == "Base/Scans/sub"


def test_upload_folder_traversal_is_sanitized(client):
    r = upload(client, text_pdf("evil"), "../../evil.pdf", folder="../../etc/./x")
    d = doc(client, r["id"])
    assert ".." not in d["folder"] and d["filename"] == "evil.pdf"
    assert (db.LIBRARY_DIR / d["folder"] / "evil.pdf").is_file()


# --------------------------------------------------------------------------- content search

def _corpus(client):
    upload(client, text_pdf("This is an invoice for the year 2024.", "Ez egy pontos keresés teszt."), "inv.pdf")
    upload(client, text_pdf("Contract agreement draft", "Tax and invoices summary"), "contract.pdf")
    upload(client, text_pdf("Holiday photos", "VAT refund"), "misc.pdf")


def test_search_words_all_must_match(client):
    _corpus(client)
    assert search(client, q="invoice") == ["contract.pdf", "inv.pdf"]  # prefix: invoices
    assert search(client, q="invoice 2024") == ["inv.pdf"]
    assert search(client, q="invoice holiday") == []


def test_search_exact_phrase(client):
    _corpus(client)
    assert search(client, q='"pontos keresés"') == ["inv.pdf"]
    assert search(client, q='"keresés pontos"') == []
    assert search(client, q='"Ez egy pontos keresés teszt"') == ["inv.pdf"]


def test_search_accent_and_case_insensitive(client):
    _corpus(client)
    assert search(client, q="KERESES") == ["inv.pdf"]
    assert search(client, q='"pontos kereses"') == ["inv.pdf"]


def test_search_exclude_and_or(client):
    _corpus(client)
    assert search(client, q="invoice -draft") == ["inv.pdf"]
    assert search(client, q='invoice -"agreement draft"') == ["inv.pdf"]
    assert search(client, q="holiday OR contract") == ["contract.pdf", "misc.pdf"]


def test_search_matches_filename_too(client):
    _corpus(client)
    assert search(client, q="misc") == ["misc.pdf"]


def test_search_snippet_highlight(client):
    _corpus(client)
    items = client.get("/api/documents", params={"q": "refund"}).json()["items"]
    assert "\x02refund\x03" in items[0]["snippet"].lower() or "\x02REFUND\x03" in items[0]["snippet"]


def test_search_snippet_context_length(client):
    # 60 numbered words on each side of the hit: the snippet should show a wide context (>40 words)
    before = [f"w{i}" for i in range(60)]
    after = [f"x{i}" for i in range(60)]
    lines = [" ".join(before[i:i + 10]) for i in range(0, 60, 10)] + ["needle"] + \
            [" ".join(after[i:i + 10]) for i in range(0, 60, 10)]
    upload(client, text_pdf(*lines), "long.pdf")
    snip = client.get("/api/documents", params={"q": "needle"}).json()["items"][0]["snippet"]
    assert "\x02needle\x03" in snip
    assert len(snip.replace("\x02", " ").replace("\x03", " ").split()) > 40

def test_search_weird_input_does_not_fail(client):
    _corpus(client)
    for q in ['"', "a\"b(c", "-", "OR", "*", "NEAR(", "^", "'; DROP TABLE documents; --"]:
        r = client.get("/api/documents", params={"q": q})
        assert r.status_code == 200, q
        assert r.json()["query_error"] is None, q


# --------------------------------------------------------------------------- file name search

def test_name_search(client):
    upload(client, text_pdf("a"), "Számla_2024_január.pdf")
    upload(client, text_pdf("b"), "Szerződés 2023.pdf")
    upload(client, text_pdf("c"), "invoice-2024.pdf")
    assert search(client, name="2024") == ["Számla_2024_január.pdf", "invoice-2024.pdf"]
    assert search(client, name="szamla") == ["Számla_2024_január.pdf"]      # accent-insensitive
    assert search(client, name="SZERZŐDÉS") == ["Szerződés 2023.pdf"]       # case-insensitive
    assert search(client, name="2024 januar") == ["Számla_2024_január.pdf"]  # all words
    assert search(client, name="ice-20") == ["invoice-2024.pdf"]            # substring
    assert search(client, name="%") == []                                   # LIKE wildcards escaped
    assert search(client, name="_") == ["Számla_2024_január.pdf"]


def test_name_search_combined_with_content_search(client):
    upload(client, text_pdf("budget report"), "report-2024.pdf")
    upload(client, text_pdf("budget plan"), "plan-2024.pdf")
    assert search(client, q="budget", name="report") == ["report-2024.pdf"]


# --------------------------------------------------------------------------- folders

def test_folder_filter_includes_subfolders(client):
    upload(client, text_pdf("1"), "root.pdf")
    upload(client, text_pdf("2"), "a.pdf", folder="A")
    upload(client, text_pdf("3"), "ab.pdf", folder="A/B")
    upload(client, text_pdf("4"), "a2.pdf", folder="A2")  # must not match folder "A"
    assert search(client, folder="A") == ["a.pdf", "ab.pdf"]
    assert search(client, folder="A/B") == ["ab.pdf"]
    assert search(client, folder="/") == ["root.pdf"]
    f = client.get("/api/folders").json()
    assert f["root"] == 1
    assert {x["path"]: x["count"] for x in f["folders"]} == {"A": 2, "A/B": 1, "A2": 1}


def test_folder_with_like_wildcards(client):
    upload(client, text_pdf("1"), "x.pdf", folder="100%_done")
    upload(client, text_pdf("2"), "y.pdf", folder="100xxdone")
    assert search(client, folder="100%_done") == ["x.pdf"]


# --------------------------------------------------------------------------- tags

def test_tags_edit_filter_cleanup(client):
    a = upload(client, text_pdf("1"), "a.pdf", tags="red, blue")["id"]
    b = upload(client, text_pdf("2"), "b.pdf", tags="blue")["id"]
    assert search(client, tag="blue") == ["a.pdf", "b.pdf"]
    assert search(client, tag=["blue", "red"]) == ["a.pdf"]

    r = client.patch(f"/api/documents/{a}", json={"tags": ["green", "Blue", "blue "]})
    assert r.json()["tags"] == ["Blue", "green"] or r.json()["tags"] == ["blue", "green"]
    names = {t["name"].lower() for t in client.get("/api/tags").json()}
    assert names == {"blue", "green"}  # "red" no longer used -> removed

    client.post("/api/bulk", json={"ids": [a, b], "action": "add_tags", "tags": ["shared"]})
    assert search(client, tag="shared") == ["a.pdf", "b.pdf"]
    client.post("/api/bulk", json={"ids": [b], "action": "remove_tags", "tags": ["shared"]})
    assert search(client, tag="shared") == ["a.pdf"]


# --------------------------------------------------------------------------- edit / delete

def test_rename_and_move(client):
    r = upload(client, text_pdf("movable"), "old.pdf", folder="X")
    d = client.patch(f"/api/documents/{r['id']}", json={"filename": "Új név", "folder": "Y/Z"}).json()
    assert d["filename"] == "Új név.pdf" and d["folder"] == "Y/Z"
    assert (db.LIBRARY_DIR / "Y/Z/Új név.pdf").is_file()
    assert not (db.LIBRARY_DIR / "X").exists()  # empty folder removed
    assert search(client, q="új") == ["Új név.pdf"]  # FTS filename updated
    assert search(client, name="uj nev") == ["Új név.pdf"]


def test_bulk_move(client):
    a = upload(client, text_pdf("1"), "a.pdf", folder="S")["id"]
    b = upload(client, text_pdf("2"), "b.pdf")["id"]
    client.post("/api/bulk", json={"ids": [a, b], "action": "move", "folder": "T"})
    assert search(client, folder="T") == ["a.pdf", "b.pdf"]


def test_delete(client):
    r = upload(client, text_pdf("bye"), "bye.pdf", folder="Del/Sub", tags="gone")
    assert client.delete(f"/api/documents/{r['id']}").status_code == 200
    assert client.get(f"/api/documents/{r['id']}").status_code == 404
    assert not (db.LIBRARY_DIR / "Del").exists()
    assert search(client, q="bye") == []
    assert client.get("/api/tags").json() == []


# --------------------------------------------------------------------------- rescan

def test_rescan_adds_and_removes(client):
    kept = upload(client, text_pdf("kept"), "kept.pdf")["id"]
    gone = upload(client, text_pdf("gone"), "gone.pdf", folder="G")
    (db.LIBRARY_DIR / "Dropped").mkdir()
    (db.LIBRARY_DIR / "Dropped/new file.pdf").write_bytes(text_pdf("dropped manually"))
    (db.LIBRARY_DIR / "Dropped/ignore.txt").write_text("not a pdf")
    shutil.rmtree(db.LIBRARY_DIR / "G")

    assert client.post("/api/rescan").json() == {"added": 1, "removed": 1}
    names = search(client)
    assert names == ["kept.pdf", "new file.pdf"]
    assert client.get(f"/api/documents/{gone['id']}").status_code == 404
    assert doc(client, kept)["filename"] == "kept.pdf"

    new = client.get("/api/documents", params={"name": "new file"}).json()["items"][0]
    assert new["folder"] == "Dropped"
    wait_for(lambda: doc(client, new["id"])["text_status"] == "done")
    assert search(client, q='"dropped manually"') == ["new file.pdf"]
    assert client.post("/api/rescan").json() == {"added": 0, "removed": 0}


# --------------------------------------------------------------------------- files / misc

def test_file_download_and_inline(client):
    r = upload(client, text_pdf("file"), "Ékezetes név.pdf")
    inline = client.get(f"/api/documents/{r['id']}/file")
    assert inline.status_code == 200 and inline.content.startswith(b"%PDF")
    assert inline.headers["content-type"] == "application/pdf"
    assert inline.headers["content-disposition"].startswith("inline")
    dl = client.get(f"/api/documents/{r['id']}/file", params={"download": 1})
    assert dl.headers["content-disposition"].startswith("attachment")


def test_text_endpoint(client):
    r = upload(client, text_pdf("plain text layer"), "t.pdf")
    t = client.get(f"/api/documents/{r['id']}/text").json()
    assert "plain text layer" in t["text"] and t["ocr_text"] == ""


def test_sorting_and_paging(client):
    for n in ["b.pdf", "a.pdf", "c.pdf"]:
        upload(client, text_pdf(n), n)
    items = client.get("/api/documents", params={"sort": "name", "order": "asc"}).json()["items"]
    assert [d["filename"] for d in items] == ["a.pdf", "b.pdf", "c.pdf"]
    page2 = client.get("/api/documents", params={"sort": "name", "order": "asc", "page": 2, "page_size": 2}).json()
    assert page2["total"] == 3 and [d["filename"] for d in page2["items"]] == ["c.pdf"]


def test_status_and_static(client):
    upload(client, text_pdf("x"), "x.pdf")
    s = client.get("/api/status").json()
    assert s["total"] == 1 and s["ocr_not_done"] == 1
    page = client.get("/")
    assert page.status_code == 200 and "PDF Manager" in page.text and "{{APP_NAME}}" not in page.text


def test_app_name_configurable(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "APP_NAME", "My <Docs>")
    for path in ("/", "/index.html"):
        page = client.get(path).text
        assert "<title>My &lt;Docs&gt;</title>" in page and "PDF Manager" not in page
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["name"] == m.json()["short_name"] == "My <Docs>"
    assert m.json()["icons"]



def test_builtin_viewer_served(client):
    page = client.get("/viewer.html")
    assert page.status_code == 200 and "viewer.js" in page.text
    pdfjs = client.get("/pdfjs/web/viewer.html")
    assert pdfjs.status_code == 200 and "viewer.mjs" in pdfjs.text
    assert client.get("/pdfjs/build/pdf.mjs").headers["content-type"].startswith("text/javascript")


# --------------------------------------------------------------------------- incremental loading (infinite scroll)

def test_offset_limit_paging(client):
    for i in range(25):
        upload(client, text_pdf(f"doc {i}"), f"d{i:02}.pdf", wait_text=False)
    p = {"sort": "name", "order": "asc"}
    first = client.get("/api/documents", params={**p, "offset": 0, "limit": 10}).json()
    assert first["total"] == 25 and first["offset"] == 0
    assert [d["filename"] for d in first["items"]] == [f"d{i:02}.pdf" for i in range(10)]
    more = client.get("/api/documents", params={**p, "offset": 10, "limit": 20}).json()
    assert [d["filename"] for d in more["items"]] == [f"d{i:02}.pdf" for i in range(10, 25)]
    assert client.get("/api/documents", params={**p, "offset": 25, "limit": 20}).json()["items"] == []
    # limit is capped, negative offset clamped
    r = client.get("/api/documents", params={"offset": -5, "limit": 999999}).json()
    assert r["offset"] == 0 and len(r["items"]) == 25


def test_batch_endpoint(client):
    a = upload(client, text_pdf("a"), "a.pdf", tags="x")["id"]
    b = upload(client, text_pdf("b"), "b.pdf")["id"]
    items = client.post("/api/documents/batch", json={"ids": [b, a, 999999, a]}).json()["items"]
    assert sorted(d["id"] for d in items) == sorted([a, b])
    assert next(d for d in items if d["id"] == a)["tags"] == ["x"]
    assert client.post("/api/documents/batch", json={"ids": []}).json() == {"items": []}


def test_status_change_log(client):
    seq = client.get("/api/status", params={"since": 0}).json()["changes"]["seq"]
    r = upload(client, text_pdf("change log"), "c.pdf")  # upload -> reload, extraction -> id
    ch = client.get("/api/status", params={"since": seq}).json()["changes"]
    assert ch["reload"] is True and r["id"] in ch["ids"] and ch["seq"] > seq

    seq = ch["seq"]
    assert client.get("/api/status", params={"since": seq}).json()["changes"] == {"seq": seq, "ids": [], "reload": False}

    client.patch(f"/api/documents/{r['id']}", json={"tags": ["t"]})  # single row -> id only
    ch = client.get("/api/status", params={"since": seq}).json()["changes"]
    assert ch["ids"] == [r["id"]] and ch["reload"] is False

    # a client that saw a higher seq (server restarted since) is told to reload
    assert client.get("/api/status", params={"since": ch["seq"] + 1000}).json()["changes"]["reload"] is True
    assert "changes" not in client.get("/api/status").json()


# --------------------------------------------------------------------------- PWA

def test_pwa_files(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    manifest = m.json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "./"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        r = client.get("/" + icon["src"])
        assert r.status_code == 200, icon["src"]
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    page = client.get("/").text
    assert 'rel="manifest"' in page and "serviceWorker" in client.get("/app.js").text


# --------------------------------------------------------------------------- duplicates

def _dup_library(client):
    a, b = text_pdf("Duplicate alpha"), text_pdf("Duplicate bravo")
    ids = {
        "a1": upload(client, a, "a1.pdf")["id"],
        "a2": upload(client, a, "a2.pdf", folder="Copies", skip_duplicates="false", tags="important")["id"],
        "a3": upload(client, a, "a3.pdf", folder="Other", skip_duplicates="false", tags="old")["id"],
        "b1": upload(client, b, "b1.pdf")["id"],
        "b2": upload(client, b, "b2.pdf", folder="Copies", skip_duplicates="false")["id"],
        "c": upload(client, text_pdf("Unique charlie"), "c.pdf")["id"],
    }
    return ids


def test_duplicates_list(client):
    ids = _dup_library(client)
    r = client.get("/api/duplicates").json()
    assert r["duplicates"] == 3
    groups = {frozenset(d["id"] for d in g["items"]): g for g in r["groups"]}
    assert set(groups) == {frozenset([ids["a1"], ids["a2"], ids["a3"]]), frozenset([ids["b1"], ids["b2"]])}
    ga = groups[frozenset([ids["a1"], ids["a2"], ids["a3"]])]
    assert ga["count"] == 3 and r["reclaimable"] == sum(g["size"] * (g["count"] - 1) for g in r["groups"])
    # suggested keeper: most tags, then oldest
    assert ga["items"][0]["id"] == ids["a2"]


def test_duplicates_none(client):
    upload(client, text_pdf("one"), "one.pdf")
    upload(client, text_pdf("two"), "two.pdf")
    assert client.get("/api/duplicates").json() == {"groups": [], "duplicates": 0, "reclaimable": 0}


def test_duplicates_delete(client):
    ids = _dup_library(client)
    r = client.post("/api/duplicates/delete", json={"keep": [ids["a1"], ids["b2"]]}).json()
    assert r == {"deleted": 3, "skipped": 0}
    assert search(client) == ["a1.pdf", "b2.pdf", "c.pdf"]
    assert client.get("/api/duplicates").json()["groups"] == []
    # tags of the deleted copies moved to the kept one
    assert doc(client, ids["a1"])["tags"] == ["important", "old"]
    # files gone from disk, empty folder removed
    assert not (db.LIBRARY_DIR / "Other").exists()
    assert not (db.LIBRARY_DIR / "Copies" / "a2.pdf").exists()
    assert (db.LIBRARY_DIR / "Copies" / "b2.pdf").is_file()


def test_duplicates_delete_only_listed_groups(client):
    ids = _dup_library(client)
    r = client.post("/api/duplicates/delete", json={"keep": [ids["b1"]]}).json()
    assert r == {"deleted": 1, "skipped": 0}
    assert search(client) == ["a1.pdf", "a2.pdf", "a3.pdf", "b1.pdf", "c.pdf"]


def test_duplicates_delete_compares_bytes(client):
    """A file changed on disk since import (stale hash) is never deleted as a duplicate."""
    ids = _dup_library(client)
    with open(db.LIBRARY_DIR / "Other" / "a3.pdf", "ab") as fh:
        fh.write(b"\n% changed\n")
    r = client.post("/api/duplicates/delete", json={"keep": [ids["a1"]]}).json()
    assert r == {"deleted": 1, "skipped": 1}
    assert search(client, folder="Other") == ["a3.pdf"]
    assert search(client, folder="Copies") == ["b2.pdf"]
