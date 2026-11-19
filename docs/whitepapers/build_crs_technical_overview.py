"""Build the CAD Repair System white paper (research-paper layout).

Run:  python docs/whitepapers/build_crs_technical_overview.py
Needs reportlab. Every figure quoted here comes from a committed evidence README under
devtools/validation/evidence/ or from docs/superpowers/specs/; update the paper with the evidence,
never ahead of it.
"""
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/whitepapers/hexera-crs-technical-overview.pdf"

PAGE_W, PAGE_H = letter
MARGIN = 1.0 * inch
CONTENT_W = PAGE_W - 2 * MARGIN

INK = colors.HexColor("#111111")
GREY = colors.HexColor("#555555")
RULE = colors.HexColor("#222222")
FAINT = colors.HexColor("#BBBBBB")

TITLE = "Evidence-Bounded CAD Repair for Simulation Meshing"
RUNNING = "Hexera CAD Repair System — Technical White Paper"


# ---------------------------------------------------------------------------------------------
# Fonts: Charter (macOS) for text, Menlo for code; the PDF base-14 serif and Courier otherwise.
# ---------------------------------------------------------------------------------------------
def register_fonts():
    sup = Path("/System/Library/Fonts/Supplemental")
    charter = sup / "Charter.ttc"
    times = {
        "Serif": sup / "Times New Roman.ttf",
        "Serif-Bold": sup / "Times New Roman Bold.ttf",
        "Serif-Italic": sup / "Times New Roman Italic.ttf",
        "Serif-BoldItalic": sup / "Times New Roman Bold Italic.ttf",
    }
    faces = None
    try:
        if charter.exists():
            for name, idx in (("Serif", 0), ("Serif-Italic", 1), ("Serif-BoldItalic", 2),
                              ("Serif-Bold", 3)):
                pdfmetrics.registerFont(TTFont(name, str(charter), subfontIndex=idx))
            faces = ("Serif", "Serif-Bold", "Serif-Italic", "Serif-BoldItalic")
    except Exception:
        faces = None
    if faces is None and all(p.exists() for p in times.values()):
        for name, path in times.items():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        faces = ("Serif", "Serif-Bold", "Serif-Italic", "Serif-BoldItalic")
    if faces is None:
        faces = ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic")
    pdfmetrics.registerFontFamily(faces[0], normal=faces[0], bold=faces[1], italic=faces[2],
                                  boldItalic=faces[3])
    mono = "Courier"
    menlo = Path("/System/Library/Fonts/Menlo.ttc")
    try:
        if menlo.exists():
            pdfmetrics.registerFont(TTFont("Mono", str(menlo), subfontIndex=0))
            mono = "Mono"
    except Exception:
        pass
    return faces, mono


(SERIF, SERIF_B, SERIF_I, SERIF_BI), MONO = register_fonts()

# reportlab opens every canvas and drawing in Helvetica; keep the whole document serif.
from reportlab import rl_config  # noqa: E402

rl_config.canvas_basefontname = SERIF
from reportlab.graphics import shapes as _shapes  # noqa: E402

_shapes.STATE_DEFAULTS["fontName"] = SERIF

BODY_SIZE = 10.5
BODY_LEADING = 14.2

S = {
    "title": ParagraphStyle("title", fontName=SERIF_B, fontSize=19, leading=23, alignment=TA_CENTER,
                            textColor=INK, spaceAfter=6),
    "subtitle": ParagraphStyle("subtitle", fontName=SERIF_I, fontSize=12.5, leading=16,
                               alignment=TA_CENTER, textColor=INK, spaceAfter=14),
    "author": ParagraphStyle("author", fontName=SERIF, fontSize=11, leading=14, alignment=TA_CENTER,
                             textColor=INK),
    "affil": ParagraphStyle("affil", fontName=SERIF_I, fontSize=9.5, leading=12.5,
                            alignment=TA_CENTER, textColor=GREY),
    "abs_head": ParagraphStyle("abs_head", fontName=SERIF_B, fontSize=10, leading=13,
                               alignment=TA_CENTER, spaceBefore=16, spaceAfter=5),
    "abstract": ParagraphStyle("abstract", fontName=SERIF, fontSize=9.6, leading=12.8,
                               alignment=TA_JUSTIFY, leftIndent=0.45 * inch,
                               rightIndent=0.45 * inch, spaceAfter=6),
    "body": ParagraphStyle("body", fontName=SERIF, fontSize=BODY_SIZE, leading=BODY_LEADING,
                           alignment=TA_JUSTIFY, textColor=INK, firstLineIndent=14, spaceAfter=0),
    "body0": ParagraphStyle("body0", fontName=SERIF, fontSize=BODY_SIZE, leading=BODY_LEADING,
                            alignment=TA_JUSTIFY, textColor=INK, spaceAfter=0),
    "body_left": ParagraphStyle("body_left", fontName=SERIF, fontSize=BODY_SIZE,
                                leading=BODY_LEADING, alignment=TA_LEFT, textColor=INK),
    "h1": ParagraphStyle("h1", fontName=SERIF_B, fontSize=12.5, leading=16, spaceBefore=16,
                         spaceAfter=6, textColor=INK),
    "h2": ParagraphStyle("h2", fontName=SERIF_B, fontSize=10.8, leading=14, spaceBefore=10,
                         spaceAfter=4, textColor=INK),
    "h3": ParagraphStyle("h3", fontName=SERIF_BI, fontSize=BODY_SIZE, leading=BODY_LEADING,
                         spaceBefore=6, spaceAfter=1, textColor=INK),
    "bullet": ParagraphStyle("bullet", fontName=SERIF, fontSize=BODY_SIZE, leading=BODY_LEADING,
                             alignment=TA_JUSTIFY, leftIndent=22, bulletIndent=8, spaceBefore=2,
                             bulletFontName=SERIF, bulletFontSize=BODY_SIZE),
    "caption": ParagraphStyle("caption", fontName=SERIF, fontSize=9, leading=11.6,
                              alignment=TA_JUSTIFY, textColor=INK),
    "cell": ParagraphStyle("cell", fontName=SERIF, fontSize=8.7, leading=10.8, textColor=INK,
                           alignment=TA_LEFT),
    "cellb": ParagraphStyle("cellb", fontName=SERIF_B, fontSize=8.7, leading=10.8, textColor=INK),
    "cellbr": ParagraphStyle("cellbr", fontName=SERIF_B, fontSize=8.7, leading=10.8,
                             textColor=INK, alignment=2),
    "cellr": ParagraphStyle("cellr", fontName=SERIF, fontSize=8.7, leading=10.8, textColor=INK,
                            alignment=2),
    "ref": ParagraphStyle("ref", fontName=SERIF, fontSize=9, leading=11.8, leftIndent=22,
                          firstLineIndent=-22, spaceAfter=3, alignment=TA_LEFT),
    "algo": ParagraphStyle("algo", fontName=MONO, fontSize=8.1, leading=10.6, textColor=INK),
    "keywords": ParagraphStyle("keywords", fontName=SERIF, fontSize=9.3, leading=12.4,
                               leftIndent=0.45 * inch, rightIndent=0.45 * inch, spaceAfter=4),
}


# ---------------------------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------------------------
def c(text):
    """Inline code."""
    return f'<font face="{MONO}" size="8">{text}</font>'


def P(text, style="body"):
    return Paragraph(text, S[style])


def H1(num, text):
    return P(f"{num}&nbsp;&nbsp;&nbsp;{text}", "h1")


def H2(num, text):
    return P(f"{num}&nbsp;&nbsp;&nbsp;{text}", "h2")


def H3(text):
    return P(f"{text}.", "h3")


def bullets(items, numbered=False):
    out = []
    for i, item in enumerate(items, 1):
        mark = f"({i})" if numbered else "•"
        out.append(Paragraph(item, S["bullet"], bulletText=mark))
    return out


class Gap(Spacer):
    def __init__(self, h=6):
        super().__init__(1, h)


_table_no = [0]
_figure_no = [0]
_algo_no = [0]


def table(caption, rows, widths, align_right=(), bold_first_col=False, note=None):
    """A booktabs-style table: heavy top and bottom rules, light mid-rule, no verticals."""
    _table_no[0] += 1
    cap = P(f"<b>Table {_table_no[0]}.</b> {caption}", "caption")
    data = []
    for r, row in enumerate(rows):
        cells = []
        for col, val in enumerate(row):
            if isinstance(val, Flowable):
                cells.append(val)
                continue
            if r == 0:
                style = "cellbr" if col in align_right else "cellb"
            elif col in align_right:
                style = "cellr"
            elif bold_first_col and col == 0:
                style = "cellb"
            else:
                style = "cell"
            cells.append(Paragraph(str(val), S[style]))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="CENTER")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), SERIF),
        ("LINEABOVE", (0, 0), (-1, 0), 1.0, RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 1.0, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
    ]))
    cap.keepWithNext = True
    lead = Gap(4)
    lead.keepWithNext = True
    parts = [CondPageBreak(1.6 * inch), Gap(8), cap, lead, t]
    if note:
        parts += [Gap(3), P(note, "caption")]
    parts.append(Gap(8))
    return parts


def figure(drawing, caption):
    _figure_no[0] += 1
    cap = P(f"<b>Figure {_figure_no[0]}.</b> {caption}", "caption")
    drawing.hAlign = "CENTER"
    return KeepTogether([Gap(8), drawing, Gap(5), cap, Gap(8)])


