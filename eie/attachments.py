"""Reads text out of email attachments so the classifier can use it as supporting context (FRD 4.2).

Only text that is already in the file is read: plain text, CSV, HTML, the text layer of a PDF, and the body of a
Word (.docx) file. There is no OCR, so scanned PDFs and images yield nothing, which is what Phase 1 specifies.
Nothing here ever raises: a damaged or odd file just gives no text.
"""
import html
import io
import logging
import re
import zipfile

from .textutil import html_to_text, normalize

log = logging.getLogger("eie")

MAX_BYTES = 2_000_000        # larger files are listed but not read
MAX_FILES = 5                # attachments read per email
MAX_CHARS = 4000             # kept per attachment
MAX_PDF_PAGES = 10
MAX_XML_BYTES = 5_000_000    # a .docx's document.xml, uncompressed (guards against zip bombs)

_TEXT_EXT = (".txt", ".csv", ".tsv", ".log", ".md")
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _kind(name: str, content_type: str) -> str:
    """text | html | pdf | docx | '' (not readable)."""
    name, ctype = (name or "").lower(), (content_type or "").lower()
    if ctype == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if ctype == _DOCX or name.endswith(".docx"):
        return "docx"
    if ctype in ("text/html", "application/xhtml+xml") or name.endswith((".html", ".htm")):
        return "html"
    if ctype in ("text/plain", "text/csv", "text/tab-separated-values", "text/markdown") or name.endswith(_TEXT_EXT):
        return "text"
    return ""


def eligible(name: str, content_type: str, size, inline: bool = False) -> bool:
    """Worth downloading and reading? Inline images, huge files and unreadable types are skipped."""
    if inline or not _kind(name, content_type):
        return False
    return size is None or size <= MAX_BYTES


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # only a byte-order mark says UTF-16; Latin-1 text can look like it
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        return data.decode("cp1252", errors="replace")  # what Windows tools write when it isn't UTF-8


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return ""
    return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > MAX_XML_BYTES:
            return ""
        xml = z.read(info).decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>|<w:br\s*/>|<w:tab\s*/>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def extract_text(name: str, content_type: str, data: bytes) -> str:
    """The readable text of an attachment (at most MAX_CHARS), or '' if there is none."""
    try:
        kind = _kind(name, content_type)
        if not kind or not data or len(data) > MAX_BYTES:
            return ""
        raw = {"text": lambda: _decode(data), "html": lambda: html_to_text(_decode(data)),
               "pdf": lambda: _pdf(data), "docx": lambda: _docx(data)}[kind]()
        return normalize(raw)[:MAX_CHARS]
    except Exception as exc:  # corrupt PDF, bad zip, ...
        log.info("Couldn't read attachment %s: %s", name, exc)
        return ""
