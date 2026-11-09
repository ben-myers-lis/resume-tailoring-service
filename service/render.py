"""Render approved structured content to DOCX and PDF, then check page count.

PDF engines: LibreOffice converts the DOCX (exact match, needs the soffice binary - Docker image);
ReportLab draws the same layout directly in Python (no system packages - used on Vercel). With
RTS_PDF_ENGINE=auto, LibreOffice is used when installed, otherwise ReportLab.

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
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

BODY_PT = 11
FONT = "Calibri"
FONT_DIR = Path(__file__).resolve().parent / "fonts"      # Carlito: metric-compatible with Calibri (OFL)
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
    """LibreOffice conversion of the DOCX. None when LibreOffice isn't installed."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as profile:                    # isolated profile: safe in parallel
        subprocess.run([exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf",
                        "--outdir", str(docx_path.parent), str(docx_path)],
                       check=True, capture_output=True, timeout=120)
    pdf = docx_path.with_suffix(".pdf")
    return pdf if pdf.exists() else None


_PDF_FONTS: tuple[str, str, str, str] | None = None


def _pdf_fonts() -> tuple[str, str, str, str]:
    """Register Carlito once; fall back to the built-in Helvetica family if the files are missing."""
    global _PDF_FONTS
    if _PDF_FONTS is None:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        names = {"Carlito": "Regular", "Carlito-Bold": "Bold", "Carlito-Italic": "Italic",
                 "Carlito-BoldItalic": "BoldItalic"}
        try:
            for name, style in names.items():
                pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / f"Carlito-{style}.ttf")))
            addMapping("Carlito", 0, 0, "Carlito")
            addMapping("Carlito", 1, 0, "Carlito-Bold")
            addMapping("Carlito", 0, 1, "Carlito-Italic")
            addMapping("Carlito", 1, 1, "Carlito-BoldItalic")
            _PDF_FONTS = ("Carlito", "Carlito-Bold", "Carlito-Italic", "Carlito-BoldItalic")
        except Exception:
            _PDF_FONTS = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
    return _PDF_FONTS


def _x(text: str) -> str:
    """Escape for ReportLab markup, keeping double spaces (it collapses whitespace like HTML)."""
    return escape(text).replace("  ", "&nbsp; ")


def _markup(block: dict) -> str:
    return "".join(f"<b>{_x(seg)}</b>" if bold else _x(seg) for seg, bold in _spans(block))


