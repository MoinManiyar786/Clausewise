"""Safe text extraction from uploaded files (.txt, .md, .pdf).

Files are read in memory only and never written to disk. File type is
checked by content signature, not just by extension.
"""
from __future__ import annotations

import io

from .validation import ValidationError

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}


def extract_text(filename: str, data: bytes, max_pdf_pages: int = 60) -> str:
    name = (filename or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("Unsupported file type. Please upload a .pdf, .txt or .md file.")
    if not data:
        raise ValidationError("The file is empty.")

    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValidationError("This file does not look like a valid PDF.")
        return _pdf_text(data, max_pdf_pages)

    if b"\x00" in data[:4096]:
        raise ValidationError("This file looks like a binary file, not text.")
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("Could not read the text in this file.")  # pragma: no cover


def _pdf_text(data: bytes, max_pages: int) -> str:
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
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ValidationError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError("This PDF could not be read. Try copying and pasting the text instead.") from exc
    if len(text.strip()) < 40:
        raise ValidationError("No text found in this PDF. It may be a scanned image. Try pasting the text instead.")
    return text
