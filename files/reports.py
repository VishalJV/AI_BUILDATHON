"""
reports.py - renderers: in-app floor maps (HTML), print-ready room PDFs and the course timetable table.
All text that originates from user-supplied data is escaped before it reaches HTML / PDF markup.
"""
import io
import pandas as pd
from collections import defaultdict
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from engine import CHRONO, get_stats, pd


def room_map_html(r, color_of):
    legend = "".join(f"<span style='display:inline-block;margin-right:10px;font-size:11px;color:#cbd5e1'><span style='display:inline-block;width:10px;height:10px;border-radius:2px;background:{col};margin-right:4px'></span>{xml_escape(str(c))}</span>"
                     for c, col in color_of.items())
    parts = [f"<div style='margin:4px 0 8px'>{legend}</div><div style='text-align:center;background:#1e293b;border:1px solid #475569;border-radius:6px;padding:5px;font-size:12px;color:#f8fafc;margin-bottom:6px'>FRONT - BLACKBOARD / TEACHER PODIUM</div>"]

    def cell(e):
        if e is None:
            return "<div style='min-width:96px;padding:4px 6px;border-radius:6px;font-size:10px;border:1px dashed #475569;color:#64748b'>vacant</div>"
        col = color_of.get(e["Course"], "#64748b")
        return (f"<div style='min-width:96px;padding:4px 6px;border-radius:6px;font-size:10px;background:{col}33;border:1px solid {col};color:#e2e8f0'>"
                f"<b>{xml_escape(e['RollNo'])}</b><br>{xml_escape(e['Name'].split()[0])} | {xml_escape(e['Course'])}</div>")

    for w, benches in r["grid"].items():
        parts.append(f"<div style='margin:8px 0 2px;color:#94a3b8;font-size:12px;font-weight:600'>{w if w != 'Straight' else 'Straight rows'}</div>")
        for b, row in enumerate(benches, 1):
            label = f"Row {b}" if w == "Straight" else f"Bench {b}"
            parts.append(f"<div style='display:flex;gap:4px;margin-bottom:4px;align-items:center'><span style='width:58px;color:#64748b;font-size:11px'>{label}</span>{''.join(cell(e) for e in row)}</div>")
    return "".join(parts)


