"""Tests for shared expenses: splits, payments, rollback, participant lookup and permissions."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from expenses.forms import MAX_PARTICIPANTS
from expenses.models import Expense, ExpenseParticipant, ExpenseType, PaymentMethod
from expenses.selectors import find_user_by_username_or_email
from expenses.services import ParticipantRow, SplitType, save_shared_expense

from .helpers import create_expense, create_personal_expense, create_user, default_category


def money(value: str) -> Decimal:
    return Decimal(value)


class SharedExpenseTestCase(TestCase):
    """Three friends and an outsider, with Rahul logged in."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rohit = create_user("rohit")
        cls.outsider = create_user("outsider")
        cls.food = default_category()

    def setUp(self):
        self.client.force_login(self.rahul)

    def form_data(self, rows, split_type="equal", **overrides) -> dict:
        """Build the POST data of the expense form. Each row is (person, paid, share)."""
        data = {
            "title": "Dinner",
            "description": "",
            "amount": "3000.00",
            "date": "2026-11-01",
            "category": self.food.pk,
            "payment_method": "upi",
            "expense_type": "shared",
            "split_type": split_type,
            "participants-TOTAL_FORMS": len(rows),
            "participants-INITIAL_FORMS": 0,
            "participants-MIN_NUM_FORMS": 1,
            "participants-MAX_NUM_FORMS": MAX_PARTICIPANTS,
        }
        for index, (person, paid_amount, share_value) in enumerate(rows):
            data[f"participants-{index}-person"] = person
            data[f"participants-{index}-paid_amount"] = paid_amount
            data[f"participants-{index}-share_value"] = share_value
        data.update(overrides)
        return data

    def add_expense(self, rows, split_type="equal", **overrides):
        return self.client.post(
            reverse("expense_create"), self.form_data(rows, split_type, **overrides)
        )

    def rows_of(self, expense) -> dict:
        """Return {username: (paid, share)} as stored in the database."""
        return {
            row.user.username: (row.paid_amount, row.share_amount)
            for row in expense.participants.select_related("user")
        }

    def assert_nothing_was_saved(self):
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(ExpenseParticipant.objects.count(), 0)

    def create_shared_expense(self, created_by=None, amount="3000.00") -> Expense:
        """The dinner example: paid 1500/1000/500 with an equal split of 3000."""
        expense = Expense(
            title="Dinner",
            amount=money(amount),
            date=date(2026, 11, 1),
            category=self.food,
            payment_method=PaymentMethod.UPI,
        )
        rows = [
            ParticipantRow(self.rahul, money("1500.00"), 0),
            ParticipantRow(self.aman, money("1000.00"), 0),
            ParticipantRow(self.rohit, money("500.00"), 0),
        ]
        return save_shared_expense(expense, created_by or self.rahul, SplitType.EQUAL, rows)


