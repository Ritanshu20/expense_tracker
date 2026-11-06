"""Tests for the REST API: login, CRUD, validation, isolation and settlement output."""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from expenses.models import (
    Budget,
    Category,
    Expense,
    ExpenseParticipant,
    ExpenseType,
    SettlementPayment,
)

from .helpers import create_expense, create_personal_expense, create_user, default_category

STRONG_PASSWORD = "correct-horse-battery-42"


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


class ApiTestCase(APITestCase):
    """Three friends and an outsider, with Rahul logged in through a token."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rohit = create_user("rohit")
        cls.outsider = create_user("outsider")
        cls.food = default_category()
        cls.list_url = reverse("api_expense_list")

    def setUp(self):
        self.login_as(self.rahul)

    def login_as(self, user: User) -> None:
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def detail_url(self, expense) -> str:
        return reverse("api_expense_detail", args=[expense.pk])

    def personal_body(self, **overrides) -> dict:
        body = {
            "title": "Lunch",
            "amount": "250.50",
            "date": "2026-11-05",
            "category": self.food.pk,
            "payment_method": "upi",
            "expense_type": "personal",
        }
        body.update(overrides)
        return body

    def shared_body(self, **overrides) -> dict:
        body = {
            "title": "Dinner",
            "amount": "3000.00",
            "date": "2026-11-10",
            "category": self.food.pk,
            "payment_method": "upi",
            "expense_type": "shared",
            "split_type": "equal",
            "participant_rows": [
                {"person": "rahul", "paid_amount": "1500.00"},
                {"person": "aman@example.com", "paid_amount": "1000.00"},
                {"person": "ROHIT", "paid_amount": "500.00"},
            ],
        }
        body.update(overrides)
        return body

    def post_expense(self, body: dict):
        return self.client.post(self.list_url, body, format="json")

    def create_dinner(self, created_by=None) -> Expense:
        expense = create_expense(
            created_by or self.rahul,
            title="Dinner",
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            expense_type=ExpenseType.SHARED,
        )
        add_rows(
            expense,
            (self.rahul, "1500.00", "1000.00"),
            (self.aman, "1000.00", "1000.00"),
            (self.rohit, "500.00", "1000.00"),
        )
        return expense

    def assert_nothing_was_saved(self):
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(ExpenseParticipant.objects.count(), 0)


class ApiLoginTests(ApiTestCase):
    def test_every_endpoint_answers_an_anonymous_request_with_a_401(self):
        """An API client gets a 401, not a redirect to the HTML login page."""
        self.client.credentials()
        expense = create_personal_expense(self.rahul)
        urls = (
            self.list_url,
            self.detail_url(expense),
            reverse("api_dashboard"),
            reverse("api_report_monthly"),
            reverse("api_settlements"),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_anonymous_request_cannot_write(self):
        """A 401 also covers POST, PUT and DELETE, and nothing changes."""
        self.client.credentials()
        expense = create_personal_expense(self.rahul, title="Mine")

        self.assertEqual(self.post_expense(self.personal_body()).status_code, 401)
        self.assertEqual(self.client.delete(self.detail_url(expense)).status_code, 401)
        self.assertEqual(Expense.objects.count(), 1)

    def test_token_endpoint_gives_a_token_for_the_right_password(self):
        """An API client exchanges a username and password for a token that works."""
        User.objects.create_user("meera", password=STRONG_PASSWORD)
        self.client.credentials()

        response = self.client.post(
            reverse("api_token"), {"username": "meera", "password": STRONG_PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    def test_token_endpoint_refuses_a_wrong_password(self):
        """No token without the right password."""
        User.objects.create_user("meera", password=STRONG_PASSWORD)
        self.client.credentials()

        response = self.client.post(
            reverse("api_token"), {"username": "meera", "password": "wrong"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("token", response.data)

    def test_invalid_token_gets_a_401(self):
        """A made-up token opens nothing."""
        self.client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")

        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_session_login_works_for_the_browser(self):
        """A logged-in website user can use the API too."""
        client = APIClient()
        client.force_login(self.rahul)

        self.assertEqual(client.get(self.list_url).status_code, 200)

    def test_session_login_needs_a_csrf_token_to_write(self):
        """CSRF protection still applies to cookie logins."""
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.rahul)

        response = client.post(self.list_url, self.personal_body(), format="json")

        self.assertEqual(response.status_code, 403)
        self.assert_nothing_was_saved()


class ApiDocsTests(ApiTestCase):
    def test_schema_generates_with_no_errors_or_warnings(self):
        """A view the schema cannot describe would fail here."""
        output = StringIO()

        call_command("spectacular", "--validate", "--fail-on-warn", stdout=output)

        self.assertIn("/api/expenses/", output.getvalue())
        self.assertIn("/api/settlements/", output.getvalue())

    def test_swagger_page_opens_for_a_logged_in_user(self):
        """The docs page and its schema load."""
        client = APIClient()
        client.force_login(self.rahul)

        self.assertEqual(client.get(reverse("api_docs")).status_code, 200)
        self.assertEqual(client.get(reverse("api_schema")).status_code, 200)

    def test_docs_are_not_public(self):
        """An anonymous visitor gets a 401 from the docs page and the schema."""
        client = APIClient()

        self.assertEqual(client.get(reverse("api_docs")).status_code, 401)
        self.assertEqual(client.get(reverse("api_schema")).status_code, 401)


class ApiCreateTests(ApiTestCase):
    def test_personal_expense_gets_one_row_for_the_creator(self):
        """RULE: paid_amount = share_amount = amount, the same as on the website."""
        response = self.post_expense(self.personal_body())

        self.assertEqual(response.status_code, 201)
        expense = Expense.objects.get()
        self.assertEqual(expense.created_by, self.rahul)
        self.assertEqual(expense.expense_type, ExpenseType.PERSONAL)
        row = expense.participants.get()
        self.assertEqual(row.user, self.rahul)
        self.assertEqual(row.paid_amount, money("250.50"))
        self.assertEqual(row.share_amount, money("250.50"))
        self.assertEqual(response.json()["my_share"], "250.50")

    def test_shared_expense_is_split_by_the_same_engine_as_the_website(self):
        """Paid 1500/1000/500, equal split of 3000: a share of 1000 each."""
        response = self.post_expense(self.shared_body())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.data["participants"],
            [
                {"username": "rahul", "paid_amount": "1500.00", "share_amount": "1000.00"},
                {"username": "aman", "paid_amount": "1000.00", "share_amount": "1000.00"},
                {"username": "rohit", "paid_amount": "500.00", "share_amount": "1000.00"},
            ],
        )

    def test_equal_split_keeps_every_paisa(self):
        """100 between 3 gives 33.34, 33.33 and 33.33."""
        response = self.post_expense(
            self.shared_body(
                amount="100.00",
                participant_rows=[
                    {"person": "rahul", "paid_amount": "100.00"},
                    {"person": "aman"},
                    {"person": "rohit"},
                ],
            )
        )

        shares = [row["share_amount"] for row in response.data["participants"]]
        self.assertEqual(shares, ["33.34", "33.33", "33.33"])

    def test_custom_split(self):
        """The shares are what the client sent."""
        response = self.post_expense(
            self.shared_body(
                amount="1000.00",
                split_type="custom",
                participant_rows=[
                    {"person": "rahul", "paid_amount": "1000.00", "share_value": "700.00"},
                    {"person": "aman", "share_value": "300.00"},
                ],
            )
        )

        self.assertEqual(response.status_code, 201)
        shares = [row["share_amount"] for row in response.data["participants"]]
        self.assertEqual(shares, ["700.00", "300.00"])

    def test_percentage_split(self):
        """The shares come from the percentages."""
        response = self.post_expense(
            self.shared_body(
                amount="1000.00",
                split_type="percentage",
                participant_rows=[
                    {"person": "rahul", "paid_amount": "1000.00", "share_value": "60"},
                    {"person": "aman", "share_value": "40"},
                ],
            )
        )

        self.assertEqual(response.status_code, 201)
        shares = [row["share_amount"] for row in response.data["participants"]]
        self.assertEqual(shares, ["600.00", "400.00"])

    def test_creator_in_the_request_is_ignored(self):
        """An expense cannot be created in someone else's name."""
        self.post_expense(self.personal_body(created_by=self.aman.pk))

        self.assertEqual(Expense.objects.get().created_by, self.rahul)

    def test_response_does_not_reveal_emails(self):
        """Adding someone by email does not put emails in the response."""
        response = self.post_expense(self.shared_body())

        self.assertNotIn("@example.com", str(response.data))


