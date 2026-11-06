"""Tests for the home page and the dashboard, with hand-calculated expected numbers."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from expenses.models import Category, ExpenseParticipant, ExpenseType, PaymentMethod
from expenses.selectors import (
    get_category_totals,
    get_month_range,
    get_payment_method_totals,
    get_person_totals,
    get_settlement_summary,
    get_spending_summary,
    get_week_range,
    get_year_range,
)
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


class HomePageTests(SimpleTestCase):
    def test_home_page_is_public_and_returns_200(self):
        """An anonymous visitor can open the landing page."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Smart Expense Tracker")

    def test_home_page_is_rendered_with_the_base_template(self):
        """The landing page extends base.html so it gets the shared layout."""
        response = self.client.get(reverse("home"))

        self.assertTemplateUsed(response, "dashboard/home.html")
        self.assertTemplateUsed(response, "base.html")

    def test_home_page_lives_at_the_site_root(self):
        """The named route 'home' resolves to '/'."""
        self.assertEqual(reverse("home"), "/")

    def test_custom_error_pages_are_used(self):
        """A missing page uses the project's layout, not Django's plain page."""
        response = self.client.get("/this-page-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")

    def test_anonymous_visitor_sees_login_and_register_links(self):
        """The navigation offers a way in, and no logout button."""
        response = self.client.get(reverse("home"))

        self.assertContains(response, reverse("login"))
        self.assertContains(response, reverse("register"))
        self.assertNotContains(response, reverse("logout"))


class NavigationTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("rahul"))

    def test_logged_in_user_sees_profile_and_logout(self):
        """The navigation changes after logging in, and logout is a POST form."""
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, reverse("profile"))
        self.assertContains(response, f'<form method="post" action="{reverse("logout")}">')
        self.assertNotContains(response, f'href="{reverse("login")}"')

    def test_home_page_sends_a_logged_in_user_to_the_dashboard(self):
        """The landing page is for visitors."""
        self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"))


class DateRangeTests(SimpleTestCase):
    def test_week_runs_from_monday_to_sunday_for_every_day_of_that_week(self):
        """RULE: both the Monday and the Sunday belong to the same week."""
        for day in (9, 10, 11, 12, 13, 14, 15):
            with self.subTest(day=day):
                week = get_week_range(date(2026, 11, day))
                self.assertEqual(week, (date(2026, 11, 9), date(2026, 11, 15)))

    def test_week_can_cross_a_month_and_a_year(self):
        """Thursday 31 Dec 2026 is in the week of Monday 28 Dec to Sunday 3 Jan."""
        week = get_week_range(date(2026, 12, 31))

        self.assertEqual(week, (date(2026, 12, 28), date(2027, 1, 3)))

    def test_month_runs_from_its_first_to_its_last_day(self):
        """Months have different lengths, including February in a leap year."""
        cases = (
            (date(2026, 11, 11), date(2026, 11, 1), date(2026, 11, 30)),
            (date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31)),
            (date(2026, 2, 10), date(2026, 2, 1), date(2026, 2, 28)),
            (date(2028, 2, 10), date(2028, 2, 1), date(2028, 2, 29)),
        )
        for day, first, last in cases:
            with self.subTest(day=day):
                self.assertEqual(get_month_range(day), (first, last))

    def test_year_runs_from_1_january_to_31_december(self):
        """A year is the calendar year."""
        self.assertEqual(get_year_range(date(2026, 6, 15)), (date(2026, 1, 1), date(2026, 12, 31)))

    def test_local_date_follows_india_not_utc(self):
        """At 19:00 UTC on 10 Nov it is already 00:30 on 11 Nov in Asia/Kolkata."""
        moment = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)

        with mock.patch("django.utils.timezone.now", return_value=moment):
            self.assertEqual(timezone.localdate(), date(2026, 11, 11))