class SharedExpenseCreateTests(SharedExpenseTestCase):
    def test_case_1_1000_split_equally_between_2_people(self):
        """One person pays and both owe 500."""
        response = self.add_expense([("rahul", "1000", ""), ("aman", "0", "")], amount="1000.00")

        expense = Expense.objects.get()
        self.assertRedirects(response, reverse("expense_detail", args=[expense.pk]))
        self.assertEqual(expense.expense_type, ExpenseType.SHARED)
        self.assertEqual(expense.created_by, self.rahul)
        self.assertEqual(
            self.rows_of(expense),
            {
                "rahul": (money("1000.00"), money("500.00")),
                "aman": (money("0.00"), money("500.00")),
            },
        )

    def test_case_2_1500_split_equally_between_3_people(self):
        """Each of the three owes 500."""
        self.add_expense(
            [("rahul", "1500", ""), ("aman", "0", ""), ("rohit", "0", "")], amount="1500.00"
        )

        shares = [share for _, share in self.rows_of(Expense.objects.get()).values()]
        self.assertEqual(shares, [money("500.00")] * 3)

    def test_case_3_different_paid_amounts_with_an_equal_split(self):
        """The acceptance scenario: paid 1500/1000/500, with a share of 1000 each."""
        self.add_expense([("rahul", "1500", ""), ("aman", "1000", ""), ("rohit", "500", "")])

        self.assertEqual(
            self.rows_of(Expense.objects.get()),
            {
                "rahul": (money("1500.00"), money("1000.00")),
                "aman": (money("1000.00"), money("1000.00")),
                "rohit": (money("500.00"), money("1000.00")),
            },
        )

    def test_case_4_different_shares_with_a_custom_split(self):
        """Each person's share is what was typed."""
        self.add_expense(
            [("rahul", "1000", "700"), ("aman", "0", "300")], "custom", amount="1000.00"
        )

        self.assertEqual(
            self.rows_of(Expense.objects.get()),
            {
                "rahul": (money("1000.00"), money("700.00")),
                "aman": (money("0.00"), money("300.00")),
            },
        )

    def test_percentage_split_of_60_and_40(self):
        """The shares are calculated from the percentages."""
        self.add_expense(
            [("rahul", "1000", "60"), ("aman", "0", "40")], "percentage", amount="1000.00"
        )

        self.assertEqual(
            self.rows_of(Expense.objects.get()),
            {
                "rahul": (money("1000.00"), money("600.00")),
                "aman": (money("0.00"), money("400.00")),
            },
        )

    def test_100_between_3_people_keeps_every_paisa(self):
        """The stored shares add up to the exact amount, with the extra paisa on the first row."""
        self.add_expense(
            [("rahul", "100", ""), ("aman", "0", ""), ("rohit", "0", "")], amount="100.00"
        )

        rows = self.rows_of(Expense.objects.get())
        self.assertEqual(rows["rahul"][1], money("33.34"))
        self.assertEqual(rows["aman"][1], money("33.33"))
        self.assertEqual(rows["rohit"][1], money("33.33"))

    def test_blank_paid_amount_counts_as_zero(self):
        """Leaving Paid empty means this person paid nothing."""
        self.add_expense([("rahul", "1000", ""), ("aman", "", "")], amount="1000.00")

        self.assertEqual(self.rows_of(Expense.objects.get())["aman"][0], money("0.00"))

    def test_empty_extra_row_is_ignored(self):
        """The form always offers one more row, and leaving it empty is fine."""
        self.add_expense([("rahul", "1000", ""), ("aman", "0", ""), ("", "", "")], amount="1000.00")

        self.assertEqual(ExpenseParticipant.objects.count(), 2)

    def test_creator_does_not_have_to_be_a_participant(self):
        """Someone can record an expense for friends without taking part in it."""
        self.add_expense([("aman", "1000", ""), ("rohit", "0", "")], amount="1000.00")

        expense = Expense.objects.get()
        self.assertEqual(expense.created_by, self.rahul)
        self.assertNotIn("rahul", self.rows_of(expense))

    def test_creator_in_the_submitted_data_is_ignored(self):
        """A shared expense cannot be created in someone else's name."""
        self.add_expense(
            [("rahul", "1000", ""), ("aman", "0", "")], amount="1000.00", created_by=self.aman.pk
        )

        self.assertEqual(Expense.objects.get().created_by, self.rahul)

    def test_personal_expense_ignores_the_participant_rows(self):
        """Rows left in the hidden section must not turn into participants."""
        self.add_expense(
            [("aman", "1000", ""), ("rohit", "0", "")],
            amount="1000.00",
            expense_type="personal",
        )

        expense = Expense.objects.get()
        self.assertEqual(expense.expense_type, ExpenseType.PERSONAL)
        self.assertEqual(self.rows_of(expense), {"rahul": (money("1000.00"), money("1000.00"))})


