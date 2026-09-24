"""Upload validation for source-intake files."""
import io
import zipfile
from pathlib import Path

from .config import SUPPORTED_EXTENSIONS

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".sql": "application/sql",
    ".csv": "text/csv",
}


def validate_file_content(filename: str, content: bytes, client_content_type: str | None) -> tuple[str, list[str]]:
    """Validate bytes independently from the browser-provided MIME type."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported document format '{suffix or '[none]'}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if not content:
        raise ValueError("The uploaded source is empty")

    detected = CONTENT_TYPES[suffix]
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("The file extension is .pdf, but the content is not a valid PDF file")
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError("missing Office document parts")
                if archive.testzip() is not None:
                    raise ValueError("the Office document archive is corrupt")
        except (zipfile.BadZipFile, ValueError) as exc:
            raise ValueError("The uploaded DOCX file is unreadable or corrupted. Please choose a valid Word document.") from exc
    else:
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"The {suffix[1:].upper()} file is not valid UTF-8 text") from exc
        if not decoded.replace("\x00", "").strip():
            raise ValueError("The document contains no readable text")
        if "\x00" in decoded:
            raise ValueError("The text file contains binary content and is not readable")
        if suffix == ".json":
            import json
            try:
                json.loads(decoded)
            except json.JSONDecodeError as exc:
                raise ValueError("The uploaded JSON file is invalid or corrupted") from exc

    messages: list[str] = []
    if client_content_type and client_content_type.lower() != detected:
        messages.append(
            f"Client MIME type '{client_content_type}' differed from detected type '{detected}'; detected type was used"
        )
    return detected, messages
