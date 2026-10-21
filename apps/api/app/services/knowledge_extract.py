"""Plain text out of uploaded files: text and Markdown, CSV, HTML, Word (.docx) and PDF.

Only the standard library is used for everything except PDF. Files are untrusted:
sizes are capped, a .docx is a zip so its expanded size is capped too, and a PDF
with no text layer (a scan) is refused with a clear message rather than stored empty."""

import io
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 10_000_000
MAX_TEXT_CHARS = 500_000
MAX_EXPANDED_BYTES = 50_000_000
MAX_PDF_PAGES = 500
SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".docx", ".pdf")


class ExtractError(ValueError):
    pass


class _Text(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", text)).strip()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractError("The file is not UTF-8 text") from exc


def _docx(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_EXPANDED_BYTES:
                raise ExtractError("The Word file is too large once opened")
            xml = archive.read(info)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractError("This is not a valid .docx file") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for para in ElementTree.fromstring(xml).iter(f"{ns}p"):
        runs = []
        for node in para.iter():
            if node.tag == f"{ns}t" and node.text:
                runs.append(node.text)
            elif node.tag in (f"{ns}tab", f"{ns}br"):
                runs.append(" ")
        line = "".join(runs).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _pdf(raw: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ExtractError("The PDF is password protected")
        pages = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
    except ExtractError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as exc:
        raise ExtractError("This is not a readable PDF") from exc
    if not pages:
        raise ExtractError("This PDF has no text in it (is it a scan?). Upload a text version.")
    return "\n\n".join(pages)


def extract_text(filename: str, raw: bytes) -> str:
    name = filename.lower()
    if not name.endswith(SUFFIXES):
        raise ExtractError(f"Unsupported file type; allowed: {', '.join(SUFFIXES)}")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ExtractError("File exceeds 10 MB")
    if name.endswith(".pdf"):
        text = _pdf(raw)
    elif name.endswith(".docx"):
        text = _docx(raw)
    elif name.endswith((".html", ".htm")):
        text = html_to_text(_decode(raw))
    else:
        text = _decode(raw)
    text = text.strip()
    if not text:
        raise ExtractError("File is empty")
    return text[:MAX_TEXT_CHARS]