class SharedExpenseValidationTests(SharedExpenseTestCase):
    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assert_nothing_was_saved()

    def test_case_5_paid_amounts_that_do_not_add_up_save_nothing(self):
        """Paid must equal the bill, and no partial records remain."""
        response = self.add_expense([("rahul", "1000", ""), ("aman", "500", "")])

        self.assert_refused(response, "Paid amounts must add up")

    def test_case_5_custom_shares_that_do_not_add_up_save_nothing(self):
        """Shares must equal the bill, and no partial records remain."""
        response = self.add_expense(
            [("rahul", "1000", "700"), ("aman", "0", "200")], "custom", amount="1000.00"
        )

        self.assert_refused(response, "Custom shares must add up")

    def test_case_5_percentages_that_do_not_add_up_save_nothing(self):
        """Percentages must total exactly 100, and no partial records remain."""
        response = self.add_expense(
            [("rahul", "1000", "60"), ("aman", "0", "30")], "percentage", amount="1000.00"
        )

        self.assert_refused(response, "exactly 100")

    def test_blank_custom_shares_are_refused_by_the_total_check(self):
        """A blank share counts as zero, so the sum no longer matches."""
        response = self.add_expense(
            [("rahul", "1000", ""), ("aman", "0", "")], "custom", amount="1000.00"
        )

        self.assert_refused(response, "Custom shares must add up")

    def test_negative_amounts_are_refused(self):
        """Even when the total would match, nobody pays or consumes a negative amount."""
        for rows in (
            [("rahul", "1200", ""), ("aman", "-200", "")],
            [("rahul", "1000", "1200"), ("aman", "0", "-200")],
        ):
            with self.subTest(rows=rows):
                response = self.add_expense(rows, "custom", amount="1000.00")
                self.assert_refused(response, "greater than or equal to 0")

    def test_amount_with_more_than_two_decimals_is_refused(self):
        """A paisa is the smallest unit."""
        response = self.add_expense([("rahul", "1000.001", ""), ("aman", "0", "")])

        self.assert_refused(response, "no more than 2 decimal places")

    def test_no_participants_are_refused(self):
        """A shared expense must have someone in it."""
        response = self.add_expense([])

        self.assert_refused(response, "At least one participant")

    def test_same_person_twice_is_refused_even_by_username_and_email(self):
        """Participants must be distinct, however they were typed."""
        response = self.add_expense([("rahul", "3000", ""), ("RAHUL@example.com", "0", "")])

        self.assert_refused(response, "only once")

    def test_unknown_split_type_is_refused(self):
        """A hand-edited form cannot invent a split type."""
        response = self.add_expense([("rahul", "3000", ""), ("aman", "0", "")], "shares")

        self.assert_refused(response, "Select a valid choice")

    def test_missing_split_type_is_refused(self):
        """A shared expense cannot be saved without knowing how to split it."""
        response = self.add_expense([("rahul", "3000", ""), ("aman", "0", "")], "")

        self.assert_refused(response, "Unknown split type")

    def test_missing_row_data_is_refused_without_a_crash(self):
        """A hand-made request without the row counters gets an error."""
        data = self.form_data([("rahul", "3000", "")])
        del data["participants-TOTAL_FORMS"]
        del data["participants-INITIAL_FORMS"]

        response = self.client.post(reverse("expense_create"), data)

        self.assertEqual(response.status_code, 200)
        self.assert_nothing_was_saved()

    def test_too_many_rows_are_refused(self):
        """The number of rows has a limit, so one request cannot cause endless lookups."""
        rows = [("rahul", "3000", "")] + [("aman", "0", "")] * MAX_PARTICIPANTS

        response = self.add_expense(rows)

        self.assertEqual(response.status_code, 200)
        self.assert_nothing_was_saved()


class SharedExpenseRollbackTests(SharedExpenseTestCase):
    def fail_on_second_row(self):
        """Patch the row save so the first row succeeds and the second one crashes."""
        real_update_or_create = ExpenseParticipant.objects.update_or_create
        saved_rows = []

        def update_or_create(**kwargs):
            if saved_rows:
                raise RuntimeError("database failure on the second row")
            saved_rows.append(kwargs)
            return real_update_or_create(**kwargs)

        return mock.patch.object(ExpenseParticipant.objects, "update_or_create", update_or_create)

    def test_failure_on_a_later_row_leaves_nothing_behind_when_adding(self):
        """Case 5, rollback: the expense and the first row are undone too."""
        with self.fail_on_second_row(), self.assertRaises(RuntimeError):
            self.create_shared_expense()

        self.assert_nothing_was_saved()

    def test_failure_on_a_later_row_restores_everything_when_editing(self):
        """Case 5, rollback: an edit that fails midway changes nothing at all."""
        expense = self.create_shared_expense()
        rows_before = self.rows_of(expense)
        expense.title = "Changed"
        expense.amount = money("100.00")
        rows = [
            ParticipantRow(self.rahul, money("100.00"), 0),
            ParticipantRow(self.outsider, money("0.00"), 0),
        ]

        with self.fail_on_second_row(), self.assertRaises(RuntimeError):
            save_shared_expense(expense, self.rahul, SplitType.EQUAL, rows)

        expense.refresh_from_db()
        self.assertEqual(expense.title, "Dinner")
        self.assertEqual(expense.amount, money("3000.00"))
        self.assertEqual(self.rows_of(expense), rows_before)

    def test_invalid_numbers_are_refused_before_anything_is_written(self):
        """The service checks the numbers first, so a refused edit changes nothing."""
        expense = self.create_shared_expense()
        rows_before = self.rows_of(expense)
        rows = [ParticipantRow(self.rahul, money("1.00"), 0)]

        with self.assertRaisesMessage(ValidationError, "Paid amounts must add up"):
            save_shared_expense(expense, self.rahul, SplitType.EQUAL, rows)

        self.assertEqual(self.rows_of(expense), rows_before)


