"""Document Management System primitives: paths, extraction, section-aware
chunking, patching, diffing, and encrypted payload framing.

Zero external dependencies: everything here runs on the Python standard
library and Compartment's bundled local embedder, keeping the offline and
cross-platform wheel guarantees intact.
"""
from __future__ import annotations

import difflib
import html.parser
import io
import json
import mimetypes
import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .crypto import CryptoError, canonical_json

#: Binary envelope magic inside AEAD-sealed document versions.
DOC_ENVELOPE_MAGIC = b"CDOC\x01"

#: Default per-document size ceiling (32 MiB uncompressed).
DEFAULT_MAX_DOC_BYTES = 32 * 1024 * 1024

#: Above this compressed size, a document write commits via atomic Vault.save()
#: instead of appending a huge journal frame (stays well below MAX_JOURNAL_ENTRY).
JOURNAL_INLINE_MAX_BYTES = 8 * 1024 * 1024

#: Maximum semantic chunks embedded per document version (~100k tokens).
#: Full text beyond this cap remains searchable in `doc_fts`.
MAX_DOC_CHUNKS = 256

#: Valid lifecycle states for a document.
VALID_DOC_STATUSES = ("active", "draft", "archived")

#: Subtle multiplicative ranking boosts for document search.
W_DOC_PINNED = 0.12
W_DOC_TITLE_MATCH = 0.20

#: Extensions known to hold UTF-8 / text content.
_TEXT_EXT_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".rst": "text/x-rst",
    ".org": "text/x-org",
    ".tex": "application/x-tex",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".env": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".rs": "text/x-rust",
    ".go": "text/x-go",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cc": "text/x-c++",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".swift": "text/x-swift",
    ".kt": "text/x-kotlin",
    ".java": "text/x-java",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".ps1": "text/x-powershell",
    ".sql": "application/sql",
    ".log": "text/plain",
    ".diff": "text/x-diff",
    ".patch": "text/x-diff",
}

_BINARY_EXT_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".epub": "application/epub+zip",
    ".rtf": "application/rtf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
}

