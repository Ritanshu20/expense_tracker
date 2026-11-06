"""
Export files for expenses: CSV, XLSX and PDF.

What lives here: turning a list of expenses into file contents.
Who calls it: reports/views.py, which has already limited the expenses to one user.
What it must never do: run a query of its own, or write user text into a spreadsheet unescaped.
"""

import csv
from collections.abc import Iterable
from decimal import Decimal
from io import BytesIO, StringIO
from typing import Any
from xml.sax.saxutils import escape

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from expenses.models import ZERO_AMOUNT, Expense

EXPORT_HEADERS = [
    "Date",
    "Title",
    "Description",
    "Category",
    "Payment method",
    "Type",
    "Amount",
    "My share",
    "Paid by me",
    "Created by",
]
# A spreadsheet treats a cell starting with one of these as a formula. Tab and carriage return
# are included because they are read the same way once a spreadsheet trims them.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# The file starts with a byte order mark, or Excel on Windows misreads non-English letters.
CSV_BYTE_ORDER_MARK = "﻿"


def escape_spreadsheet_formula(text: str) -> str:
    """
    Stop a spreadsheet from running user text as a formula.

    Why: SAFETY: a title like =HYPERLINK("http://evil.example") would otherwise run when
    someone opens the file. An apostrophe in front makes a spreadsheet treat it as plain text.

    Args:
        text: The text that a user typed.

    Returns:
        The same text, with an apostrophe in front if it could start a formula.

    Example:
        "=1+1" becomes "'=1+1", and "Dinner" stays "Dinner".
    """
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def _expense_to_row(expense: Expense) -> list[Any]:
    """Return the cells of one expense, in the order of EXPORT_HEADERS."""
    return [
        expense.date,
        expense.title,
        expense.description,
        expense.category.name,
        expense.get_payment_method_display(),
        expense.get_expense_type_display(),
        expense.amount,
        expense.my_share,
        expense.my_paid,
        expense.created_by.username,
    ]


def _expense_to_spreadsheet_row(expense: Expense) -> list[Any]:
    """Return the cells of one expense with all text made safe for a spreadsheet."""
    # SAFETY: every text cell is escaped, so a column added later is covered automatically.
    cells = _expense_to_row(expense)
    return [escape_spreadsheet_formula(cell) if isinstance(cell, str) else cell for cell in cells]


def build_csv(expenses: Iterable[Expense]) -> bytes:
    """Return the expenses as a CSV file, encoded as UTF-8."""
    output = StringIO()
    output.write(CSV_BYTE_ORDER_MARK)
    writer = csv.writer(output)
    writer.writerow(EXPORT_HEADERS)
    for expense in expenses:
        writer.writerow(_expense_to_spreadsheet_row(expense))
    return output.getvalue().encode("utf-8")


def build_xlsx(expenses: Iterable[Expense]) -> bytes:
    """Return the expenses as an Excel file. Dates and amounts stay real dates and numbers."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Expenses"
    sheet.append(EXPORT_HEADERS)
    for expense in expenses:
        sheet.append(_expense_to_spreadsheet_row(expense))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _pdf_cell(value: Any, style: Any) -> Paragraph:
    """Make a wrapping table cell. Paragraph reads markup, so user text must be escaped first."""
    text = "" if value is None else escape(str(value))
    return Paragraph(text, style)


def build_pdf(expenses: Iterable[Expense]) -> bytes:
    """
    Return the expenses as a PDF table, with my totals at the end.

    Why: a PDF is for reading, so unlike CSV and XLSX it adds up the two totals.
    RULE: my spending and paid by me stay separate, as everywhere else in the project.
    """
    styles = getSampleStyleSheet()
    cell_style = styles["BodyText"]
    rows = [[_pdf_cell(header, cell_style) for header in EXPORT_HEADERS]]
    total_share: Decimal = ZERO_AMOUNT
    total_paid: Decimal = ZERO_AMOUNT
    for expense in expenses:
        # A PDF does not run formulas, so it uses the plain values, not the spreadsheet row.
        rows.append([_pdf_cell(value, cell_style) for value in _expense_to_row(expense)])
        total_share += expense.my_share or ZERO_AMOUNT
        total_paid += expense.my_paid or ZERO_AMOUNT
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    summary = f"My spending: {total_share}. Paid by me: {total_paid}."
    output = BytesIO()
    # Left uncompressed so the file's text stays readable, which is what the tests check.
    document = SimpleDocTemplate(output, pagesize=landscape(A4), pageCompression=0)
    document.build(
        [
            Paragraph("Expenses", styles["Heading1"]),
            table,
            Paragraph(escape(summary), styles["Heading3"]),
        ]
    )
    return output.getvalue()
