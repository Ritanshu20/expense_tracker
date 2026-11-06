"""Tests for CSV, XLSX and PDF exports: contents, filters, headers, safety and isolation."""

import csv
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from expenses.models import Category, ExpenseParticipant, ExpenseType, PaymentMethod
from expenses.tests.helpers import (
    create_expense,
    create_personal_expense,
    create_user,
    default_category,
)
from reports.exports import EXPORT_HEADERS, escape_spreadsheet_formula


def money(value: str) -> Decimal:
    return Decimal(value)


class FormulaEscapingTests(SimpleTestCase):
    def test_text_that_could_start_a_formula_gets_an_apostrophe(self):
        """Every prefix a spreadsheet reads as a formula is neutralised."""
        for text in ("=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd"):
            with self.subTest(text=text):
                self.assertEqual(escape_spreadsheet_formula(text), f"'{text}")

    def test_normal_text_is_left_alone(self):
        """Only the first character matters, and empty text stays empty."""
        for text in ("Dinner", "a=b", "2+2", "user@example.com", ""):
            with self.subTest(text=text):
                self.assertEqual(escape_spreadsheet_formula(text), text)


class ExportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.food = default_category("Food")
        cls.travel = default_category("Travel")

    def setUp(self):
        self.client.force_login(self.rahul)

    def export(self, file_format: str, **filters):
        return self.client.get(reverse("export_expenses", args=[file_format]), filters)

    def csv_rows(self, **filters) -> list[list[str]]:
        response = self.export("csv", **filters)
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        return list(csv.reader(StringIO(text)))

    def xlsx_rows(self, **filters) -> list[tuple]:
        response = self.export("xlsx", **filters)
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(BytesIO(response.content)).active
        return list(sheet.iter_rows(values_only=True))


class ExportContentTests(ExportTestCase):
    def setUp(self):
        super().setUp()
        self.lunch = create_personal_expense(
            self.rahul,
            title="Lunch",
            description="With the team",
            amount=money("250.50"),
            date=date(2026, 11, 5),
            category=self.food,
            payment_method=PaymentMethod.UPI,
        )
        dinner = create_expense(
            self.aman,
            title="Dinner",
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            category=self.travel,
            payment_method=PaymentMethod.CASH,
            expense_type=ExpenseType.SHARED,
        )
        ExpenseParticipant.objects.create(
            expense=dinner,
            user=self.rahul,
            paid_amount=money("1500.00"),
            share_amount=money("1000.00"),
        )

    def test_csv_has_the_header_row_and_one_row_per_expense(self):
        """Newest first, with my share and paid by me as separate columns."""
        rows = self.csv_rows()

        self.assertEqual(rows[0], EXPORT_HEADERS)
        self.assertEqual(
            rows[1],
            [
                "2026-11-10",
                "Dinner",
                "",
                "Travel",
                "Cash",
                "Shared",
                "3000.00",
                "1000.00",
                "1500.00",
                "aman",
            ],
        )
        self.assertEqual(
            rows[2],
            [
                "2026-11-05",
                "Lunch",
                "With the team",
                "Food",
                "UPI",
                "Personal",
                "250.50",
                "250.50",
                "250.50",
                "rahul",
            ],
        )
        self.assertEqual(len(rows), 3)

    def test_csv_keeps_non_english_text_readable_in_excel(self):
        """The byte order mark tells Excel that the file is UTF-8."""
        create_personal_expense(self.rahul, title="चाय", date=date(2026, 11, 20))

        response = self.export("csv")

        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("चाय", response.content.decode("utf-8-sig"))

    def test_xlsx_has_the_header_row_and_real_dates_and_numbers(self):
        """A spreadsheet can sort and add these columns."""
        rows = self.xlsx_rows()

        self.assertEqual(list(rows[0]), EXPORT_HEADERS)
        dinner_row = rows[1]
        self.assertEqual(dinner_row[1], "Dinner")
        self.assertEqual(dinner_row[0].date(), date(2026, 11, 10))
        self.assertEqual(dinner_row[6], 3000)
        self.assertEqual(dinner_row[7], 1000)
        self.assertEqual(dinner_row[8], 1500)
        self.assertEqual(len(rows), 3)

    def test_pdf_is_a_pdf_with_the_rows_and_my_totals(self):
        """Spending: 250.50 + 1000 = 1250.50. Paid: 250.50 + 1500 = 1750.50."""
        response = self.export("pdf")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
        for text in (b"Lunch", b"Dinner", b"1250.50", b"1750.50"):
            with self.subTest(text=text):
                self.assertIn(text, response.content)

    def test_download_headers_are_correct_for_every_format(self):
        """The browser saves the file instead of showing it."""
        expected = {
            "csv": "text/csv; charset=utf-8",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf",
        }
        for file_format, content_type in expected.items():
            with self.subTest(file_format=file_format):
                response = self.export(file_format)

                self.assertEqual(response["Content-Type"], content_type)
                self.assertIn("attachment; filename=", response["Content-Disposition"])
                self.assertIn(f'.{file_format}"', response["Content-Disposition"])

    def test_unknown_format_gives_a_404(self):
        """Only the three formats exist."""
        self.assertEqual(self.export("docx").status_code, 404)

    def test_export_only_accepts_get(self):
        """A download changes nothing."""
        url = reverse("export_expenses", args=["csv"])

        self.assertEqual(self.client.post(url).status_code, 405)

    def test_empty_export_still_gives_a_valid_file_in_every_format(self):
        """A period with no expenses is not an error."""
        filters = {"date_from": "2000-01-01", "date_to": "2000-01-02"}

        self.assertEqual(self.csv_rows(**filters), [EXPORT_HEADERS])
        self.assertEqual(self.xlsx_rows(**filters), [tuple(EXPORT_HEADERS)])
        self.assertTrue(self.export("pdf", **filters).content.startswith(b"%PDF"))


