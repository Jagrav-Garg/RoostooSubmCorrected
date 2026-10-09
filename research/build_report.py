"""Build the research PDF. ReportLab and Matplotlib are documentation dependencies."""
from pathlib import Path
import re
from xml.sax.saxutils import escape
import matplotlib

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/Roostoo_Strategy_Research.pdf"
NAVY = colors.HexColor("#17334B")
INK = colors.HexColor("#243645")
BLUE = colors.HexColor("#2B6389")
MUTED = colors.HexColor("#647480")
WIDTH = A4[0] - 96
FONT_ROOT = Path(matplotlib.get_data_path()) / "fonts/ttf"
for name, filename in [("ReportSans", "DejaVuSans.ttf"), ("ReportSans-Bold", "DejaVuSans-Bold.ttf"), ("ReportSans-Oblique", "DejaVuSans-Oblique.ttf"), ("ReportSans-BoldOblique", "DejaVuSans-BoldOblique.ttf"), ("ReportMono", "DejaVuSansMono.ttf")]:
    pdfmetrics.registerFont(TTFont(name, str(FONT_ROOT / filename)))
pdfmetrics.registerFontFamily("ReportSans", normal="ReportSans", bold="ReportSans-Bold", italic="ReportSans-Oblique", boldItalic="ReportSans-BoldOblique")
styles = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=21, leading=25, textColor=NAVY, spaceAfter=13),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12.8, leading=16, textColor=NAVY, spaceBefore=12, spaceAfter=7, keepWithNext=True),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.4, leading=13.2, textColor=INK, spaceAfter=7.5),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.4, leading=13.2, textColor=INK, spaceAfter=5.5, leftIndent=11, firstLineIndent=-7),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.0, leading=10.8, textColor=INK, spaceAfter=0, splitLongWords=True),
    "headcell": ParagraphStyle("headcell", fontName="Helvetica-Bold", fontSize=8.0, leading=10.8, textColor=colors.white, spaceAfter=0),
    "callout": ParagraphStyle("callout", fontName="Helvetica", fontSize=10.1, leading=14, textColor=NAVY, spaceAfter=0),
    "caption": ParagraphStyle("caption", fontName="Helvetica", fontSize=8.3, leading=11.5, textColor=MUTED, spaceAfter=8)
}
styles["source"] = ParagraphStyle("source", parent=styles["body"], fontSize=8.5, leading=11, spaceAfter=3)
styles["sourcebullet"] = ParagraphStyle("sourcebullet", parent=styles["bullet"], fontSize=8.5, leading=11, spaceAfter=2)
for style in styles.values():
    style.fontName = "ReportSans-Bold" if "Bold" in style.fontName else "ReportSans"
    style.allowWidows = 0
    style.allowOrphans = 0


def inline(text):
    text = text.replace("−", "-").replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    html = escape(text)
    html = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", lambda m: f'<link href="{escape(m[2], {chr(34): "&quot;"})}" color="#2B6389">{m[1]}</link>', html)
    html = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", html)
    html = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", html)
    html = re.sub(r"`([^`]+)`", r'<font name="ReportMono">\1</font>', html)
    return html


def table(lines):
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    rows = [rows[0]] + [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
    n = len(rows[0])
    if n == 7:
        ratios = [.28, .12, .12, .12, .11, .13, .12]
    elif n == 3:
        ratios = [.29, .35, .36]
    else:
        ratios = [1 / n] * n
    data = [[Paragraph(inline(c), styles["headcell"] if i == 0 else styles["cell"]) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=[WIDTH * r for r in ratios], repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), .5, NAVY),
        ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#D8E0E5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F1F5F8"), colors.white])
    ]))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D2DDE5"))
    canvas.line(48, 38, A4[0] - 48, 38)
    canvas.setFont("ReportSans", 7.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(48, 25, "ROOSTOO SENTINEL  |  RESEARCH CANDIDATE  |  9 OCT 2026")
    canvas.drawRightString(A4[0] - 48, 25, str(doc.page))
    canvas.restoreState()


def main(source="STRATEGY_RESEARCH.md",output=OUT):
    lines = (ROOT / source).read_text().splitlines()
    flow = []
    i = 0
    in_sources = False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("# "):
            flow.append(Paragraph(inline(line[2:]), styles["title"]))
            i += 1
        elif line.startswith("## "):
            in_sources = line[3:].strip() == "Source links"
            flow.append(Paragraph(inline(line[3:]), styles["h2"]))
            i += 1
        elif line.startswith("|"):
            group = []
            while i < len(lines) and lines[i].startswith("|"):
                group.append(lines[i]); i += 1
            flow.extend([table(group), Spacer(1, 10)])
        elif line.startswith("!["):
            figure = re.search(r"\]\(([^)]+)\)",line).group(1)
            flow.append(Image(str(ROOT / figure), width=WIDTH, height=WIDTH * 4.8 / 11))
            caption = "Historical Binance price proxies, full-size simulated fills and modeled fees. Matching assumptions remain unverified on Roostoo." if source == "TICK_STRATEGY_RESEARCH.md" else "Historical data, simulated execution and configured costs. Passive references do not establish trading-activity compliance."
            flow.append(Paragraph(caption, styles["caption"]))
            i += 1
        elif line.startswith("- "):
            flow.append(Paragraph("&#8226; " + inline(line[2:]), styles["sourcebullet" if in_sources else "bullet"]))
            i += 1
        else:
            group = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ", "![")):
                group.append(lines[i]); i += 1
            content = inline(" ".join(group))
            if content.startswith("<b>Finding:"):
                box = Table([[Paragraph(content, styles["callout"])]], colWidths=[WIDTH])
                box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF1F6")), ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#C4D5E3")), ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
                flow.extend([box, Spacer(1, 12)])
            else:
                flow.append(Paragraph(content, styles["source" if in_sources else "body"]))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=48, leftMargin=48, topMargin=43, bottomMargin=53, title=lines[0].lstrip('# '), author="Roostoo Sentinel research", allowSplitting=True)
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    print(output)


if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--source',default='STRATEGY_RESEARCH.md')
    p.add_argument('--output',default=str(OUT))
    a=p.parse_args()
    main(a.source,a.output)
