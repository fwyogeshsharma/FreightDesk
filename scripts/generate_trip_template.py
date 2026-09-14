"""Generate the blank .xlsx template for the next batch of trip data (same shape
of migration as import_payment_report.py, but this time WE control the columns
instead of adapting an export from another app).

Column choices are deliberate, based on lessons from the payment-report import:
  - Separate Origin/Destination columns instead of one free-text "Trip Route"
    string — that one column needed a regex parser to handle multi-leg and
    duplicate-leg trips ("A to B, A to C", "A to B, A to B"). Asking for the
    two places directly avoids that whole class of parsing bugs.
  - Trip Reference No is optional but recommended: if given, it's used for
    idempotency (safe to re-run the importer without double-inserting) instead
    of relying on the spreadsheet row number, which breaks if rows get
    reordered/deleted while the sheet is being filled in over time.
  - Load Status and the DB's two loaded_status values (LOADED/UNLOADED) are a
    real enum (pipeline/reports.py only accepts those two) — dropdown-restricted.
    Vehicle/Body/Axle Type are free text in the DB (no server-side enum) —
    dropdown-suggested but not restricted, so unusual real values aren't blocked.
  - Driver Phone is formatted as text so Excel doesn't mangle a 10-digit number
    into scientific notation or drop a leading zero.

Re-run this whenever the column set needs to change — it always regenerates the
file from scratch (it's a template, not data).

Usage:
    .venv\\Scripts\\python.exe scripts\\generate_trip_template.py [output.xlsx]
"""
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

DEFAULT_OUT = str(Path(__file__).resolve().parent.parent / "trip_data_template.xlsx")

# (header, width, required, note-for-cell-comment)
COLUMNS = [
    ("S.No", 6, False, "Just for your own reference — not imported."),
    ("Trip Reference No", 18, False,
     "Optional but recommended — e.g. a booking/bilty number. If you re-submit "
     "an updated sheet later, matching reference numbers let us avoid creating "
     "duplicate entries for the same trip."),
    ("Vehicle Number", 16, True, "License plate, e.g. UP15FT1043. Required — rows without a plate are skipped."),
    ("Driver Name", 18, False, "As you'd greet them on a call, e.g. Ramesh Kumar."),
    ("Driver Phone Number", 16, True,
     "10-digit Indian mobile number, no +91 or leading 0. Required — rows without "
     "a valid 10-digit number are skipped."),
    ("Transporter / Company Name", 24, False, "Fleet or owner name, if known."),
    ("Vehicle Type", 16, False, "e.g. Truck, Trailer, Tanker, Container."),
    ("Body Type", 16, False, "e.g. Open, Closed, Flatbed."),
    ("Material / Cargo Type", 20, False, "What the truck carries, e.g. Steel, Cement, Grains, FMCG."),
    ("Axle Type", 14, False, "e.g. 2 Axle, 3 Axle, Multi-Axle."),
    ("Number of Wheels", 14, False, "Numeric, if known, e.g. 10."),
    ("Load Status", 14, False, "Loaded or Empty."),
    ("Origin Location", 20, True, "Where the trip started, e.g. Jaipur. Required."),
    ("Destination Location", 20, False,
     "Where the trip ended, e.g. Meerut. Optional, but strongly recommended — "
     "brokers can search either location, so a second leg roughly doubles a "
     "truck's chance of being found."),
    ("Trip Date", 16, True, "Format DD-MM-YYYY, e.g. 15-03-2026. Required."),
    ("Remarks / Other Info", 30, False, "Anything else worth keeping — free text."),
]

EXAMPLE_ROW = [
    1, "BILTY-10456", "UP15FT1043", "Ramesh Kumar", "9811008120",
    "Balaji Roadlines", "Truck", "Open", "Steel", "2 Axle", 10,
    "Loaded", "Jaipur", "Meerut", "15-03-2026", "Regular customer, prefers morning calls",
]

HEADER_FILL = PatternFill("solid", fgColor="1E3A5F")
HEADER_FONT = Font(color="FFFFFF", bold=True)
REQUIRED_FILL = PatternFill("solid", fgColor="FFF3CD")
EXAMPLE_FILL = PatternFill("solid", fgColor="F2F2F2")
EXAMPLE_FONT = Font(italic=True, color="808080")
THIN_BORDER = Border(*([Side(style="thin", color="D9D9D9")] * 4))