class ParticipantLookupTests(SharedExpenseTestCase):
    def test_finds_a_user_by_exact_username_or_email_in_any_letter_case(self):
        """Both identifiers work, and case does not matter."""
        for identifier in ("aman", "AMAN", "aman@example.com", "Aman@Example.COM"):
            with self.subTest(identifier=identifier):
                self.assertEqual(find_user_by_username_or_email(identifier), self.aman)

    def test_partial_text_finds_nobody(self):
        """RULE: no search as you type, only an exact match."""
        for identifier in ("am", "ama", "aman@", "example.com", "%", "_____"):
            with self.subTest(identifier=identifier):
                self.assertIsNone(find_user_by_username_or_email(identifier))

    def test_deactivated_user_cannot_be_added(self):
        """A closed account no longer takes part in new expenses."""
        User.objects.filter(pk=self.aman.pk).update(is_active=False)

        self.assertIsNone(find_user_by_username_or_email("aman"))

    def test_ambiguous_match_finds_nobody(self):
        """If one person's username is another person's email, guessing would be wrong."""
        User.objects.create_user(username="aman@example.com", email="other@example.com")

        self.assertIsNone(find_user_by_username_or_email("aman@example.com"))

    def test_participant_can_be_added_by_email(self):
        """The form accepts an email as well as a username."""
        self.add_expense([("rahul", "1000", ""), ("AMAN@example.com", "0", "")], amount="1000.00")

        self.assertIn("aman", self.rows_of(Expense.objects.get()))

    def test_unknown_person_shows_an_error_and_saves_nothing(self):
        """The message says only that this lookup found nobody."""
        response = self.add_expense([("rahul", "3000", ""), ("nobody", "0", "")])

        self.assertContains(response, "No user found with this exact username or email")
        self.assert_nothing_was_saved()

    def test_add_form_does_not_list_any_other_user(self):
        """RULE: no user directory. Other accounts never appear on the page."""
        response = self.client.get(reverse("expense_create"))

        for private_text in ("aman", "rohit", "outsider", "@example.com"):
            with self.subTest(text=private_text):
                self.assertNotContains(response, private_text)

    def test_add_form_starts_with_the_creator_in_the_first_row(self):
        """The creator usually takes part, so their name is already filled in."""
        response = self.client.get(reverse("expense_create"))

        self.assertContains(response, 'value="rahul"')


