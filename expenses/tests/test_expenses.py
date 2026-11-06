"""Tests for personal expenses: CRUD, validation, search, filters, pagination and isolation."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from expenses.models import Category, Expense, ExpenseParticipant, ExpenseType, PaymentMethod
from expenses.services import save_personal_expense
from expenses.views import EXPENSES_PER_PAGE

from .helpers import create_expense, create_personal_expense, create_user, default_category


class ExpenseTestCase(TestCase):
    """Two users with Rahul logged in, and a helper for valid form data."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.food = default_category("Food")
        cls.travel = default_category("Travel")

    def setUp(self):
        self.client.force_login(self.rahul)

    def form_data(self, **overrides) -> dict:
        data = {
            "title": "Lunch",
            "description": "",
            "amount": "250.50",
            "date": "2026-11-01",
            "category": self.food.pk,
            "payment_method": "upi",
            "expense_type": "personal",
        }
        data.update(overrides)
        return data


class ExpenseUrlTests(ExpenseTestCase):
    def test_urls_match_the_agreed_addresses(self):
        """The addresses follow the project's URL list."""
        self.assertEqual(reverse("expense_list"), "/expenses/")
        self.assertEqual(reverse("expense_create"), "/expenses/add/")
        self.assertEqual(reverse("expense_detail", args=[5]), "/expenses/5/")
        self.assertEqual(reverse("expense_edit", args=[5]), "/expenses/5/edit/")
        self.assertEqual(reverse("expense_delete", args=[5]), "/expenses/5/delete/")

    def test_every_expense_page_sends_anonymous_visitors_to_login(self):
        """Login is required for all five pages."""
        expense = create_personal_expense(self.rahul)
        self.client.logout()
        urls = (
            reverse("expense_list"),
            reverse("expense_create"),
            reverse("expense_detail", args=[expense.pk]),
            reverse("expense_edit", args=[expense.pk]),
            reverse("expense_delete", args=[expense.pk]),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_navigation_links_to_expenses_when_logged_in(self):
        """The list can be reached from the navigation."""
        response = self.client.get(reverse("category_list"))

        self.assertContains(response, reverse("expense_list"))


class ExpenseCreateTests(ExpenseTestCase):
    def test_form_starts_with_todays_date(self):
        """The date defaults to today in Asia/Kolkata, the most common case."""
        response = self.client.get(reverse("expense_create"))

        self.assertEqual(response.context["form"].initial["date"], timezone.localdate())
        self.assertContains(response, timezone.localdate().isoformat())

    def test_valid_expense_is_saved_and_shown(self):
        """The details are stored exactly as typed and the user lands on the new expense."""
        response = self.client.post(
            reverse("expense_create"), self.form_data(description="With the team")
        )

        expense = Expense.objects.get()
        self.assertRedirects(response, reverse("expense_detail", args=[expense.pk]))
        self.assertEqual(expense.title, "Lunch")
        self.assertEqual(expense.description, "With the team")
        self.assertEqual(expense.amount, Decimal("250.50"))
        self.assertEqual(expense.date, date(2026, 11, 1))
        self.assertEqual(expense.category, self.food)
        self.assertEqual(expense.payment_method, PaymentMethod.UPI)

    def test_personal_expense_gets_one_participant_row_for_the_creator(self):
        """RULE: paid_amount = share_amount = amount, for the creator only."""
        self.client.post(reverse("expense_create"), self.form_data())

        expense = Expense.objects.get()
        self.assertEqual(expense.created_by, self.rahul)
        self.assertEqual(expense.expense_type, ExpenseType.PERSONAL)
        participant = expense.participants.get()
        self.assertEqual(participant.user, self.rahul)
        self.assertEqual(participant.paid_amount, Decimal("250.50"))
        self.assertEqual(participant.share_amount, Decimal("250.50"))

    def test_creator_in_the_submitted_data_is_ignored(self):
        """A user cannot create an expense in someone else's name."""
        self.client.post(reverse("expense_create"), self.form_data(created_by=self.aman.pk))

        expense = Expense.objects.get()
        self.assertEqual(expense.created_by, self.rahul)
        self.assertEqual(expense.participants.get().user, self.rahul)

    def test_user_can_use_their_own_custom_category(self):
        """Custom categories appear next to the default ones."""
        pets = Category.objects.create(name="Pets", owner=self.rahul)

        self.client.post(reverse("expense_create"), self.form_data(category=pets.pk))

        self.assertEqual(Expense.objects.get().category, pets)

    def test_another_users_category_cannot_be_used(self):
        """IDOR check: typing another user's category id is refused."""
        gym = Category.objects.create(name="Gym", owner=self.aman)

        response = self.client.post(reverse("expense_create"), self.form_data(category=gym.pk))

        self.assertContains(response, "Select a valid choice")
        self.assertEqual(Expense.objects.count(), 0)

    def test_another_users_category_is_not_offered_in_the_form(self):
        """Other users' category names stay private."""
        Category.objects.create(name="Gym", owner=self.aman)

        response = self.client.get(reverse("expense_create"))

        self.assertNotContains(response, "Gym")

    def test_invalid_input_is_rejected_and_saves_nothing(self):
        """Server-side validation covers every field, whatever the browser allows."""
        invalid_inputs = (
            {"title": ""},
            {"title": "   "},
            {"title": "x" * 201},
            {"amount": ""},
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "abc"},
            {"amount": "1.234"},
            {"amount": "100000000000"},
            {"amount": "NaN"},
            {"amount": "Infinity"},
            {"date": ""},
            {"date": "not-a-date"},
            {"date": "2026-02-30"},
            {"category": ""},
            {"category": "999999"},
            {"payment_method": ""},
            {"payment_method": "bitcoin"},
            {"expense_type": ""},
            {"expense_type": "business"},
        )
        for overrides in invalid_inputs:
            with self.subTest(overrides=overrides):
                response = self.client.post(reverse("expense_create"), self.form_data(**overrides))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(Expense.objects.count(), 0)
                self.assertEqual(ExpenseParticipant.objects.count(), 0)

    def test_largest_amount_the_column_can_store_is_accepted(self):
        """The limit is exactly what numeric(12, 2) can hold."""
        response = self.client.post(
            reverse("expense_create"), self.form_data(amount="9999999999.99")
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Expense.objects.get().amount, Decimal("9999999999.99"))

    def test_failure_while_saving_the_participant_row_leaves_no_expense_behind(self):
        """The transaction rolls back, so an expense never exists without its row."""
        expense = Expense(
            title="Lunch",
            amount=Decimal("100.00"),
            date=date(2026, 11, 1),
            category=self.food,
            payment_method=PaymentMethod.CASH,
        )

        with mock.patch.object(
            ExpenseParticipant.objects, "update_or_create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                save_personal_expense(expense, self.rahul)

        self.assertEqual(Expense.objects.count(), 0)

    def test_form_without_a_csrf_token_is_rejected(self):
        """CSRF protection is on for this form."""
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(reverse("expense_create"), self.form_data())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Expense.objects.count(), 0)


