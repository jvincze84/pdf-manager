"""Shared fixtures. Tests run inside the Docker test image (see tests/run.sh): they need
PyMuPDF and tesseract, exactly like the app."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Must happen before the app is imported: app.db reads DATA_DIR at import time.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pdfm-test-")
os.environ.setdefault("OCR_DPI", "150")  # faster OCR in tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from conftest_helpers import scan_pdf, scan_pdf_pages, text_pdf  # noqa: E402,F401
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_library(request):
    """Every test starts with an empty library (skipped for tests that don't use the client)."""
    if "client" not in request.fixturenames:
        yield
        return
    c = request.getfixturevalue("client")
    c.post("/api/ocr/cancel")
    ids = [d["id"] for d in c.get("/api/documents", params={"page_size": 500}).json()["items"]]
    if ids:
        c.post("/api/bulk", json={"ids": ids, "action": "delete"})
    # wait until the OCR worker is idle so a cancelled job cannot leak into the next test
    wait_for(lambda: c.get("/api/status").json()["ocr_running"] == 0, timeout=60)
    for p in sorted(db.LIBRARY_DIR.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    yield


# --------------------------------------------------------------------------- helpers

def wait_for(cond, timeout=30.0, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def upload(client, data: bytes, filename="doc.pdf", wait_text=True, **form):
    form = {k: str(v) for k, v in form.items()}
    r = client.post("/api/documents/upload", files={"file": (filename, data, "application/pdf")}, data=form)
    assert r.status_code == 200, r.text
    res = r.json()
    if wait_text and res["status"] == "ok":
        wait_for(lambda: client.get(f"/api/documents/{res['id']}").json()["text_status"] != "pending")
    return res


def search(client, **params) -> list[str]:
    """Return the file names matching the given /api/documents params."""
    r = client.get("/api/documents", params={"page_size": 500, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query_error"] is None, body
    return sorted(d["filename"] for d in body["items"])


def doc(client, doc_id: int) -> dict:
    return client.get(f"/api/documents/{doc_id}").json()


def wait_ocr_idle(client, timeout=120):
    wait_for(lambda: (lambda s: s["ocr_queued"] + s["ocr_running"] == 0)(client.get("/api/status").json()),
             timeout=timeout, interval=0.5)
