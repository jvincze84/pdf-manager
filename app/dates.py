"""Document date: when the document was made, not when it was uploaded.

Order of preference (first usable value wins):
  1. PDF creation date   (Info /CreationDate, else XMP xmp:CreateDate)  -> "pdf_created"
  2. PDF modification date (Info /ModDate, else XMP xmp:ModifyDate)      -> "pdf_modified"
  3. a date in the file name (2026-10-21, 2026_10_21, 2026.10.21, 20261021,
     optionally followed by a time such as _17-04-23)                    -> "filename"
  4. file modification time (sent by the browser on upload / disk mtime) -> "file"
  5. upload time                                                         -> "added"
Dates outside 1980..now+1 day are treated as missing (broken scanner clocks, zeroed fields).
Values are stored as UTC ISO strings like file_date / added_at; dates without a time zone
are taken as the server's local time.
"""
import re
from datetime import datetime, timedelta, timezone

SOURCES = ("pdf_created", "pdf_modified", "filename", "file", "added")

_PDF_DATE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"\s*(?:(Z)|([+-])(\d{2})'?(\d{2})?'?)?"
)
_XMP_DATE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(?:(Z)|([+-])(\d{2}):?(\d{2}))?"
)
# date in a file name; the digits must not be part of a longer number
_NAME_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})([-_.]?)(\d{2})\2(\d{2})(?!\d)"
    r"(?:[ _T.-]{1,3}(\d{2})[-_.:](\d{2})(?:[-_.:](\d{2}))?(?!\d))?"
)


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.year < 1980 or dt > now + timedelta(days=1):
        return None
    return dt


def _build(y, mo, d, h, mi, s, z, sign, tzh, tzm) -> datetime | None:
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if z:
        dt = dt.replace(tzinfo=timezone.utc)
    elif sign:
        off = timedelta(hours=int(tzh), minutes=int(tzm or 0))
        dt = dt.replace(tzinfo=timezone(off if sign == "+" else -off))
    else:
        dt = dt.astimezone()  # naive -> server local time
    return _valid(dt)


def parse_pdf_date(s: str | None) -> datetime | None:
    """'D:20260202093229+01'00'' -> aware datetime (None if missing/invalid)."""
    if not s:
        return None
    m = _PDF_DATE.match(s.strip())
    return _build(*m.groups()) if m else None


def parse_xmp_date(s: str | None) -> datetime | None:
    """'2026-02-02T09:32:29+01:00' -> aware datetime."""
    if not s:
        return None
    m = _XMP_DATE.match(s.strip())
    return _build(*m.groups()) if m else None


def _xmp_value(xml: str, name: str) -> str | None:
    """xmp:CreateDate as attribute (xmp:CreateDate="...") or element (<xmp:CreateDate>...)."""
    m = (re.search(rf'{name}\s*=\s*"([^"]+)"', xml)
         or re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", xml))
    return m.group(1) if m else None


def date_from_filename(name: str) -> datetime | None:
    for m in _NAME_DATE.finditer(name):
        y, _sep, mo, d, h, mi, s = m.groups()
        dt = _build(y, mo, d, h, mi, s, None, None, None, None)
        if dt:
            return dt
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def pdf_dates(pdf) -> tuple[datetime | None, datetime | None]:
    """(created, modified) from an open PyMuPDF document."""
    meta = pdf.metadata or {}
    created = parse_pdf_date(meta.get("creationDate"))
    modified = parse_pdf_date(meta.get("modDate"))
    if not (created and modified):
        try:
            xml = pdf.get_xml_metadata() or ""
        except Exception:  # noqa: BLE001
            xml = ""
        if xml:
            created = created or parse_xmp_date(_xmp_value(xml, "xmp:CreateDate"))
            modified = modified or parse_xmp_date(_xmp_value(xml, "xmp:ModifyDate"))
    return created, modified


def resolve(pdf, filename: str, file_date: str | None, added_at: str) -> tuple[str, str]:
    """(iso date, source) for a document; pdf may be None (unreadable file)."""
    created, modified = pdf_dates(pdf) if pdf is not None else (None, None)
    if created:
        return _iso(created), "pdf_created"
    if modified:
        return _iso(modified), "pdf_modified"
    from_name = date_from_filename(filename)
    if from_name:
        return _iso(from_name), "filename"
    if file_date:
        return file_date, "file"
    return added_at, "added"