class ApiValidationTests(ApiTestCase):
    def assert_refused(self, response, text: str = ""):
        self.assertEqual(response.status_code, 400)
        self.assertIn(text, str(response.data))
        self.assert_nothing_was_saved()

    def test_paid_amounts_that_do_not_add_up_save_nothing(self):
        """Paid must equal the bill."""
        body = self.shared_body(
            participant_rows=[{"person": "rahul", "paid_amount": "1.00"}, {"person": "aman"}]
        )

        self.assert_refused(self.post_expense(body), "Paid amounts must add up")

    def test_custom_shares_that_do_not_add_up_save_nothing(self):
        """Shares must equal the bill."""
        body = self.shared_body(
            amount="1000.00",
            split_type="custom",
            participant_rows=[
                {"person": "rahul", "paid_amount": "1000.00", "share_value": "700.00"},
                {"person": "aman", "share_value": "200.00"},
            ],
        )

        self.assert_refused(self.post_expense(body), "Custom shares must add up")

    def test_percentages_that_do_not_add_up_save_nothing(self):
        """Percentages must total exactly 100."""
        body = self.shared_body(
            amount="1000.00",
            split_type="percentage",
            participant_rows=[
                {"person": "rahul", "paid_amount": "1000.00", "share_value": "60"},
                {"person": "aman", "share_value": "30"},
            ],
        )

        self.assert_refused(self.post_expense(body), "exactly 100")

    def test_unknown_or_partial_person_is_refused(self):
        """RULE: exact match only, and the message reveals nothing more."""
        for person in ("nobody", "rah", "@example.com"):
            with self.subTest(person=person):
                body = self.shared_body(participant_rows=[{"person": person}])
                self.assert_refused(self.post_expense(body), "No user found")

    def test_same_person_twice_is_refused(self):
        """Participants must be distinct."""
        body = self.shared_body(
            participant_rows=[
                {"person": "rahul", "paid_amount": "3000.00"},
                {"person": "RAHUL@example.com"},
            ]
        )

        self.assert_refused(self.post_expense(body), "only once")

    def test_shared_expense_without_participants_is_refused(self):
        """A shared expense must have someone in it."""
        body = self.shared_body()
        del body["participant_rows"]

        self.assert_refused(self.post_expense(body), "At least one participant")

    def test_another_users_category_is_refused(self):
        """IDOR check: a private category id cannot be used."""
        secret = Category.objects.create(name="Secret", owner=self.aman)

        self.assert_refused(self.post_expense(self.personal_body(category=secret.pk)))

    def test_invalid_fields_are_refused(self):
        """The same field rules apply as on the website."""
        invalid_bodies = (
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "abc"},
            {"amount": "1.234"},
            {"amount": "100000000000"},
            {"title": ""},
            {"date": "nonsense"},
            {"payment_method": "bitcoin"},
            {"expense_type": "business"},
            {"split_type": "shares"},
            {"category": 999999},
        )
        for overrides in invalid_bodies:
            with self.subTest(overrides=overrides):
                self.assert_refused(self.post_expense(self.personal_body(**overrides)))

    def test_negative_amounts_in_a_row_are_refused(self):
        """Nobody pays a negative amount."""
        body = self.shared_body(
            participant_rows=[
                {"person": "rahul", "paid_amount": "3200.00"},
                {"person": "aman", "paid_amount": "-200.00"},
            ]
        )

        self.assert_refused(self.post_expense(body))


