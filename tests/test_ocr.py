"""OCR tests (run real tesseract, so they take a few seconds each). OCR_PARALLEL is 2 here."""
import time

from app import db
from conftest import doc, scan_pdf, scan_pdf_pages, search, text_pdf, upload, wait_for, wait_ocr_idle


def test_scan_without_text_layer(client):
    r = upload(client, scan_pdf("Scanned contract number 98765"), "scan.pdf")
    d = doc(client, r["id"])
    assert d["text_status"] == "done" and not d["has_text"] and d["ocr_status"] == "none"
    assert search(client, q="contract") == []
    assert search(client, ocr="no_text") == ["scan.pdf"]


def test_ocr_single_document(client):
    r = upload(client, scan_pdf("Scanned contract number 98765"), "scan.pdf")
    assert client.post(f"/api/documents/{r['id']}/ocr").json() == {"queued": 1}
    wait_for(lambda: doc(client, r["id"])["ocr_status"] == "done", timeout=120)
    d = doc(client, r["id"])
    assert d["ocr_at"] and d["ocr_error"] is None
    assert search(client, q="contract 98765") == ["scan.pdf"]
    assert search(client, q='"contract number"') == ["scan.pdf"]
    assert "98765" in client.get(f"/api/documents/{r['id']}/text").json()["ocr_text"]
    assert search(client, ocr="done") == ["scan.pdf"]


def test_ocr_unprocessed_and_all(client):
    a = upload(client, scan_pdf("Alpha document"), "a.pdf")["id"]
    upload(client, scan_pdf("Bravo document"), "b.pdf")
    client.post(f"/api/documents/{a}/ocr")
    wait_ocr_idle(client)

    # only b is still unprocessed
    assert client.post("/api/ocr/unprocessed").json() == {"queued": 1}
    wait_ocr_idle(client)
    assert client.post("/api/ocr/unprocessed").json() == {"queued": 0}
    assert search(client, q="bravo") == ["b.pdf"]

    # "all" re-queues everything
    assert client.post("/api/ocr/all").json() == {"queued": 2}
    wait_ocr_idle(client)
    assert search(client, ocr="done") == ["a.pdf", "b.pdf"]


def test_ocr_folder(client):
    upload(client, scan_pdf("Folder one"), "in1.pdf", folder="Scans")
    upload(client, scan_pdf("Folder two"), "in2.pdf", folder="Scans/Old")
    upload(client, scan_pdf("Outside"), "out.pdf", folder="Other")
    upload(client, scan_pdf("Similar name"), "sim.pdf", folder="Scans2")

    # folder includes subfolders, but not "Scans2"
    assert client.post("/api/ocr/unprocessed", params={"folder": "Scans"}).json() == {"queued": 2}
    wait_ocr_idle(client)
    assert search(client, ocr="done") == ["in1.pdf", "in2.pdf"]
    assert search(client, ocr="not_done") == ["out.pdf", "sim.pdf"]

    assert client.post("/api/ocr/unprocessed", params={"folder": "Scans"}).json() == {"queued": 0}
    assert client.post("/api/ocr/all", params={"folder": "Scans/Old"}).json() == {"queued": 1}
    wait_ocr_idle(client)


def test_ocr_folder_root_only(client):
    upload(client, scan_pdf("Root doc"), "root.pdf")
    upload(client, scan_pdf("Sub doc"), "sub.pdf", folder="Sub")
    assert client.post("/api/ocr/unprocessed", params={"folder": "/"}).json() == {"queued": 1}
    wait_ocr_idle(client)
    assert search(client, ocr="done") == ["root.pdf"]


def test_ocr_cancel_queued(client):
    ids = [upload(client, scan_pdf(f"Doc {i}"), f"{i}.pdf")["id"] for i in range(3)]
    client.post("/api/ocr/all")
    r = client.post("/api/ocr/cancel").json()
    assert r["cancelled"] >= 1
    wait_ocr_idle(client)
    statuses = {doc(client, i)["ocr_status"] for i in ids}
    assert statuses <= {"none", "done"}  # nothing left queued/running


def test_ocr_bulk_selected(client):
    a = upload(client, scan_pdf("Selected"), "sel.pdf")["id"]
    upload(client, scan_pdf("Not selected"), "not.pdf")
    assert client.post("/api/bulk", json={"ids": [a], "action": "ocr"}).json() == {"affected": 1}
    wait_ocr_idle(client)
    assert search(client, ocr="done") == ["sel.pdf"]


def test_ocr_on_text_pdf_keeps_text(client):
    r = upload(client, text_pdf("Digital invoice text"), "digital.pdf")
    client.post(f"/api/documents/{r['id']}/ocr")
    wait_ocr_idle(client)
    assert doc(client, r["id"])["ocr_status"] == "done"
    assert search(client, q='"digital invoice"') == ["digital.pdf"]


def test_ocr_documents_in_parallel(client):
    """Single-page documents can't fill the worker pool alone: several run side by side."""
    for i, word in enumerate(["Alpha", "Bravo", "Charlie", "Delta"]):
        upload(client, scan_pdf(f"{word} parallel"), f"{i}.pdf")
    client.post("/api/ocr/all")
    max_running, end = 0, time.time() + 120
    while time.time() < end:
        s = client.get("/api/status").json()
        max_running = max(max_running, s["ocr_running"])
        assert s["ocr_running"] <= s["ocr_parallel"] == 2
        if s["ocr_queued"] + s["ocr_running"] == 0:
            break
        time.sleep(0.05)
    assert max_running == 2
    assert search(client, ocr="done") == ["0.pdf", "1.pdf", "2.pdf", "3.pdf"]
    assert search(client, q="charlie") == ["2.pdf"]


def test_ocr_multi_page_order(client):
    r = upload(client, scan_pdf_pages("First page", "Second page", "Third page"), "multi.pdf")
    client.post(f"/api/documents/{r['id']}/ocr")
    wait_ocr_idle(client)
    d = doc(client, r["id"])
    assert d["ocr_status"] == "done" and d["pages"] == 3
    text = client.get(f"/api/documents/{r['id']}/text").json()["ocr_text"]
    assert text.index("First") < text.index("Second") < text.index("Third")


def test_ocr_cancel_running(client):
    r = upload(client, scan_pdf_pages(*[f"Page {i}" for i in range(12)]), "long.pdf")
    client.post(f"/api/documents/{r['id']}/ocr")
    wait_for(lambda: doc(client, r["id"])["ocr_status"] == "running", timeout=30, interval=0.05)
    client.post(f"/api/documents/{r['id']}/ocr/cancel")
    wait_ocr_idle(client)
    assert doc(client, r["id"])["ocr_status"] == "none"
    assert list(db.TMP_DIR.iterdir()) == []  # rendered pages cleaned up


def test_duplicate_delete_keeps_ocr_text(client):
    data = scan_pdf("Duplicate scan 4711")
    a = upload(client, data, "a.pdf")["id"]
    b = upload(client, data, "b.pdf", skip_duplicates="false")["id"]
    client.post(f"/api/documents/{a}/ocr")
    wait_ocr_idle(client)
    # keep the copy that was never OCR'd: it inherits the OCR text of the deleted one
    assert client.post("/api/duplicates/delete", json={"keep": [b]}).json()["deleted"] == 1
    d = doc(client, b)
    assert d["ocr_status"] == "done" and d["ocr_at"]
    assert search(client, q="4711") == ["b.pdf"]