class ExportFilterTests(ExportTestCase):
    def setUp(self):
        super().setUp()
        for day, title, category, method in (
            (date(2026, 11, 1), "First", self.food, PaymentMethod.CASH),
            (date(2026, 11, 15), "Middle", self.travel, PaymentMethod.UPI),
            (date(2026, 11, 30), "Last", self.food, PaymentMethod.UPI),
            (date(2026, 12, 1), "Next month", self.food, PaymentMethod.UPI),
        ):
            create_personal_expense(
                self.rahul, title=title, date=day, category=category, payment_method=method
            )

    def titles(self, **filters) -> list[str]:
        return [row[1] for row in self.csv_rows(**filters)[1:]]

    def test_date_range_includes_both_ends(self):
        """1 Nov and 30 Nov are in, and 1 Dec is out."""
        titles = self.titles(date_from="2026-11-01", date_to="2026-11-30")

        self.assertEqual(titles, ["Last", "Middle", "First"])

    def test_category_payment_method_and_search_filters_apply(self):
        """The export uses the same filters as the list page."""
        self.assertEqual(self.titles(category=self.travel.pk), ["Middle"])
        self.assertEqual(self.titles(payment_method="cash"), ["First"])
        self.assertEqual(self.titles(q="next"), ["Next month"])

    def test_filters_combine(self):
        """All the conditions must match together."""
        titles = self.titles(date_to="2026-11-30", category=self.food.pk, payment_method="upi")

        self.assertEqual(titles, ["Last"])

    def test_every_format_respects_the_filters(self):
        """XLSX and PDF get the same rows as CSV."""
        filters = {"date_from": "2026-11-15", "date_to": "2026-11-15"}

        xlsx_titles = [row[1] for row in self.xlsx_rows(**filters)[1:]]
        pdf_content = self.export("pdf", **filters).content

        self.assertEqual(xlsx_titles, ["Middle"])
        self.assertIn(b"Middle", pdf_content)
        self.assertNotIn(b"First", pdf_content)

    def test_invalid_filter_goes_back_to_the_list_with_a_message(self):
        """A bad date gives a message, not a crash or a misleading file."""
        response = self.client.get(
            reverse("export_expenses", args=["csv"]), {"date_from": "nonsense"}, follow=True
        )

        self.assertRedirects(response, reverse("expense_list"))
        self.assertContains(response, "export filters are not valid")

    def test_list_page_download_links_keep_the_current_filters(self):
        """The file matches what the user is looking at, but not the page number."""
        response = self.client.get(reverse("expense_list"), {"q": "First", "page": "1"})

        self.assertContains(response, f"{reverse('export_expenses', args=['csv'])}?q=First")
        self.assertNotContains(response, f"{reverse('export_expenses', args=['csv'])}?q=First&")

    def test_report_page_download_links_use_the_period_of_the_report(self):
        """The file covers the same days as the report."""
        response = self.client.get(reverse("report_monthly"), {"date": "2026-11-15"})

        self.assertContains(response, "?date_from=2026-11-01&amp;date_to=2026-11-30")


