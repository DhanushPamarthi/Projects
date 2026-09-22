import io, json, os, re
from pathlib import Path
from typing import Any
from .config import OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS

def _sections(text: str) -> list[dict[str, Any]]:
    result, current, current_page = [], None, None
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        page_marker = re.match(r"^\[page\s+(\d+)\]$", line, re.I)
        if page_marker:
            current_page = int(page_marker.group(1))
            continue
        heading = bool(re.match(r"^(#{1,6}\s+|\d+(?:\.\d+)*[.)]?\s+)[^\n]+$", line)) or (len(line) < 120 and line.isupper())
        if heading:
            reference = f"page {current_page}, line {line_no}" if current_page else f"line {line_no}"
            current = {"heading": re.sub(r"^#+\s*", "", line), "reference": reference, "page_number": current_page, "fragment_type": "paragraph", "paragraph_index": line_no, "table_index": None, "row_index": None, "cell_index": None, "text": ""}
            result.append(current)
        elif current:
            current["text"] = (current["text"] + "\n" + line).strip()
        else:
            reference = f"page {current_page}, line {line_no}" if current_page else f"line {line_no}"
            current = {"heading": "Unsectioned content", "reference": reference, "page_number": current_page, "fragment_type": "paragraph", "paragraph_index": line_no, "table_index": None, "row_index": None, "cell_index": None, "text": line}
            result.append(current)
    return result


def _docx_sections(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    from docx import Document
    from docx.document import Document as DocumentType
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P

    doc = Document(io.BytesIO(content))
    sections: list[dict[str, Any]] = []
    text_parts: list[str] = []
    current: dict[str, Any] | None = None

    def iter_blocks(parent: DocumentType | _Cell):
        """Yield DOCX paragraphs and tables in their original document order."""
        parent_element = parent.element.body if isinstance(parent, DocumentType) else parent._tc
        for child in parent_element.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield Table(child, parent)

    def is_heading(value: str) -> bool:
        return bool(re.match(r"^(#{1,6}\s+|\d+(?:\.\d+)*[.)]?\s+)[^\n]+$", value)) or (len(value) < 120 and value.isupper())

    paragraph_index = 0
    table_index = 0
    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            paragraph_index += 1
            value = block.text.strip()
            if not value:
                continue
            text_parts.append(value)
            if is_heading(value):
                current = {"heading": re.sub(r"^#+\s*", "", value), "reference": f"paragraph {paragraph_index}", "page_number": None, "fragment_type": "paragraph", "paragraph_index": paragraph_index, "table_index": None, "row_index": None, "cell_index": None, "text": ""}
                sections.append(current)
            elif current:
                current["text"] = (current["text"] + "\n" + value).strip()
            else:
                current = {"heading": "Unsectioned content", "reference": f"paragraph {paragraph_index}", "page_number": None, "fragment_type": "paragraph", "paragraph_index": paragraph_index, "table_index": None, "row_index": None, "cell_index": None, "text": value}
                sections.append(current)
            continue

        table_index += 1
        table_text = []
        table_heading = current["heading"] if current else f"Table {table_index}"
        for row_index, row in enumerate(block.rows, 1):
            values = [cell.text.strip() for cell in row.cells]
            row_text = " | ".join(values).strip()
            if not row_text:
                continue
            table_text.append(row_text)
            sections.append({"heading": table_heading, "reference": f"table {table_index}, row {row_index}", "page_number": None, "fragment_type": "table", "paragraph_index": None, "table_index": table_index, "row_index": row_index, "cell_index": None, "text": row_text})
        if table_text:
            text_parts.extend(table_text)

    text = "\n".join(text_parts).replace("\x00", "").strip()
    if not text:
        raise ValueError("The document contains no extractable text")
    return text, sections

def extract_local(path: str, content: bytes) -> tuple[str, list[dict[str, Any]]]:
    suffix = Path(path).suffix.lower()
    if suffix in {".txt", ".md"}:
        text = content.decode("utf-8", errors="replace")
    elif suffix == ".docx":
        return _docx_sections(content)
    elif suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for number, page in enumerate(reader.pages, 1):
            pages.append(f"[page {number}]\n{page.extract_text() or ''}")
        text = "\n".join(pages)
    else:
        raise ValueError(f"Unsupported file extension: {suffix or '[none]'}")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("The document contains no extractable text")
    return text, _sections(text)

def missing_sections(sections: list[dict[str, Any]], required: list[str]) -> list[str]:
    def normalize(value: str) -> str:
        value = re.sub(r"^\s*(?:section\s+)?\d+(?:\.\d+)*[.)]?\s*", "", value, flags=re.I)
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    headings = {normalize(str(section.get("heading", ""))) for section in sections}
    return [name for name in required if normalize(name) not in headings]

def extract_openai(text: str) -> tuple[str, list[dict[str, Any]]]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured for OpenAI extraction")
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_SECONDS)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "heading": {"type": "string"},
                        "reference": {"type": "string"},
                        "page_number": {"type": ["integer", "null"]},
                        "text": {"type": "string"},
                    },
                    "required": ["heading", "reference", "page_number", "text"],
                },
            },
        },
        "required": ["text", "sections"],
    }
    prompt = (
        "You are extracting a business requirements document for audit and test design.\n\n"
        "Return the complete usable text, not a summary. Preserve wording, numbers, dates, rules, constraints, headings, and ordering. "
        "Do not invent, infer, normalize, or omit requirements. Split sections at actual headings. Preserve the supplied source references "
        "such as page markers and line references. If content has no heading, use 'Unsectioned content'. "
        "The input may contain [page N] markers; use those to populate page_number. "
        "Every section's text must be copied from the input.\n\nSOURCE TEXT:\n" + text
    )
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        store=False,
        text={"format": {"type": "json_schema", "name": "brd_extraction", "strict": True, "schema": schema}},
    )
    if not response.output_text:
        raise ValueError("OpenAI returned no extraction output")
    data = json.loads(response.output_text)
    extracted = str(data.get("text") or "").strip()
    sections = data.get("sections") or []
    if not extracted:
        raise ValueError("OpenAI returned empty extracted text")
    if not isinstance(sections, list):
        raise ValueError("OpenAI returned invalid section data")
    for section in sections:
        if not all(isinstance(section.get(field), str) for field in ("heading", "reference", "text")):
            raise ValueError("OpenAI returned an invalid section")
    return extracted, sections
