"""Tests for reports: period boundaries, totals, settlement info and isolation."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from expenses.models import Category, ExpenseParticipant, ExpenseType, PaymentMethod
from expenses.selectors import DateRange, get_report_data
from expenses.tests.helpers import (
    create_expense,
    create_personal_expense,
    create_user,
    default_category,
)

# A Wednesday. Its week runs from Monday 9 Nov to Sunday 15 Nov.
TODAY = date(2026, 11, 11)


def money(value: str) -> Decimal:
    return Decimal(value)


def add_rows(expense, *rows) -> None:
    """Add participant rows. Each row is (user, paid, share)."""
    for user, paid_amount, share_amount in rows:
        ExpenseParticipant.objects.create(
            expense=expense,
            user=user,
            paid_amount=money(paid_amount),
            share_amount=money(share_amount),
        )


class ReportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rohit = create_user("rohit")

    def setUp(self):
        self.client.force_login(self.rahul)

    def open_report(self, name: str, **query):
        with mock.patch("reports.views.timezone.localdate", return_value=TODAY):
            return self.client.get(reverse(name), query)

    def total_of(self, name: str, **query) -> Decimal:
        response = self.open_report(name, **query)
        self.assertEqual(response.status_code, 200)
        return response.context["summary"]["total"]


class ReportAccessTests(ReportTestCase):
    def test_urls_match_the_agreed_addresses(self):
        """The addresses in the project's URL list are the ones used."""
        self.assertEqual(reverse("report_index"), "/reports/")
        self.assertEqual(reverse("report_monthly"), "/reports/monthly/")
        self.assertEqual(reverse("report_custom"), "/reports/custom/")

    def test_every_report_sends_anonymous_visitors_to_login(self):
        """Reports hold private data."""
        self.client.logout()
        names = (
            "report_index",
            "report_daily",
            "report_weekly",
            "report_monthly",
            "report_yearly",
            "report_custom",
        )

        for name in names:
            with self.subTest(name=name):
                url = reverse(name)
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_index_links_to_every_report(self):
        """All the reports can be reached from one page."""
        response = self.client.get(reverse("report_index"))

        for name in ("report_daily", "report_weekly", "report_monthly", "report_yearly"):
            with self.subTest(name=name):
                self.assertContains(response, reverse(name))
        self.assertContains(response, reverse("report_custom"))

    def test_navigation_links_to_reports(self):
        """The reports can be reached from the navigation."""
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, reverse("report_index"))


