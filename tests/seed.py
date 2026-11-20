"""Upload sample PDFs to a running PDF Manager (demo / screenshot data).

    python seed.py http://localhost:8000          5 realistic samples (+ OCR one)
    python seed.py http://localhost:8000 --many 300   300 small numbered PDFs (list/scroll tests)

Needs PyMuPDF + httpx2: run it in the test image (see screenshot.sh / scroll_check.sh).
"""
import sys

import httpx2

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from conftest_helpers import scan_pdf, text_pdf  # noqa: E402

args = sys.argv[1:]
many = 0
if "--many" in args:
    i = args.index("--many")
    many = int(args[i + 1])
    del args[i:i + 2]
base = (args[0] if args else "http://localhost:8000").rstrip("/") + "/api/"

if many:
    with httpx2.Client(timeout=60) as c:
        for n in range(many):
            c.post(base + "documents/upload", data={"folder": f"Bulk/{n // 100:02}"},
                   files={"file": (f"bulk_{n:05}.pdf", text_pdf(f"Bulk document number {n}"), "application/pdf")})
    print(f"uploaded {many} documents")
    sys.exit(0)

samples = [
    ("Invoice_2024_001.pdf", "Invoices/2024", "invoice, 2024", text_pdf("Invoice 2024/001", "Total: 1200 EUR", "Ez egy pontos keresés teszt.")),
    ("Invoice_2024_002.pdf", "Invoices/2024", "invoice, 2024", text_pdf("Invoice 2024/002", "Total: 80 EUR")),
    ("Szerződés.pdf", "Contracts", "contract", text_pdf("Rental contract agreement", "Bérleti szerződés")),
    ("scanned_letter.pdf", "Scans", "", scan_pdf("Scanned letter", "Reference 98765")),
    ("scanned_receipt.pdf", "Scans/Old", "receipt", scan_pdf("Receipt for coffee")),
]
with httpx2.Client(timeout=60) as c:
    for name, folder, tags, data in samples:
        r = c.post(base + "documents/upload", files={"file": (name, data, "application/pdf")},
                   data={"folder": folder, "tags": tags})
        print(name, "->", r.json().get("status"))
    # exact duplicates for the "Find duplicates" dialog
    # (enough groups to overflow the dialog, plus a long path to check wrapping)
    for name, folder, tags, data in samples[:3]:
        c.post(base + "documents/upload", files={"file": ("copy_of_" + name, data, "application/pdf")},
               data={"folder": "Downloads", "tags": tags if name.endswith("1.pdf") else "", "skip_duplicates": "false"})
    c.post(base + "documents/upload",
           files={"file": ("Invoice_2024_001_final_version_scanned_and_signed_by_accounting.pdf", samples[0][3], "application/pdf")},
           data={"folder": "Archive/2024/Finance department/Quarterly reports and invoices/Q1", "skip_duplicates": "false"})
    first_scan = c.get(base + "documents", params={"name": "scanned_letter"}).json()["items"][0]["id"]
    c.post(base + f"documents/{first_scan}/ocr")
    print("OCR queued for scanned_letter.pdf")