def build(out_path: str):
    wb = Workbook()

    ws = wb.active
    ws.title = "Trip Data"
    for col_idx, (header, width, required, _note) in enumerate(COLUMNS, start=1):
        letter = get_column_letter(col_idx)
        ws.column_dimensions[letter].width = width
        cell = ws.cell(row=1, column=col_idx, value=header + (" *" if required else ""))
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.comment = Comment(_note, "FreightDesk")
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"

    for col_idx, value in enumerate(EXAMPLE_ROW, start=1):
        cell = ws.cell(row=2, column=col_idx, value=value)
        cell.fill = EXAMPLE_FILL
        cell.font = EXAMPLE_FONT
        cell.border = THIN_BORDER
    ws.cell(row=2, column=1).comment = Comment(
        "This is an example row showing the expected format — delete it before submitting.", "FreightDesk")

    # Driver Phone as text so Excel never mangles/reformats the number.
    phone_col = next(i for i, c in enumerate(COLUMNS, start=1) if c[0] == "Driver Phone Number")
    for row in range(2, 502):
        ws.cell(row=row, column=phone_col).number_format = "@"

    # Light pre-formatting for the next 500 rows so it's obviously a fill-in sheet.
    for row in range(3, 502):
        for col_idx in range(1, len(COLUMNS) + 1):
            ws.cell(row=row, column=col_idx).border = THIN_BORDER

    # Load Status: a real DB enum (LOADED/UNLOADED) — restrict to the two values.
    load_col_letter = get_column_letter(next(i for i, c in enumerate(COLUMNS, start=1) if c[0] == "Load Status"))
    dv_load = DataValidation(type="list", formula1='"Loaded,Empty"', allow_blank=True, showDropDown=False)
    dv_load.error = "Please choose Loaded or Empty from the dropdown."
    ws.add_data_validation(dv_load)
    dv_load.add(f"{load_col_letter}2:{load_col_letter}501")

    # Axle Type / Vehicle Type / Body Type are free text in the DB (no server-side
    # enum) — dropdown-suggested for consistency, but not restricted, since real
    # unusual values shouldn't be blocked.
    def suggest(col_name, options):
        letter = get_column_letter(next(i for i, c in enumerate(COLUMNS, start=1) if c[0] == col_name))
        dv = DataValidation(type="list", formula1=f'"{options}"', allow_blank=True, showErrorMessage=False)
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}501")

    suggest("Axle Type", "2 Axle,3 Axle,4 Axle,Multi-Axle")
    suggest("Vehicle Type", "Truck,Trailer,Tanker,Container")
    suggest("Body Type", "Open,Closed,Flatbed")

    # ── Instructions sheet ──────────────────────────────────────────────────
    ins = wb.create_sheet("Instructions")
    ins.column_dimensions["A"].width = 26
    ins.column_dimensions["B"].width = 12
    ins.column_dimensions["C"].width = 70
    for col_idx, header in enumerate(["Column", "Required?", "Notes"], start=1):
        cell = ins.cell(row=1, column=col_idx, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    ins.freeze_panes = "A2"
    r = 2
    for header, _width, required, note in COLUMNS:
        ins.cell(row=r, column=1, value=header).font = Font(bold=required)
        req_cell = ins.cell(row=r, column=2, value="Required" if required else "Optional")
        if required:
            req_cell.fill = REQUIRED_FILL
        ins.cell(row=r, column=3, value=note).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    r += 1
    ins.cell(row=r, column=1, value="How one row becomes broker data").font = Font(bold=True, size=12)
    r += 1
    tips = [
        "One filled-in row = one trip. If both Origin and Destination are given, the "
        "truck becomes searchable/callable at BOTH locations (two entries internally) "
        "— you only need to fill in one row per trip either way.",
        "Please don't repeat a value several times in one cell (e.g. \"Steel, Steel, "
        "Steel\") — just enter it once.",
        "Trip Date must be an actual date in DD-MM-YYYY format, not text — a "
        "malformed date is one of the most common reasons a row gets skipped.",
        "Driver Phone Number must be a plain 10-digit Indian mobile number — no "
        "spaces, no +91, no leading 0.",
        "Rows missing Vehicle Number, Driver Phone Number, Origin Location, or Trip "
        "Date will be skipped — everything else is optional.",
    ]
    for tip in tips:
        ins.cell(row=r, column=1, value=f"• {tip}").alignment = Alignment(wrap_text=True, vertical="top")
        ins.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ins.row_dimensions[r].height = 34
        r += 1

    wb.save(out_path)
    return out_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    path = build(out)
    print(f"Wrote {path}")