class PeriodBoundaryTests(ReportTestCase):
    def add_on(self, *days) -> None:
        for day in days:
            create_personal_expense(self.rahul, amount=money("1.00"), date=day)

    def test_daily_report_counts_only_that_day(self):
        """The day before and the day after are out."""
        self.add_on(date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12))

        self.assertEqual(self.total_of("report_daily", date="2026-11-11"), money("1.00"))

    def test_weekly_report_includes_monday_and_sunday_only(self):
        """Sunday 8 Nov and Monday 16 Nov are out. Monday 9 and Sunday 15 are in."""
        self.add_on(date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 15), date(2026, 11, 16))

        self.assertEqual(self.total_of("report_weekly", date="2026-11-11"), money("2.00"))

    def test_any_day_of_the_week_gives_the_same_weekly_report(self):
        """Choosing the Monday or the Sunday gives the same week."""
        self.add_on(date(2026, 11, 9), date(2026, 11, 15))

        self.assertEqual(self.total_of("report_weekly", date="2026-11-09"), money("2.00"))
        self.assertEqual(self.total_of("report_weekly", date="2026-11-15"), money("2.00"))

    def test_monthly_report_includes_the_first_and_last_day_only(self):
        """31 Oct and 1 Dec are out. 1 Nov and 30 Nov are in."""
        self.add_on(date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 30), date(2026, 12, 1))

        self.assertEqual(self.total_of("report_monthly", date="2026-11-15"), money("2.00"))

    def test_monthly_report_handles_february_in_a_leap_year(self):
        """29 Feb 2028 is in February, and 1 Mar is not."""
        self.add_on(date(2028, 2, 29), date(2028, 3, 1))

        self.assertEqual(self.total_of("report_monthly", date="2028-02-10"), money("1.00"))

    def test_yearly_report_includes_1_january_and_31_december_only(self):
        """31 Dec 2025 and 1 Jan 2027 are out."""
        self.add_on(date(2025, 12, 31), date(2026, 1, 1), date(2026, 12, 31), date(2027, 1, 1))

        self.assertEqual(self.total_of("report_yearly", date="2026-06-15"), money("2.00"))

    def test_custom_report_includes_both_end_dates(self):
        """The first and last chosen days both count."""
        self.add_on(date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 20), date(2026, 3, 21))

        total = self.total_of("report_custom", date_from="2026-03-10", date_to="2026-03-20")

        self.assertEqual(total, money("2.00"))

    def test_custom_report_can_cover_a_single_day(self):
        """The same start and end date is a valid range."""
        self.add_on(date(2026, 3, 10), date(2026, 3, 11))

        total = self.total_of("report_custom", date_from="2026-03-10", date_to="2026-03-10")

        self.assertEqual(total, money("1.00"))

    def test_report_without_a_date_uses_today(self):
        """Opening a report shows the current period."""
        self.add_on(TODAY, date(2026, 10, 1))

        response = self.open_report("report_monthly")

        self.assertEqual(response.context["summary"]["total"], money("1.00"))
        self.assertEqual(response.context["date_range"], (date(2026, 11, 1), date(2026, 11, 30)))

    def test_invalid_date_shows_an_error_and_falls_back_to_today(self):
        """A hand-edited URL does not crash the page."""
        response = self.open_report("report_monthly", date="nonsense")

        self.assertContains(response, "Enter a valid date")
        self.assertEqual(response.context["date_range"], (date(2026, 11, 1), date(2026, 11, 30)))

    def test_previous_and_next_links_point_to_the_neighbouring_periods(self):
        """31 Oct is in the previous month, and 1 Dec is in the next one."""
        response = self.open_report("report_monthly", date="2026-11-15")

        self.assertContains(response, "?date=2026-10-31")
        self.assertContains(response, "?date=2026-12-01")

    def test_custom_report_without_dates_shows_only_the_form(self):
        """Nothing can be reported until a range is chosen."""
        response = self.client.get(reverse("report_custom"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("summary", response.context)

    def test_custom_report_needs_both_dates(self):
        """A missing date gives a form error."""
        response = self.client.get(reverse("report_custom"), {"date_from": "2026-03-10"})

        self.assertContains(response, "This field is required")
        self.assertNotIn("summary", response.context)

    def test_custom_report_refuses_a_start_after_the_end(self):
        """A backwards range gives a form error."""
        response = self.client.get(
            reverse("report_custom"), {"date_from": "2026-03-20", "date_to": "2026-03-10"}
        )

        self.assertContains(response, "start date cannot be after the end date")
        self.assertNotIn("summary", response.context)


class ReportTotalsTests(ReportTestCase):
    """November data for Rahul, with every expected number worked out by hand."""

    NOVEMBER = DateRange(date(2026, 11, 1), date(2026, 11, 30))

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        food = default_category("Food")
        travel = default_category("Travel")
        create_personal_expense(
            cls.rahul,
            amount=money("100.00"),
            date=date(2026, 11, 5),
            category=food,
            payment_method=PaymentMethod.CASH,
        )
        create_personal_expense(
            cls.rahul,
            amount=money("50.00"),
            date=date(2026, 11, 6),
            category=travel,
            payment_method=PaymentMethod.UPI,
        )
        # Outside November, so it must not count.
        create_personal_expense(cls.rahul, amount=money("999.00"), date=date(2026, 12, 1))
        dinner = create_expense(
            cls.rahul,
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            category=food,
            payment_method=PaymentMethod.UPI,
            expense_type=ExpenseType.SHARED,
        )
        add_rows(
            dinner,
            (cls.rahul, "1500.00", "1000.00"),
            (cls.aman, "1000.00", "1000.00"),
            (cls.rohit, "500.00", "1000.00"),
        )

    def setUp(self):
        super().setUp()
        self.report = get_report_data(self.rahul, self.NOVEMBER)

    def test_my_spending_and_paid_by_me_are_separate(self):
        """Spending: 100 + 50 + 1000 = 1150. Paid: 100 + 50 + 1500 = 1650."""
        self.assertEqual(self.report["summary"]["total"], money("1150.00"))
        self.assertEqual(self.report["summary"]["paid_by_me"], money("1650.00"))

    def test_personal_and_shared_totals(self):
        """150 personal and 1000 shared."""
        self.assertEqual(self.report["summary"]["personal"], money("150.00"))
        self.assertEqual(self.report["summary"]["shared"], money("1000.00"))

    def test_expense_count_covers_the_period_only(self):
        """Three expenses in November. The December one is out."""
        self.assertEqual(self.report["expense_count"], 3)

    def test_category_totals(self):
        """Food: 100 + 1000. Travel: 50."""
        self.assertEqual(
            self.report["category_totals"],
            [
                {"label": "Food", "total": money("1100.00")},
                {"label": "Travel", "total": money("50.00")},
            ],
        )

    def test_payment_method_totals(self):
        """UPI: 50 + 1000. Cash: 100."""
        self.assertEqual(
            self.report["payment_method_totals"],
            [
                {"label": "UPI", "total": money("1050.00")},
                {"label": "Cash", "total": money("100.00")},
            ],
        )

    def test_person_totals(self):
        """Rahul's line equals his spending for the period."""
        self.assertEqual(
            self.report["person_totals"],
            [
                {"label": "rahul", "total": money("1150.00")},
                {"label": "aman", "total": money("1000.00")},
                {"label": "rohit", "total": money("1000.00")},
            ],
        )

    def test_settlement_info_comes_from_expenses_in_the_period(self):
        """In November Rohit owes Rahul 500. In October nothing was shared."""
        october = DateRange(date(2026, 10, 1), date(2026, 10, 31))

        self.assertEqual(
            self.report["settlements"].owed_to_you, [("rohit", "rahul", money("500.00"))]
        )
        self.assertEqual(get_report_data(self.rahul, october)["settlements"].owed_to_you, [])

    def test_empty_period_gives_zeros_and_no_lines(self):
        """A period with no expenses is valid."""
        report = get_report_data(self.rahul, DateRange(date(2020, 1, 1), date(2020, 1, 31)))

        self.assertEqual(report["summary"]["total"], money("0.00"))
        self.assertEqual(report["expense_count"], 0)
        self.assertEqual(report["category_totals"], [])

    def test_page_shows_the_numbers(self):
        """The template displays what the selector calculated."""
        response = self.open_report("report_monthly", date="2026-11-15")

        for text in ("1150.00", "1650.00", "1100.00", "1050.00", "rohit owes you", "3 expenses"):
            with self.subTest(text=text):
                self.assertContains(response, text)

    def test_query_count_stays_fixed_however_much_data_there_is(self):
        """Session, user, totals, three breakdowns, settlements and payments."""
        for _ in range(20):
            create_personal_expense(self.rahul, date=date(2026, 11, 20))

        with self.assertNumQueries(8):
            self.open_report("report_monthly", date="2026-11-15")


class ReportIsolationTests(ReportTestCase):
    NOVEMBER = DateRange(date(2026, 11, 1), date(2026, 11, 30))

    def test_another_users_personal_expense_does_not_count(self):
        """Isolation: nothing of Aman's reaches Rahul's report."""
        create_personal_expense(self.aman, amount=money("7777.00"), date=date(2026, 11, 5))

        report = get_report_data(self.rahul, self.NOVEMBER)

        self.assertEqual(report["summary"]["total"], money("0.00"))
        self.assertEqual(report["category_totals"], [])

    def test_shared_expense_without_me_does_not_count(self):
        """Aman and Rohit's expense stays between them."""
        expense = create_expense(
            self.aman,
            amount=money("800.00"),
            date=date(2026, 11, 5),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(expense, (self.aman, "800.00", "400.00"), (self.rohit, "0.00", "400.00"))

        report = get_report_data(self.rahul, self.NOVEMBER)

        self.assertEqual(report["person_totals"], [])
        self.assertEqual(report["settlements"].you_owe, [])
        self.assertEqual(report["settlements"].owed_to_you, [])

    def test_expense_i_created_without_taking_part_does_not_count(self):
        """RULE: creating an expense is not the same as taking part in it."""
        expense = create_expense(
            self.rahul,
            amount=money("800.00"),
            date=date(2026, 11, 5),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(expense, (self.aman, "800.00", "400.00"), (self.rohit, "0.00", "400.00"))

        report = get_report_data(self.rahul, self.NOVEMBER)

        self.assertEqual(report["summary"]["total"], money("0.00"))
        self.assertEqual(report["expense_count"], 0)

    def test_another_users_custom_category_name_never_appears(self):
        """A private category name stays private."""
        secret = Category.objects.create(name="Secret category", owner=self.aman)
        create_personal_expense(self.aman, category=secret, date=date(2026, 11, 5))

        response = self.open_report("report_monthly", date="2026-11-15")

        self.assertNotContains(response, "Secret category")

    def test_script_in_a_category_name_is_escaped(self):
        """User input is never rendered as HTML."""
        category = Category.objects.create(name="<script>alert(1)</script>", owner=self.rahul)
        create_personal_expense(self.rahul, category=category, date=date(2026, 11, 5))

        response = self.open_report("report_monthly", date="2026-11-15")

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
