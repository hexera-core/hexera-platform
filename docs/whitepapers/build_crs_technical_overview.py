from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/whitepapers/hexera-crs-technical-overview.pdf"
LOGO = ROOT / "apps/console/public/static/assets/logo.png"

PAGE_W, PAGE_H = letter
MARGIN_X = 0.68 * inch
MARGIN_TOP = 0.70 * inch
MARGIN_BOTTOM = 0.58 * inch
CONTENT_W = PAGE_W - 2 * MARGIN_X

INK = colors.HexColor("#111315")
MUTED = colors.HexColor("#5D646B")
LIGHT = colors.HexColor("#EEF1F3")
LINE = colors.HexColor("#D4D9DD")
WHITE = colors.white
GREEN = colors.HexColor("#138A57")
GREEN_BG = colors.HexColor("#EAF6F0")
AMBER = colors.HexColor("#A65A00")
AMBER_BG = colors.HexColor("#FFF4E5")
RED = colors.HexColor("#B42318")
RED_BG = colors.HexColor("#FDECEA")
BLUE = colors.HexColor("#1769AA")
BLUE_BG = colors.HexColor("#EAF2F8")


def register_fonts():
    candidates = {
        "Inter": "/System/Library/Fonts/Supplemental/Arial.ttf",
        "Inter-Bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "Inter-Italic": "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    }
    for name, path in candidates.items():
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont(name, path))
    return "Inter" if "Inter" in pdfmetrics.getRegisteredFontNames() else "Helvetica"


FONT = register_fonts()
BOLD = "Inter-Bold" if "Inter-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
ITALIC = "Inter-Italic" if "Inter-Italic" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Oblique"


styles = getSampleStyleSheet()
styles.add(ParagraphStyle(
    name="BodyX", fontName=FONT, fontSize=9.4, leading=13.3, textColor=INK,
    spaceAfter=7,
))
styles.add(ParagraphStyle(
    name="SmallX", fontName=FONT, fontSize=7.6, leading=10.3, textColor=MUTED,
))
styles.add(ParagraphStyle(
    name="TinyX", fontName=FONT, fontSize=6.7, leading=8.7, textColor=MUTED,
))
styles.add(ParagraphStyle(
    name="H1X", fontName=BOLD, fontSize=21, leading=24, textColor=INK,
    spaceBefore=4, spaceAfter=11,
))
styles.add(ParagraphStyle(
    name="H2X", fontName=BOLD, fontSize=13.2, leading=16, textColor=INK,
    spaceBefore=10, spaceAfter=6,
))
styles.add(ParagraphStyle(
    name="H3X", fontName=BOLD, fontSize=9.2, leading=11.5, textColor=INK,
    spaceBefore=3, spaceAfter=3,
))
styles.add(ParagraphStyle(
    name="KickerX", fontName=BOLD, fontSize=7.4, leading=9, textColor=BLUE,
    spaceAfter=8,
))
styles.add(ParagraphStyle(
    name="CoverTitle", fontName=BOLD, fontSize=29, leading=32, textColor=WHITE,
    spaceAfter=12,
))
styles.add(ParagraphStyle(
    name="CoverSub", fontName=FONT, fontSize=13, leading=18, textColor=colors.HexColor("#D9DEE2"),
    spaceAfter=18,
))
styles.add(ParagraphStyle(
    name="CoverMeta", fontName=FONT, fontSize=8.3, leading=12, textColor=colors.HexColor("#B8C0C6"),
))
styles.add(ParagraphStyle(
    name="Callout", fontName=BOLD, fontSize=10.4, leading=14.2, textColor=INK,
    spaceAfter=0,
))
styles.add(ParagraphStyle(
    name="BulletX", fontName=FONT, fontSize=9.0, leading=12.2, textColor=INK,
    leftIndent=14, firstLineIndent=-8, bulletIndent=0, spaceAfter=4,
))
styles.add(ParagraphStyle(
    name="TableHead", fontName=BOLD, fontSize=7.4, leading=9.4, textColor=WHITE,
))
styles.add(ParagraphStyle(
    name="TableBody", fontName=FONT, fontSize=7.5, leading=9.7, textColor=INK,
))
styles.add(ParagraphStyle(
    name="TableStrong", fontName=BOLD, fontSize=7.5, leading=9.7, textColor=INK,
))
styles.add(ParagraphStyle(
    name="Metric", fontName=BOLD, fontSize=16, leading=18, textColor=INK, alignment=TA_CENTER,
))
styles.add(ParagraphStyle(
    name="MetricLabel", fontName=FONT, fontSize=7.2, leading=9, textColor=MUTED, alignment=TA_CENTER,
))