def generate_room_pdf(room_allocs, approved_at=""):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    story, styles = [], getSampleStyleSheet()
    esc = lambda s: xml_escape(str(s))
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=12, textColor=colors.HexColor('#1f2937'), spaceAfter=2)
    subtitle_style = ParagraphStyle('SubTitleStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#4b5563'), spaceAfter=8)
    section_style = ParagraphStyle('SectionStyle', parent=styles['Heading2'], fontSize=10, textColor=colors.HexColor('#4f46e5'), spaceBefore=6, spaceAfter=3)
    cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontSize=6.5, leading=8, alignment=1)
    aisle_style = ParagraphStyle('AisleCellStyle', parent=styles['Normal'], fontSize=7, fontName='Helvetica-Bold', textColor=colors.HexColor('#94a3b8'), alignment=1)
    board_style = ParagraphStyle('BoardTextStyle', parent=styles['Normal'], fontSize=9.5, leading=12, alignment=1, textColor=colors.HexColor('#f8fafc'))
    wing_style = ParagraphStyle('WingTitleStyle', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#1e3a8a'), spaceBefore=4, spaceAfter=2)

    def sc(e):
        if not e:
            return Paragraph("<i>(Vacant)</i>", cell_style)
        return Paragraph(f"<b>{esc(e['RollNo'])}</b><br/>{esc(e['Name'].split()[0])} | {esc(e['Course'])}", cell_style)

    base_grid = [('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                 ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#3b82f6')),
                 ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                 ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e0e7ff')),
                 ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, 0), 7.5)]

    items = list(room_allocs.items())
    for idx, (room_key, r) in enumerate(items):
        story.append(Paragraph(f"<b>JKLU CLASSROOM FLOOR SEATING MAP ({esc(r['layout'].upper())})</b>", title_style))
        story.append(Paragraph(f"<b>Room &amp; Slot:</b> {esc(room_key)} | <b>Invigilators:</b> {esc(r['invigilators'])} | <b>Total Students:</b> {len(r['students'])} | <b>Approved:</b> {esc(approved_at)}", subtitle_style))
        t_board = Table([[Paragraph("<b>[ FRONT: CLASSROOM BLACKBOARD / TEACHER PODIUM ]</b>", board_style)]], colWidths=[550])
        t_board.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1e293b')), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                                     ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                                     ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#475569'))]))
        story += [t_board, Spacer(1, 6), Paragraph("<b>Top-Down Floor Plan (adjacent seats hold different courses):</b>", section_style)]

        for w, benches in r["grid"].items():
            if w == "Straight":
                data = [["Row", "Seat Box 1", "Seat Box 2", "Seat Box 3", "Seat Box 4"]]
                for b, row in enumerate(benches, 1):
                    data.append([f"Row {b}"] + [sc(row[k]) if k < len(row) else "-" for k in range(4)])
                t = Table(data, colWidths=[70, 120, 120, 120, 120], repeatRows=1)
                t.setStyle(TableStyle(base_grid))
            elif w == "Middle Wing":
                story.append(Paragraph(f"<b>{w} (Equally Distributed Bench Seating)</b>", wing_style))
                data, extra = [["Bench", "Seat Box 1", "Seat Box 2", "Seat Box 3"]], []
                for b, row in enumerate(benches, 1):
                    ri = len(data)
                    if len(row) == 1:
                        data.append([f"Bench {b}", sc(row[0]), "", ""])
                        extra.append(('SPAN', (1, ri), (3, ri)))
                    elif len(row) == 2:
                        data.append([f"Bench {b}", sc(row[0]), Paragraph("<b>[ GAP ]</b>", aisle_style), sc(row[1])])
                        extra.append(('BACKGROUND', (2, ri), (2, ri), colors.HexColor('#f1f5f9')))
                    else:
                        data.append([f"Bench {b}"] + [sc(x) for x in row[:3]])
                t = Table(data, colWidths=[70, 160, 160, 160], repeatRows=1)
                t.setStyle(TableStyle(base_grid + extra))
            else:
                story.append(Paragraph(f"<b>{w} (Side Arc Layout with Walkway Aisle Gap)</b>", wing_style))
                data = [["Bench", "Left Section Seats", "Walkway Aisle / Gap", "Right Section Seats"]]
                for b, row in enumerate(benches, 1):
                    data.append([f"Bench {b}", sc(row[0]), Paragraph("<b>[ AISLE / GAP ]</b>", aisle_style), sc(row[1]) if len(row) > 1 else "-"])
                t = Table(data, colWidths=[70, 200, 80, 200], repeatRows=1)
                t.setStyle(TableStyle(base_grid + [('BACKGROUND', (2, 1), (2, -1), colors.HexColor('#f1f5f9'))]))
            story += [t, Spacer(1, 4)]

        story.append(PageBreak())
        story.append(Paragraph("<b>JKLU MASTER ATTENDANCE ROLL CALL &amp; SIGNATURE SHEET</b>", title_style))
        story.append(Paragraph(f"<b>Room &amp; Slot:</b> {esc(room_key)} | <b>Invigilators:</b> {esc(r['invigilators'])} | <b>Total Seated:</b> {len(r['students'])}", subtitle_style))
        story.append(Spacer(1, 4))
        td = [["S.No", "Roll Number", "Student Name", "Course", "Box Position", "Signature"]]
        for i, s in enumerate(r["students"]):
            td.append([str(i + 1), s["RollNo"], s["Name"], s["Course"], s["Position"], ""])
        tm = Table(td, colWidths=[30, 90, 115, 70, 140, 65], repeatRows=1)
        tm.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2563eb')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                                ('ALIGN', (0, 0), (-1, -1), 'LEFT'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, 0), 8),
                                ('BOTTOMPADDING', (0, 0), (-1, 0), 4), ('TOPPADDING', (0, 0), (-1, 0), 4),
                                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f9fafb')),
                                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')), ('FONTSIZE', (0, 1), (-1, -1), 7.5)]))
        story.append(tm)
        if idx < len(items) - 1:
            story.append(PageBreak())
    if not items:
        story.append(Paragraph("No sessions to print.", title_style))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def course_table(state, alloc):
    rows = []
    where = defaultdict(set)
    for r in alloc["room_allocs"].values():
        for s in r["students"]:
            where[s["Course"]].add(r["room_name"])
    _, sizes = get_stats(state)
    for c, (d, s) in sorted(state["plan"].items(), key=lambda kv: (CHRONO.index(kv[1]), kv[0])):
        rows.append({"Course": c, "Day": d, "Slot": s, "Students": sizes[c], "Rooms": ", ".join(sorted(where[c]))})
    for c in state["pending"]:
        rows.append({"Course": c, "Day": "UNSCHEDULED", "Slot": "-", "Students": sizes.get(c, 0), "Rooms": "-"})
    return pd.DataFrame(rows)
