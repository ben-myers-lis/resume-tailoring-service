"""Render approved structured content to DOCX (and PDF via LibreOffice), then check page count.

Layout follows the prompt package's rules: Letter portrait, ~11 pt body, single column, no tables
or text boxes, bold only where the draft's `highlights` say so, max 2 pages. Page count comes
from the actual PDF, so layout status is renderer evidence, never a model claim.
Identity (name, contacts, employers, titles, dates, education) comes from the stored profile.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

BODY_PT = 11
FONT = "Calibri"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def fmt_date(value: str | None) -> str:
    if not value:
        return "Present"
    return f"{MONTHS[int(value[5:7]) - 1]} {value[:4]}" if len(value) >= 7 else value


def _spans(block: dict) -> list[tuple[str, bool]]:
    """Split text into (segment, bold) using the draft's highlight phrases/occurrences."""
    text, ranges = block["text"], []
    for h in block.get("highlights", []):
        start, n = -1, 0
        while n < h["occurrence"]:
            start = text.find(h["phrase"], start + 1)
            if start < 0:
                break
            n += 1
        if start >= 0:
            ranges.append((start, start + len(h["phrase"])))
    out, pos = [], 0
    for a, b in sorted(ranges):
        if a > pos:
            out.append((text[pos:a], False))
        out.append((text[a:b], True))
        pos = b
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def _para(doc, text="", size=BODY_PT, bold=False, italic=False, align=None, before=0, after=2):
    p = doc.add_paragraph()
    if text:
        r = p.add_run(text)
        r.bold, r.italic, r.font.size = bold, italic, Pt(size)
    pf = p.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(before), Pt(after), 1.0
    if align:
        p.alignment = align
    return p


def _heading(doc, title: str):
    p = _para(doc, title.upper(), size=BODY_PT + 0.5, bold=True, before=9, after=3)
    p.runs[0].font.color.rgb = RGBColor(0x1F, 0x2A, 0x44)
    # thin bottom border (a paragraph border, not a table or graphic)
    ppr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("val", "single"), ("sz", "4"), ("space", "1"), ("color", "8A94A6")):
        bottom.set(qn(f"w:{k}"), v)
    bdr.append(bottom)
    ppr.append(bdr)
    p.paragraph_format.keep_with_next = True


def _bullet(doc, block: dict):
    p = doc.add_paragraph(style="List Bullet")
    for seg, bold in _spans(block):
        r = p.add_run(seg)
        r.bold, r.font.size = bold, Pt(BODY_PT)
    pf = p.paragraph_format
    pf.space_after, pf.line_spacing = Pt(1.5), 1.0


def build_docx(request: dict, draft: dict, path: Path) -> None:
    profile = request["candidate"]
    roles = {r["role_id"]: r for r in profile["employment"]}
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)           # US Letter portrait
    sec.top_margin = sec.bottom_margin = Inches(0.6)
    sec.left_margin = sec.right_margin = Inches(0.7)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(BODY_PT)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    for notice in draft.get("notices", []):                            # illustrative labels stay visible
        _para(doc, notice, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)

    if profile.get("name"):
        _para(doc, profile["name"], size=20, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=1)
    if profile.get("headline"):
        _para(doc, profile["headline"], align=WD_ALIGN_PARAGRAPH.CENTER, after=1)
    if profile.get("contacts"):
        _para(doc, "  |  ".join(c["value"] for c in profile["contacts"]), size=BODY_PT - 0.5,
              align=WD_ALIGN_PARAGRAPH.CENTER, after=4)

    if draft["summary"]["text"].strip():
        _heading(doc, "Professional Summary")
        p = _para(doc, after=2)
        for seg, bold in _spans(draft["summary"]):
            r = p.add_run(seg)
            r.bold, r.font.size = bold, Pt(BODY_PT)

    groups = [g for g in draft["skills"] if g["items"]]
    if groups:
        _heading(doc, "Technical Skills")
        for g in groups:                                              # no category labels, no bold
            _bullet(doc, {"text": ", ".join(i["text"] for i in g["items"]), "highlights": []})

    if draft["roles"]:
        _heading(doc, "Professional Experience")
    for block in draft["roles"]:
        r = roles[block["role_id"]]
        p = _para(doc, before=5, after=0)
        p.add_run(r["employer"]).bold = True
        if r.get("location"):
            loc = ("Headquarters: " if r.get("location_type") == "HEADQUARTERS" else "") + r["location"]
            p.add_run(f"  ·  {loc}")
        p.paragraph_format.keep_with_next = True
        p2 = _para(doc, after=1)
        t = p2.add_run(r["official_title"])
        t.italic = True
        end = None if r["is_current"] else r["end_date"]
        p2.add_run(f"  |  {fmt_date(r['start_date'])} – {fmt_date(end)}")
        p2.paragraph_format.keep_with_next = bool(block["bullets"] or block["scope"]["text"])
        if block["scope"]["text"].strip():
            p3 = _para(doc, after=1)
            for seg, bold in _spans(block["scope"]):
                run = p3.add_run(seg)
                run.bold = bold
        for b in block["bullets"]:
            _bullet(doc, b)

    for key, title in (("education", "Education"), ("certifications", "Certifications")):
        if profile.get(key):
            _heading(doc, title)
            for item in profile[key]:
                _para(doc, item, after=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def to_pdf(docx_path: Path) -> Path | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as profile:                    # isolated profile: safe in parallel
        subprocess.run([exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf",
                        "--outdir", str(docx_path.parent), str(docx_path)],
                       check=True, capture_output=True, timeout=120)
    pdf = docx_path.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def render_files(request: dict, result: dict, out_dir: Path, make_pdf: bool = True) -> dict:
    """Write resume.md / resume.docx / resume.pdf. Withheld if blocking findings remain."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if result["resume_markdown"] is None:
        return {"withheld": True, "reason": "Unresolved blocking findings", "files": {}, "layout_status": "NOT_RENDERED"}
    files = {}
    md = out_dir / "resume.md"
    md.write_text(result["resume_markdown"])
    files["md"] = md.name
    docx = out_dir / "resume.docx"
    build_docx(request, result["draft"], docx)
    files["docx"] = docx.name
    pages, layout = None, "LAYOUT_UNVERIFIED"
    if make_pdf:
        try:
            pdf = to_pdf(docx)
        except (subprocess.SubprocessError, OSError):
            pdf = None
        if pdf:
            files["pdf"] = pdf.name
            pages = page_count(pdf)
            max_pages = request["options"]["max_pages"]
            layout = "VERIFIED_WITHIN_LIMIT" if pages <= max_pages else "OVER_PAGE_LIMIT"
    return {"withheld": False, "files": files, "pages": pages, "layout_status": layout,
            "page_size": "Letter", "body_pt": BODY_PT}