class ExpenseDetailTests(ExpenseTestCase):
    def test_creator_can_view_their_expense(self):
        """The page shows the details and the participant row."""
        expense = create_personal_expense(self.rahul, title="Dinner", amount=Decimal("123.45"))

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertContains(response, "Dinner")
        self.assertContains(response, "123.45")
        self.assertContains(response, "rahul")

    def test_creator_sees_edit_and_delete_links(self):
        """The creator is offered the actions they may take."""
        expense = create_personal_expense(self.rahul)

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertContains(response, reverse("expense_edit", args=[expense.pk]))
        self.assertContains(response, reverse("expense_delete", args=[expense.pk]))

    def test_another_users_expense_gives_a_404(self):
        """IDOR check: user B requesting user A's /expenses/<id>/ fails."""
        expense = create_personal_expense(self.aman)

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertEqual(response.status_code, 404)

    def test_expense_that_does_not_exist_looks_the_same_as_someone_elses(self):
        """Both give a 404, so ids cannot be probed."""
        response = self.client.get(reverse("expense_detail", args=[999999]))

        self.assertEqual(response.status_code, 404)

    def test_script_in_title_and_description_is_escaped_on_the_page(self):
        """User input is never rendered as HTML."""
        expense = create_personal_expense(
            self.rahul,
            title="<script>alert(1)</script>",
            description="<img src=x onerror=alert(2)>",
        )

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<img src=x")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


