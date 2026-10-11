"""CSV and Excel downloads for result tables shown in the chat."""

from __future__ import annotations

import csv
import io
import re
import time

from openpyxl import Workbook
from openpyxl.styles import Font

from .sessions import ResultTable

SOURCE = "US Open Census Data (SafeGraph) on the Snowflake Marketplace: ACS 5-year estimates, summed over block groups"


def filename(table: ResultTable, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (table.purpose or "census-result").lower()).strip("-")[:60] or "census-result"
    return f"{slug}.{ext}"


def to_csv(table: ResultTable) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(table.columns)
    w.writerows([_excel_safe(v) for v in row] for row in table.rows)
    # UTF-8 with BOM so Excel opens accented place names (e.g. "Doña Ana County") correctly.
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(table: ResultTable) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(table.columns)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in table.rows:
        ws.append([_excel_safe(v) for v in row])
    ws.freeze_panes = "A2"
    for i, col in enumerate(table.columns, start=1):
        width = max([len(str(col))] + [len(str(r[i - 1])) for r in table.rows[:200]]) + 2
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 50)

    about = wb.create_sheet("About")
    for k, v in [
        ("What", table.purpose or "Query result"),
        ("Rows", f"{len(table.rows):,}" + (" (truncated: the full result was larger)" if table.truncated else "")),
        ("Source", SOURCE),
        ("Generated", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(table.created))),
        ("SQL", table.sql),
    ]:
        about.append([k, v])
    about.column_dimensions["A"].width = 12
    about.column_dimensions["B"].width = 110
    for cell in about["A"]:
        cell.font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _excel_safe(v: object) -> object:
    # Text starting with = + - @ is treated as a formula by Excel ("formula injection"). Census values are
    # numbers and place names, but the rule is cheap and the data came through a model-written query.
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v
