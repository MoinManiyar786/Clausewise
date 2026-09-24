"""Safe text extraction from uploaded files (.txt, .md, .pdf).

Files are read in memory only and never written to disk. File type is
checked by content signature, not just by extension.
"""
from __future__ import annotations

import io

from .validation import ValidationError

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}


def extract_text(filename: str, data: bytes, max_pdf_pages: int = 60, max_chars: int = 60_000) -> str:
    name = (filename or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("Unsupported file type. Please upload a .pdf, .txt or .md file.")
    if not data:
        raise ValidationError("The file is empty.")

    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValidationError("This file does not look like a valid PDF.")
        return _pdf_text(data, max_pdf_pages, max_chars)

    if b"\x00" in data[:4096]:
        raise ValidationError("This file looks like a binary file, not text.")
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("Could not read the text in this file.")  # pragma: no cover


def _pdf_text(data: bytes, max_pages: int, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise ValidationError("PDF support is not installed on the server.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValidationError("This PDF is password-protected. Please upload an unlocked copy.")
        if len(reader.pages) > max_pages:
            raise ValidationError(f"This PDF has more than {max_pages} pages. Please upload the relevant pages only.")
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            chunks.append(page_text)
            total += len(page_text)
            if total > max_chars:  # stop early: the rest would be discarded anyway
                break
        text = "\n\n".join(chunks)
    except ValidationError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError("This PDF could not be read. Try copying and pasting the text instead.") from exc
    if len(text.strip()) < 40:
        raise ValidationError("No text found in this PDF. It may be a scanned image. Try pasting the text instead.")
    return text