def build_pdf(request: dict, draft: dict, path: Path) -> None:
    """The same layout as build_docx, drawn with ReportLab: Letter, 11 pt, single column, no tables."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate

    regular, bold_font, _italic, _bold_italic = _pdf_fonts()
    lead = BODY_PT * 1.22                                           # Word "single" spacing for Calibri
    body = ParagraphStyle("body", fontName=regular, fontSize=BODY_PT, leading=lead, spaceAfter=2)
    center = ParagraphStyle("center", parent=body, alignment=TA_CENTER, spaceAfter=1)
    name_style = ParagraphStyle("name", parent=center, fontName=bold_font, fontSize=20, leading=24)
    contact = ParagraphStyle("contact", parent=center, fontSize=BODY_PT - 0.5, leading=(BODY_PT - 0.5) * 1.22,
                             spaceAfter=4)
    notice = ParagraphStyle("notice", parent=center, fontName=bold_font, spaceAfter=4)
    heading = ParagraphStyle("heading", parent=body, fontName=bold_font, fontSize=BODY_PT + 0.5,
                             leading=(BODY_PT + 0.5) * 1.22, spaceBefore=9, spaceAfter=0,
                             textColor=colors.HexColor("#1F2A44"), keepWithNext=True)
    role_head = ParagraphStyle("role", parent=body, spaceBefore=5, spaceAfter=0, keepWithNext=True)
    role_title = ParagraphStyle("title", parent=body, spaceAfter=1)
    line = ParagraphStyle("line", parent=body, spaceAfter=1)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=0.25 * inch, bulletIndent=0.07 * inch,
                            bulletFontName=regular, bulletFontSize=BODY_PT + 2, spaceAfter=1.5)

    profile = request["candidate"]
    roles = {r["role_id"]: r for r in profile["employment"]}
    story: list = []

    def add_heading(title: str):
        story.append(Paragraph(escape(title.upper()), heading))
        rule = HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#8A94A6"), spaceBefore=1,
                          spaceAfter=3)
        rule.keepWithNext = True
        story.append(rule)

    def add_bullet(text_markup: str):
        story.append(Paragraph(text_markup, bullet, bulletText="\u2022"))

    for n in draft.get("notices", []):                               # illustrative labels stay visible
        story.append(Paragraph(escape(n), notice))
    if profile.get("name"):
        story.append(Paragraph(escape(profile["name"]), name_style))
    if profile.get("headline"):
        story.append(Paragraph(escape(profile["headline"]), center))
    if profile.get("contacts"):
        story.append(Paragraph(_x("  |  ".join(c["value"] for c in profile["contacts"])), contact))

    if draft["summary"]["text"].strip():
        add_heading("Professional Summary")
        story.append(Paragraph(_markup(draft["summary"]), body))

    groups = [g for g in draft["skills"] if g["items"]]
    if groups:
        add_heading("Technical Skills")
        for g in groups:                                              # no category labels, no bold
            add_bullet(escape(", ".join(i["text"] for i in g["items"])))

    if draft["roles"]:
        add_heading("Professional Experience")
    for block in draft["roles"]:
        r = roles[block["role_id"]]
        head = f"<b>{escape(r['employer'])}</b>"
        if r.get("location"):
            loc = ("Headquarters: " if r.get("location_type") == "HEADQUARTERS" else "") + r["location"]
            head += f"&nbsp; \u00b7&nbsp; {escape(loc)}"
        story.append(Paragraph(head, role_head))
        end = None if r["is_current"] else r["end_date"]
        title_style = ParagraphStyle("t", parent=role_title,
                                     keepWithNext=bool(block["bullets"] or block["scope"]["text"]))
        story.append(Paragraph(f"<i>{escape(r['official_title'])}</i>&nbsp; |&nbsp; "
                               f"{escape(fmt_date(r['start_date']))} \u2013 {escape(fmt_date(end))}", title_style))
        if block["scope"]["text"].strip():
            story.append(Paragraph(_markup(block["scope"]), line))
        for b in block["bullets"]:
            add_bullet(_markup(b))

    for key, title in (("education", "Education"), ("certifications", "Certifications")):
        if profile.get(key):
            add_heading(title)
            for item in profile[key]:
                story.append(Paragraph(escape(item), line))

    path.parent.mkdir(parents=True, exist_ok=True)
    pad = 6                                                         # SimpleDocTemplate's frame padding (pt)
    doc = SimpleDocTemplate(str(path), pagesize=letter, topMargin=0.6 * inch - pad, bottomMargin=0.6 * inch - pad,
                            leftMargin=0.7 * inch - pad, rightMargin=0.7 * inch - pad,
                            title=f"{profile.get('name') or 'Resume'}", author=profile.get("name") or "")
    doc.build(story)


def make_pdf_file(request: dict, draft: dict, docx_path: Path, engine: str = "auto") -> Path | None:
    """Produce resume.pdf next to the DOCX with the chosen engine. None if no engine is available."""
    if engine in ("auto", "libreoffice"):
        try:
            pdf = to_pdf(docx_path)
        except (subprocess.SubprocessError, OSError):
            pdf = None
        if pdf or engine == "libreoffice":
            return pdf
    try:
        pdf = docx_path.with_suffix(".pdf")
        build_pdf(request, draft, pdf)
        return pdf
    except ImportError:                                               # reportlab not installed
        return None


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def render_files(request: dict, result: dict, out_dir: Path, make_pdf: bool = True, pdf_engine: str = "auto") -> dict:
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
        pdf = make_pdf_file(request, result["draft"], docx, pdf_engine)
        if pdf:
            files["pdf"] = pdf.name
            pages = page_count(pdf)
            max_pages = request["options"]["max_pages"]
            layout = "VERIFIED_WITHIN_LIMIT" if pages <= max_pages else "OVER_PAGE_LIMIT"
    return {"withheld": False, "files": files, "pages": pages, "layout_status": layout,
            "page_size": "Letter", "body_pt": BODY_PT}