class SharedExpensePermissionTests(SharedExpenseTestCase):
    def setUp(self):
        super().setUp()
        self.expense = self.create_shared_expense(created_by=self.aman)
        self.detail_url = reverse("expense_detail", args=[self.expense.pk])
        self.edit_url = reverse("expense_edit", args=[self.expense.pk])
        self.delete_url = reverse("expense_delete", args=[self.expense.pk])

    def test_participant_can_view_an_expense_they_did_not_create(self):
        """Rahul takes part in Aman's expense, so he can open it."""
        response = self.client.get(self.detail_url)

        self.assertContains(response, "Dinner")

    def test_participant_is_not_offered_edit_or_delete(self):
        """The page does not show actions the user may not take."""
        response = self.client.get(self.detail_url)

        self.assertNotContains(response, self.edit_url)
        self.assertNotContains(response, self.delete_url)

    def test_participant_cannot_edit(self):
        """RULE: only the creator edits. The participant knows the expense, so it is a 403."""
        data = self.form_data([("rahul", "3000", ""), ("aman", "0", "")], title="Hacked")

        self.assertEqual(self.client.get(self.edit_url).status_code, 403)
        self.assertEqual(self.client.post(self.edit_url, data).status_code, 403)
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.title, "Dinner")

    def test_participant_cannot_delete(self):
        """RULE: only the creator deletes."""
        self.assertEqual(self.client.get(self.delete_url).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url).status_code, 403)
        self.assertTrue(Expense.objects.filter(pk=self.expense.pk).exists())

    def test_outsider_gets_a_404_everywhere(self):
        """An outsider cannot even learn that the expense exists."""
        self.client.force_login(self.outsider)

        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)
        self.assertEqual(self.client.post(self.edit_url).status_code, 404)
        self.assertEqual(self.client.get(self.delete_url).status_code, 404)
        self.assertEqual(self.client.post(self.delete_url).status_code, 404)
        self.assertTrue(Expense.objects.filter(pk=self.expense.pk).exists())

    def test_creator_who_is_not_a_participant_can_view_edit_and_delete(self):
        """Creating an expense is enough to manage it."""
        expense = Expense(
            title="For friends",
            amount=money("100.00"),
            date=date(2026, 11, 1),
            category=self.food,
            payment_method=PaymentMethod.CASH,
        )
        save_shared_expense(
            expense,
            self.rahul,
            SplitType.EQUAL,
            [ParticipantRow(self.aman, money("100.00"), 0)],
        )

        self.assertEqual(
            self.client.get(reverse("expense_detail", args=[expense.pk])).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse("expense_edit", args=[expense.pk])).status_code, 200
        )
        self.client.post(reverse("expense_delete", args=[expense.pk]))
        self.assertFalse(Expense.objects.filter(pk=expense.pk).exists())

    def test_detail_page_shows_usernames_but_not_emails(self):
        """Taking part in an expense does not reveal other people's email addresses."""
        response = self.client.get(self.detail_url)

        self.assertContains(response, "rohit")
        self.assertNotContains(response, "rohit@example.com")

    def test_list_shows_a_shared_expense_to_a_participant_with_their_own_share(self):
        """The list shows what this person consumed, not the whole bill."""
        response = self.client.get(reverse("expense_list"))

        expense = response.context["page"][0]
        self.assertEqual(expense.pk, self.expense.pk)
        self.assertEqual(expense.my_share, money("1000.00"))
        self.assertContains(response, "1000.00")

    def test_list_does_not_show_a_shared_expense_to_an_outsider(self):
        """Isolation also applies to the list."""
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("expense_list"))

        self.assertNotContains(response, "Dinner")

    def test_creator_who_is_not_a_participant_has_no_share_in_the_list(self):
        """Their spending is not affected, so the list shows a dash."""
        self.client.force_login(self.outsider)
        expense = create_expense(self.outsider, expense_type=ExpenseType.SHARED)
        ExpenseParticipant.objects.create(expense=expense, user=self.aman)

        response = self.client.get(reverse("expense_list"))

        self.assertIsNone(response.context["page"][0].my_share)


