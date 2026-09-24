import hashlib, io, json, os, re
from pathlib import Path
from typing import Any
from .config import OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS

OPENAI_PROMPT_TEMPLATE = (
    "You are extracting a business requirements document for audit and test design.\n\n"
    "Return the complete usable text, not a summary. Preserve wording, numbers, dates, rules, constraints, headings, and ordering. "
    "Do not invent, infer, normalize, or omit requirements. Split sections at actual headings. Preserve the supplied source references "
    "such as page markers and line references. If content has no heading, use 'Unsectioned content'. "
    "The input may contain [page N] markers; use those to populate page_number. "
    "Every section's text must be copied from the input.\n\nSOURCE TEXT:\n"
)

def extraction_prompt_hash() -> str:
    return hashlib.sha256(OPENAI_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()

def _sections(text: str) -> list[dict[str, Any]]:
    result, current_heading, current_page = [], "Unsectioned content", None
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
            current_heading = re.sub(r"^#+\s*", "", line)
        else:
            reference = f"page {current_page}, line {line_no}" if current_page else f"line {line_no}"
            result.append({"heading": current_heading, "reference": reference, "page_number": current_page, "fragment_type": "paragraph", "paragraph_index": line_no, "table_index": None, "row_index": None, "cell_index": None, "text": line})
    return result


def _docx_sections(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    from docx import Document
    from docx.document import Document as DocumentType
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.ns import qn

    doc = Document(io.BytesIO(content))
    sections: list[dict[str, Any]] = []
    text_parts: list[str] = []
    current_heading = "Unsectioned content"

    def iter_blocks(parent: DocumentType | _Cell):
        """Yield DOCX paragraphs and tables in their original document order."""
        parent_element = parent.element.body if isinstance(parent, DocumentType) else parent._tc
        for child in parent_element.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield Table(child, parent)

    def is_heading(value: str, block: Paragraph | None = None) -> bool:
        style_name = getattr(getattr(block, "style", None), "name", "") or ""
        if style_name.lower().startswith("heading"):
            return True
        return bool(re.match(r"^(#{1,6}\s+|\d+(?:\.\d+)*[.)]?\s+)[^\n]+$", value)) or (len(value) < 120 and value.isupper())

    paragraph_index = 0
    table_index = 0
    current_page = 1

    def has_page_break(paragraph: Paragraph) -> bool:
        return any(
            node.tag == qn("w:lastRenderedPageBreak")
            or (node.tag == qn("w:br") and node.get(qn("w:type")) == "page")
            for node in paragraph._p.iter()
        )

    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            paragraph_index += 1
            value = block.text.strip()
            if not value:
                if has_page_break(block):
                    current_page += 1
                continue
            text_parts.append(value)
            if is_heading(value, block):
                current_heading = re.sub(r"^#+\s*", "", value)
            else:
                sections.append({"heading": current_heading, "reference": f"paragraph {paragraph_index}", "page_number": current_page, "fragment_type": "paragraph", "paragraph_index": paragraph_index, "table_index": None, "row_index": None, "cell_index": None, "text": value})
            if has_page_break(block):
                current_page += 1
            continue

        table_index += 1
        table_text = []
        table_heading = current_heading or f"Table {table_index}"
        for row_index, row in enumerate(block.rows, 1):
            values = [cell.text.strip() for cell in row.cells]
            row_text = " | ".join(values).strip()
            if not row_text:
                continue
            table_text.append(row_text)
            for cell_index, value in enumerate(values, 1):
                if value:
                    sections.append({"heading": table_heading, "reference": f"table {table_index}, row {row_index}, cell {cell_index}", "page_number": current_page, "fragment_type": "table", "paragraph_index": None, "table_index": table_index, "row_index": row_index, "cell_index": cell_index, "text": value})
        if table_text:
            text_parts.extend(table_text)
        if any(has_page_break(paragraph) for row in block.rows for cell in row.cells for paragraph in cell.paragraphs):
            current_page += 1

    text = "\n".join(text_parts).replace("\x00", "").strip()
    if not text:
        raise ValueError("The document contains no extractable text")
    return text, sections

def extract_local(path: str, content: bytes) -> tuple[str, list[dict[str, Any]]]:
    suffix = Path(path).suffix.lower()
    if suffix in {".txt", ".md", ".yaml", ".yml", ".sql", ".csv"}:
        text = content.decode("utf-8", errors="replace")
    elif suffix == ".json":
        try:
            text = json.dumps(json.loads(content.decode("utf-8-sig")), indent=2, ensure_ascii=False)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("The uploaded JSON file is invalid or corrupted") from exc
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
    sections = _sections(text) if text else []
    if not text or not any(str(section.get("text") or "").strip() for section in sections):
        if suffix == ".pdf":
            raise ValueError("The PDF contains no readable text. It may be scanned or image-only.")
        raise ValueError("The document contains no extractable text")
    return text, sections

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
    prompt = OPENAI_PROMPT_TEMPLATE + text
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