def P(text, style="BodyX"):
    return Paragraph(text, styles[style])


def bullet(text):
    return Paragraph(f"- {text}", styles["BulletX"])


def header_footer(canvas, doc):
    canvas.saveState()
    page = canvas.getPageNumber()
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.45)
        canvas.line(MARGIN_X, PAGE_H - 0.42 * inch, PAGE_W - MARGIN_X, PAGE_H - 0.42 * inch)
        canvas.setFont(BOLD, 7)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN_X, PAGE_H - 0.30 * inch, "HEXERA | CAD REPAIR SYSTEM")
        canvas.setFont(FONT, 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 0.30 * inch, "TECHNICAL OVERVIEW")
    canvas.setFont(FONT, 6.8)
    canvas.setFillColor(MUTED if page > 1 else colors.HexColor("#AAB2B8"))
    canvas.drawString(MARGIN_X, 0.28 * inch, "Client briefing | October 2026")
    canvas.drawRightString(PAGE_W - MARGIN_X, 0.28 * inch, str(page))
    canvas.restoreState()


def page_background(canvas, doc):
    page = canvas.getPageNumber()
    if page == 1:
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#090B0D"))
        canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#1B2024"))
        canvas.rect(0, 0, PAGE_W, 1.45 * inch, fill=1, stroke=0)
        canvas.restoreState()
    header_footer(canvas, doc)