class SharedExpenseEditTests(SharedExpenseTestCase):
    def setUp(self):
        super().setUp()
        self.expense = self.create_shared_expense()
        self.edit_url = reverse("expense_edit", args=[self.expense.pk])

    def test_edit_page_starts_from_the_stored_rows_as_a_custom_split(self):
        """The split type is not stored, so the stored shares are shown as custom amounts."""
        response = self.client.get(self.edit_url)

        self.assertEqual(response.context["form"].initial["split_type"], SplitType.CUSTOM)
        self.assertContains(response, 'value="rahul"')
        self.assertContains(response, 'value="aman"')
        self.assertContains(response, 'value="1500.00"')
        self.assertContains(response, 'value="1000.00"')

    def test_saving_the_edit_page_unchanged_keeps_the_same_numbers(self):
        """Opening and saving must not change anybody's money."""
        rows_before = self.rows_of(self.expense)

        self.client.post(
            self.edit_url,
            self.form_data(
                [
                    ("rahul", "1500.00", "1000.00"),
                    ("aman", "1000.00", "1000.00"),
                    ("rohit", "500.00", "1000.00"),
                ],
                "custom",
            ),
        )

        self.assertEqual(self.rows_of(self.expense), rows_before)

    def test_edit_recalculates_the_shares(self):
        """A new amount with an equal split gives new shares."""
        self.client.post(
            self.edit_url,
            self.form_data(
                [("rahul", "600", ""), ("aman", "0", ""), ("rohit", "0", "")], amount="600.00"
            ),
        )

        self.expense.refresh_from_db()
        self.assertEqual(self.expense.amount, money("600.00"))
        self.assertEqual(self.rows_of(self.expense)["aman"], (money("0.00"), money("200.00")))

    def test_edit_can_add_and_remove_people(self):
        """The rows end up matching exactly the people on the form."""
        self.client.post(
            self.edit_url, self.form_data([("rahul", "3000", ""), ("outsider", "0", "")])
        )

        self.assertEqual(set(self.rows_of(self.expense)), {"rahul", "outsider"})

    def test_invalid_edit_changes_nothing(self):
        """A refused edit leaves the amount and every row as they were."""
        rows_before = self.rows_of(self.expense)

        response = self.client.post(
            self.edit_url,
            self.form_data([("rahul", "1", ""), ("aman", "1", "")], amount="999.00"),
        )

        self.assertContains(response, "Paid amounts must add up")
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.amount, money("3000.00"))
        self.assertEqual(self.rows_of(self.expense), rows_before)

    def test_shared_expense_can_become_personal(self):
        """The other people's rows go away and the creator keeps the whole amount."""
        self.client.post(
            self.edit_url,
            self.form_data([], expense_type="personal", amount="500.00"),
        )

        self.expense.refresh_from_db()
        self.assertEqual(self.expense.expense_type, ExpenseType.PERSONAL)
        self.assertEqual(self.rows_of(self.expense), {"rahul": (money("500.00"), money("500.00"))})

    def test_personal_expense_can_become_shared(self):
        """The new rows replace the creator's single row."""
        expense = create_personal_expense(self.rahul, amount=money("1000.00"))

        self.client.post(
            reverse("expense_edit", args=[expense.pk]),
            self.form_data([("rahul", "1000", ""), ("aman", "0", "")], amount="1000.00"),
        )

        expense.refresh_from_db()
        self.assertEqual(expense.expense_type, ExpenseType.SHARED)
        self.assertEqual(self.rows_of(expense)["aman"], (money("0.00"), money("500.00")))


class SharedExpenseDetailTests(SharedExpenseTestCase):
    def test_detail_page_shows_who_pays_whom(self):
        """The acceptance scenario ends with Rohit paying Rahul 500."""
        expense = self.create_shared_expense()

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertContains(response, "rohit pays rahul 500.00")

    def test_personal_expense_has_no_settlement_section(self):
        """Nobody owes anything on a personal expense."""
        expense = create_personal_expense(self.rahul)

        response = self.client.get(reverse("expense_detail", args=[expense.pk]))

        self.assertNotContains(response, "Who pays whom")


class SharedExpensePageTests(SharedExpenseTestCase):
    def test_form_loads_its_script_and_offers_an_empty_row_template(self):
        """The add and remove buttons depend on both."""
        response = self.client.get(reverse("expense_create"))

        self.assertContains(response, "js/expense-form.js")
        self.assertContains(response, 'id="empty-participant-row"')
        self.assertContains(response, "participants-__prefix__-person")

    def test_list_query_count_stays_fixed_with_many_shared_expenses(self):
        """No N+1: my_share comes from a subquery, not one query per row."""
        for _ in range(10):
            self.create_shared_expense()

        with self.assertNumQueries(5):
            self.client.get(reverse("expense_list"))

    def test_detail_query_count_stays_fixed_with_many_participants(self):
        """No N+1: participants and their users come from one query."""
        expense = self.create_shared_expense()

        with self.assertNumQueries(4):
            self.client.get(reverse("expense_detail", args=[expense.pk]))