class ApiReadTests(ApiTestCase):
    def test_list_shows_only_the_users_own_expenses_newest_first(self):
        """Isolation: Aman's private expense is not in Rahul's list."""
        create_personal_expense(self.rahul, title="Older", date=date(2026, 1, 1))
        create_personal_expense(self.rahul, title="Newer", date=date(2026, 2, 1))
        create_personal_expense(self.aman, title="Not mine")

        response = self.client.get(self.list_url)

        self.assertEqual(response.data["count"], 2)
        titles = [expense["title"] for expense in response.data["results"]]
        self.assertEqual(titles, ["Newer", "Older"])

    def test_list_uses_the_same_filters_as_the_website(self):
        """Search and date filters work."""
        create_personal_expense(self.rahul, title="Team dinner", date=date(2026, 11, 1))
        create_personal_expense(self.rahul, title="Taxi", date=date(2026, 11, 2))

        response = self.client.get(self.list_url, {"q": "dinner", "date_to": "2026-11-30"})

        self.assertEqual([row["title"] for row in response.data["results"]], ["Team dinner"])

    def test_invalid_filter_gives_a_400(self):
        """A bad date is reported to the client."""
        response = self.client.get(self.list_url, {"date_from": "nonsense"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_from", response.data)

    def test_list_is_paginated(self):
        """A long list comes in pages."""
        for number in range(25):
            create_personal_expense(self.rahul, title=f"Expense {number}")

        first_page = self.client.get(self.list_url)
        second_page = self.client.get(self.list_url, {"page": 2})

        self.assertEqual(first_page.data["count"], 25)
        self.assertEqual(len(first_page.data["results"]), 20)
        self.assertEqual(len(second_page.data["results"]), 5)

    def test_detail_shows_participants_and_my_share(self):
        """Money is sent as strings, so it stays exact."""
        expense = self.create_dinner()

        response = self.client.get(self.detail_url(expense))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["amount"], "3000.00")
        self.assertEqual(response.json()["my_share"], "1000.00")
        self.assertEqual(len(response.data["participants"]), 3)

    def test_my_share_is_empty_for_a_creator_who_does_not_take_part(self):
        """Creating an expense is not the same as taking part in it."""
        expense = create_expense(self.rahul, expense_type=ExpenseType.SHARED)
        add_rows(expense, (self.aman, "100.00", "100.00"))

        response = self.client.get(self.detail_url(expense))

        self.assertIsNone(response.data["my_share"])

    def test_list_query_count_does_not_grow_with_the_number_of_rows(self):
        """No N+1: token, count, rows, participants and their users."""
        for _ in range(10):
            self.create_dinner()

        with self.assertNumQueries(5):
            response = self.client.get(self.list_url)

        self.assertEqual(len(response.data["results"]), 10)


class ApiUpdateAndDeleteTests(ApiTestCase):
    def test_put_replaces_the_expense_and_recalculates_shares(self):
        """A new amount with an equal split gives new shares."""
        expense = self.create_dinner()

        response = self.client.put(
            self.detail_url(expense),
            self.shared_body(
                title="Changed",
                amount="600.00",
                participant_rows=[
                    {"person": "rahul", "paid_amount": "600.00"},
                    {"person": "aman"},
                ],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Changed")
        self.assertEqual(
            response.data["participants"],
            [
                {"username": "rahul", "paid_amount": "600.00", "share_amount": "300.00"},
                {"username": "aman", "paid_amount": "0.00", "share_amount": "300.00"},
            ],
        )
        self.assertFalse(expense.participants.filter(user=self.rohit).exists())

    def test_invalid_put_changes_nothing(self):
        """A refused update leaves every number as it was."""
        expense = self.create_dinner()

        response = self.client.put(
            self.detail_url(expense),
            self.shared_body(
                title="Changed",
                participant_rows=[{"person": "rahul", "paid_amount": "1.00"}],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        expense.refresh_from_db()
        self.assertEqual(expense.title, "Dinner")
        self.assertEqual(expense.participants.count(), 3)

    def test_delete_removes_the_expense_and_its_rows(self):
        """Nothing is left behind."""
        expense = self.create_dinner()

        response = self.client.delete(self.detail_url(expense))

        self.assertEqual(response.status_code, 204)
        self.assert_nothing_was_saved()

    def test_patch_is_not_offered(self):
        """The API has GET, PUT and DELETE on one expense."""
        expense = self.create_dinner()

        response = self.client.patch(self.detail_url(expense), {"title": "X"}, format="json")

        self.assertEqual(response.status_code, 405)


class ApiPermissionTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.expense = self.create_dinner(created_by=self.aman)
        self.url = self.detail_url(self.expense)

    def test_participant_can_view(self):
        """Rahul takes part in Aman's expense."""
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_participant_cannot_replace_or_delete(self):
        """RULE: only the creator changes it. The participant knows it exists, so it is a 403."""
        response = self.client.put(self.url, self.personal_body(), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.delete(self.url).status_code, 403)
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.title, "Dinner")

    def test_outsider_gets_a_404(self):
        """IDOR check: an outsider cannot learn that the expense exists."""
        self.login_as(self.outsider)

        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.put(self.url, self.personal_body(), format="json").status_code, 404
        )
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        self.assertTrue(Expense.objects.filter(pk=self.expense.pk).exists())

    def test_outsider_list_is_empty(self):
        """Isolation also applies to the list."""
        self.login_as(self.outsider)

        self.assertEqual(self.client.get(self.list_url).data["count"], 0)


class ApiDashboardTests(ApiTestCase):
    def test_dashboard_gives_the_hand_calculated_numbers(self):
        """Spending: 100 + 1000. Paid: 100 + 1500. Rohit owes Rahul 500."""
        self.create_dinner()
        create_personal_expense(self.rahul, amount=money("100.00"), date=date(2026, 11, 5))
        today = timezone.localdate()
        Budget.objects.create(
            user=self.rahul, year=today.year, month=today.month, amount=money("9000.00")
        )

        data = self.client.get(reverse("api_dashboard")).data

        self.assertEqual(data["summary"]["total"], "1100.00")
        self.assertEqual(data["summary"]["paid_by_me"], "1600.00")
        self.assertEqual(data["summary"]["personal"], "100.00")
        self.assertEqual(data["summary"]["shared"], "1000.00")
        self.assertEqual(
            data["settlements"]["owed_to_you"],
            [{"from_user": "rohit", "to_user": "rahul", "amount": "500.00"}],
        )
        self.assertEqual(data["budget"]["amount"], "9000.00")
        self.assertEqual(len(data["recent_expenses"]), 2)

    def test_user_with_no_data_gets_zeros_and_no_budget(self):
        """An empty dashboard is valid."""
        self.login_as(self.outsider)

        data = self.client.get(reverse("api_dashboard")).data

        self.assertEqual(data["summary"]["total"], "0.00")
        self.assertIsNone(data["budget"])
        self.assertEqual(data["recent_expenses"], [])

    def test_dashboard_does_not_include_other_users_data(self):
        """Isolation."""
        create_personal_expense(self.aman, amount=money("7777.00"))

        data = self.client.get(reverse("api_dashboard")).data

        self.assertEqual(data["summary"]["total"], "0.00")
        self.assertNotIn("7777", str(data))


class ApiMonthlyReportTests(ApiTestCase):
    def report(self, **query):
        return self.client.get(reverse("api_report_monthly"), query)

    def test_report_covers_the_month_of_the_given_date_with_both_ends(self):
        """1 Nov and 30 Nov are in. 31 Oct and 1 Dec are out."""
        for day in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 30), date(2026, 12, 1)):
            create_personal_expense(self.rahul, amount=money("1.00"), date=day)

        data = self.report(date="2026-11-15").data

        self.assertEqual(data["start"], "2026-11-01")
        self.assertEqual(data["end"], "2026-11-30")
        self.assertEqual(data["summary"]["total"], "2.00")
        self.assertEqual(data["expense_count"], 2)

    def test_report_gives_breakdowns_and_settlement_info(self):
        """The dinner in November."""
        self.create_dinner()

        data = self.report(date="2026-11-15").data

        self.assertEqual(data["category_totals"], [{"label": "Food", "total": "1000.00"}])
        self.assertEqual(data["payment_method_totals"], [{"label": "Cash", "total": "1000.00"}])
        self.assertEqual(len(data["person_totals"]), 3)
        self.assertEqual(data["settlements"]["total_owed_to_you"], "500.00")

    def test_invalid_date_gives_a_400(self):
        """A bad date is reported to the client."""
        response = self.report(date="nonsense")

        self.assertEqual(response.status_code, 400)
        self.assertIn("date", response.data)

    def test_report_without_a_date_uses_the_current_month(self):
        """The date is optional."""
        self.assertEqual(self.report().status_code, 200)

    def test_report_does_not_include_other_users_data(self):
        """Isolation."""
        create_personal_expense(self.aman, amount=money("7777.00"), date=date(2026, 11, 5))

        data = self.report(date="2026-11-15").data

        self.assertEqual(data["summary"]["total"], "0.00")


class ApiSettlementsTests(ApiTestCase):
    def test_output_shows_who_owes_whom(self):
        """The acceptance scenario: Rohit owes Rahul 500."""
        self.create_dinner()

        data = self.client.get(reverse("api_settlements")).data

        self.assertEqual(
            data["settlements"]["owed_to_you"],
            [{"from_user": "rohit", "to_user": "rahul", "amount": "500.00"}],
        )
        self.assertEqual(data["settlements"]["you_owe"], [])
        self.assertEqual(data["payments"], [])

    def test_output_is_updated_by_a_recorded_payment(self):
        """After Rohit pays 200, he owes 300, and the payment is listed."""
        self.create_dinner()
        SettlementPayment.objects.create(
            from_user=self.rohit,
            to_user=self.rahul,
            created_by=self.rohit,
            amount=money("200.00"),
            date=date(2026, 11, 12),
        )

        data = self.client.get(reverse("api_settlements")).data

        self.assertEqual(data["settlements"]["total_owed_to_you"], "300.00")
        self.assertEqual(data["payments"][0]["from_user"], "rohit")
        self.assertEqual(data["payments"][0]["amount"], "200.00")

    def test_outsider_sees_no_debts_or_payments(self):
        """Isolation."""
        self.create_dinner()
        SettlementPayment.objects.create(
            from_user=self.rohit,
            to_user=self.rahul,
            created_by=self.rohit,
            amount=money("200.00"),
            date=date(2026, 11, 12),
        )
        self.login_as(self.outsider)

        data = self.client.get(reverse("api_settlements")).data

        self.assertEqual(data["settlements"]["owed_to_you"], [])
        self.assertEqual(data["settlements"]["you_owe"], [])
        self.assertEqual(data["payments"], [])


class ApiUsesTheSameRulesAsTheWebsiteTests(TestCase):
    def test_expense_created_through_the_api_appears_on_the_website(self):
        """The API and the website share one set of tables and rules."""
        rahul = create_user("rahul")
        token = Token.objects.create(user=rahul)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        api.post(
            reverse("api_expense_list"),
            {
                "title": "From the API",
                "amount": "10.00",
                "date": "2026-11-05",
                "category": default_category().pk,
                "payment_method": "cash",
                "expense_type": "personal",
            },
            format="json",
        )

        self.client.force_login(rahul)
        self.assertContains(self.client.get(reverse("expense_list")), "From the API")