_TEXT_MIME_EXACT = {
    "application/json",
    "application/x-ndjson",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/xml",
    "application/xhtml+xml",
    "application/sql",
    "application/javascript",
    "application/x-tex",
    "image/svg+xml",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


# ------------------------------------------------------------------- paths

def slugify(text: str) -> str:
    """Turn a free-form title into a safe path segment."""
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s[:80] or "document"


def normalize_doc_path(path: str | None = None, *, title: str | None = None,
                       mime_type: str | None = None) -> str:
    """Canonical relative POSIX document path (e.g. 'taxes/2026/summary.md').

    Rejects traversal segments ('..', '.'), NUL bytes and control characters
    so paths are safe keys inside the vault and safe relative export targets."""
    raw = (path or "").strip()
    if not raw:
        if not (title or "").strip():
            raise CryptoError(
                "A document needs either a 'path' (e.g. 'notes/spec.md') "
                "or a 'title'.")
        ext = ".md"
        mt = (mime_type or "").lower()
        if mt == "text/plain":
            ext = ".txt"
        elif mt == "application/json":
            ext = ".json"
        elif mt and not is_text_mime(mt):
            guessed = mimetypes.guess_extension(mt) or ""
            ext = guessed or ".bin"
        raw = f"documents/{slugify(title)}{ext}"
    if "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise CryptoError(f"Invalid control character in document path {path!r}")
    raw = raw.replace("\\", "/").strip("/")
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if not parts:
        raise CryptoError(f"Invalid empty document path {path!r}")
    for seg in parts:
        if seg in (".", ".."):
            raise CryptoError(
                f"Document path {path!r} must not contain '.' or '..' segments")
        if ":" in seg and len(seg) == 2 and seg[0].isalpha():
            raise CryptoError(
                f"Document path {path!r} must be relative, not a drive path")
    return "/".join(parts)


def folder_of(path: str) -> str:
    """Parent folder of a normalized document path, or '' at root."""
    d = posixpath.dirname(path)
    return "" if d in ("", ".") else d


def default_title(path: str, text: str | None = None) -> str:
    """Derive a clean human title from a document's first H1 or filename."""
    if text:
        in_fence = False
        seen_nonempty = 0
        for line in text.splitlines():
            stripped = line.strip()
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence or not stripped:
                continue
            seen_nonempty += 1
            m = _HEADING_RE.match(stripped)
            if m and len(m.group(1)) == 1:
                h = m.group(2).strip()
                if h:
                    return h[:200]
            if seen_nonempty >= 5:
                break
    base = posixpath.basename(path)
    stem = posixpath.splitext(base)[0] if "." in base else base
    return (stem or base)[:200]


# --------------------------------------------------------- mime & extraction

def is_text_mime(mime_type: str) -> bool:
    mt = (mime_type or "").split(";")[0].strip().lower()
    if mt.startswith("text/"):
        return True
    if mt in _TEXT_MIME_EXACT:
        return True
    return mt.endswith("+json") or mt.endswith("+xml")


def detect_mime_type(path: str, raw: bytes | None = None,
                     explicit: str | None = None) -> tuple[str, bool]:
    """Return (mime_type, is_binary) from explicit override, extension, or bytes."""
    if explicit and explicit.strip():
        mt = explicit.split(";")[0].strip().lower()
        return mt, not is_text_mime(mt)
    ext = posixpath.splitext(path)[1].lower()
    if ext in _TEXT_EXT_MIME:
        return _TEXT_EXT_MIME[ext], False
    if ext in _BINARY_EXT_MIME:
        return _BINARY_EXT_MIME[ext], True
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        mt = guessed.lower()
        return mt, not is_text_mime(mt)
    if raw is not None:
        if not raw:
            return "text/plain", False
        sample = raw[:8192]
        if b"\x00" not in sample:
            try:
                sample.decode("utf-8")
                return "text/plain", False
            except UnicodeDecodeError:
                pass
        return "application/octet-stream", True
    return "text/markdown", False


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("script", "style", "noscript"):
            self._skip += 1
        elif t in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
                   "h5", "h6", "section", "article", "blockquote", "pre"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("script", "style", "noscript") and self._skip > 0:
            self._skip -= 1
        elif t in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip == 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        joined = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _strip_html(text: str) -> str:
    p = _HTMLTextExtractor()
    p.feed(text)
    p.close()
    return p.get_text()


def _extract_xml_paragraphs(xml_bytes: bytes, para_tags: tuple[str, ...]) -> str:
    root = ET.fromstring(xml_bytes)
    lines: list[str] = []
    para_set = set(para_tags)
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in para_set:
            txt = "".join(el.itertext()).strip()
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def _extract_docx(raw: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist()
                 if n == "word/document.xml"
                 or (n.startswith("word/header") and n.endswith(".xml"))
                 or (n.startswith("word/footer") and n.endswith(".xml"))]
        names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
        for name in names:
            txt = _extract_xml_paragraphs(zf.read(name), ("p",))
            if txt:
                parts.append(txt)
    return "\n\n".join(parts)


def _extract_odt(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "content.xml" not in zf.namelist():
            return ""
        return _extract_xml_paragraphs(zf.read("content.xml"), ("p", "h"))


def _extract_pptx(raw: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        slides = sorted(
            n for n in zf.namelist()
            if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for idx, name in enumerate(slides, 1):
            txt = _extract_xml_paragraphs(zf.read(name), ("p",))
            if txt:
                parts.append(f"## Slide {idx}\n{txt}")
    return "\n\n".join(parts)


def _extract_xlsx(raw: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "xl/sharedStrings.xml" in zf.namelist():
            txt = _extract_xml_paragraphs(zf.read("xl/sharedStrings.xml"),
                                          ("si",))
            if txt:
                parts.append(txt)
        sheets = sorted(
            n for n in zf.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for name in sheets:
            txt = _extract_xml_paragraphs(zf.read(name), ("is",))
            if txt:
                parts.append(txt)
    return "\n\n".join(parts)


def _extract_epub(raw: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        docs = sorted(
            n for n in zf.namelist()
            if n.lower().endswith((".xhtml", ".html", ".htm")))
        for name in docs:
            html_str = zf.read(name).decode("utf-8", errors="replace")
            txt = _strip_html(html_str)
            if txt:
                parts.append(txt)
    return "\n\n".join(parts)


def _extract_rtf(raw: bytes) -> str:
    text = raw.decode("latin-1", errors="replace")
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes.fromhex(m.group(1)).decode("latin-1"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_BT_RE = re.compile(rb"BT\b(.*?)\bET", re.DOTALL)
_PDF_STR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _pdf_unescape(raw_lit: bytes) -> str:
    inner = raw_lit[1:-1]
    out = bytearray()
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == 92 and i + 1 < n:  # backslash
            nxt = inner[i + 1]
            if nxt == ord("n"):
                out.append(10)
                i += 2
            elif nxt == ord("r"):
                out.append(13)
                i += 2
            elif nxt == ord("t"):
                out.append(9)
                i += 2
            elif nxt in (ord("("), ord(")"), ord("\\")):
                out.append(nxt)
                i += 2
            elif ord("0") <= nxt <= ord("7"):
                j = i + 1
                while j < min(n, i + 4) and ord("0") <= inner[j] <= ord("7"):
                    j += 1
                out.append(int(inner[i + 1:j], 8) & 0xFF)
                i = j
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(ch)
            i += 1
    return out.decode("latin-1", errors="replace")


def _extract_pdf_text(raw: bytes) -> str:
    """Lightweight, pure-stdlib best-effort text extraction for simple PDFs.

    Handles uncompressed and FlateDecode content streams safely with strict
    size caps. Never raises on encrypted, scanned, or complex PDFs (returns
    '' cleanly so caller-provided extracted_text or metadata still works)."""
    if not raw.startswith(b"%PDF"):
        return ""
    blocks: list[str] = []
    max_decompressed = 8 * 1024 * 1024
    for m in _PDF_STREAM_RE.finditer(raw):
        stream_data = m.group(1)
        if not stream_data:
            continue
        decoded = None
        if b"BT" in stream_data and b"ET" in stream_data:
            decoded = stream_data
        else:
            try:
                dobj = zlib.decompressobj()
                chunk = dobj.decompress(stream_data, max_decompressed)
                if b"BT" in chunk and b"ET" in chunk:
                    decoded = chunk
            except zlib.error:
                decoded = None
        if not decoded:
            continue
        for bt in _PDF_BT_RE.finditer(decoded):
            body = bt.group(1)
            pieces = [_pdf_unescape(s.group(0))
                      for s in _PDF_STR_RE.finditer(body)]
            joined = "".join(pieces).strip()
            if joined and any(c.isalnum() for c in joined):
                blocks.append(joined)
        if sum(len(b) for b in blocks) > 2 * 1024 * 1024:
            break
    return "\n".join(blocks).strip()


def extract_searchable_text(raw: bytes, mime_type: str,
                            path: str = "") -> str:
    """Extract clean searchable UTF-8 text from raw bytes using stdlib only.

    Never fails on arbitrary binary files: unreadable formats return ''."""
    if not raw:
        return ""
    mt = (mime_type or "").split(";")[0].strip().lower()
    ext = posixpath.splitext(path)[1].lower()
    try:
        if mt in ("text/html", "application/xhtml+xml") or ext in (".html", ".htm"):
            return _strip_html(raw.decode("utf-8", errors="replace"))
        if is_text_mime(mt):
            return raw.decode("utf-8", errors="replace")
        if ext == ".docx" or "wordprocessingml" in mt:
            return _extract_docx(raw)
        if ext == ".odt" or "opendocument.text" in mt:
            return _extract_odt(raw)
        if ext == ".pptx" or "presentationml" in mt:
            return _extract_pptx(raw)
        if ext == ".xlsx" or "spreadsheetml" in mt:
            return _extract_xlsx(raw)
        if ext == ".epub" or mt == "application/epub+zip":
            return _extract_epub(raw)
        if ext == ".rtf" or mt == "application/rtf":
            return _extract_rtf(raw)
        if ext == ".pdf" or mt == "application/pdf":
            return _extract_pdf_text(raw)
    except Exception:                                   # noqa: BLE001
        return ""
    return ""


# ------------------------------------------------------- payload framing

def pack_version_payload(raw: bytes, *, extracted_text: str = "",
                         filename: str | None = None) -> tuple[bytes, str]:
    """Pack raw bytes + extracted text into a binary envelope with optional
    zlib compression (skipped when already compressed, e.g. PNG/PDF/ZIP)."""
    meta: dict = {}
    if extracted_text:
        meta["extracted_text"] = extracted_text
    if filename:
        meta["filename"] = filename
    meta_b = canonical_json(meta)
    envelope = (DOC_ENVELOPE_MAGIC
                + struct.pack(">I", len(meta_b))
                + meta_b
                + raw)
    if len(envelope) >= 256:
        comp = zlib.compress(envelope, level=6)
        if len(comp) + 64 < len(envelope) and len(comp) < len(envelope) * 0.92:
            return comp, "zlib"
    return envelope, "none"


def unpack_version_payload(blob: bytes,
                           compression: str = "none") -> tuple[bytes, dict]:
    """Unpack a version payload back into (raw_bytes, meta_dict)."""
    data = zlib.decompress(blob) if compression == "zlib" else blob
    if len(data) < len(DOC_ENVELOPE_MAGIC) + 4 or not data.startswith(DOC_ENVELOPE_MAGIC):
        raise CryptoError("Document version payload envelope is corrupt")
    off = len(DOC_ENVELOPE_MAGIC)
    (meta_len,) = struct.unpack(">I", data[off:off + 4])
    off += 4
    if off + meta_len > len(data):
        raise CryptoError("Document version payload metadata is truncated")
    meta = json.loads(data[off:off + meta_len].decode("utf-8"))
    raw = data[off + meta_len:]
    return raw, meta


# ------------------------------------------- outline, sections, & chunking

def extract_outline(text: str) -> list[dict]:
    """Parse Markdown ATX headings (skipping fenced code blocks) into a TOC."""
    if not text:
        return []
    headings: list[dict] = []
    stack: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    pos = 0
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        raw_line = line.rstrip("\r\n")
        fm = _FENCE_RE.match(raw_line)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            pos += len(line)
            continue
        if not in_fence:
            hm = _HEADING_RE.match(raw_line)
            if hm:
                level = len(hm.group(1))
                title = hm.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                headings.append({
                    "level": level,
                    "heading": title,
                    "path": " > ".join(h for _, h in stack),
                    "line": lineno,
                    "char_start": pos,
                    "char_end": len(text),
                })
        pos += len(line)
    # Compute each section's end: the start of the next heading of level <= it.
    for i, h in enumerate(headings):
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                h["char_end"] = headings[j]["char_start"]
                break
    return headings


def extract_section(text: str, section_query: str) -> dict | None:
    """Locate one Markdown section by heading or 'Parent > Child' breadcrumb."""
    q = (section_query or "").strip().lstrip("#").strip().lower()
    if not q or not text:
        return None
    outline = extract_outline(text)
    if not outline:
        return None
    # Priority: exact breadcrumb -> exact heading -> suffix breadcrumb -> substring.
    hit = None
    for pred in (
        lambda h: h["path"].lower() == q,
        lambda h: h["heading"].lower() == q,
        lambda h: h["path"].lower().endswith(" > " + q),
        lambda h: q in h["heading"].lower() or q in h["path"].lower(),
    ):
        hit = next((h for h in outline if pred(h)), None)
        if hit is not None:
            break
    if hit is None:
        return None
    start, end = hit["char_start"], hit["char_end"]
    content = text[start:end].rstrip("\n")
    start_line = hit["line"]
    end_line = start_line + content.count("\n")
    return {
        "heading": hit["heading"],
        "path": hit["path"],
        "level": hit["level"],
        "start_line": start_line,
        "end_line": end_line,
        "char_start": start,
        "char_end": end,
        "content": content,
    }


def _split_sections(text: str) -> list[tuple[str, int, int, str]]:
    """Split text into (section_path, char_start, char_end, body_text) spans."""
    outline = extract_outline(text)
    if not outline:
        return [("", 0, len(text), text)]
    spans: list[tuple[str, int, int, str]] = []
    # Preamble before the first heading.
    first_start = outline[0]["char_start"]
    if first_start > 0 and text[:first_start].strip():
        spans.append(("", 0, first_start, text[:first_start]))
    for i, h in enumerate(outline):
        seg_start = h["char_start"]
        seg_end = (outline[i + 1]["char_start"]
                   if i + 1 < len(outline) else len(text))
        seg = text[seg_start:seg_end]
        if seg.strip():
            spans.append((h["path"], seg_start, seg_end, seg))
    return spans or [("", 0, len(text), text)]


def chunk_document(text: str, embedder, *, title: str = "", path: str = "",
                   summary: str = "", tags: list[str] | None = None,
                   max_chunks: int = MAX_DOC_CHUNKS) -> list[dict]:
    """Structure-aware chunking of a document for hybrid vector + FTS search.

    Splits along Markdown section boundaries first, then sub-chunks long
    sections using the vault's own token-aware `embedder.chunk` windowing so
    no fact is cut in half and every chunk records its section breadcrumb and
    exact character offsets. Each chunk's `embed_text` carries a concise
    contextual header (title, path, section) so semantic retrieval knows where
    the passage lives, while `text` preserves the exact passage."""
    body = (text or "").strip()
    if not body:
        # Binary attachment without extracted text: index its metadata card so
        # it stays discoverable semantically and lexically by title/path/tags.
        parts = [p for p in (title, path, summary,
                             " ".join(tags or [])) if p]
        meta_text = "\n".join(parts) or path or title or "document"
        return [{
            "seq": 0,
            "section": "",
            "char_start": 0,
            "char_end": 0,
            "text": meta_text,
            "embed_text": meta_text,
        }]

    chunks: list[dict] = []
    for section, seg_start, seg_end, seg_text in _split_sections(text):
        if len(chunks) >= max_chunks:
            break
        remaining = max_chunks - len(chunks)
        sub_windows = (embedder.chunk(seg_text, max_chunks=remaining)
                       if len(seg_text) > 350 else [seg_text])
        cursor = 0
        for win in sub_windows:
            if not win.strip():
                continue
            rel_idx = seg_text.find(win, cursor)
            if rel_idx < 0:
                rel_idx = max(0, seg_text.find(win[:64]))
            c_start = seg_start + rel_idx
            c_end = min(len(text), c_start + len(win))
            cursor = max(cursor, rel_idx + 1)
            ctx_parts = [p for p in (title, path) if p]
            ctx = " · ".join(ctx_parts)
            if section:
                ctx = f"{ctx} [{section}]" if ctx else f"[{section}]"
            embed_text = f"{ctx}\n{win}" if ctx else win
            chunks.append({
                "seq": len(chunks),
                "section": section,
                "char_start": c_start,
                "char_end": c_end,
                "text": win,
                "embed_text": embed_text,
            })
            if len(chunks) >= max_chunks:
                break
    return chunks


# -------------------------------------------- patching, slicing, & diffing

def patch_document_text(
    current_text: str,
    *,
    append: str | None = None,
    replace_old: str | None = None,
    replace_new: str | None = None,
    replace_all: bool = False,
    section: str | None = None,
    section_content: str | None = None,
    create_section: bool = True,
) -> str:
    """Apply a deterministic, targeted edit to a text document."""
    has_replace = replace_old is not None
    has_section = section is not None
    has_append = append is not None
    if not (has_replace or has_section or has_append):
        raise CryptoError(
            "doc_patch needs at least one edit: 'replace_old'+'replace_new', "
            "'section'+'section_content', or 'append'.")

    text = current_text
    if has_replace:
        if replace_new is None:
            raise CryptoError(
                "doc_patch with 'replace_old' also requires 'replace_new'.")
        if not replace_old:
            raise CryptoError("doc_patch 'replace_old' must not be empty.")
        count = text.count(replace_old)
        if count == 0:
            raise CryptoError(
                "doc_patch: 'replace_old' was not found in the document.")
        if count > 1 and not replace_all:
            raise CryptoError(
                f"doc_patch: 'replace_old' matched {count} locations; pass "
                "more surrounding context or replace_all=True.")
        text = (text.replace(replace_old, replace_new)
                if replace_all else text.replace(replace_old, replace_new, 1))

    if has_section:
        if section_content is None:
            raise CryptoError(
                "doc_patch with 'section' also requires 'section_content'.")
        hit = extract_section(text, section)
        if hit is not None:
            # Preserve the original heading line and replace the section body.
            seg = text[hit["char_start"]:hit["char_end"]]
            heading_line = seg.splitlines()[0] if seg else f"## {section}"
            body = section_content.strip("\n")
            new_seg = f"{heading_line}\n\n{body}\n\n" if body else f"{heading_line}\n\n"
            text = text[:hit["char_start"]] + new_seg + text[hit["char_end"]:]
            text = text.rstrip("\n") + "\n"
        else:
            if not create_section:
                raise CryptoError(
                    f"doc_patch: section {section!r} not found in document.")
            heading_clean = section.strip().lstrip("#").strip()
            body = section_content.strip("\n")
            block = f"## {heading_clean}\n\n{body}\n" if body else f"## {heading_clean}\n"
            if text and not text.endswith("\n"):
                text += "\n"
            if text and not text.endswith("\n\n"):
                text += "\n"
            text += block

    if has_append:
        addition = append
        if addition:
            if text and not text.endswith("\n"):
                text += "\n"
            if text and not text.endswith("\n\n") and not addition.startswith("\n"):
                text += "\n"
            text += addition
            if not text.endswith("\n"):
                text += "\n"

    return text


def diff_versions(old_text: str, new_text: str, *, from_label: str,
                  to_label: str, context_lines: int = 3) -> str:
    """Produce a unified diff between two versions of a text document."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=from_label, tofile=to_label,
        n=max(0, int(context_lines))))


def slice_text(text: str, *, start_line: int | None = None,
               end_line: int | None = None, offset: int | None = None,
               max_chars: int | None = None) -> tuple[str, dict]:
    """Windowed read of a text document so large docs never blow context."""
    total_chars = len(text)
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    if start_line is not None or end_line is not None:
        s = max(1, int(start_line or 1))
        e = min(total_lines, int(end_line or total_lines)) if total_lines else 0
        out = "".join(lines[s - 1:e]) if s <= e else ""
        truncated = (s > 1) or (e < total_lines)
        if max_chars is not None and len(out) > max_chars:
            out = out[:max_chars]
            truncated = True
        return out, {
            "total_lines": total_lines,
            "total_chars": total_chars,
            "start_line": s,
            "end_line": e,
            "returned_chars": len(out),
            "truncated": truncated,
        }
    start = max(0, int(offset or 0))
    if max_chars is not None:
        lim = max(0, int(max_chars))
        out = text[start:start + lim]
        truncated = (start + lim) < total_chars
    else:
        out = text[start:]
        truncated = False
    return out, {
        "total_lines": total_lines,
        "total_chars": total_chars,
        "offset": start,
        "returned_chars": len(out),
        "truncated": truncated,
    }


def compile_memories_markdown(memories: list[dict], *, title: str,
                              description: str | None = None) -> str:
    """Deterministically compile atomic memories into a clean Markdown dossier."""
    lines = [f"# {title.strip()}", ""]
    if description:
        lines.extend([description.strip(), ""])
    facts = [m for m in memories if (m.get("kind") or "fact") != "opinion"]
    opinions = [m for m in memories if (m.get("kind") or "fact") == "opinion"]
    if opinions:
        lines.extend(["## Decisions & Preferences", ""])
        for m in opinions:
            tags = [t for t in (m.get("tags") or [])
                    if not t.startswith(("id:", "claude-file:", "project:"))]
            tag_s = f" `{', '.join(tags)}`" if tags else ""
            lines.append(f"- {m['text']}{tag_s} _(mem:{m['id'][:8]})_")
        lines.append("")
    if facts:
        lines.extend(["## Facts & Notes", ""])
        for m in facts:
            tags = [t for t in (m.get("tags") or [])
                    if not t.startswith(("id:", "claude-file:", "project:"))]
            tag_s = f" `{', '.join(tags)}`" if tags else ""
            lines.append(f"- {m['text']}{tag_s} _(mem:{m['id'][:8]})_")
        lines.append("")
    if not facts and not opinions:
        lines.extend(["_No matching memories._", ""])
    return "\n".join(lines)
