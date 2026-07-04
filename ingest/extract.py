"""Extract text from a report file (.md/.txt direct, .pdf via pypdf)."""
from pathlib import Path


def extract(path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text), ...]."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return [(i + 1, (pg.extract_text() or "")) for i, pg in enumerate(reader.pages)]
    return [(1, path.read_text(encoding="utf-8"))]