def callout(title, body, bg=BLUE_BG, edge=BLUE):
    data = [[P(title, "H3X"), P(body, "SmallX")]]
    t = Table(data, colWidths=[1.62 * inch, CONTENT_W - 1.62 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 4, edge),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def metric_strip(items):
    cells = []
    for value, label in items:
        cells.append([P(value, "Metric"), P(label, "MetricLabel")])
    table = Table([cells], colWidths=[CONTENT_W / len(items)] * len(items))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def capability_table():
    rows = [
        [P("Defect class", "TableHead"), P("CRS response today", "TableHead"), P("Boundary", "TableHead")],
        [P("Invalid B-rep and orientation failures", "TableStrong"), P("Localized whole-shape repair; result is re-inspected.", "TableBody"), P("Kept only when validity improves without breaching shape caps.", "TableBody")],
        [P("Wire gaps and face-boundary inconsistency", "TableStrong"), P("Repairs wire ordering, p-curves and face boundaries.", "TableBody"), P("Target must be identified; no blind topology rewrite.", "TableBody")],
        [P("Tiny or degenerate edges", "TableStrong"), P("Attempts local edge and boundary healing.", "TableBody"), P("Warnings may remain when removal would alter intended detail.", "TableBody")],
        [P("Sliver faces", "TableStrong"), P("Deployment-controlled sweep of measured sub-tolerance faces.", "TableBody"), P("Every removed face is counted and bounded; otherwise rollback.", "TableBody")],
        [P("Self-intersecting wires/faces", "TableStrong"), P("Targeted healing on kernel-identified geometry.", "TableBody"), P("Deployment-controlled; must reduce error severity on re-scan.", "TableBody")],
        [P("Open shells and missing faces", "TableStrong"), P("Sews coincident boundaries; may patch an isolated bounded opening.", "TableBody"), P("Hole filling is guarded and refuses ambiguous through-passages.", "TableBody")],
        [P("Non-manifold, intent-sensitive or staging defects", "TableStrong"), P("Detects and routes with evidence.", "TableBody"), P("Not automatically rewritten when engineering intent is uncertain.", "TableBody")],
    ]
    t = Table(rows, colWidths=[1.73 * inch, 2.63 * inch, CONTENT_W - 4.36 * inch], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("BOX", (0, 0), (-1, -1), 0.55, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#F8F9FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def workflow_table():
    steps = [
        ("01", "Inspect", "Read B-rep and localize defects"),
        ("02", "Plan", "Choose the narrowest eligible repair"),
        ("03", "Execute", "Run the CAD kernel on a snapshot"),
        ("04", "Measure", "Re-scan geometry and compare caps"),
        ("05", "Promote", "Stage repaired CAD for the mesh engine"),
    ]
    row = []
    for number, title, body in steps:
        row.append([P(number, "KickerX"), P(title, "H3X"), P(body, "TinyX")])
    t = Table([row], colWidths=[CONTENT_W / 5] * 5)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.45, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def evidence_table():
    rows = [
        [P("Measure", "TableHead"), P("Before CRS", "TableHead"), P("After CRS", "TableHead"), P("Interpretation", "TableHead")],
        [P("OpenCASCADE validity", "TableStrong"), P("Invalid", "TableBody"), P("Valid", "TableBody"), P("Error-level topology was resolved.", "TableBody")],
        [P("Solids", "TableStrong"), P("8", "TableBody"), P("8", "TableBody"), P("No solid body was lost.", "TableBody")],
        [P("Faces", "TableStrong"), P("421", "TableBody"), P("419", "TableBody"), P("Bounded cleanup changed topology without changing extents.", "TableBody")],
        [P("Reported findings", "TableStrong"), P("171 total", "TableBody"), P("162 warnings", "TableBody"), P("CRS resolved the blocking errors; it did not claim every warning disappeared.", "TableBody")],
        [P("cfMesh CAD staging", "TableStrong"), P("16.87 s", "TableBody"), P("7.99 s", "TableBody"), P("Both staged; repaired input completed in about half the time in this run.", "TableBody")],
        [P("Surface triangles", "TableStrong"), P("147,545", "TableBody"), P("153,117", "TableBody"), P("The mesher consumed the repaired topology and produced a different surface discretization.", "TableBody")],
    ]
    t = Table(rows, colWidths=[1.55 * inch, 1.08 * inch, 1.08 * inch, CONTENT_W - 3.71 * inch], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("BOX", (0, 0), (-1, -1), 0.55, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#F8F9FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


class CRSBrief(BaseDocTemplate):
    def __init__(self, filename):
        super().__init__(
            filename,
            pagesize=letter,
            rightMargin=MARGIN_X,
            leftMargin=MARGIN_X,
            topMargin=MARGIN_TOP,
            bottomMargin=MARGIN_BOTTOM,
            title="Hexera CAD Repair System - Technical Overview",
            author="Hexera",
            subject="Client-facing overview of CRS capabilities, safety controls, and Formula 1000 evidence",
        )
        frame = Frame(
            MARGIN_X,
            MARGIN_BOTTOM,
            CONTENT_W,
            PAGE_H - MARGIN_TOP - MARGIN_BOTTOM,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(PageTemplate(id="main", frames=[frame], onPage=page_background))


def story():
    s = []

    # Cover
    s.append(Spacer(1, 0.55 * inch))
    if LOGO.exists():
        logo = Image(str(LOGO), 1.27 * inch, 1.27 * inch)
        logo.hAlign = "LEFT"
        s.append(logo)
    s.append(Spacer(1, 0.24 * inch))
    s.append(P("TECHNICAL OVERVIEW", "KickerX"))
    s.append(P("CAD Repair System", "CoverTitle"))
    s.append(P("Evidence-bounded geometry repair before CFD and FEA meshing", "CoverSub"))
    s.append(Spacer(1, 0.24 * inch))
    s.append(P(
        "CRS locates defects in CAD topology, attempts narrow kernel-level repairs, and keeps a change only when independent re-inspection shows measurable progress inside conservative geometry limits.",
        "CoverSub",
    ))
    s.append(Spacer(1, 1.07 * inch))
    s.append(P("Prepared by Hexera  |  October 2026  |  Client briefing", "CoverMeta"))
    s.append(PageBreak())

    # Executive summary
    s.append(P("SYSTEM OVERVIEW", "KickerX"))
    s.append(P("Repair is a measured transaction, not a cleanup filter", "H1X"))
    s.append(P(
        "Simulation workflows often fail before solver setup because imported CAD is topologically invalid, internally inconsistent, or burdened by geometry much smaller than the intended mesh scale. CRS sits between CAD intake and mesh generation. It diagnoses the B-rep, applies only an eligible repair, then judges the written result rather than trusting the repair operation's own success flag.",
    ))
    s.append(Spacer(1, 5))
    s.append(workflow_table())
    s.append(Spacer(1, 13))
    s.append(callout(
        "Core guarantee",
        "The customer upload remains immutable. If repair is unsupported, unsafe, inconclusive, over budget, or rejected by engine staging, the repaired candidate is not promoted.",
        GREEN_BG,
        GREEN,
    ))
    s.append(P("What the system is designed to improve", "H2X"))
    s.extend([
        bullet("B-rep validity and consistency before surface or volume meshing."),
        bullet("Meshability of assemblies that contain local topology defects, sliver geometry, or broken boundaries."),
        bullet("Repeatability: defect identity, attempted operation, measurements, and verdict are recorded for each run."),
        bullet("Time to a usable mesh by avoiding manual CAD cleanup when the repair is mechanically defensible."),
    ])
    s.append(P("What it does not claim", "H2X"))
    s.append(P(
        "CRS does not infer engineering intent, certify boundary conditions, validate material models, or prove a CFD/FEA solution is physically correct. It prepares and verifies geometry for the next stage. Solver validation remains an engineering responsibility.",
    ))
    s.append(PageBreak())

    # Capabilities
    s.append(P("CURRENT CAPABILITIES", "KickerX"))
    s.append(P("Defects CRS detects and can address", "H1X"))
    s.append(P(
        "The table distinguishes a repair operation from a guaranteed outcome. A defect is repaired only when it is localized, an operation claims it, the result improves on re-scan, and all safety checks pass.",
    ))
    s.append(Spacer(1, 5))
    s.append(capability_table())
    s.append(Spacer(1, 12))
    s.append(callout(
        "Conservative by design",
        "CRS may leave non-blocking warnings in place. Removing every small feature would be defeaturing, which can change the physics of a model. The system prioritizes simulation readiness without pretending that all fine geometry is disposable.",
        AMBER_BG,
        AMBER,
    ))
    s.append(P("Typical dispositions", "H2X"))
    dispositions = [
        [P("REPAIR", "TableStrong"), P("Localized defect with a bounded operation and measurable progress.", "TableBody")],
        [P("MESH AS IS", "TableStrong"), P("No blocking defect, or only accepted warnings for the chosen workflow.", "TableBody")],
        [P("REVIEW", "TableStrong"), P("The geometry is understood but the right change depends on engineering intent.", "TableBody")],
        [P("REFUSE", "TableStrong"), P("The candidate damages topology, exceeds a cap, or fails independent staging.", "TableBody")],
    ]
    dt = Table(dispositions, colWidths=[1.2 * inch, CONTENT_W - 1.2 * inch])
    dt.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    s.append(dt)
    s.append(PageBreak())

    # Safety architecture
    s.append(P("SAFETY ARCHITECTURE", "KickerX"))
    s.append(P("Every candidate must earn promotion", "H1X"))
    s.append(P(
        "A kernel reporting success is not sufficient. CRS evaluates each candidate against both local progress and cumulative geometry limits measured from the original upload.",
    ))
    checks = [
        ("Target integrity", "The defect must still exist at the identified entity. Stale or untraceable targets are refused."),
        ("Topology sanity", "A candidate may not lose solids, erase undeclared faces, leave no meshable faces, or turn a valid body invalid."),
        ("Geometry bounds", "Tolerance growth, overall size, surface area, and volume are compared with the original and the operation's declared change."),
        ("Independent progress", "The candidate is re-inspected. The target defect must disappear or materially improve without introducing a new warning-or-worse defect."),
        ("Engine staging", "The repaired file must stage for the selected mesh engine before it can replace the original in the run."),
    ]
    rows = []
    for i, (title, body) in enumerate(checks, 1):
        rows.append([P(f"{i:02d}", "KickerX"), P(title, "TableStrong"), P(body, "TableBody")])
    t = Table(rows, colWidths=[0.5 * inch, 1.55 * inch, CONTENT_W - 2.05 * inch])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.55, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, colors.HexColor("#F8F9FA")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    s.append(Spacer(1, 5))
    s.append(t)
    s.append(P("Deterministic operating envelope", "H2X"))
    s.extend([
        bullet("Repair rules execute in a fixed priority order from narrow local operations to broader fallback operations."),
        bullet("Attempt count, elapsed time, and consecutive non-progress outcomes are bounded."),
        bullet("Every failed or rolled-back operation is retained as evidence and is not silently retried."),
        bullet("Optional model guidance can propose a repair, but the CAD kernel and the same deterministic checks still decide whether it is kept."),
    ])
    s.append(callout(
        "Rollback behavior",
        "Operations work from snapshots. A declined, failed, or rejected attempt restores the prior accepted shape. No rejected candidate becomes customer output.",
        RED_BG,
        RED,
    ))
    s.append(PageBreak())

    # Evidence
    s.append(P("FORMULA 1000 EVIDENCE", "KickerX"))
    s.append(P("Full vehicle body: blocking errors removed, solids preserved", "H1X"))
    s.append(P(
        "Hexera evaluated an open Formula 1000 race-car body distributed by Ludemann Engineering. The source file was used as received for the before case. CRS produced a separate repaired STEP file for the after case.",
    ))
    s.append(metric_strip([
        ("171", "findings before"),
        ("8 -> 8", "solids preserved"),
        ("INVALID -> VALID", "kernel verdict"),
        ("16.87 -> 7.99 s", "cfMesh staging"),
    ]))
    s.append(Spacer(1, 12))
    s.append(evidence_table())
    s.append(P("Defects observed before repair", "H2X"))
    s.append(P(
        "The initial inspection reported two invalid B-rep findings, two self-intersecting-wire findings, 132 sliver-face findings, and 35 tiny-edge findings. CRS resolved the blocking invalidity and removed four measured sliver faces during the accepted sequence. It left 162 warning-level findings rather than over-defeaturing the body.",
    ))
    s.append(callout(
        "How to read this result",
        "This is evidence of CAD repair and mesh-engine readiness on one public vehicle model. It is not a benchmark across all CAD, and the surface staging result is not a completed CFD or FEA solve.",
        AMBER_BG,
        AMBER,
    ))
    s.append(Spacer(1, 11))
    s.append(P(
        "Model attribution: Formula 1000 Race Car Design Files by Ludemann Engineering. Source and license: <link href='https://github.com/LudemannEngineering/Formula-1000-Race-Car' color='#1769AA'>github.com/LudemannEngineering/Formula-1000-Race-Car</link>. The repaired file is a Hexera-generated modification used for evaluation. Ludemann Engineering does not endorse Hexera or this result. Independent suitability verification is required by the source license and by normal simulation practice.",
        "TinyX",
    ))
    s.append(PageBreak())

    # Deployment and next steps
    s.append(P("DEPLOYMENT FIT", "KickerX"))
    s.append(P("Where CRS fits in a simulation workflow", "H1X"))
    s.append(P(
        "CRS is intended as a pre-meshing quality gate for CAD-driven CFD and FEA. It is most valuable when teams receive geometry from multiple CAD systems, cannot standardize authoring practices, or spend repeated engineering time converting almost-usable files into meshable bodies.",
    ))
    s.append(callout(
        "Companion part result",
        "On the Formula front suspension rocker, independent inspection measured 12 warnings before and 8 after repair. CRS reduced sliver faces from four to one, preserved the single solid, and kept the B-rep valid. Remaining warnings were reported rather than hidden.",
        BLUE_BG,
        BLUE,
    ))
    s.append(Spacer(1, 5))
    two_col = Table([
        [P("Best-fit use cases", "H2X"), P("Evidence delivered with a run", "H2X")],
        [
            [
                bullet("External-aerodynamics and conjugate-heat-transfer preparation."),
                bullet("Mechanical assemblies with import or translation damage."),
                bullet("Batch simulation pipelines where manual cleanup blocks throughput."),
                bullet("Engineering services that need an auditable before/after record."),
            ],
            [
                bullet("Original and promoted geometry identity."),
                bullet("Located defects with severity and geometric context."),
                bullet("Each attempted operation and keep/rollback verdict."),
                bullet("Post-repair measurements and remaining findings."),
                bullet("Selected mesh-engine staging outcome."),
            ],
        ],
    ], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
    two_col.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEAFTER", (0, 0), (0, -1), 0.5, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    s.append(two_col)
    s.append(P("Acceptance criteria for a production evaluation", "H2X"))
    s.extend([
        bullet("Use representative customer CAD rather than synthetic-only fixtures."),
        bullet("Define the target mesh engine, scale, and simulation objective before judging warnings."),
        bullet("Compare original and repaired CAD for body count, extents, tolerances, area/volume, and expected passages."),
        bullet("Run the normal mesh quality checks and at least one solver smoke case on promoted geometry."),
        bullet("Escalate intent-sensitive changes to an engineer; do not tune caps until an unsafe repair passes."),
    ])
    s.append(callout(
        "Bottom line",
        "CRS automates geometry repair where the defect, operation, and proof of improvement are all explicit. It refuses to convert uncertainty into a clean-looking but untrustworthy model.",
        GREEN_BG,
        GREEN,
    ))
    s.append(Spacer(1, 14))
    s.append(P("Hexera", "H2X"))
    s.append(P("CAD intelligence and deterministic mesh preparation for engineering simulation.", "SmallX"))
    return s


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = CRSBrief(str(OUT))
    doc.build(story())
    print(OUT)
