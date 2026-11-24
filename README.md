# PDF Manager

Small self-hosted PDF library with full-text search, tags and on-demand OCR. Runs in Docker.

Screenshots avaiable in screenshots folder (github)

## Start

```bash
docker compose up -d --build
```

Open http://localhost:8000. There is no built-in authentication: put it behind your reverse proxy.
All URLs are relative, so it also works under a sub-path (e.g. `https://example.com/pdf/`).

## Features

- **Bulk upload**: multiple files or whole folders (folder structure is kept), drag & drop, target folder, tags for all files, duplicate detection (same content).
- **Text extraction**: runs automatically after upload (PDF text layer).
- **OCR**: never automatic. Each row has a red (not done) / green (done) / yellow (queued/running) **OCR** badge and a button. Global buttons: **OCR unprocessed documents** and **OCR all documents**. When a folder is selected in the Folder filter, **OCR unprocessed** / **OCR all** buttons for that folder (including subfolders) appear in the status line. OCR runs in a background queue, survives restarts and can be cancelled.
- **Content search** (top search box; file name + text layer + OCR text, case and accents ignored):
  - `invoice 2024`: all words (prefix match)
  - `"exact phrase"`: exact phrase
  - `-draft`: exclude, `tax OR vat`: either
- **File name search** (separate "File name" box): substring match, every word must occur, case and accents ignored (`szamla 2024` finds `Számla_2024_január.pdf`). Can be combined with the content search and all filters.
- **Tags**, folder filter, OCR status filter, sortable columns (name, folder, size, pages, document date, added), bulk actions (OCR, tag, move, delete).
- **Clear all filters & search**: appears next to the filters whenever a search or filter is active (and in the "no match" message); resets the content search, file-name search, folder, tag and OCR filters in one click (the sort order is kept).
- **Large libraries**: the list loads the first 100 documents, then 20 more each time you scroll near the bottom, so the browser never renders thousands of rows at once. Status changes (OCR progress etc.) update the affected rows in place.
- **Extracted text viewer** (edit dialog → "Show extracted text"): text layer / OCR text tabs with a **Copy to clipboard** button.
- **Built-in PDF viewer** ([PDF.js](https://mozilla.github.io/pdf.js/), bundled at image build time): clicking a file name opens it inside the app with a Back button (works the same in the installed app on Android/iOS), plus *Open in browser* (the browser's own PDF viewer) and *Download* – both are also in each row's action buttons. The PDF.js version is the `PDFJS_VERSION` build arg in the Dockerfile.
- **Installable as an app (PWA)**: in Chrome/Edge use the install icon in the address bar (or menu → "Install PDF Manager"), on Android "Add to home screen", on iOS Safari Share → "Add to Home Screen". Requires HTTPS (or `localhost`).
- Light / dark theme (follows the system by default, toggle top right).
- **Find duplicates** (⋯ menu): lists groups of files with exactly the same content (byte-identical, SHA-256), wherever they are and whatever their names. Pick the copy to keep in each group (suggested: the OCR'd one, then the one with most tags, then the oldest) or *Keep all*, then delete the rest in one go. Tags of the deleted copies – and their OCR text, if the kept copy has none – are moved to the kept copy. Files are compared byte by byte again right before deleting. Direct link: `/#duplicates`.
- **Rescan library folder** (⋯ menu): imports PDFs copied directly into `data/library`, and removes entries whose file is gone.
- **Document date** (*Date* column) vs. **upload date** (*Added* column) – see below.

## Document date

The *Date* column shows when the document was made, not when it was uploaded. It is taken from the first available of:

1. the PDF's creation date (Info `CreationDate`, else XMP `xmp:CreateDate`);
2. the PDF's modification date (`ModDate` / `xmp:ModifyDate`);
3. a date in the file name: `2026-10-21`, `2026_10_21`, `2026.10.21` or `20261021`, optionally followed by a time (`2026-10-21 _17-04-23`) – e.g. CamScanner file names;
4. the file's modification time (sent by the browser on upload, or the disk mtime for rescanned files);
5. the upload date.

Dates before 1980 or in the future are ignored (broken scanner clocks). Hover the date to see where it came from, the file modification time and the upload date. The upload date is kept in its own *Added* column (hidden on narrow screens), and both columns are sortable; the default order is newest document date first.

The date is computed in the background together with the text extraction (upload, rescan). It is recomputed automatically when a document is renamed (the name may contain the date), and for every existing document the first time a version with this feature starts. To recompute all dates by hand – e.g. after replacing files on disk – use **⋯ → Refresh document dates** (or `POST /api/dates/refresh`, optionally `?folder=...`).

## Rescan library folder

Syncs the database with the files on disk (`data/library`): PDFs that are on disk but not in the database are added (folder = sub-directory, text extracted automatically), and entries whose file no longer exists are removed. Use it after copying files into the library directly or when mounting an existing collection. Renaming/moving files on disk counts as delete + add (tags and OCR text are lost) – rename/move inside the app instead.

## Configuration (docker-compose.yml)

| Variable        | Default   | Meaning |
|-----------------|-----------|---------|
| `APP_NAME`      | `PDF Manager` | Name shown in the header, browser tab and installed app (PWA) |
| `OCR_LANG`      | `eng+hun` | Tesseract languages (installed in the image: eng, hun, deu) |
| `OCR_DPI`       | `300`     | Render resolution for OCR |
| `OCR_PARALLEL`  | `2`       | Tesseract processes running at once (shared by all documents: a long document uses all of them for its pages, short documents are OCR'd side by side). Up to about the number of CPU cores. |
| `MAX_UPLOAD_MB` | `500`     | Max size per uploaded file |
| `TZ`            | `UTC`     | Time zone used for dates that have none (times in file names, some PDF dates), e.g. `Europe/Budapest` |

## Data

Everything is in the `/data` volume (`./data` with the compose file):

- `data/library/` – the PDF files, in their folders
- `data/db/pdfmanager.db` – SQLite database (metadata, tags, FTS5 search index)

Backup = copy the `data` directory (preferably with the container stopped).

The container runs as root, so the files under `./data` are owned by root. To change that, add `user: "1000:1000"` to the service (and `chown` the existing `data` directory).

Reverse proxy notes:
- allow large request bodies for uploads (nginx: `client_max_body_size 500m;`);
- PWA install and the service worker need HTTPS;
- the manifest is requested with credentials (`crossorigin="use-credentials"`), so cookie / basic-auth protected proxies work. `sw.js`, `manifest.webmanifest` and `icons/` must be reachable through the same auth as the page.

## API

Everything the UI does goes through a small JSON API under `/api/` (e.g. `GET /api/documents?q=...&name=...&folder=...&tag=...&ocr=...`, `POST /api/documents/upload`, `POST /api/ocr/unprocessed?folder=...`, `POST /api/rescan`, `POST /api/dates/refresh?folder=...`, `GET /api/duplicates`, `POST /api/duplicates/delete`). Interactive docs: `/docs`.

## Development & tests

Tests run inside Docker (same image as production + pytest), nothing needs to be installed locally:

```bash
tests/run.sh                    # JS syntax check + all pytest tests (~1 min)
tests/run.sh -k name_search     # only some tests
tests/run.sh tests/test_ocr.py  # one file
tests/screenshot.sh             # start app with sample data, save light/dark/search/duplicates/viewer screenshots to tests/output/
tests/scroll_check.sh           # browser check of the incremental list loading (230 docs)
```

App icons are generated from the SVG in `scripts/make_icons.py`:
`docker run --rm -u $(id -u):$(id -g) -v "$PWD:/w" -w /w pdf-manager:test python scripts/make_icons.py`

| File | What |
|------|------|
| `tests/test_search_syntax.py` | search string -> FTS5 query translation (unit tests) |
| `tests/test_api.py` | upload, duplicate skipping, find/delete duplicates, content & file-name search, folders, tags, rename/move, delete, rescan, downloads, offset/limit paging, batch refresh, change log, PWA files |
| `tests/test_dates.py` | document date: PDF/XMP date and file-name parsing, priority order, rename, refresh, DB migration |
| `tests/test_ocr.py` | OCR single / unprocessed / all / per folder / cancel (queued + running) / bulk / multi-page / parallel documents (real tesseract) |
| `tests/seed.py` | uploads sample PDFs to a running instance (`python tests/seed.py http://host:port [--many N]`) |
| `tests/viewer_check.js` | built-in PDF viewer check in a current headless Chrome (list → viewer → Back), run by `screenshot.sh` |