class ExportSafetyTests(ExportTestCase):
    def test_formula_in_every_text_column_is_escaped_in_csv(self):
        """SAFETY: title, description, category and creator can all hold user text."""
        evil_user = create_user("@evil")
        category = Category.objects.create(name="=CATEGORY()", owner=evil_user)
        expense = create_expense(
            evil_user,
            title="=HYPERLINK(1)",
            description="+DESCRIPTION()",
            category=category,
            expense_type=ExpenseType.SHARED,
        )
        ExpenseParticipant.objects.create(expense=expense, user=self.rahul)

        row = self.csv_rows()[1]

        self.assertEqual(row[1], "'=HYPERLINK(1)")
        self.assertEqual(row[2], "'+DESCRIPTION()")
        self.assertEqual(row[3], "'=CATEGORY()")
        self.assertEqual(row[9], "'@evil")

    def test_formula_in_a_title_does_not_become_a_formula_in_xlsx(self):
        """SAFETY: openpyxl would store unescaped text starting with = as a real formula."""
        create_personal_expense(self.rahul, title="=1+1")

        response = self.export("xlsx")

        sheet = load_workbook(BytesIO(response.content)).active
        cell = sheet.cell(row=2, column=2)
        self.assertEqual(cell.value, "'=1+1")
        self.assertEqual(cell.data_type, "s")

    def test_markup_in_a_title_does_not_break_the_pdf(self):
        """SAFETY: the PDF library reads markup, so user text is escaped before it arrives."""
        create_personal_expense(
            self.rahul, title="<b>Bold</b> & <font size=99>big</font>", description="<para>"
        )

        response = self.export("pdf")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_another_users_expenses_are_never_exported(self):
        """Isolation: only the user's own data is in the file, in every format."""
        create_personal_expense(self.aman, title="Aman secret", amount=money("7777.00"))
        create_personal_expense(self.rahul, title="Mine")

        self.assertEqual([row[1] for row in self.csv_rows()[1:]], ["Mine"])
        self.assertEqual([row[1] for row in self.xlsx_rows()[1:]], ["Mine"])
        self.assertNotIn(b"Aman", self.export("pdf").content)

    def test_another_users_category_cannot_be_used_as_a_filter(self):
        """A hand-edited link with a private category id is refused."""
        secret = Category.objects.create(name="Secret", owner=self.aman)

        response = self.export("csv", category=secret.pk)

        self.assertRedirects(response, reverse("expense_list"))

    def test_anonymous_visitor_is_sent_to_login(self):
        """Exports hold private data."""
        self.client.logout()
        url = reverse("export_expenses", args=["csv"])

        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_expense_i_created_without_taking_part_has_empty_my_columns(self):
        """It is mine to see, but it is not part of my spending."""
        expense = create_expense(self.rahul, title="For friends", expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=expense, user=self.aman)

        row = self.csv_rows()[1]

        self.assertEqual(row[1], "For friends")
        self.assertEqual(row[7], "")
        self.assertEqual(row[8], "")

    def test_query_count_does_not_grow_with_the_number_of_rows(self):
        """No N+1: session, user, category dropdown check and one expense query."""
        for number in range(30):
            create_personal_expense(self.rahul, title=f"Expense {number}")

        with self.assertNumQueries(3):
            response = self.export("csv")

        self.assertEqual(response.status_code, 200)