class DashboardDataTestCase(TestCase):
    """Controlled data for Rahul, with every expected number worked out by hand."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rohit = create_user("rohit")
        food = default_category("Food")
        travel = default_category("Travel")
        cash = PaymentMethod.CASH
        upi = PaymentMethod.UPI

        # Rahul's personal expenses, in and around this week and this month.
        for day, amount, category, method in (
            (date(2026, 11, 11), "100.00", food, cash),  # today
            (date(2026, 11, 9), "50.00", travel, upi),  # Monday of this week
            (date(2026, 11, 8), "30.00", food, cash),  # Sunday of last week
            (date(2026, 11, 1), "20.00", food, upi),  # first day of this month
            (date(2026, 10, 31), "10.00", food, cash),  # last day of last month
        ):
            create_personal_expense(
                cls.rahul, amount=money(amount), date=day, category=category, payment_method=method
            )

        # The dinner: Rahul paid 1500 and his share is 1000.
        cls.dinner = create_expense(
            cls.aman,
            title="Dinner",
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            category=food,
            payment_method=upi,
            expense_type=ExpenseType.SHARED,
        )
        add_rows(
            cls.dinner,
            (cls.rahul, "1500.00", "1000.00"),
            (cls.aman, "1000.00", "1000.00"),
            (cls.rohit, "500.00", "1000.00"),
        )

        # None of these three may count for Rahul.
        create_personal_expense(cls.aman, title="Aman private", amount=money("7777.00"))
        without_rahul = create_expense(
            cls.aman,
            title="Without Rahul",
            amount=money("800.00"),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(without_rahul, (cls.aman, "800.00", "400.00"), (cls.rohit, "0.00", "400.00"))
        created_only = create_expense(
            cls.rahul,
            title="Created only",
            amount=money("900.00"),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(created_only, (cls.aman, "900.00", "450.00"), (cls.rohit, "0.00", "450.00"))


class SpendingSummaryTests(DashboardDataTestCase):
    def setUp(self):
        self.summary = get_spending_summary(self.rahul, TODAY)

    def test_total_is_the_sum_of_my_shares_not_of_what_i_paid(self):
        """100 + 50 + 30 + 20 + 10 personal, plus 1000 from the dinner, is 1210."""
        self.assertEqual(self.summary["total"], money("1210.00"))

    def test_paid_by_me_is_shown_separately(self):
        """100 + 50 + 30 + 20 + 10 personal, plus 1500 paid for the dinner, is 1710."""
        self.assertEqual(self.summary["paid_by_me"], money("1710.00"))

    def test_today_counts_only_todays_date(self):
        """Only the 100 from 11 Nov."""
        self.assertEqual(self.summary["today"], money("100.00"))

    def test_this_week_includes_monday_and_excludes_the_sunday_before(self):
        """100 + 50 (Monday) + 1000 (dinner on Tuesday). The 30 from Sunday 8 Nov is out."""
        self.assertEqual(self.summary["this_week"], money("1150.00"))

    def test_this_month_includes_the_first_and_excludes_the_day_before(self):
        """100 + 50 + 30 + 20 (the 1st) + 1000. The 10 from 31 Oct is out."""
        self.assertEqual(self.summary["this_month"], money("1200.00"))

    def test_personal_and_shared_add_up_to_the_total(self):
        """210 personal and 1000 shared."""
        self.assertEqual(self.summary["personal"], money("210.00"))
        self.assertEqual(self.summary["shared"], money("1000.00"))
        self.assertEqual(self.summary["personal"] + self.summary["shared"], self.summary["total"])

    def test_sunday_at_the_end_of_the_week_counts_in_that_week(self):
        """RULE: the last day of the week is included."""
        create_personal_expense(self.rahul, amount=money("5.00"), date=date(2026, 11, 15))

        summary = get_spending_summary(self.rahul, TODAY)

        self.assertEqual(summary["this_week"], money("1155.00"))

    def test_last_day_of_the_month_counts_in_that_month(self):
        """RULE: the last day of the month is included."""
        create_personal_expense(self.rahul, amount=money("5.00"), date=date(2026, 11, 30))

        summary = get_spending_summary(self.rahul, TODAY)

        self.assertEqual(summary["this_month"], money("1205.00"))

    def test_user_with_no_expenses_sees_zeros(self):
        """Empty totals are 0.00 instead of None."""
        summary = get_spending_summary(create_user("new_user"), TODAY)

        for name, value in summary.items():
            with self.subTest(name=name):
                self.assertEqual(value, money("0.00"))


class SpendingBreakdownTests(DashboardDataTestCase):
    def test_category_totals_use_my_share(self):
        """Food: 100 + 30 + 20 + 10 + 1000 = 1160. Travel: 50."""
        self.assertEqual(
            get_category_totals(self.rahul),
            [
                {"label": "Food", "total": money("1160.00")},
                {"label": "Travel", "total": money("50.00")},
            ],
        )

    def test_payment_method_totals_use_my_share_and_readable_labels(self):
        """UPI: 50 + 20 + 1000 = 1070. Cash: 100 + 30 + 10 = 140."""
        self.assertEqual(
            get_payment_method_totals(self.rahul),
            [
                {"label": "UPI", "total": money("1070.00")},
                {"label": "Cash", "total": money("140.00")},
            ],
        )

    def test_person_totals_cover_only_expenses_i_take_part_in(self):
        """Rahul's line equals his total. Aman and Rohit show only the shared dinner."""
        self.assertEqual(
            get_person_totals(self.rahul),
            [
                {"label": "rahul", "total": money("1210.00")},
                {"label": "aman", "total": money("1000.00")},
                {"label": "rohit", "total": money("1000.00")},
            ],
        )

    def test_a_zero_share_does_not_appear_as_a_line(self):
        """Someone who consumed nothing has nothing to report."""
        expense = create_expense(
            self.rahul,
            amount=money("10.00"),
            category=default_category("Medical"),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(expense, (self.rahul, "10.00", "0.00"), (self.aman, "0.00", "10.00"))

        labels = [row["label"] for row in get_category_totals(self.rahul)]

        self.assertNotIn("Medical", labels)


class SettlementSummaryTests(DashboardDataTestCase):
    def test_dinner_means_rohit_owes_rahul_500(self):
        """The acceptance scenario: paid 1500/1000/500 with a share of 1000 each."""
        summary = get_settlement_summary(self.rahul)

        self.assertEqual(summary.owed_to_you, [("rohit", "rahul", money("500.00"))])
        self.assertEqual(summary.you_owe, [])
        self.assertEqual(summary.total_owed_to_you, money("500.00"))
        self.assertEqual(summary.total_you_owe, money("0.00"))

    def test_rohit_sees_the_same_debt_from_his_side(self):
        """Both people see the same number."""
        summary = get_settlement_summary(self.rohit)

        self.assertIn(("rohit", "rahul", money("500.00")), summary.you_owe)

    def test_someone_with_a_zero_balance_owes_nothing_from_that_expense(self):
        """Aman paid exactly his share of the dinner."""
        summary = get_settlement_summary(self.aman)

        self.assertNotIn("rahul", [debt.to_user for debt in summary.you_owe])

    def test_debts_in_both_directions_between_two_people_are_netted(self):
        """Rahul is owed 500 by Rohit, and now owes Rohit 200: Rohit owes Rahul 300."""
        trip = create_expense(self.rohit, amount=money("400.00"), expense_type=ExpenseType.SHARED)
        add_rows(trip, (self.rohit, "400.00", "200.00"), (self.rahul, "0.00", "200.00"))

        summary = get_settlement_summary(self.rahul)

        self.assertEqual(summary.owed_to_you, [("rohit", "rahul", money("300.00"))])
        self.assertEqual(summary.you_owe, [])

    def test_debts_that_cancel_out_disappear(self):
        """Rahul is owed 500 by Rohit and owes Rohit 500: nobody owes anybody."""
        trip = create_expense(self.rohit, amount=money("1000.00"), expense_type=ExpenseType.SHARED)
        add_rows(trip, (self.rohit, "1000.00", "500.00"), (self.rahul, "0.00", "500.00"))

        summary = get_settlement_summary(self.rahul)

        self.assertEqual(summary.owed_to_you, [])
        self.assertEqual(summary.you_owe, [])

    def test_debts_between_other_people_are_not_shown(self):
        """A debt between Aman and Rohit is not Rahul's business, even on a shared expense."""
        split = create_expense(self.rahul, amount=money("300.00"), expense_type=ExpenseType.SHARED)
        add_rows(
            split,
            (self.rahul, "100.00", "100.00"),
            (self.aman, "200.00", "100.00"),
            (self.rohit, "0.00", "100.00"),
        )

        summary = get_settlement_summary(self.rahul)

        for debt in summary.you_owe + summary.owed_to_you:
            self.assertIn("rahul", (debt.from_user, debt.to_user))

    def test_deleting_an_expense_recalculates_the_balance(self):
        """Nothing is stored, so removing the dinner removes the debt."""
        self.dinner.delete()

        self.assertEqual(get_settlement_summary(self.rahul).owed_to_you, [])

    def test_user_with_no_shared_expenses_owes_nothing(self):
        """Empty is valid."""
        summary = get_settlement_summary(create_user("new_user"))

        self.assertEqual(summary.total_owed_to_you, money("0.00"))
        self.assertEqual(summary.total_you_owe, money("0.00"))


class DashboardPageTests(DashboardDataTestCase):
    def open_dashboard(self):
        with mock.patch("dashboard.views.timezone.localdate", return_value=TODAY):
            return self.client.get(reverse("dashboard"))

    def setUp(self):
        self.client.force_login(self.rahul)

    def test_anonymous_visitor_is_sent_to_login(self):
        """The dashboard holds private data."""
        self.client.logout()
        url = reverse("dashboard")

        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_login_lands_on_the_dashboard(self):
        """It is the first page a logged-in user sees."""
        User.objects.create_user("meera", password="correct-horse-battery-42")
        self.client.logout()

        response = self.client.post(
            reverse("login"), {"username": "meera", "password": "correct-horse-battery-42"}
        )

        self.assertRedirects(response, reverse("dashboard"))

    def test_page_shows_the_hand_calculated_numbers(self):
        """Every card shows the expected total."""
        response = self.open_dashboard()

        for number in ("1210.00", "1710.00", "1150.00", "1200.00", "1160.00", "1070.00"):
            with self.subTest(number=number):
                self.assertContains(response, number)
        self.assertContains(response, "rohit owes you")

    def test_page_shows_recent_expenses_with_my_share_newest_first(self):
        """Recent expenses are limited and sorted."""
        response = self.open_dashboard()

        recent = list(response.context["recent_expenses"])
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0].date, date(2026, 11, 11))
        self.assertEqual(recent[1], self.dinner)
        self.assertEqual(recent[1].my_share, money("1000.00"))

    def test_page_never_shows_other_users_private_expenses(self):
        """Isolation: Aman's private expense and his expense without Rahul stay hidden."""
        response = self.open_dashboard()

        self.assertNotContains(response, "Aman private")
        self.assertNotContains(response, "Without Rahul")
        self.assertNotContains(response, "7777")

    def test_new_user_sees_zeros_and_friendly_messages(self):
        """An empty dashboard still loads."""
        self.client.force_login(create_user("new_user"))

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "0.00")
        self.assertContains(response, "Nobody owes anything")
        self.assertContains(response, "No expenses yet")

    def test_query_count_stays_fixed_however_much_data_there_is(self):
        """Session, user, summary, breakdowns, recent, settlements, payments and the budget."""
        for _ in range(20):
            create_personal_expense(self.rahul)

        with self.assertNumQueries(10):
            self.open_dashboard()

    def test_page_gives_the_charts_their_data_as_exact_strings(self):
        """The script reads the totals from JSON embedded in the page."""
        response = self.open_dashboard()

        self.assertContains(response, 'id="category-chart"')
        self.assertContains(response, 'id="payment-method-chart"')
        self.assertContains(response, "js/dashboard-charts.js")
        self.assertContains(response, '"total": "1160.00"')

    def test_new_user_gets_no_empty_charts(self):
        """With no spending there is nothing to draw."""
        self.client.force_login(create_user("new_user"))

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, 'id="category-chart"')
        self.assertNotContains(response, "<canvas")

    def test_script_in_a_category_name_cannot_break_out_of_the_chart_data(self):
        """SAFETY: json_script escapes the angle brackets, so user text stays data."""
        category = Category.objects.create(
            name="</script><script>alert(1)</script>", owner=self.rahul
        )
        create_personal_expense(self.rahul, category=category, amount=money("5.00"))

        response = self.open_dashboard()

        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_dashboard_only_accepts_get(self):
        """The page changes nothing, so other methods are refused."""
        self.assertEqual(self.client.post(reverse("dashboard")).status_code, 405)