class ExpenseEditTests(ExpenseTestCase):
    def test_edit_page_shows_the_current_values(self):
        """The form starts with what is stored, including the date in browser format."""
        expense = create_personal_expense(self.rahul, title="Dinner", date=date(2026, 3, 9))

        response = self.client.get(reverse("expense_edit", args=[expense.pk]))

        self.assertContains(response, 'value="Dinner"')
        self.assertContains(response, 'value="2026-03-09"')

    def test_creator_can_change_the_details(self):
        """A valid edit is saved and the user returns to the expense."""
        expense = create_personal_expense(self.rahul)

        response = self.client.post(
            reverse("expense_edit", args=[expense.pk]),
            self.form_data(title="Brunch", category=self.travel.pk, payment_method="cash"),
        )

        self.assertRedirects(response, reverse("expense_detail", args=[expense.pk]))
        expense.refresh_from_db()
        self.assertEqual(expense.title, "Brunch")
        self.assertEqual(expense.category, self.travel)
        self.assertEqual(expense.payment_method, PaymentMethod.CASH)

    def test_changing_the_amount_also_updates_the_participant_row(self):
        """RULE: paid_amount and share_amount must keep matching the amount."""
        expense = create_personal_expense(self.rahul, amount=Decimal("100.00"))

        self.client.post(reverse("expense_edit", args=[expense.pk]), self.form_data(amount="750"))

        participant = expense.participants.get()
        self.assertEqual(participant.paid_amount, Decimal("750.00"))
        self.assertEqual(participant.share_amount, Decimal("750.00"))

    def test_invalid_edit_changes_nothing(self):
        """A rejected form leaves the stored expense as it was."""
        expense = create_personal_expense(self.rahul, title="Dinner", amount=Decimal("100.00"))

        response = self.client.post(
            reverse("expense_edit", args=[expense.pk]), self.form_data(title="Brunch", amount="-1")
        )

        self.assertEqual(response.status_code, 200)
        expense.refresh_from_db()
        self.assertEqual(expense.title, "Dinner")
        self.assertEqual(expense.amount, Decimal("100.00"))

    def test_another_users_expense_cannot_be_edited(self):
        """IDOR check: changing the id in the URL gives a 404 and changes nothing."""
        expense = create_personal_expense(self.aman, title="Aman's")
        url = reverse("expense_edit", args=[expense.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.form_data()).status_code, 404)
        expense.refresh_from_db()
        self.assertEqual(expense.title, "Aman's")