def algorithm(title, code):
    _algo_no[0] += 1
    head = P(f"<b>Algorithm {_algo_no[0]}.</b> {title}", "caption")
    body = Preformatted(code.strip("\n"), S["algo"])
    t = Table([[head], [body]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), SERIF),
        ("LINEABOVE", (0, 0), (-1, 0), 1.0, RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 1.0, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return KeepTogether([Gap(8), t, Gap(8)])


# ---------------------------------------------------------------------------------------------
# Figures (vector, monochrome)
# ---------------------------------------------------------------------------------------------
def _box(d, x, y, w, h, label, sub=None, dashed=False):
    r = Rect(x, y, w, h, strokeColor=INK, fillColor=colors.white, strokeWidth=0.7)
    if dashed:
        r.strokeDashArray = [2, 2]
    d.add(r)
    ty = y + h / 2 + (2 if sub else -3)
    d.add(String(x + w / 2, ty, label, fontName=SERIF_B, fontSize=8.2, textAnchor="middle"))
    if sub:
        d.add(String(x + w / 2, y + h / 2 - 9, sub, fontName=SERIF_I, fontSize=7.2,
                     textAnchor="middle", fillColor=GREY))


def _arrow(d, x1, y1, x2, y2):
    d.add(Line(x1, y1, x2, y2, strokeColor=INK, strokeWidth=0.7))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    s = 4.2
    p1 = (x2 - s * math.cos(a - 0.4), y2 - s * math.sin(a - 0.4))
    p2 = (x2 - s * math.cos(a + 0.4), y2 - s * math.sin(a + 0.4))
    d.add(Polygon([x2, y2, p1[0], p1[1], p2[0], p2[1]], fillColor=INK, strokeColor=INK,
                  strokeWidth=0.3))


def fig_pipeline():
    w, h = CONTENT_W, 150
    d = Drawing(w, h)
    bw, bh, gap = 72, 38, (w - 6 * 72) / 5
    labels = [("Upload", "immutable bytes"), ("Inspect", "locate + fingerprint"),
              ("Triage", "route the part"), ("Repair session", "transactions"),
              ("Final check", "re-scan + stage"), ("Promote", "or refuse")]
    y = 92
    xs = []
    for i, (lab, sub) in enumerate(labels):
        x = i * (bw + gap)
        xs.append(x)
        _box(d, x, y, bw, bh, lab, sub)
        if i:
            _arrow(d, xs[i - 1] + bw, y + bh / 2, x, y + bh / 2)
    # transaction loop under "Repair session"
    sx = xs[3]
    loop = [("plan", 0), ("snapshot", 1), ("apply", 2), ("measure", 3)]
    lw, lh = 52, 18
    ly = 22
    base = sx + bw / 2 - (4 * lw + 3 * 8) / 2
    for lab, i in loop:
        x = base + i * (lw + 8)
        d.add(Rect(x, ly, lw, lh, strokeColor=INK, fillColor=colors.white, strokeWidth=0.5))
        d.add(String(x + lw / 2, ly + 5.5, lab, fontName=SERIF, fontSize=7.5, textAnchor="middle"))
        if i:
            _arrow(d, x - 8, ly + lh / 2, x, ly + lh / 2)
    right = base + 4 * lw + 3 * 8
    d.add(Line(right, ly + lh / 2, right + 10, ly + lh / 2, strokeColor=INK, strokeWidth=0.5))
    d.add(Line(right + 10, ly + lh / 2, right + 10, ly - 8, strokeColor=INK, strokeWidth=0.5))
    d.add(Line(right + 10, ly - 8, base - 10, ly - 8, strokeColor=INK, strokeWidth=0.5))
    d.add(Line(base - 10, ly - 8, base - 10, ly + lh / 2, strokeColor=INK, strokeWidth=0.5))
    _arrow(d, base - 10, ly + lh / 2, base, ly + lh / 2)
    d.add(String((base + right) / 2, ly - 18, "keep, or roll back to the snapshot",
                 fontName=SERIF_I, fontSize=7.2, textAnchor="middle", fillColor=GREY))
    d.add(Line(sx + bw / 2, y, sx + bw / 2, ly + lh + 4, strokeColor=FAINT, strokeWidth=0.6,
               strokeDashArray=[2, 2]))
    # refusal path from triage
    d.add(String(xs[2] + bw / 2, y - 12, "mesh as is / manual cleanup /", fontName=SERIF_I,
                 fontSize=6.8, textAnchor="middle", fillColor=GREY))
    d.add(String(xs[2] + bw / 2, y - 21, "ask customer", fontName=SERIF_I,
                 fontSize=6.8, textAnchor="middle", fillColor=GREY))
    return d


def fig_regions():
    w, h = CONTENT_W, 136
    d = Drawing(w, h)
    top, bot = 84, 16
    _box(d, 0, top, 88, 36, "Whole-shape scan", "1 of 2 per run")
    _box(d, 108, top, 82, 36, "Decompose", "regions, over-merge")
    _box(d, 210, top, 82, 36, "Author package", "rules or model")
    _box(d, 312, top, 70, 36, "Execute", "inside locus")
    _box(d, 402, top, 66, 36, "Scoped proof", "predicate + caps")
    mid = top + 18
    _arrow(d, 88, mid, 108, mid)
    _arrow(d, 190, mid, 210, mid)
    _arrow(d, 292, mid, 312, mid)
    _arrow(d, 382, mid, 402, mid)
    _box(d, 210, bot, 82, 30, "Revert + feed back")
    _box(d, 330, bot, 70, 30, "Commit region")
    _box(d, 408, bot, 60, 30, "Escalate", dashed=True)
    _arrow(d, 430, top, 365, bot + 30)
    _arrow(d, 438, top, 438, bot + 30)
    _arrow(d, 422, top, 270, bot + 30)
    _arrow(d, 251, bot + 30, 251, top)
    d.add(String(244, bot + 46, "retry within", fontName=SERIF_I, fontSize=6.8,
                 textAnchor="end", fillColor=GREY))
    d.add(String(244, bot + 38, "region budget", fontName=SERIF_I, fontSize=6.8,
                 textAnchor="end", fillColor=GREY))
    _box(d, 0, bot, 130, 30, "Merge: whole-shape scan", "2 of 2; one global verify")
    # committed regions flow to the merge, routed beneath the row of outcome boxes
    for seg in ((365, bot, 365, 5), (365, 5, 65, 5)):
        d.add(Line(*seg, strokeColor=INK, strokeWidth=0.5, strokeDashArray=[3, 2]))
    _arrow(d, 65, 5, 65, bot)
    d.add(String(200, 8, "after the last region", fontName=SERIF_I, fontSize=6.8,
                 textAnchor="middle", fillColor=GREY))
    return d


# ---------------------------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------------------------
def on_page(canvas, doc):
    canvas.saveState()
    page = canvas.getPageNumber()
    if page > 1:
        canvas.setFont(SERIF_I, 8.5)
        canvas.setFillColor(GREY)
        canvas.drawCentredString(PAGE_W / 2, PAGE_H - 0.62 * inch, RUNNING)
        canvas.setStrokeColor(FAINT)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, PAGE_H - 0.7 * inch, PAGE_W - MARGIN, PAGE_H - 0.7 * inch)
    canvas.setFont(SERIF, 9)
    canvas.setFillColor(INK)
    canvas.drawCentredString(PAGE_W / 2, 0.6 * inch, str(page))
    canvas.restoreState()


class Paper(BaseDocTemplate):
    def __init__(self, filename):
        super().__init__(
            filename, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN,
            title=f"{TITLE}: The Hexera CAD Repair System",
            author="Hexera Engineering",
            subject="Design and measured behaviour of the Hexera CAD Repair System (CRS)",
            keywords="CAD repair, B-rep, OpenCASCADE, meshing, gmsh, verification, LLM",
        )
        frame = Frame(MARGIN, MARGIN, CONTENT_W, PAGE_H - 2 * MARGIN, leftPadding=0,
                      rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates(PageTemplate(id="main", frames=[frame], onPage=on_page))


# ---------------------------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------------------------
def front_matter():
    s = [
        Gap(6),
        P(TITLE, "title"),
        P("Design and Measured Behaviour of the Hexera CAD Repair System", "subtitle"),
        P("Hexera Engineering", "author"),
        P("Technical White Paper, Version 2.0 — November 2026", "affil"),
        P("Abstract", "abs_head"),
    ]
    s.append(P(
        "CAD models imported for computational fluid dynamics (CFD) and finite-element analysis "
        "(FEA) often fail before the solver runs. Their boundary representations are invalid, "
        "open, or full of geometry far smaller than the mesh, and the mesh generator rejects "
        "them. Hand cleanup is the usual remedy, and it is slow. This paper describes the Hexera "
        "CAD Repair System (CRS), which treats every geometry edit as a <i>transaction</i>. Each "
        "edit starts from a located defect and applies one narrow kernel operation to a "
        "snapshot. The result is then inspected again, independently, and the edit is kept only "
        "if it shows measurable progress within limits taken from the original upload. "
        "Otherwise the snapshot is restored. We describe three extensions built on this core. "
        "The first is a region-scoped driver, so that a local edit needs only a local proof. "
        "The second is a mesh probe that turns mesher failures into located defects. The third "
        "is a model author paired with a separate semantic reviewer, where the reviewer sees only "
        "measured evidence. We report results on public and internal corpora, including the "
        "negative ones. On all 33 NIST MBE PMI files, the region driver produces exactly the "
        "same outcomes as the baseline. On a 50-part defect campaign it cuts whole-shape "
        "re-inspections from 566 to 54, and it changes none of 40 clean control parts. On 80 "
        "real-world STEP files that fail a gmsh volume mesh, the defects the inspector reports "
        "turn out to be a poor predictor of mesh failure: for 15 of the files it reports none. "
        "New operations aimed at the main failure families broke no file that meshed before. "
        "However, none of the measured mesh gains can be credited to them. A harvest of 100,000 "
        "real parts then yielded a corpus of 9,956 genuine mesh failures (9.9%), which settles "
        "which failure families are worth spending on. Finally we describe, without claiming a "
        "result for it, an agentic second generation of the loop: goal profiles, a ladder of "
        "cheap in-loop oracles with the large meshers banned by an enforced import blocklist, a "
        "bounded investigator that may look, measure, probe and edit on a throwaway copy, a "
        "tournament of candidate repairs rehearsed on clones, and a two-tier reviewer whose "
        "objections are measurements. It is built and merged; no evidence pass for it has been "
        "run, and every one of its posture flags defaults off.",
        "abstract",
    ))
    s.append(P(
        "<b>Keywords:</b> CAD repair; boundary representation; OpenCASCADE; mesh generation; "
        "verification; transactional editing; LLM-guided repair; benchmark methodology.",
        "keywords",
    ))
    return s


def introduction():
    s = [H1("1", "Introduction")]
    s.append(P(
        "A simulation pipeline begins with geometry it did not author. Parts reach it from many "
        "CAD systems, usually through a neutral exchange format such as STEP, and the "
        "translation leaves damage behind. Faces lose their shared edges and shells open up. "
        "Trimming curves no longer agree with their surfaces. Slivers and edges a few nanometres "
        "long appear. Some of this violates the validity rules of the boundary representation "
        "(B-rep), and the rest is valid but still unmeshable. Either way the mesh generator fails "
        "before an engineer has set a single boundary condition. In practice, an analyst then "
        "repairs the model by hand in a CAD tool, which is skilled work that adds no value and "
        "has to be repeated for every revision of the part.", "body0"))
    s.append(P(
        "Automatic healing tools are widely available, OpenCASCADE's ShapeFix toolkit being one "
        "example, and most commercial pre-processors ship their own. The usual pattern is to run "
        "the healer and accept its result. That trust is misplaced for three reasons. First, a "
        "healing call that reports success has only stopped reporting errors. It may have done "
        "so by widening tolerances until the kernel stops checking. Second, removing small "
        "features is defeaturing, which can change the physics the model is meant to capture. "
        "Third, nothing in the pattern records what was changed, or why, in a form a reviewer "
        "can audit."))
    s.append(P(
        "CRS starts from the opposite assumption: <i>a kernel's success flag is not evidence.</i> "
        "Every candidate edit is measured independently and compared with the original upload. "
        "It is kept only when that measurement shows progress and every limit holds; otherwise "
        "it is undone. The upload itself is never modified. This paper documents the resulting "
        "design and what we have measured about it. Its contributions are:"))
    s += bullets([
        "<b>A transactional repair model</b> (Sections 3 and 4.4). Each edit is located, "
        "declared, snapshotted, independently re-inspected and judged against cumulative limits "
        "taken from the upload, so a rejected candidate can never become output.",
        "<b>A region-scoped repair driver</b> (Section 4.5). It groups defects into regions and "
        "proves each local edit locally. A run then needs only two whole-shape scans instead of "
        "one per attempt.",
        "<b>Mesh-failure localisation</b> (Section 4.7). An isolated gmsh probe turns mesher "
        "failures into fingerprinted defects, and new operations are aimed at the main failure "
        "families.",
        "<b>Model-authored repair under an independent gate</b> (Section 4.8). A language model "
        "proposes scoped work orders and states its predicted outcome before anything runs. A "
        "second, separate model reviews each committed edit using measured evidence only.",
        "<b>An evaluation discipline and its results</b> (Sections 6 and 7). Runs are packaged as "
        "sealed evidence bundles, judged against clean no-op controls, and each one states what "
        "it does <i>not</i> show. We report the results under that discipline, failures "
        "included.",
        "<b>An agentic second generation, described and not yet measured</b> (Section 5). Goal "
        "profiles, a ladder of cheap in-loop oracles with the large meshers banned by an "
        "enforced import blocklist, a bounded investigator that may look, measure, probe and "
        "edit on a throwaway copy, a tournament of candidate repairs rehearsed on clones, and a "
        "two-tier reviewer whose objections are measurements. It is built, tested and merged, "
        "and carries no result: Table 5 states for every piece what is missing, and Section 9 "
        "states what cannot be claimed.",
    ], numbered=True)
    s.append(Gap(4))
    s.append(P(
        "Section 2 reviews related work. Section 3 states the problem formally and Section 4 "
        "describes the architecture as evaluated. Section 5 describes the agentic loop built on "
        "top of it, which no measurement in this paper covers. Sections 6 and 7 cover the "
        "evaluation method and results. "
        "Section 8 discusses what the results mean, Section 9 lists limitations and open work, "
        "and Section 10 concludes."))
    return s


def background():
    s = [H1("2", "Background and Related Work")]
    s.append(H2("2.1", "B-rep validity and healing"))
    s.append(P(
        "A B-rep describes a solid by its bounding surfaces. It is built from faces lying on "
        "surfaces, bounded by wires of edges, which lie on curves and end at vertices. Faces are "
        "grouped into shells, and closed shells bound solids. A valid B-rep needs consistent "
        "topology: shared edges, closed wires, consistently oriented shells, and parameter-space "
        "curves that match their 3D counterparts within a tolerance. In OpenCASCADE Technology "
        "(OCCT) [11], " + c("BRepCheck_Analyzer") + " checks these rules, and the ShapeFix and "
        "ShapeUpgrade packages provide local fixes: sewing, wire repair, removal of small edges "
        "and faces, and splitting of closed faces. CRS uses these primitives but never trusts "
        "their return values on their own. A valid shape is necessary for meshing but, as "
        "Section 7.4 shows, far from sufficient.", "body0"))
    s.append(H2("2.2", "Meshing defective geometry directly"))
    s.append(P(
        "Another approach skips repair and builds the mesher to tolerate defects. fTetWild [4] "
        "tetrahedralises imperfect triangle soups robustly inside a user-set envelope. For many "
        "analyses that is the right tool. It gives up the parametric B-rep, though, and our "
        "product requires the repaired output to remain editable CAD that the customer can "
        "inspect, re-open and re-mesh with any engine. CRS therefore fixes the CAD and leaves "
        "mesher settings alone. Mesh <i>quality</i>, meaning element shape and conditioning "
        "[5, 6], belongs to the meshing stage and is outside the scope of this paper."))
    s.append(H2("2.3", "Language models in refinement loops"))
    s.append(P(
        "Self-Refine [1] showed that one model can generate, critique and revise its own output, "
        "with sizeable gains on several tasks. Huang et al. [2] showed the limit of that result: "
        "without an external oracle, intrinsic self-correction often makes reasoning accuracy "
        "<i>worse</i>. More recently, a study of agent loops [3] measured what it calls a "
        "<i>progress mirage</i>. Over 54 improvement cycles a frontier agent claimed progress "
        "every time, yet 56% of those cycles were flat or worse. The strongest in-band judge "
        "tested, which read the full artefact and diff, still accepted cycles of which 44% were "
        "real regressions. The mirage disappeared only when success could be checked against the "
        "artefact itself. We take this as a design constraint. In CRS, a model may propose an "
        "edit, but kernel measurement decides whether the edit stays, and the model's view of "
        "its own work counts as a claim to be tested, never as evidence."))
    s.append(H2("2.4", "Agents for simulation set-up"))
    s.append(P(
        "Multi-agent systems such as MetaOpenFOAM [7] and Foam-Agent 2.0 [8] automate solver "
        "configuration and run orchestration from natural-language task descriptions. They "
        "assume the geometry already meshes. CRS works on the stage before them."))
    return s


def formulation():
    s = [H1("3", "Problem Formulation")]
    s.append(P(
        "Let <i>U</i> be the uploaded shape. It is never modified. An <i>inspection</i> "
        "<i>I</i>(<i>S</i>) of a shape <i>S</i> returns a set of located defects. Each defect "
        "<i>d</i> has a code, a severity (info, warning, error or critical), the entity it sits "
        "on, measurements, and a <i>fingerprint</i>: an identity computed from the entity's "
        "geometry, so the same entity gets the same name across runs and across rescans. An "
        "<i>operation</i> <i>o</i> takes a shape and a target defect and returns a candidate "
        "<i>S</i>′ along with a <i>declaration</i> δ, which states the faces it adds "
        "or removes, the area and volume it expects to move, and the solids it adds.", "body0"))
    s.append(P(
        "A candidate <i>S</i>′ produced from the current accepted shape <i>S</i> is "
        "<b>kept</b> only if all four of the following conditions hold. If any one fails, the "
        "session restores <i>S</i>."))
    s += bullets([
        "<b>Kernel floor.</b> <i>S</i>′ is valid, no solid is lost, no face disappears "
        "beyond what was declared, and at least one meshable face remains. No package or "
        "directive can waive this.",
        "<b>Cumulative caps, measured against <i>U</i>.</b> No sub-shape tolerance may exceed "
        "<i>τ</i><sub>max</sub> (default 0.1 mm). The surface may move at most "
        "<i>ρ</i> = 0.001 of the bounding-box diagonal. Area and volume may differ from "
        "<i>U</i> plus the declared change by at most 2%. Because these limits are measured "
        "against the upload rather than the previous step, a sequence of individually small "
        "edits cannot add up to a large one.",
        "<b>Progress.</b> On independent re-inspection, the target defect has gone or materially "
        "improved, and no new finding of warning severity or worse has appeared.",
        "<b>Declaration honesty.</b> A measured change that exceeds the declaration is refused, "
        "even if the change itself would be harmless. In the words of the code comment, an "
        "operation that declared 50 mm² and moved 200 is refused “not for moving "
        "surface, but for not understanding what it did.”",
    ])
    s.append(Gap(4))
    s.append(P(
        "A <i>run</i> is a sequence of kept transactions under three budgets: number of "
        "operations, wall-clock time, and consecutive attempts without progress. Before "
        "promotion, the final shape is inspected again and must stage for the chosen mesh engine. "
        "We call a part <i>clean</i> when no defect of error severity or worse remains. The "
        "longer-term goal set out in the design (Section 8) is stricter: every remaining defect "
        "is either removed or explicitly reclassified with a recorded rationale, <i>and</i> the "
        "part meshes within a declared budget."))
    s.append(P(
        "The safety argument relies on one asymmetry. A wrongly <i>rejected</i> edit costs a "
        "missed repair. A wrongly <i>accepted</i> edit ships a part with altered geometry to a "
        "customer. CRS is therefore built to refuse whenever it is uncertain: an unsupported "
        "defect, an ambiguous target, an exhausted budget or a failed measurement all lead to "
        "rollback or refusal, and a silent acceptance is never the fallback."))
    return s


def architecture():
    s = [H1("4", "System Architecture")]
    s.append(P(
        "Figure 1 shows the path a part takes. CRS sits between CAD intake and mesh generation. "
        "It reads an immutable upload, inspects it, routes it, and may open a repair session. "
        "If it does, it promotes a separate repaired artefact only if that artefact passes a "
        "final check.", "body0"))
    s.append(figure(fig_pipeline(),
                    "The CRS pipeline. Inside the repair session, each operation runs as a "
                    "transaction against a snapshot. A candidate that fails any condition from "
                    "Section 3 is rolled back, and nothing rejected becomes output."))

    s.append(H2("4.1", "Inspection and localisation"))
    s.append(P(
        "The inspector reads the B-rep and reports defects using a deliberately small, stable "
        "vocabulary (Table 1). It keeps facts about the CAD kernel separate from facts about the "
        "surface mesh: a STEP file can be a valid B-rep and still give a bad staged surface, and "
        "the report has to say which layer failed. Each defect is tied to a kernel entity and a "
        "fingerprint. When a later operation rebuilds topology, an entity mapping links old "
        "names to new ones and records whether each link came from the kernel's own history or "
        "from geometric matching (" + c("basis: kernel | geometric") + "), so any downstream "
        "credit assignment knows how reliable the link is.", "body0"))
    s.extend(table(
        "Defect vocabulary. The kernel codes come from the original design. The " + c("mesh_*")
        + " codes come from the mesh probe (Section 4.7), have error severity, and are not "
        "reserved judgement codes.",
        [["Code", "Meaning"],
         [c("invalid_brep"), "The OCCT validity check fails."],
         [c("open_shell"), "A body that must be closed still has free boundaries."],
         [c("wire_gap"), "Adjacent wire endpoints are disconnected."],
         [c("small_edge") + ", " + c("small_face"), "An edge length or face area is below the "
          "profile threshold."],
         [c("degenerate_edge"), "An edge is degenerate or nearly zero in length."],
         [c("curve_inconsistency"), "A 2D or 3D curve is missing or wrong, or same-parameter "
          "consistency is broken."],
         [c("self_intersection"), "A wire or face intersects itself."],
         [c("engine_staging_failure"), "The selected mesh engine cannot stage the geometry."],
         [c("mesh_overlapping_facets"), "gmsh reports overlapping facets on a surface."],
         [c("mesh_edge_through_face"), "gmsh reports a segment crossing a facet (PLC error)."],
         [c("mesh_open_loop"), "A face's 1D mesh does not form a closed loop."],
         [c("mesh_periodic_surface"), "gmsh cannot mesh a closed, periodic surface."],
         [c("mesh_no_volume") + ", " + c("mesh_timeout"), "A solid yields no volume elements, "
          "or the probe exceeds its budget. Both apply to the whole part and are unlocated."]],
        [2.05 * inch, CONTENT_W - 2.05 * inch]))

    s.append(H2("4.2", "Triage"))
    s.append(P(
        "Triage gives every inspected part one of four routes. A part with no blocking defect "
        "goes to <i>mesh as is</i>. A part whose blocking defects all belong to families the "
        "catalogue can address goes to <i>conservative repair</i>. A part whose correct fix "
        "depends on engineering intent goes to <i>manual cleanup</i>, and one that needs an "
        "answer only the customer can give goes to <i>ask customer</i>. Only conservative repair "
        "opens a session. One consequence comes up again in Section 7.6: a part the inspector "
        "calls clean is never scanned further, even if it then fails to mesh.", "body0"))

    s.append(H2("4.3", "The operation catalogue"))
    s.append(P(
        "Each operation implements one protocol. It declares which defect codes it answers and "
        "what evidence it will produce, it can decline before touching the shape, and it returns "
        "a candidate together with its declaration. Table 2 lists the catalogue. Apart from the "
        "basic connectivity repairs, every operation sits behind its own deployment flag, and "
        "all of those flags are off by default.", "body0"))
    s.extend(table(
        "The repair catalogue. The OCCT column names the main kernel tool; most operations wrap "
        "it in extra pre- and post-checks. “Flag” means the operation is off unless "
        "a deployment turns it on.",
        [["Operation", "Answers", "Principal OCCT tool", "Posture"],
         [c("sew_faces"), "open shells, coincident boundaries", c("BRepBuilderAPI_Sewing"),
          "base"],
         [c("fix_small_edges"), "small and degenerate edges", c("ShapeFix_Wireframe"), "base"],
         [c("fix_face_boundary"), "wire ordering, p-curves", c("ShapeFix_Face"), "base"],
         [c("fix_shape_global"), "residual validity faults (fallback)", c("ShapeFix_Shape"),
          "base"],
         [c("fix_small_faces"), "sub-tolerance slivers", c("ShapeFix_FixSmallFace"), "flag"],
         [c("fill_planar_hole"), "bounded planar openings", c("BRepBuilderAPI_MakeFace"), "flag"],
         [c("fill_nonplanar_hole"), "bounded curved openings", c("BRepFill_Filling") + " (G1)",
          "flag"],
         [c("heal_self_intersections"), "kernel-identified crossings",
          c("ShapeFix_IntersectionWire"), "flag"],
         [c("split_closed_faces"), c("mesh_periodic_surface"),
          c("ShapeUpgrade_ShapeDivideClosed"), "flag"],
         [c("fragment_bodies"), c("mesh_edge_through_face") + " on assemblies",
          c("BOPAlgo_Builder"), "flag"],
         [c("rebuild_face"), c("mesh_overlapping_facets") + " (2D stage)",
          "re-trim from boundary wire", "flag; needs reviewer"]],
        [1.72 * inch, 1.68 * inch, 2.12 * inch, CONTENT_W - 5.52 * inch]))
    s.append(P(
        "Three guards protect the operations most likely to change what the part means. First, "
        "hole filling refuses openings wider than 0.75 of the part diagonal, measured across the "
        "hole: beyond that the opening is a missing wall, and a flat lid over a missing wall "
        "makes a different part. Second, a <i>paired-rim</i> guard refuses to patch an opening "
        "whose rim pairs with another rim to form a through-passage, because patching both ends "
        "would seal a hole the designer intended. Third, face removal needs an exact face-count "
        "declaration and is refused if even one more face disappears."))

    s.append(H2("4.4", "The repair session"))
    s.append(P(
        "The session owns one shape. Before each operation it takes a snapshot by reference. It "
        "measures the candidate (validity, counts, area, volume and tolerances), applies the "
        "four conditions from Section 3, and then either keeps the candidate or restores the "
        "snapshot. Every attempt is written to a run history that records the operation, its "
        "target, the declaration, the measurements and the verdict. A failed or rolled-back "
        "attempt is kept as evidence and is never silently retried on the same aim. When several "
        "defects belong to one tangle and so propose the same heal, the history records the "
        "decline once, keyed by aim, rather than once per member face.", "body0"))
    s.append(P(
        "We learned to keep instrumentation separate from verdicts, and the lesson came from a "
        "failed gate (Section 7.2). An analyser that only observes, such as a face-crossing scan "
        "run after every operation, can demote outcomes simply by measuring things the baseline "
        "never measured. CRS now runs that scan only at a session's first scan and for the "
        "re-inspection that judges a heal. A crossing that only the analyser sees is reported as "
        + c("analyzer_only") + " and does not change the inspection status, the triage route or "
        "how the loop ends."))

    s.append(H2("4.5", "Region-scoped repair"))
    s.append(P(
        "The original driver re-proved the whole part after every attempt. One local edit "
        "triggered a global proof. That made each iteration expensive, so budgets were kept "
        "small: six operations, two stalls and 120 s by default. The stall limit applied to the "
        "whole run, so two unrelated failures anywhere ended it. The region-scoped driver "
        "(Algorithm 1, Figure 2) is built on one observation. A <i>region</i> is a natural unit "
        "for four purposes at once: understanding (what a planner reasons about), verification "
        "(a local edit needs only a local proof), reversal (a wrong hypothesis costs one region, "
        "not the whole run) and cost (regions are independent).", "body0"))
    s.append(P(
        "Regions are formed by growing each located defect's bounding box and merging any boxes "
        "that intersect, transitively. The rule deliberately merges too much rather than too "
        "little: the members of a self-intersection tangle all propose the same heal, and "
        "splitting the tangle across two regions would set two packages against the same "
        "geometry. Defects with no measurable extent are not merged with everything else, since "
        "that would collapse the part into one region. They go into a single trailing "
        "<i>unlocated</i> region, which is flagged, never dropped, and escalated rather than "
        "edited. Regions are visited in order of their most severe defect. An earlier spatial "
        "ordering let a run's clock expire before it reached an error in region 47 of 103."))
    s.append(algorithm(
        "Region-scoped repair. Kernel mutation is serialised; only package authoring may "
        "overlap.",
        """
input : upload U, budgets B (ops, seconds, attempts per region)
S <- U ;  D <- WholeShapeScan(S)                          # scan 1 of 2
R <- Decompose(D)            # grown-bbox clustering; unlocated defects -> one trailing region
for r in R ordered by max severity (unlocated last):
    if not r.located: Escalate(r); continue
    for attempt in 1 .. B.attempts:
        pkg <- Author(r, D[r], history[r])                 # rules author, or model (Sec. 4.8)
        if pkg is None: break
        snap <- Snapshot(S)
        S'   <- Execute(pkg.plan, S, within = r.locus)
        if ScopedProof(S', pkg.predicate, caps(U)) and Review(pkg, evidence) == accept:
            S <- S' ; Commit(r) ; break
        S <- Restore(snap) ; history[r].append(measured delta)  # fed to the next attempt
    else: Escalate(r)                                     # budget spent: others still ship
D_final <- WholeShapeScan(S)                              # scan 2 of 2; one global verify
return S, report(history, D_final)
"""))
    s.append(figure(fig_regions(),
                    "Control flow of the region driver. A failed predicate or a review rejection "
                    "reverts only its own region, and the measured difference goes back to the "
                    "author. When a region's budget runs out it escalates while the other regions "
                    "still commit."))
    s.append(P(
        "In the target design, kernel mutation remains serialised: the session owns one shape, "
        "and OCCT does not support concurrent mutation of one " + c("TopoDS_Shape") + ". Only "
        "the model calls that author packages may overlap. The cost saving does not depend on parallelism anyway. It comes from "
        "avoided scans: a run takes two whole-shape scans, one at the start and one at the "
        "merge, and in between uses scoped scans that check only a region's faces. Some "
        "invariants are global, such as the paired-rim guard, and they stay part-wide. An early "
        "version checked rims only within one region, so two rims of the same passage in "
        "different regions were both patched and the through-hole was sealed. The corpus caught "
        "this, and it was fixed before merge (Section 7.3).", "body"))

    s.append(H2("4.6", "Target packages and pre-registered predicates"))
    s.append(P(
        "In the region driver, the unit of action is a <i>target package</i>, a scoped work "
        "order with five parts: a <i>locus</i> (entity fingerprints and the region they cover), "
        "an <i>intent</i> in plain words, a <i>plan</i> (an ordered list of catalogue operations "
        "within the locus), a <i>predicate</i>, and a <i>budget</i>. The predicate states, "
        "<i>before execution</i>, which fingerprints will be cleared, which will remain "
        "unchanged, and, for mesh defects, which faces will mesh. The harness then checks that "
        "exact claim, so the author cannot grade its own work. It can only make a claim that "
        "something else measures. A claim that names a fingerprint nobody reported fails, and "
        "so does a vacuous claim such as “the shape is still valid”. A package is "
        "also never a cap waiver: the caps, rollback and progress rule apply to it exactly as "
        "they apply to the deterministic planner.", "body0"))

    s.append(H2("4.7", "The mesh probe as an inspector"))
    s.append(P(
        "Meshing a part is the actual goal, and the inspector's defects turned out to be a poor "
        "predictor of meshing failure (Section 7.4). CRS therefore adds a detector that runs the "
        "mesh itself. The probe uses gmsh 4.15.2 [12]. It imports the shape through gmsh's OCC "
        "kernel, sets " + c("MeshSizeMax") + " to the bounding-box diagonal divided by 40, and "
        "generates first the 2D and then the 3D mesh on one thread, with a 120 s budget. A part "
        "passes if it yields more than zero volume elements. The probe runs in a freshly "
        "exec’d interpreter, never a fork, inside its own process group, so a gmsh crash "
        "or hang cannot take the session with it.", "body0"))
    s.append(P(
        "gmsh identifies a failing surface by its tag. The probe imports exactly the shape it is "
        "given, so tags follow that shape's own face map and can be translated into face "
        "fingerprints. PLC failures on assemblies are traced to the pair of bodies in contact. A "
        "failure the probe cannot map to an entity is still reported, as an unlocated defect. "
        "If the final whole-part volume mesh fails, the run stops with the reason "
        + c("unmeshable") + " and names the bodies. Packages already committed stay committed, "
        "because each one passed its own checks."))
    s.append(P(
        "A subtle point follows from the fact that gmsh reports only the <i>first</i> failure it "
        "meets. An operation that fixes that failure lets the probe go further, and the next "
        "failure then appears in the rescan as if it were new. Treated as new damage, this "
        "would roll back an operation exactly where it worked. That happened in practice: "
        + c("fragment_bodies") + " cleared an edge-through-face failure on one ABC part, and the "
        "probe then found overlapping facets on a face that had been failing all along. CRS "
        "therefore treats a new finding as <i>revealed</i> rather than <i>caused</i> only when "
        "all three of the following hold: the operation answers the mesh code it was aimed at "
        "and that defect has cleared; the new finding is itself a mesh code; and it does not sit "
        "on geometry the operation created, moved or changed. A revealed finding still stays on "
        "the defect list, so it blocks a " + c("resolved") + " ending."))
    s.append(P(
        "Three operations target the mesh families. " + c("split_closed_faces") + " splits "
        "periodic faces along their seams. " + c("fragment_bodies") + " gives touching bodies a "
        "shared, conformal interface while keeping every body. It declines when bodies "
        "interpenetrate, because then there is no safe default owner for the shared volume. "
        "Fusing bodies is deliberately not offered: bodies that touch are often separate parts "
        "on purpose. " + c("rebuild_face") + " re-trims a single face from its boundary wire. "
        "It acts only on overlaps raised in gmsh's 2D stage, and it must not be enabled for "
        "customers until the reviewer of Section 4.8 is live."))

    s.append(H2("4.8", "Model author and semantic reviewer"))
    s.append(P(
        "The region driver accepts any author that maps (region, defects, prior attempts) to a "
        "package. A deterministic <i>rules author</i>, which maps each defect code directly to "
        "its operation, is the baseline. A <i>model author</i> can fill the same slot. For each "
        "region it receives a bounded observation: the defects with their measurements and "
        "gmsh's own error text; each target face's surface type, area, bounds and neighbours; "
        "and what earlier attempts did and why they were undone. It may reply in one of three "
        "ways: with a package; with a verdict that the CAD is sound and the mesher is the "
        "problem; or with a pass. The reply is treated as data, never as code. It is validated "
        "strictly against a schema. A reply that names an operation outside the active "
        "catalogue, or a fingerprint not among the region's defects, is refused before anything "
        "runs. A timeout, a parse failure or a missing model router all produce no package. A "
        "“mesher's problem” verdict clears nothing, because if it did it would become "
        "an easy way out on hard parts.", "body0"))
    s.append(P(
        "There is no numeric “barely moved” threshold. The owner chose instead to have "
        "each committed package judged by a <i>separate</i> model call with its own prompt. The "
        "reviewer is sealed off from the author. It never sees the author's raw reply, its "
        "self-assessment or the observation it read. The author's intent reaches it only as a "
        "field labelled as a claim. What the reviewer does see is measured: faces, solids, area "
        "and volume moved, which faces changed and their neighbours, the mesh-test result, and "
        "a sampled one-sided surface deviation from the pre-package shape (32 points, using "
        + c("BRepExtrema_DistShapeShape") + "). The evidence also states how sparse that sample "
        "was. A measurement that could not be taken appears as the string "
        "“unmeasured”, never as zero, because zero would read as “nothing "
        "moved”. The verdict is either accept or reject. Unsure counts as reject, as does "
        "no answer, and a rejection reverts the package just as a failed predicate does. There "
        "is no retrying until the reviewer accepts: one rejection buys one more authored attempt, "
        "and a second rejection sends the region to a person. The kernel floor and the "
        "declaration accounting remain hard limits throughout, and the reviewer depends on them "
        "to see what actually happened."))

    s.append(H2("4.9", "Configuration posture and audit"))
    s.append(P(
        "Every capability is controlled by a " + c("CAD_REPAIR_*") + " setting that <i>fails "
        "closed</i>: any value it does not recognise is read as off, because “a typo in a "
        "deployment must not put a model in charge of geometry.” Model-directed features "
        "move through the stages " + c("off") + ", " + c("shadow") + " and " + c("live") + ". "
        "In shadow, the model's proposals are recorded next to the run and nothing they contain "
        "is executed. Repaired artefacts are content-addressed, so delivering the same input "
        "again writes identical bytes. The repair report records every observation hash, model "
        "reply (clipped), validation outcome, attempt and review verdict. It serves both as the "
        "audit record and as a transition log for future learning. Appendix A lists the "
        "settings.", "body0"))
    return s



def agentic():
    """Section 5 - the agentic loop. NOTHING HERE IS MEASURED.

    Every statement is about design or about code that exists. The build ledger
    (.superpowers/sdd/2026-11-20-crs-v3-master/progress.md) is the source: 20 of 31 tasks merged,
    no evidence pass run, every posture flag default-off. Do not add a number to this section
    unless an evidence README carries it.
    """
    s = [H1("5", "The Agentic Loop")]
    s.append(P(
        "Section 4 describes the loop that produced every measurement in this paper. This section "
        "describes what has been built on top of it since. None of it carries a result yet. The "
        "evidence passes are written and have not been run, so everything below is design and "
        "code that exists, with its status stated piece by piece in Table 5. The reason for "
        "building it is in Sections 7.4 and 7.8: the deterministic catalogue's reach is bounded "
        "by the geometry it can name, and the families that dominate real mesh failure are not "
        "of that kind.", "body0"))

    s.append(H2("5.1", "Repair as a job, not a mesher subroutine"))
    s.append(P(
        "Repair used to be something the meshing pipeline called. It is now a job with a "
        "<i>goal profile</i>, and the profile decides only how the run finishes. The loop, the "
        "caps, the rollback and the audit record are the same in all three cases.", "body0"))
    s += bullets([
        "<b>Standalone.</b> Clean CAD back to the customer. The finishing verdict is kernel "
        "validity, one tier-2 meshability reading as a quality score, and semantic review of the "
        "committed packages. The product mesher is not involved at all.",
        "<b>Meshing.</b> Make the part meshable, then mesh it. The finishing verdict is the name "
        "of the product mesher, which runs once, at the end, on the winning shape.",
        "<b>Benchmark.</b> Evaluation against the sealed track.",
    ])
    s.append(P(
        "A profile also decides what the run reserves. " + c("standalone") + " and "
        + c("benchmark") + " hold back one tier-2 hard cap plus its slack so that the finishing "
        "reading can actually be taken. " + c("meshing") + " and an unset profile reserve "
        "nothing, which leaves an existing run's deadline byte-for-byte what it was. A profile "
        "never raises: an internal failure is recorded as a profile and an error, so a run that "
        "cannot produce a verdict still reports what it could not do."))

    s.append(H2("5.2", "The cheap-oracle ladder"))
    s.append(P(
        "The in-loop question is whether this part is better than it was, and asking a "
        "production mesher is the wrong way to ask it. The answer is correct and arrives minutes "
        "later, on a machine the repair loop has no business claiming. The ladder is the set of "
        "answers repair may buy.", "body0"))
    s += table(
        "The verification ladder. Only tiers marked in-loop are reachable from repair. Tier 2's "
        "calibration is the 76/79 agreement of Section 7.5; tier 2b has no calibration at all.",
        [["Tier", "Check", "In loop", "Cost, and what it answers"],
         ["0", "kernel checks", "yes",
          "Milliseconds. Validity, counts, declarations. Every attempt."],
         ["1", "scoped surface probe", "yes",
          "Sub-second. A region's faces after an edit. Every operation."],
         ["2", "whole-part coarse mesh", "yes",
          "Seconds, hard-capped at 60 s and 10<super>6</super> elements. One process, coarse "
          "sizing, worker CPU. The loop's meshability oracle."],
         ["2b", "tolerant-mesh pass", "yes",
          "Seconds. “Would the tolerant rung eat this?” A routing answer for the "
          "volume-stage overlap majority."],
         ["3", "product mesher", "<b>no</b>",
          "Minutes, and a machine. Only when the goal is meshing, only once, on the winner. "
          "Never reachable from inside the loop."]],
        [0.5 * inch, 1.35 * inch, 0.6 * inch, CONTENT_W - 2.45 * inch])
    s.append(H3("The ban is enforced, not promised"))
    s.append(P(
        "Tier 3's exclusion is the invariant that keeps a repair run from quietly becoming a "
        "meshing farm, and a comment asking for it would not survive the first person who needed "
        "a better answer in a hurry. The enforcement is a test that parses every module of the "
        "agentic loop and refuses a set of named imports: the machine chooser, the gmsh engine "
        "module, and all but three names of the tetrahedral rung. It reads the syntax tree "
        "rather than the file's text, so a docstring that legitimately mentions a banned name "
        "does not trip it, and a <i>deferred</i> import inside a function body does not evade "
        "it. That second property was proved by mutation: a function given a lazy import of the "
        "machine chooser tripped both guards. A separate guard refuses a module-level import of "
        "gmsh or fTetWild anywhere under the ladder, so neither mesher can be loaded in the "
        "parent process even once.", "body0"))
    s.append(H3("Tier 2b reports honestly when it cannot run"))
    s.append(P(
        "Tier 2b shells out to a coarse tolerant-mesh pass in a bounded child process, killed by "
        "process group at its budget. It has five endings, and the distinction between them is "
        "load-bearing: " + c("not_asked") + " (the question did not apply), " + c("unavailable")
        + " (no binary on this image), " + c("no_answer") + " (asked, nobody answered in time), "
        "and the two real verdicts. A mutation that collapsed the timeout branch into a refusal "
        "was caught by the suite, which is the point. A part nobody could answer for must not be "
        "reported as a part the rung declined.", "body0"))

    s.append(H2("5.3", "The grind cost predictor"))
    s.append(P(
        "Before spending anything on a part, the loop predicts how expensive that part will be "
        "to mesh, from the failure kind and the face count alone. The prediction is a tier - "
        + c("cheap") + ", " + c("standard") + ", " + c("heavy") + " or " + c("unknown") + " - "
        "under seven ordered rules. A part nobody answered for, or one over 1,000 faces, is "
        "heavy. A part gmsh refused only after building the whole volume is standard. A "
        "surface-stage refusal, or a part that meshed, is cheap. A missing face count yields "
        + c("unknown") + ", and so does a probe that was skipped for want of time, since "
        "recording “cheap to mesh” about a part nobody probed would poison the "
        "aggregate it feeds. The predictor exists so that the investigator's budget is a "
        "function of the part rather than a fixed attempt count, and so that a cheap part cannot "
        "buy a heavy part's spend by failing more often.", "body0"))

    s.append(H2("5.4", "The investigator"))
    s.append(P(
        "The one-shot author of Section 4.8 gets a bounded observation and must answer with a "
        "finished package. On the live product run it produced 34 proposals and none survived "
        "execution. The machinery held, which was what that run was for, but the shape of the "
        "task was wrong: an engineer handed one photograph of a part does not write the repair "
        "either.", "body0"))
    s.append(P(
        "The investigator replaces that single turn with a bounded tool loop, one per failing "
        "region. Four tools are declared to the model."))
    s += bullets([
        "<b>look.</b> Rendered views of the region through the existing review camera.",
        "<b>measure.</b> Distances, a thickness estimate, face properties, body-pair contact. "
        "Anything the measurement could not establish comes back null, never zero, and the "
        "thickness figure is labelled an estimate in the field name itself.",
        "<b>probe.</b> Tiers 0 to 2 on a candidate state.",
        "<b>edit.</b> A catalogue operation, applied to a scratch clone.",
    ])
    s.append(P(
        "Replies remain data and are validated strictly. Arguments are checked by name: a "
        "refusal states the offending key, the enumeration it violated or the type it was not, "
        "and a reference outside its bounds is refused with the bound named. At most four "
        "targets may be named in one call. The whole observation envelope is clipped to 6 kB by "
        "dropping the longest list and recording that it was dropped, so a region with a "
        "thousand faces does not quietly truncate in the middle of a number. The two "
        "measurements that run kernel Booleans, common volume and thickness, run in a bounded "
        "forked child, so a kernel hang cannot take the parent's interpreter with it."))
    s.append(P(
        "The loop ends, and the endings are the honest ones. The model concludes with a package, "
        "with a verdict that the CAD is sound and the mesher is at fault, or with an escalation "
        "naming why the region needs a person. A model that does not conclude when only proof "
        "time remains ends as " + c("budget_spent") + ": the clock ended the investigation, and "
        "inventing a sixth verdict for that would dress a timeout as a decision. One record is "
        "kept per region and updated in place, so a second call continues the same investigation "
        "rather than reading as two."))
    s.append(H3("Budgets are compute tiers"))
    s.append(P(
        "The budget table is indexed by the grind tier and is not readable from the environment, "
        "because a budget is a policy decision measured offline in the gym rather than a "
        "deployment knob. The lookup fails closed in every direction: an unknown tier, an "
        "unrecognised name, a non-string and a predictor that raises all receive the "
        "<i>smallest</i> budget.", "body0"))
    s += table(
        "What one region's investigation may spend. Tool calls are counted, not rounds, since a "
        "native round may carry several. A capability answer - the provider declining tool "
        "calls, which falls the transport back to JSON - is not charged.",
        [["Grind tier", "Tool calls", "Model calls", "Wall", "Typical part"],
         [c("cheap"), "6", "8", "60 s",
          "Surface-stage refusal, or a part the probe already meshes."],
         [c("standard"), "12", "16", "150 s", "Refused only after the volume was built."],
         [c("heavy"), "20", "26", "240 s", "No answer at all, or over 1,000 faces."],
         [c("unknown"), "6", "8", "60 s",
          "Nothing measured. Takes the cheap budget by the fail-closed rule."]],
        [0.85 * inch, 0.72 * inch, 0.78 * inch, 0.5 * inch, CONTENT_W - 2.85 * inch],
        align_right=(1, 2, 3))
    s.append(H3("Native tool calling"))
    s.append(P(
        "The model route carries real tool definitions rather than a JSON protocol described in "
        "prose. A provider that declines them is a typed capability answer caught at the seam, "
        "falling the transport back to JSON. The reply cache canonicalises a tool conversation "
        "differently from a text one. Call identifiers become first-seen ordinals, because they "
        "are noise, while tool <i>arguments</i> and <i>results</i> are kept exactly, results by "
        "digest. The text route's scrubbing of long hexadecimal strings and absolute paths would "
        "have erased a measured fingerprint or a face identifier and merged two genuinely "
        "different investigations into one cache entry.", "body0"))

    s.append(H2("5.5", "The scratch clone"))
    s.append(P(
        "An investigator that may edit needs somewhere to edit that is not the part. The scratch "
        "session is a copy of the shape with one structural guard: the first act of an attempt "
        "to commit on a scratch session is to revert and refuse. The guard fires before the "
        "predicate is read and before any review, so there is no ordering of events in which a "
        "rehearsal commits.", "body0"))
    s.append(P(
        "The isolation is asserted by identity and by kernel topology, not by comparing bytes. "
        "This is worth stating because the obvious test does not work. No kernel operation "
        "mutates its input, so the serialised bytes of “the clone” stay identical to "
        "the part's even when the clone <i>is</i> the part. The real exposures are tolerance "
        "creep through a shared underlying topology and a reset handing back an already-edited "
        "shape, and those are what the assertions name."))
    s.append(P(
        "A consequence to keep in view: because a scratch session can never reach review, a "
        "rehearsal produces no review signal at all. That an operation was kept is a claim about "
        "the operations, not about whether the result is still the customer's part. Anything "
        "learned on a clone still has to pass review on the real session."))

    s.append(H2("5.6", "Tournament repair"))
    s.append(P(
        "One region now gets up to K candidate repair paths rather than one, proposed by the "
        "rules author and by the investigator and deduplicated by a content key over the plan. "
        "Each is rehearsed on its own clone, scored, and the best survivor is <i>nominated</i>.",
        "body0"))
    s.append(algorithm(
        "Tournament repair of one region: rehearse on clones, commit once, review last. This is "
        "the loop as designed; step 3's commit on the real session is not yet wired.",
        """
input : upload U, working shape S, region r with located defects D_r,
        sources Sr = {Rules, Investigator}, ONE region clock C, tolerance tau,
        the customer's deviation threshold theta
t <- GrindTier(D_r)                       # nothing measured -> the smallest budget
w <- C.RegionWall(t)
if w == 0: return Refuse("no_budget")      # the budget governs, not an attempt count

# 1. propose - each item names op, target fingerprints, claimed defect, declared bound
K  <- Dedupe(union over s in Sr of Propose(s, r, D_r))[:K]
reject any item failing its operation's own preconditions   # before it costs an attempt

# 2. rehearse - on copies, so a bad plan costs nothing
R <- {}
for c in K:
    if C.MustConclude: R += {(c, NotRehearsed)} ; continue
    Q <- Clone(S)                          # shares no topology with S, and cannot commit
    for i in 1 .. len(c.plan):
        u <- Apply(Q, c.plan[i], tau)
        if u != Kept: R += {(c, Failed, reason(u))} ; break
    v0 <- Tier0(Q) ;  v1 <- Tier1(Q, r)
    v2 <- Tier2(Q) if the region's share exceeds a quarter of the hard cap
    a  <- Accounted(c.declared, measured)  # did it do what it said?
    d  <- Deviation(U, Q, EditedFaces(Q))  # against the upload, never the last step
    cl, rv <- DefectDelta(D_r, Q)          # cleared / revealed
    R += {(c, Rehearsed, v0..v2, a, d, cl, rv)} ;  discard Q

# 3. rank, then commit the winner ONCE, on the real shape
w* <- Winner(R)     # rehearsed > not ; pass > no-answer > refusal ;
                    # revealed-none first, then cleared, then deviation, then fewest ops
if w* is None: return Refuse("no_candidate_survived")      # never promote a loser
snap <- Snapshot(S) ; apply w* under caps and declaration accounting

# 4. judge - measurement answered "did it work"; the model answers "is it still the part"
if d <= theta and a and ops(w*) & VertexMoving == {}:
    AutoAccept with audit                  # no model call spent
else:
    verdict, objection <- Review(MeasuredEvidence)   # measurements, never the author's claim
    if verdict != accept:
        S <- Restore(snap) ; return the MEASURED objection to the author
        on a second rejection, escalate to a person    # no retry-until-accept
return verdict      # unsure is reject; a rehearsal win is a nomination, review is acceptance
"""))
    s.append(P(
        "K defaults to 1 and is clamped to [1, 6], so the tournament is inert until a deployment "
        "asks for it and cannot be configured into an unbounded spend. The ordering rules are "
        "the part worth arguing about. A no-answer ranks above a refusal and below a pass, "
        "because the three are different facts and collapsing the first two is the "
        "dishonest-ending failure this programme keeps guarding against; a mutation that ranked "
        "a no-answer as a refusal was caught by the ordering table. A revealed defect outranks "
        "any number of cleared ones, so no candidate can buy its way past new damage with volume "
        "of repair. When nothing survives, the loop reports that and offers nothing - there is "
        "deliberately no fall-back to the best-ranked loser.", "body"))
    s.append(P(
        "Rehearsal shares <i>one</i> clock per region with the investigation rather than deriving "
        "a second, because two clocks over one deadline is how a region starves the regions after "
        "it. Tier 1 is scoped to the region. Tier 2 is unscoped and is bought only when the "
        "candidate's share of the clock exceeds a quarter of the hard cap; otherwise the "
        "rehearsal records " + c("no_time") + " rather than a verdict it did not earn."))

    s.append(H2("5.7", "Tiered review and the measured objection"))
    s.append(P(
        "The reviewer in Section 4.8 reads every committed package, which spends a model call on "
        "changes whose entire story is already in the measurements. Review is now two tiers. A "
        "package auto-accepts, with the audit record written and no model call made, when three "
        "things hold: the measured deviation is under a threshold the customer sets at upload, "
        "the declaration fully accounts for what was measured, and no kept operation is in the "
        "vertex-moving set. Everything else goes to the reviewer. The vertex-moving set is named "
        "in one place, and the session-level test pins those operation names literally - the "
        "first version of that test passed vacuously when the set was emptied, and a test that "
        "cannot fail when the protection is removed is not protecting anything.", "body0"))
    s.append(P(
        "Both numeric guards are explicitly guarded against non-finite values. The red run found "
        "that an infinitely negative deviation auto-accepted, and that a not-a-number "
        "measurement read as “fully accounted”. A float guard on a safety threshold "
        "is the kind of hole that never appears on a happy path."))
    s.append(H3("The objection the author receives is measured"))
    s.append(P(
        "A rejection used to be a dead end. It now returns an objection to the investigator, and "
        "the objection is constructed so that the reviewer's prose has no route to the author. "
        "The reviewer names one thing: a <i>field</i>, which must be a member of a closed tuple "
        "of fourteen measured evidence keys. That name is then re-fetched from the tuple rather "
        "than copied from the reply, and the value beside it is read from the session's own "
        "evidence. The free-text keys - the plan, the author's claim, the reviewer's reasons - "
        "are deliberately not members. A field the evidence does not carry reads "
        "“unmeasured”, never zero.", "body0"))
    s.append(P(
        "The seal is worth describing as a proof rather than an intention, because the first "
        "attempt at it leaked. Trusting the field name from the reply put actual reviewer prose "
        "in front of the author in a test. A second mutation, reading the value from the reply, "
        "initially <i>survived</i> because the strict schema happened to neutralise it; the "
        "proof was only completed by calling the objection builder directly, past the schema, "
        "which is the level at which a future schema widening would attack it. On the author's "
        "side, the observation handed back on a re-call gains the objection and nothing else, "
        "and the test walks the entire JSON of that observation for every word of a prose-heavy "
        "reviewer reply."))

    s.append(H2("5.8", "The corpus gym"))
    s.append(P(
        "A prompt change is a policy change, and the only honest way to ship one is to score it "
        "offline against the real-failure corpus first. The gym fingerprints the policy - twelve "
        "declared sources, among them the prompts, the catalogue text, the budget table and the "
        "ladder's constants - by parsing each source rather than importing it. Importing the "
        "repair stack to read a float would drag the stack, and on some paths a mesher, into the "
        "host gate's process.", "body0"))
    s.append(P(
        "Scoring refuses two bundles that do not declare the same stratified sample, naming both "
        "hashes. It reports newly meshed, newly broken, the clean control, a mean score and cost "
        "per fixed part, headline and per stratum, over the intersection of the two arms' rows "
        "only. The record ships <i>empty</i> and the gate skips while it is, because seeding a "
        "fingerprint from a run nobody performed would be a fabrication; a test proves the gate "
        "turns hard the instant any score lands. Today no bundle writer records which sample it "
        "drew, so the gate refuses every real bundle: the gym is built and not yet usable."))

    s.append(H2("5.9", "What is built and what is not"))
    s += table(
        "Status of the agentic loop. “Built” means the module and its tests are "
        "merged, each mutation-proved. It does not mean measured: no evidence pass for any row "
        "below has been run, and every posture flag defaults off.",
        [["Piece", "Built", "What is missing"],
         ["Goal profiles", "yes", "The standalone job surface is not wired to an entry point."],
         ["Oracle ladder, tiers 0-2", "yes", "&mdash;"],
         ["Tier 2b tolerant pass", "yes",
          "Reads " + c("unavailable") + " on the pipeline worker image, which carries no "
          "tolerant-mesh binary. No calibration."],
         ["Big-machine import blocklist", "yes", "&mdash;"],
         ["Grind predictor", "yes",
          "Predictions are recorded; nothing branches on them yet."],
         ["Investigator tool loop", "yes",
          "Native tool calling is untested against a real provider; the router test substitutes "
          "the transport."],
         ["Rendered views for " + c("look"), "no",
          "The camera seam exists and is unwired."],
         ["Scratch clone", "yes", "&mdash;"],
         ["Tournament scoring, rehearsal", "yes",
          "<b>The winner is not yet committed on the real session.</b> The nomination is "
          "computed and goes nowhere."],
         ["Tiered review", "yes",
          "<b>Inert.</b> Nothing declares a deviation threshold, so no package can take the "
          "auto-accept tier."],
         ["Measured review objection", "yes", "&mdash;"],
         ["Corpus gym", "yes",
          "No bundle written today states its sample, so scoring refuses every real bundle."],
         ["Cross-run outcome memory", "no",
          "The content-addressed plan key exists as the intended key."],
         ["Mesher handback into repair", "no",
          "The named-face refusal is already produced by the meshing ladder."]],
        [1.95 * inch, 0.5 * inch, CONTENT_W - 2.45 * inch])
    return s


def methodology():
    s = [H1("6", "Evaluation Methodology")]
    s.append(H2("6.1", "Corpora"))
    s.append(P(
        "Table 3 lists the corpora. The public ones are pinned by archive and per-file SHA-256 "
        "hashes. No CAD file is committed, since the ABC models [9] belong to their creators and "
        "the OCCT dataset has no redistribution licence. Each evidence bundle therefore contains "
        "names, hashes and fetch instructions.", "body0"))
    s.extend(table(
        "Evaluation corpora.",
        [["Corpus", "Files", "Source", "Purpose"],
         ["OCCT test dataset 7.8.0", "331 rows", "Open-Cascade-SAS release V7_8_0 [11]",
          "floor gate for catalogue growth"],
         ["NIST MBE PMI CTC/FTC", "33", "NIST conformance test files [10]",
          "repair parity, redistributable"],
         ["Generated defect campaign", "50", "internal defect generator",
          "cost of re-inspection"],
         ["Clean references", "40 of 316", "internal, all 22 part families",
          "no-op control: nothing may change"],
         [c("mesh-failure-real"), "80", "33 OCCT + 47 ABC chunk 0050 [9]",
          "real files that fail a gmsh volume mesh"],
         ["Formula 1000 vehicle body", "1", "Ludemann Engineering, public [13]",
          "end-to-end case study"]],
        [1.7 * inch, 0.72 * inch, 2.15 * inch, CONTENT_W - 4.57 * inch],
        align_right=(1,)))
    s.append(H2("6.2", "Protocol"))
    s.append(P(
        "Each comparison is a <i>sealed bundle</i> made up of a manifest (per-input hash, engine "
        "commit, platform and creation time), the exact settings with their digest, per-file "
        "rows, a summary and the verbatim log. A/B arms run on the same bytes with the same "
        "settings except for the one variable being tested. If the arms ended up differing in "
        "more than that, as happened once with the engine commit (Section 7.6), the bundle says "
        "so and limits its claims accordingly. Settings are fixed before a run, and no "
        "threshold is tuned after the results are seen. Every evidence README closes with a "
        "section titled “what this does not show”.", "body0"))
    s.append(P(
        "Two kinds of control anchor the claims. A <i>floor gate</i> requires a flags-on run to "
        "repair at least as many defects as the baseline, with every part kept by the baseline "
        "kept again. A <i>clean no-op control</i> runs clean reference parts through the same "
        "settings, and any kept operation or any change in faces, solids or volume counts as a "
        "failure. This control catches a kind of damage that defect counts cannot: an edit to "
        "geometry that had nothing wrong with it."))
    s.append(H2("6.3", "Metrics, and one we reject"))
    s.append(P(
        "We report outcome vectors (<i>repaired</i>, <i>refused</i>, <i>nothing to repair</i>, "
        "<i>not attempted</i>, <i>no change</i>), counts of kept and rolled-back operations, "
        "whole-shape versus scoped scans, and wall-clock time. For meshing we report "
        "<i>newly meshed</i> and <i>newly broken</i> files, together with the remaining failures "
        "by kind. We do not use the count of runs that end <i>resolved</i> as a repair metric. "
        "In a sweep of sliver precision, the number of resolved runs rose from 17 to 39 while "
        "kept operations fell from 32 to 12 and remaining defects rose from 371 to 396. The "
        "setting that resolved the most runs repaired the least, so the count mainly reflects "
        "how little was attempted.", "body0"))
    return s


def results():
    s = [H1("7", "Results")]
    s.append(H2("7.1", "Case study: a Formula 1000 vehicle body"))
    s.append(P(
        "We ran CRS on the public Formula 1000 race-car body from Ludemann Engineering [13], "
        "using the source file as received. The initial inspection reported two invalid-B-rep "
        "findings, two self-intersecting-wire findings, 132 sliver faces and 35 tiny edges. The "
        "accepted sequence resolved the blocking invalidity and removed four measured sliver "
        "faces. It left 162 warning-level findings in place rather than over-defeature the body "
        "(Table 4). On the companion front-suspension rocker, warnings fell from 12 to 8 and "
        "sliver faces from four to one, while the single solid was preserved and the B-rep "
        "stayed valid.", "body0"))
    s.extend(table(
        "Formula 1000 body, before and after CRS. Staging time is from a single run.",
        [["Measure", "Before", "After", "Interpretation"],
         ["OCCT validity", "invalid", "valid", "Error-level topology resolved."],
         ["Solids", "8", "8", "No body lost."],
         ["Faces", "421", "419", "Bounded clean-up; extents unchanged."],
         ["Findings", "171", "162 warnings", "Blocking errors cleared; warnings kept, not "
          "hidden."],
         ["cfMesh CAD staging", "16.87 s", "7.99 s", "Both staged; repaired input about twice "
          "as fast here."],
         ["Surface triangles", "147,545", "153,117", "The mesher used the repaired topology."]],
        [1.35 * inch, 0.8 * inch, 0.95 * inch, CONTENT_W - 3.1 * inch],
        align_right=(1, 2),
        note="This is evidence of repair and mesh-engine readiness on one public model. It is not "
             "a benchmark, and staging is not a completed CFD or FEA solve."))

    s.append(H2("7.2", "Catalogue growth under a floor gate"))
    s.append(P(
        "On the OCCT dataset we compared a baseline against a run with non-planar hole filling "
        "and self-intersection healing switched on. The first flags-on run failed the gate, "
        "repairing 318 defects against the baseline's 430. The cause was the face-crossing scan "
        "described in Section 4.4. Run after every operation, it flagged 19 files with new "
        "face-level crossings, which sent them to manual cleanup, and it rolled back sews on two "
        "files (56 defects each) because of a crossing the baseline had never measured. Once the "
        "scan stopped acting as a verdict, the second run matched the baseline outcome for "
        "outcome: 430 repaired defects, 16 promoted files all passing the final check, no "
        "paired-rim file filled, and no cap violated. Because the totals are equal, we make no "
        "claim of improvement. The new heal operation was proposed ten times and declined all "
        "ten, for stated reasons (multi-body inputs, crossings not isolated).", "body0"))

    s.append(H2("7.3", "Region-scoped driver: parity, cost and the no-op control"))
    s.append(P(
        "Both drivers ran the same bytes with the same settings, differing only in "
        + c("CAD_REPAIR_REGION_LOOP") + " (Tables 5 and 6). On NIST the outcome vectors are "
        "identical. The two arms differ in refusal records, 22 against 20, but both refuse the "
        "same 13 distinct rims: the baseline rescans the whole shape every iteration, so after "
        "an identity collision it writes a second record for the same rim. On the generated "
        "campaign, whole-shape re-inspections fell by roughly a factor of ten, and wall-clock "
        "time fell even though the region driver made 802 scoped scans. Across all 40 clean "
        "references, neither driver kept a single operation or changed a single part.",
        "body0"))
    s.extend(table(
        "NIST MBE PMI (33 files): repair parity.",
        [["", "Baseline", "Region driver"],
         ["Outcomes", "repaired 7, refused 15, nothing 4, not attempted 6, no change 1",
          "identical"],
         [c("open_shell") + " defects repaired", "36", "36"],
         ["Routes", "conservative 23, manual 6, mesh as is 4", "identical"],
         ["Distinct rims refused", "13", "13"]],
        [1.8 * inch, 2.9 * inch, CONTENT_W - 4.7 * inch], bold_first_col=False))
    s.extend(table(
        "Generated defect campaign (50 parts) and clean control (40 parts).",
        [["", "Baseline", "Region driver"],
         ["Whole-shape re-inspections (campaign)", "566", "54"],
         ["Worst case on one part", "45", "2"],
         ["Scoped re-inspections", "0", "802"],
         ["Wall-clock (campaign)", "184 s", "154 s"],
         ["Per-part verdict", "—", "41 better, 9 tied, 0 worse"],
         ["Clean control: operations kept / parts mutated", "0 / 0", "0 / 0"],
         ["Clean control: whole-shape scans", "104", "40"]],
        [2.9 * inch, 1.6 * inch, CONTENT_W - 4.5 * inch], align_right=(1, 2)))
    s.append(P(
        "The cost advantage reverses on fragmented geometry, and any published claim should "
        "include both numbers. NIST CTC-02 used its full 300 s budget, against the baseline's "
        "96.7 s. NIST CTC-04, a 637-face part that decomposes into 103 regions, took 43.9 s "
        "against 5.9 s. The saving holds where defects cluster and reverses where they are "
        "scattered. The comparison also uncovered three defects, which were fixed before merge: "
        "the spatial region walk that never reached an error late in the order; planner "
        "refusals that went unreported on the region path; and the paired-rim guard that looked "
        "only within one region, which let a through-hole be sealed. Unit tests had caught none "
        "of the three."))

    s.append(H2("7.4", "Inspector defects poorly predict mesh failure"))
    s.append(P(
        "Every NIST file that the inspector flags as " + c("open_shell") + " meshes in plain "
        "gmsh, including one whose five open shells CRS could not repair. We then assembled 80 "
        "public STEP files that genuinely fail a gmsh volume mesh: 33 from the OCCT 7.8.0 "
        "dataset and 47 out of 400 sampled from ABC chunk 0050. Before the mesh-failure work, "
        "CRS made only two of them mesh, and for 15 the inspector reported nothing at all. The "
        "failures cluster into a few families (edge through face, overlapping facets, open "
        "loop, periodic surface, no volume, timeout), and none of these appeared in the "
        "inspector's vocabulary at the time. This finding redirected the programme towards "
        "measuring meshability directly.", "body0"))

    s.append(H2("7.5", "Calibrating the in-loop probe"))
    s.append(P(
        "In the loop, the probe meshes an in-memory shape. The benchmark, by contrast, scores "
        "the delivered STEP file. We therefore compared the two on all 80 unedited files (Table "
        "7). Our gate was binary agreement on whether a part meshes. Which failure kind gmsh "
        "reports first depends on the order it processes input, and a STEP round trip can change "
        "that order.", "body0"))
    s.extend(table(
        "Probe calibration: in-loop probe compared with an OCP re-export through STEP.",
        [["Measure", "Count"],
         ["Binary agreement, in-loop vs re-export", "76 of 79 (+1 unreadable)"],
         ["Meshes-vs-fails disagreements (gating; limit 8)", "3"],
         ["Both fail, different kind (not gating)", "6"],
         ["Reader effect: gmsh reading the upload disagrees with the re-export", "17 files"],
         ["Rows where a probe reached its 120 s budget", "11"]],
        [4.4 * inch, CONTENT_W - 4.4 * inch], align_right=(1,)))
    s.append(P(
        "The reader effect is a hazard for any benchmark of this kind. Two files mesh as soon as "
        "OCP has read and re-exported them, so a repair that changes nothing would appear to "
        "“fix” them. All our meshing scores use the delivered STEP."))

    s.append(H2("7.6", "Locating mesh failures"))
    s.append(P(
        "In the first run with the probe live, <b>none</b> of the 15 previously invisible files "
        "was located, against a target of 12. The probe was working. The problem was wiring: "
        "triage sends an inspector-clean file to <i>mesh as is</i>, so no session opened and the "
        "probe, which runs inside the session's scan, never ran. That run also differed from its "
        "baseline in engine commit, and its bundle records the confound. Starting a session on "
        "every file (a benchmark switch, " + c("--force-repair") + ") located all 15 of the 15, "
        "and raised the number of files with a located mesh defect from 12 to 74 out of 80. No "
        "file that meshed before stopped meshing (" + c("newly_broken") + " = 0). Five runs "
        "now stop as " + c("unmeshable") + " where they previously ended in a way that "
        "concealed the failure.", "body0"))

    s.append(H2("7.7", "Operations for the mesh-failure families"))
    s.append(P(
        "With " + c("split_closed_faces") + ", " + c("fragment_bodies") + " and "
        + c("rebuild_face") + " enabled and the model author and reviewer off, we measured "
        "the rules baseline on the same 80 files (Table 8).", "body0"))
    s.extend(table(
        "Mesh-failure track (80 files), compared with the location baseline. One pass per arm; "
        "the arms differ in engine commit and in " + c("--force-repair") + ".",
        [["", "Location baseline", "With new operations"],
         ["Meshed before / after", "1 / 3", "2 / 5"],
         ["Newly meshed", "2", "3"],
         ["Newly broken", "0", "0"],
         ["Still failing: edge through face", "25", "27"],
         ["Still failing: overlapping facets", "31", "31"],
         ["Still failing: open loop / periodic", "4 / 3", "4 / 2"],
         ["Timeouts", "10", "7"],
         ["Stopped " + c("unmeshable"), "5", "14"],
         ["In-process run seconds", "4,858", "9,083"]],
        [2.6 * inch, 1.6 * inch, CONTENT_W - 4.2 * inch], align_right=(1, 2)))
    s.append(P(
        "<b>None of the three newly meshed files can be credited to a new operation.</b> The "
        "baseline already meshed two of them without the new operations. On one of those, "
        + c("fragment_bodies") + " was kept, and with a single arm a contribution from it cannot "
        "be ruled out completely. The third file was reached only because forcing a session let "
        "the existing " + c("fix_small_edges") + " run. Per operation, " + c("fragment_bodies")
        + " was kept on 10 files and rolled back 33 times, and " + c("split_closed_faces")
        + " ran once and was rolled back. " + c("rebuild_face") + " never ran."))
    s.append(P(
        "Two follow-up measurements explain why. An overlap-stage census re-probed the 31 "
        "overlapping-facets files and found that all 31 fail in gmsh's <i>3D</i> stage and none "
        "in the 2D stage, so " + c("rebuild_face") + ", which acts only on 2D-stage overlaps, "
        "has nothing to act on in this corpus. A body-pair census of the 23 multi-solid files "
        "with PLC errors tested every pair of bodies whose bounding boxes meet, using a Boolean "
        "common. Eleven files contain at least one overlapping pair and 12 contain only touching "
        "pairs. An operation that assumed bodies merely touch would therefore be wrong on about "
        "half of these files, which is why " + c("fragment_bodies") + " declines on overlap. "
        "Finally, on the two genuine open-loop files the rules author did propose "
        + c("fix_face_boundary") + ", but the targeted faces are already valid closed boundaries. "
        "Every relevant " + c("ShapeFix_Wire") + " pass reports nothing to do. The 1D-loop "
        "failure is something gmsh derives while discretising the edges, and no wire fixer can "
        "see it. We did not add the planned wire passes, because they would have done nothing."))
    s.append(P(
        "The clean control, re-sampled across all 22 families, kept zero operations and changed "
        "zero parts; 34 of its 40 parts meshed both before and after. The six clean parts that fail the "
        "gmsh probe are themselves a warning about how much weight a single probe failure "
        "deserves."))
    s.append(H2("7.8", "The real-failure corpus"))
    s.append(P(
        "The 80-file track above is too small to rank a backlog on. We therefore probed 100,000 "
        "real parts and kept every one that fails a volume mesh: <b>9,956 failures, 9.9% of the "
        "parts probed</b>. Table 12 is that corpus by family. It is the population every later "
        "policy change is scored against, and it settled three questions the 80-file track could "
        "only suggest.", "body0"))
    s.extend(table(
        "The 9,956-file real-failure corpus by family. “Stage” is the meshing stage "
        "gmsh refused in; a part is counted once, under the first failure reported. Figures from "
        "the committed harvest aggregate.",
        [["Family", "Files", "Surface", "Volume", "Note"],
         ["Overlapping facets", "5,871", "<b>0</b>", "5,840",
          "59% of the corpus. No surface-stage population exists."],
         ["Edge through face", "2,376", "0", "2,349",
          "982 single-solid, 1,394 multi-solid."],
         ["Timeout", "587", "386", "86", "No answer rather than a verdict."],
         ["Periodic surface", "394", "390", "1", "The seam-splitting operation's family."],
         ["Other", "288", "9", "127", "152 of unknown stage."],
         ["Open loop", "227", "223", "0", "94 single-solid, 133 multi-solid."],
         ["No volume", "168", "0", "167",
          "162 single-solid, median 21.5 faces: a tiny-shell family."],
         ["Crash", "45", "33", "12", "&mdash;"]],
        [1.3 * inch, 0.55 * inch, 0.6 * inch, 0.55 * inch, CONTENT_W - 3.0 * inch],
        align_right=(1, 2, 3)))
    s.append(H3("Face re-trimming has no reach here"))
    s.append(P(
        "Of the 5,871 overlapping-facet files, <b>zero</b> fail in the surface stage. "
        + c("rebuild_face") + " acts only on surface-stage overlaps, so on this corpus it has "
        "nothing to act on. The 80-file census above suggested this on 31 files; the harvest "
        "settles it on 5,871. The operation stays in the catalogue for directed use and is not a "
        "line of further investment.", "body0"))
    s.append(H3("The dominant family is not a CAD-surgery problem"))
    s.append(P(
        "5,840 volume-stage overlaps are, for the most part, coincident or self-touching "
        "boundaries. The honest answer for them is frequently a tolerant mesher rather than an "
        "edit to the customer's geometry, which is what tier 2b exists to decide, and why its "
        "answer is called a <i>routing</i> answer rather than a verdict.", "body0"))
    s.append(H3("Two families are small and specific"))
    s.append(P(
        "227 open loops, all surface-stage, are the only population the bounded vertex-movement "
        "operation targets, and how many carry genuine gaps rather than tolerance-phantom loops "
        "is not yet measured. The 168 no-volume files - 162 of them a single solid with a median "
        "of 21.5 faces - are a tiny-shell family the harvest found, and whether the existing "
        "sewing and filling operations already repair them is a measurement nobody has taken.",
        "body0"))
    s.append(H3("What the deterministic ceiling is"))
    s.append(P(
        "The 11-operation catalogue measured an <b>8-12%</b> ceiling on the original 80-file "
        "track. The programme's target is meshes-after at or above four times that ceiling on "
        "the 9,956-file track, with newly-broken held at zero and the clean control untouched. "
        "<b>That target is not a result.</b> The evidence passes that would measure it, a "
        "tournament evidence run and the held-out measurement pass, have not been run; until "
        "they are, this paper reports no agentic gain in either direction.", "body0"))
    return s


def discussion():
    s = [H1("8", "Discussion")]
    s.append(H3("Measurement-gated editing is safe, but safety is cheap where little is tried"))
    s.append(P(
        "Across every controlled run in this paper, no clean part was changed, no cap was "
        "violated, and no file that meshed before stopped meshing. That is what the "
        "transactional design is meant to deliver. Safety is easy to achieve by attempting "
        "nothing, though, and the honest headline from the mesh-failure track is that the main "
        "families are largely out of reach of deterministic operations on real files. 3D-stage "
        "overlaps, interpenetrating bodies and open loops that gmsh derives during "
        "discretisation do not yield to local kernel fixes. In our view this is where model "
        "judgement has a legitimate role. Whether two bodies are meant to share an interface, or "
        "whether a fault lies in the CAD or in the mesher, is a semantic question, and the "
        "architecture of Section 4.8 lets a model answer it without letting the model grade "
        "itself.", "body0"))
    s.append(H3("Locality is the unifying idea"))
    s.append(P(
        "Regions made repair cheaper, but their larger value is that they line up four things "
        "that had been separate: the unit a planner reasons about, the unit that is verified, "
        "the unit that is reverted and the unit that is paid for. The cost reversal on scattered "
        "defects marks the limit of the idea. When defects spread across a hundred regions, "
        "per-region attempts multiply faster than the savings from avoided global scans."))
    s.append(H3("The agentic loop is a bet this paper does not settle"))
    s.append(P(
        "Section 5 spends the guardrails that Sections 4 and 7 established. The trade is "
        "deliberate and it is a trade: less determinism in exchange for reach, with safety "
        "carried by verification rather than by a small cap on iteration. Whether the exchange "
        "pays is an empirical question and the measurement has not been taken. What we can say "
        "is that the ways it could fail quietly have been closed one at a time, and that most of "
        "them were found by deliberately breaking the code rather than by reading it. A timeout "
        "recorded as a refusal, a no-answer ranked as a refusal, an infinitely negative "
        "deviation sailing through an auto-accept, a not-a-number reading as fully accounted, "
        "reviewer prose reaching the author through a field name, a byte comparison that could "
        "not tell a clone from the part it copied: each of those was a passing test before it "
        "was a caught mutation. We report them because a reader's confidence in the eventual "
        "numbers should rest on how the harness behaves when it is wrong, not on the numbers.",
        "body0"))
    s.append(H3("The instruments need scrutiny too"))
    s.append(P(
        "Several of our results come down to properties of the measuring tools rather than of "
        "the geometry. These include the reader effect, the order in which gmsh reports its "
        "first failure, probe failures on clean parts, a face scan that changed verdicts simply "
        "by observing, and a " + c("resolved") + " count that rewards inaction. Each would have "
        "produced a convincing false result had the protocol not required a control and an "
        "explicit statement of what the evidence does not show. We think this discipline "
        "matters more to the credibility of automated CAD repair than any single operation."))
    return s


def limitations():
    s = [H1("9", "Limitations and Future Work")]
    s += bullets([
        "<b>Nothing model-directed is yet evaluated.</b> All results above are from the rules "
        "baseline, and that remains true of the whole agentic loop of Section 5. The one live "
        "product run of the model author produced 34 proposals of which 0 survived execution; "
        "the safety machinery held, and that is the only model-directed result we have. "
        "Evaluating the agentic path needs two corpus passes that have not been run, on a "
        "deployment with a model router. Byte-identical replay is retired for agent-driven runs "
        "- a tool conversation is not reproducible turn by turn - and replay now means "
        "re-applying the recorded packages.",
        "<b>The 4x goal is a target, not a measurement.</b> The programme aims at meshes-after "
        "at or above four times the deterministic 8-12% ceiling on the 9,956-file track, with "
        "newly-broken zero and the clean control untouched. No pass has measured any part of it. "
        "It is stated here so the eventual measurement can be held against it.",
        "<b>The agentic loop is not joined end to end.</b> The tournament computes a winner and "
        "does not yet commit it on the real session, so its ranking is exercised by its tests "
        "and by nothing else. Tiered review is inert: no caller declares a per-part deviation "
        "threshold, so every package still takes the reviewer tier. Both fail safe - the first "
        "offers nothing, the second spends a model call it could have saved - and neither can be "
        "described as working in a run.",
        "<b>Native tool calling has not met a provider.</b> The investigator's tool transport is "
        "exercised against a substituted router. Whether the configured provider honours tool "
        "calls on short non-streaming replies is untested; the JSON fallback exists for the case "
        "where it does not.",
        "<b>Tier 2b cannot answer on the worker image.</b> The tolerant-mesh pass needs a binary "
        "the pipeline worker image does not carry, so there every tier-2b question reads "
        "“unavailable”. That is the designed honest answer, and it means the routing "
        "answer for the corpus's largest family is unavailable exactly where the parts are.",
        "<b>Production runs with the agentic loop off.</b> The posture is committed "
        "configuration: shared development states the agentic flag live, production states it "
        "empty, permanently, under a test that executes the configuration step rather than "
        "matching its text. The loop is therefore unmeasured in production by construction, and "
        "the vertex-moving operations stay reviewer-gated under every posture value.",
        "<b>gmsh is not the product mesher.</b> The probe gives a quick verdict inside the loop. "
        "The final verdict belongs to the product's mesh engine, run on the winning candidate. "
        "The planned production route reverses the direction: when the product mesher fails at "
        "named faces, it hands that failure back to repair as a request scoped to those faces, "
        "within a pass budget, so only jobs that fail pay for repair.",
        "<b>Single platform, single pass.</b> All runs were on darwin-arm64 with one gmsh "
        "version, and run-to-run nondeterminism has not been measured. Some A/B comparisons "
        "differ in engine commit as well as in the variable under test, and their bundles say "
        "so.",
        "<b>The generated campaign cannot demonstrate repair.</b> Its injected defects (thin "
        "walls, debris, pin holes and the like) lie outside the current catalogue. It measures "
        "cost, not capability.",
        "<b>Open policy questions.</b> These include what licence a model-authored package "
        "receives on reserved judgement codes, whether a reclassification may persist across "
        "uploads (a judgement cache keyed by fingerprint would be a cross-tenant channel), and "
        "whether “clean” should eventually make warnings block delivery.",
    ])
    return s


def conclusion():
    s = [H1("10", "Conclusion")]
    s.append(P(
        "CRS treats CAD repair as a sequence of measured, reversible transactions rather than as "
        "a cleanup filter. That choice has held up under evaluation. Region scoping cut "
        "re-inspection cost by about a factor of ten where defects cluster, with no loss of "
        "repair parity and no change to clean parts. Measuring meshability directly showed that "
        "the old defect vocabulary barely predicted what actually blocks a mesh. The same "
        "measurement showed, without ambiguity, that the deterministic catalogue cannot reach "
        "most real mesh failures. A corpus of 9,956 real failures harvested from 100,000 probed "
        "parts then settled which families are worth spending on and which are not.", "body0"))
    s.append(P(
        "The next step is built and unmeasured. Model judgement now sits inside the transaction "
        "boundary as a bounded investigation rather than a single proposal: the model looks, "
        "measures, probes and edits on a copy; a tournament of candidates is rehearsed on clones "
        "and the best is nominated; the kernel measures; a separate reviewer, sealed from the "
        "author and answering in measurements, decides. Section 5 describes it and Table 5 "
        "states what each piece still lacks. We claim nothing for it yet. Every claim will be "
        "checked against the same sealed evidence used here, scored in the corpus gym before it "
        "ships, and reported with its negatives."))
    return s


def references():
    s = [P("References", "h1")]
    refs = [
        "A. Madaan et al. Self-Refine: Iterative refinement with self-feedback. "
        "<i>arXiv:2303.17651</i>, 2023.",
        "J. Huang et al. Large language models cannot self-correct reasoning yet. In <i>Proc. "
        "ICLR</i>, 2024. arXiv:2310.01798.",
        "When do agent loops mistake stagnation for progress? <i>arXiv:2607.25152</i>, 2026.",
        "Y. Hu, T. Schneider, B. Wang, D. Zorin and D. Panozzo. Fast tetrahedral meshing in the "
        "wild. <i>ACM Transactions on Graphics</i> 39(4), 2020. arXiv:1908.03581.",
        "J. R. Shewchuk. What is a good linear finite element? Interpolation, conditioning, "
        "anisotropy, and quality measures. In <i>Proc. 11th International Meshing "
        "Roundtable</i>, 2002.",
        "P. Knupp. Remarks on mesh quality. In <i>45th AIAA Aerospace Sciences Meeting</i>, "
        "SAND2007-8128C, 2007.",
        "Y. Chen et al. MetaOpenFOAM: An LLM-based multi-agent framework for CFD. "
        "<i>arXiv:2407.21320</i>, 2024.",
        "L. Yue et al. Foam-Agent 2.0. <i>arXiv:2509.18178</i>, 2025.",
        "S. Koch et al. ABC: A big CAD model dataset for geometric deep learning. In <i>Proc. "
        "CVPR</i>, 2019.",
        "National Institute of Standards and Technology. MBE PMI validation and conformance "
        "testing: CTC/FTC STEP files, 2024 release.",
        "Open Cascade SAS. Open CASCADE Technology 7.8.0 and test dataset. "
        "github.com/Open-Cascade-SAS/OCCT.",
        "C. Geuzaine and J.-F. Remacle. Gmsh: A 3-D finite element mesh generator with built-in "
        "pre- and post-processing facilities. <i>International Journal for Numerical Methods in "
        "Engineering</i> 79(11):1309–1331, 2009.",
        "Ludemann Engineering. Formula 1000 race car design files. "
        "github.com/LudemannEngineering/Formula-1000-Race-Car. Used under its licence; the "
        "repaired file is a Hexera-generated modification, and Ludemann Engineering does not "
        "endorse Hexera or these results.",
    ]
    for i, r in enumerate(refs, 1):
        s.append(Paragraph(f"[{i}]&nbsp;&nbsp;{r}", S["ref"]))
    return s


def appendix():
    s = [CondPageBreak(3.5 * inch), P("Appendix A&nbsp;&nbsp;&nbsp;Configuration", "h1")]
    s.append(P(
        "Every setting below fails closed: an unrecognised value is read as the default. The "
        "evaluation runs in Section 7 raised the run budgets (" + c("MAX_OPS") + " = 200, "
        + c("STALL_LIMIT") + " = 50, " + c("TIMEOUT_S") + " = 300, " + c("REGION_ATTEMPTS")
        + " = 6), and each bundle records its exact settings.", "body0"))
    s.extend(table(
        "Principal " + c("CAD_REPAIR_*") + " settings and their defaults.",
        [["Setting", "Default", "Role"],
         [c("ENABLED"), "false", "Master switch for repair."],
         [c("MAX_DEVIATION_RATIO"), "0.001", "Surface movement limit as a fraction of the "
          "diagonal."],
         [c("MAX_TOLERANCE_MM"), "0.1", "Largest tolerance any sub-shape may carry."],
         [c("MAX_AREA_CHANGE_RATIO"), "0.02", "Area residual beyond the declaration."],
         [c("MAX_VOLUME_CHANGE_RATIO"), "0.02", "Volume residual (when solid count does not "
          "rise)."],
         [c("MAX_HOLE_SPAN_RATIO"), "0.75", "Widest opening that may be patched."],
         [c("MAX_OPS") + " / " + c("STALL_LIMIT") + " / " + c("TIMEOUT_S"), "6 / 2 / 120",
          "Run budgets."],
         [c("REGION_LOOP"), "off", "Region-scoped driver (off, live)."],
         [c("REGION_ATTEMPTS"), "3", "Authored packages per region."],
         [c("MESH_PROBE"), "off", "gmsh probe as inspector and predicate."],
         [c("FILL_PLANAR_HOLES") + ", " + c("FILL_NONPLANAR_HOLES"), "false", "Hole filling."],
         [c("FIX_SMALL_FACES") + ", " + c("HEAL_SELF_INTERSECTIONS"), "false",
          "Sliver removal; crossing heal."],
         [c("SPLIT_CLOSED_FACES") + ", " + c("FRAGMENT_BODIES"), "false", "Mesh-family "
          "operations."],
         [c("REBUILD_FACE"), "false", "Face re-trim; needs the reviewer."],
         [c("LLM_AUTHOR"), "off", "Model author (off, shadow, live)."],
         [c("PACKAGE_REVIEW"), "off", "Semantic reviewer (off, live)."],
         ["<i>The agentic loop of Section 5. Every row below is new, and every row is off.</i>",
          "", ""],
         [c("TOLERANT_PROBE"), "off", "Tier 2b, the tolerant-mesh routing pass (off, live)."],
         [c("TOLERANT_PROBE_TIMEOUT_S"), "30",
          "Must stay below " + c("TIMEOUT_S") + ", or authoring has no time."],
         [c("INVESTIGATOR"), "off", "The bounded tool loop (off, shadow, live)."],
         [c("INVESTIGATOR_CALL_TIMEOUT_S"), "20", "One model call's wall."],
         [c("INVESTIGATOR_MODEL_CALLS_MAX"), "48",
          "Run-wide ceiling above the per-region budgets."],
         [c("INVESTIGATOR_REGION_SHARE"), "0.5",
          "Largest share of the clock one region may take."],
         [c("INVESTIGATOR_IMAGES"), "off",
          "Rendered views on " + c("look") + "; the channel is unwired."],
         [c("TOURNAMENT"), "off", "Tournament repair (off, live)."],
         [c("TOURNAMENT_K"), "1", "Candidates per region, clamped to [1, 6]."],
         [c("AUTO_ACCEPT"), "off",
          "The auto-accept review tier; inert without a threshold."],
         [c("REVIEW_OBJECTION"), "off", "Return the measured objection to the author."]],
        [2.75 * inch, 0.85 * inch, CONTENT_W - 3.6 * inch]))
    s.append(P("Appendix B&nbsp;&nbsp;&nbsp;Evidence and Reproduction", "h1"))
    s.append(P(
        "Each result in Section 7 has a committed evidence directory under "
        + c("devtools/validation/evidence/") + ", containing its bundle manifest, settings, "
        "summary, logs and a README that states what the evidence does not show. The "
        "mesh-failure track's manifest (" + c("devtools/validation/corpora/mesh-failure-real.json")
        + ") lists every input by name, SHA-256 and source URL, and the probe-calibration README "
        "gives the download and verification steps. Corpus runs use "
        + c("devtools/validation/repair_corpus.py") + " with " + c("--bundle") + ". Model-"
        "directed runs replay byte-identically from a recorded-reply cache.", "body_left"))
    return s


def story():
    s = []
    s += front_matter()
    s += introduction()
    s += background()
    s += formulation()
    s += architecture()
    s += agentic()
    s += methodology()
    s += results()
    s += discussion()
    s += limitations()
    s += conclusion()
    s += references()
    s += appendix()
    return s


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    from reportlab.pdfgen.canvas import Canvas

    def serif_canvas(*args, **kwargs):
        # The canvas otherwise opens every page in Helvetica, which embeds a sans font reference.
        kwargs.setdefault("initialFontName", SERIF)
        kwargs.setdefault("initialFontSize", BODY_SIZE)
        return Canvas(*args, **kwargs)

    Paper(str(OUT)).build(story(), canvasmaker=serif_canvas)
    print(OUT)