class ExpenseDeleteTests(ExpenseTestCase):
    def test_get_asks_for_confirmation_and_deletes_nothing(self):
        """Opening the page must not delete anything."""
        expense = create_personal_expense(self.rahul, title="Dinner")

        response = self.client.get(reverse("expense_delete", args=[expense.pk]))

        self.assertContains(response, "Dinner")
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())

    def test_post_deletes_the_expense_and_its_participant_rows(self):
        """Nothing is left behind."""
        expense = create_personal_expense(self.rahul)

        response = self.client.post(reverse("expense_delete", args=[expense.pk]))

        self.assertRedirects(response, reverse("expense_list"))
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(ExpenseParticipant.objects.count(), 0)

    def test_another_users_expense_cannot_be_deleted(self):
        """IDOR check: changing the id in the URL gives a 404 and deletes nothing."""
        expense = create_personal_expense(self.aman)
        url = reverse("expense_delete", args=[expense.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())

    def test_form_without_a_csrf_token_is_rejected(self):
        """A link on another site cannot delete an expense."""
        expense = create_personal_expense(self.rahul)
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(reverse("expense_delete", args=[expense.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists())


class ExpenseVisibilityTests(ExpenseTestCase):
    def test_creator_sees_an_expense_even_if_not_a_participant(self):
        """Someone can create a shared expense for others and still needs to see it."""
        expense = create_expense(self.rahul, expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=expense, user=self.aman)

        self.assertIn(expense, Expense.objects.visible_to(self.rahul))

    def test_participant_sees_an_expense_they_did_not_create(self):
        """Taking part is enough to view it."""
        expense = create_expense(self.aman, expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=expense, user=self.rahul)

        self.assertIn(expense, Expense.objects.visible_to(self.rahul))

    def test_outsider_does_not_see_an_expense(self):
        """Neither creator nor participant means no access."""
        create_personal_expense(self.aman)

        self.assertEqual(Expense.objects.visible_to(self.rahul).count(), 0)

    def test_expense_with_many_participants_appears_only_once(self):
        """A join would return one row per participant, and the Exists query must not."""
        expense = create_expense(self.rahul, expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=expense, user=self.rahul)
        ExpenseParticipant.objects.create(expense=expense, user=self.aman)
        ExpenseParticipant.objects.create(expense=expense, user=create_user("rohit"))

        self.assertEqual(Expense.objects.visible_to(self.rahul).count(), 1)


class ExpenseListTests(ExpenseTestCase):
    def titles_for(self, **query) -> list[str]:
        response = self.client.get(reverse("expense_list"), query)
        self.assertEqual(response.status_code, 200)
        return [expense.title for expense in response.context["page"]]

    def test_list_shows_only_the_users_own_expenses(self):
        """Other users' expenses never appear."""
        create_personal_expense(self.rahul, title="Mine")
        create_personal_expense(self.aman, title="Not mine")

        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Not mine")

    def test_newest_expense_comes_first(self):
        """The list is sorted by date, newest first."""
        create_personal_expense(self.rahul, title="Older", date=date(2026, 1, 1))
        create_personal_expense(self.rahul, title="Newer", date=date(2026, 2, 1))

        self.assertEqual(self.titles_for(), ["Newer", "Older"])

    def test_empty_list_shows_a_message(self):
        """A new user sees a friendly message instead of an empty table."""
        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, "No expenses found")

    def test_search_finds_text_in_the_title_in_any_letter_case(self):
        """Search is case-insensitive."""
        create_personal_expense(self.rahul, title="Team Dinner")
        create_personal_expense(self.rahul, title="Taxi")

        self.assertEqual(self.titles_for(q="dINNer"), ["Team Dinner"])

    def test_search_finds_text_in_the_description(self):
        """Search covers the description as well as the title."""
        create_personal_expense(self.rahul, title="Lunch", description="With the marketing team")
        create_personal_expense(self.rahul, title="Taxi")

        self.assertEqual(self.titles_for(q="marketing"), ["Lunch"])

    def test_search_does_not_reach_other_users_expenses(self):
        """Search only looks inside what the user may see."""
        create_personal_expense(self.aman, title="Secret dinner")

        self.assertEqual(self.titles_for(q="dinner"), [])

    def test_date_filters_include_both_boundary_days(self):
        """The first and last day of the range both count."""
        for day in (9, 10, 15, 20, 21):
            create_personal_expense(self.rahul, title=f"Day {day}", date=date(2026, 11, day))

        titles = self.titles_for(date_from="2026-11-10", date_to="2026-11-20")

        self.assertEqual(titles, ["Day 20", "Day 15", "Day 10"])

    def test_date_filters_work_on_their_own(self):
        """Only a start date, or only an end date, is allowed."""
        create_personal_expense(self.rahul, title="Early", date=date(2026, 1, 1))
        create_personal_expense(self.rahul, title="Late", date=date(2026, 12, 1))

        self.assertEqual(self.titles_for(date_from="2026-06-01"), ["Late"])
        self.assertEqual(self.titles_for(date_to="2026-06-01"), ["Early"])

    def test_start_date_after_end_date_shows_an_error(self):
        """A backwards range is refused with a message."""
        response = self.client.get(
            reverse("expense_list"), {"date_from": "2026-12-01", "date_to": "2026-01-01"}
        )

        self.assertContains(response, "start date cannot be after the end date")

    def test_invalid_filter_value_shows_an_error_and_does_not_crash(self):
        """A hand-edited URL is handled like any other bad input."""
        response = self.client.get(reverse("expense_list"), {"date_from": "nonsense"})

        self.assertContains(response, "Enter a valid date")

    def test_category_filter(self):
        """Only expenses in the chosen category remain."""
        create_personal_expense(self.rahul, title="Lunch", category=self.food)
        create_personal_expense(self.rahul, title="Taxi", category=self.travel)

        self.assertEqual(self.titles_for(category=self.travel.pk), ["Taxi"])

    def test_payment_method_filter(self):
        """Only expenses with the chosen payment method remain."""
        create_personal_expense(self.rahul, title="Lunch", payment_method=PaymentMethod.CASH)
        create_personal_expense(self.rahul, title="Taxi", payment_method=PaymentMethod.UPI)

        self.assertEqual(self.titles_for(payment_method="upi"), ["Taxi"])

    def test_expense_type_filter(self):
        """Only expenses of the chosen type remain."""
        create_personal_expense(self.rahul, title="Lunch")
        shared = create_expense(self.rahul, title="Trip", expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=shared, user=self.rahul)

        self.assertEqual(self.titles_for(expense_type="shared"), ["Trip"])
        self.assertEqual(self.titles_for(expense_type="personal"), ["Lunch"])

    def test_filters_and_search_combine(self):
        """All the conditions must match together."""
        create_personal_expense(self.rahul, title="Team dinner", category=self.food)
        create_personal_expense(self.rahul, title="Team taxi", category=self.travel)
        create_personal_expense(self.rahul, title="Solo dinner", category=self.food)

        self.assertEqual(self.titles_for(q="team", category=self.food.pk), ["Team dinner"])

    def test_another_users_category_cannot_be_used_as_a_filter(self):
        """A hand-edited URL with a private category id gets an error and reveals nothing."""
        gym = Category.objects.create(name="Gym", owner=self.aman)

        response = self.client.get(reverse("expense_list"), {"category": gym.pk})

        self.assertContains(response, "Select a valid choice")
        self.assertNotContains(response, "Gym")

    def test_script_in_a_title_and_in_the_search_box_is_escaped(self):
        """Neither stored input nor the query string is rendered as HTML."""
        create_personal_expense(self.rahul, title="<script>alert(1)</script>")

        response = self.client.get(reverse("expense_list"), {"q": "<script>alert(2)</script>"})

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<script>alert(2)</script>")


class ExpensePaginationTests(ExpenseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.extra_rows = 5
        for number in range(EXPENSES_PER_PAGE + cls.extra_rows):
            create_personal_expense(cls.rahul, title=f"Expense {number}")

    def test_first_page_holds_one_full_page(self):
        """The list is cut into pages."""
        response = self.client.get(reverse("expense_list"))

        self.assertEqual(len(response.context["page"]), EXPENSES_PER_PAGE)
        self.assertContains(response, "Page 1 of 2")

    def test_second_page_holds_the_rest(self):
        """Nothing is lost between pages."""
        response = self.client.get(reverse("expense_list"), {"page": 2})

        self.assertEqual(len(response.context["page"]), self.extra_rows)

    def test_page_links_keep_the_current_filters(self):
        """Moving to the next page must not drop the search."""
        response = self.client.get(reverse("expense_list"), {"q": "Expense"})

        self.assertContains(response, "?q=Expense&amp;page=2")

    def test_page_number_out_of_range_shows_the_last_page(self):
        """A stale link still shows something useful."""
        response = self.client.get(reverse("expense_list"), {"page": 999})

        self.assertEqual(response.context["page"].number, 2)

    def test_page_value_that_is_not_a_number_shows_the_first_page(self):
        """A hand-edited URL does not crash the page."""
        response = self.client.get(reverse("expense_list"), {"page": "abc"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].number, 1)


class ExpenseQueryCountTests(ExpenseTestCase):
    def test_list_query_count_does_not_grow_with_the_number_of_rows(self):
        """No N+1: session, user, count, rows and the category dropdown, however many rows."""
        for number in range(EXPENSES_PER_PAGE):
            create_personal_expense(self.rahul, title=f"Expense {number}")

        with self.assertNumQueries(5):
            response = self.client.get(reverse("expense_list"))

        self.assertEqual(len(response.context["page"]), EXPENSES_PER_PAGE)

    def test_detail_query_count_stays_fixed(self):
        """Session, user, the expense with its related rows, and the participants."""
        expense = create_personal_expense(self.rahul)

        with self.assertNumQueries(4):
            self.client.get(reverse("expense_detail", args=[expense.pk]))
