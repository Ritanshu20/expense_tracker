"""Tests for settle-up payments: balances after paying, validation, permissions and isolation."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from expenses.models import ExpenseParticipant, ExpenseType, SettlementPayment
from expenses.selectors import (
    DateRange,
    get_payments_for_user,
    get_settlement_summary,
    get_spending_summary,
)
from expenses.services import (
    calculate_expense_split,
    calculate_pairwise_debts,
    save_settlement_payment,
)

from .helpers import create_expense, create_user


def money(value: str) -> Decimal:
    return Decimal(value)


def dinner_rows() -> list[tuple[str, str, str]]:
    """Rohit owes Rahul 500: paid 1500/1000/500, with a share of 1000 each."""
    return [("Rahul", "1500", "1000"), ("Aman", "1000", "1000"), ("Rohit", "500", "1000")]


class PairwiseDebtsAfterPaymentsTests(SimpleTestCase):
    def test_partial_payment_lowers_the_debt(self):
        """Rohit owes 500 and pays 200: he still owes 300."""
        debts = calculate_pairwise_debts([dinner_rows()], [("Rohit", "Rahul", money("200.00"))])

        self.assertEqual(debts, [("Rohit", "Rahul", money("300.00"))])

    def test_full_payment_brings_the_balance_to_zero(self):
        """Rohit owes 500 and pays 500: nobody owes anything."""
        debts = calculate_pairwise_debts([dinner_rows()], [("Rohit", "Rahul", money("500.00"))])

        self.assertEqual(debts, [])

    def test_overpayment_flips_the_direction(self):
        """Rohit owes 500 and pays 700: now Rahul owes Rohit 200."""
        debts = calculate_pairwise_debts([dinner_rows()], [("Rohit", "Rahul", money("700.00"))])

        self.assertEqual(debts, [("Rahul", "Rohit", money("200.00"))])

    def test_several_payments_add_up(self):
        """200 and then 300 is the same as 500."""
        debts = calculate_pairwise_debts(
            [dinner_rows()],
            [("Rohit", "Rahul", money("200.00")), ("Rohit", "Rahul", money("300.00"))],
        )

        self.assertEqual(debts, [])

    def test_payment_in_the_other_direction_raises_the_debt(self):
        """If Rahul lends Rohit 100 more, Rohit owes 600."""
        debts = calculate_pairwise_debts([dinner_rows()], [("Rahul", "Rohit", money("100.00"))])

        self.assertEqual(debts, [("Rohit", "Rahul", money("600.00"))])

    def test_payment_with_no_expense_between_the_two_people_creates_a_debt(self):
        """Money paid with nothing owed is owed back."""
        debts = calculate_pairwise_debts([], [("A", "B", money("100.00"))])

        self.assertEqual(debts, [("B", "A", money("100.00"))])

    def test_multi_person_case_only_changes_the_pair_that_paid(self):
        """A paid 300, split between A, B and C. B pays A back, and C still owes A."""
        rows = [("A", "300", "100"), ("B", "0", "100"), ("C", "0", "100")]

        debts = calculate_pairwise_debts([rows], [("B", "A", money("100.00"))])

        self.assertEqual(debts, [("C", "A", money("100.00"))])

    def test_rounding_remainder_can_be_paid_back_exactly(self):
        """100 split between 3: B owes A exactly 33.33, and paying it leaves nothing."""
        shares = calculate_expense_split("100", ["A", "B", "C"], "equal")
        rows = [
            ("A", "100", shares["A"]),
            ("B", "0", shares["B"]),
            ("C", "0", shares["C"]),
        ]

        debts = calculate_pairwise_debts([rows], [("B", "A", money("33.33"))])

        self.assertEqual(debts, [("C", "A", money("33.33"))])


class SettlementPaymentTestCase(TestCase):
    """The dinner, where Rohit owes Rahul 500, with Rahul logged in."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rohit = create_user("rohit")
        cls.outsider = create_user("outsider")
        cls.dinner = create_expense(
            cls.rahul,
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            expense_type=ExpenseType.SHARED,
        )
        for user, paid_amount, share_amount in (
            (cls.rahul, "1500.00", "1000.00"),
            (cls.aman, "1000.00", "1000.00"),
            (cls.rohit, "500.00", "1000.00"),
        ):
            ExpenseParticipant.objects.create(
                expense=cls.dinner,
                user=user,
                paid_amount=money(paid_amount),
                share_amount=money(share_amount),
            )

    def setUp(self):
        self.client.force_login(self.rahul)

    def create_payment(self, from_user=None, to_user=None, created_by=None, **overrides):
        values = {"amount": money("200.00"), "date": date(2026, 11, 12)}
        values.update(overrides)
        from_user = from_user or self.rohit
        return SettlementPayment.objects.create(
            from_user=from_user,
            to_user=to_user or self.rahul,
            created_by=created_by or from_user,
            **values,
        )

    def form_data(self, **overrides) -> dict:
        data = {
            "direction": "they_paid",
            "person": "rohit",
            "amount": "200.00",
            "date": "2026-11-12",
            "note": "",
        }
        data.update(overrides)
        return data


class BalanceAfterPaymentTests(SettlementPaymentTestCase):
    def test_partial_payment_updates_both_people(self):
        """Rahul and Rohit both see 300 left after a payment of 200."""
        self.create_payment(amount=money("200.00"))

        self.assertEqual(
            get_settlement_summary(self.rahul).owed_to_you, [("rohit", "rahul", money("300.00"))]
        )
        self.assertEqual(
            get_settlement_summary(self.rohit).you_owe, [("rohit", "rahul", money("300.00"))]
        )

    def test_full_payment_clears_the_balance(self):
        """After paying 500 nobody owes anybody."""
        self.create_payment(amount=money("500.00"))

        summary = get_settlement_summary(self.rahul)

        self.assertEqual(summary.owed_to_you, [])
        self.assertEqual(summary.you_owe, [])

    def test_overpayment_flips_who_owes_whom(self):
        """After paying 700, Rahul owes Rohit 200."""
        self.create_payment(amount=money("700.00"))

        summary = get_settlement_summary(self.rahul)

        self.assertEqual(summary.owed_to_you, [])
        self.assertEqual(summary.you_owe, [("rahul", "rohit", money("200.00"))])

    def test_deleting_a_payment_restores_the_balance(self):
        """Nothing is stored, so the debt comes back."""
        payment = self.create_payment(amount=money("500.00"))

        payment.delete()

        self.assertEqual(
            get_settlement_summary(self.rahul).owed_to_you, [("rohit", "rahul", money("500.00"))]
        )

    def test_editing_a_payment_changes_the_balance(self):
        """The new amount counts."""
        payment = self.create_payment(amount=money("500.00"))

        payment.amount = money("100.00")
        payment.save()

        self.assertEqual(
            get_settlement_summary(self.rahul).owed_to_you, [("rohit", "rahul", money("400.00"))]
        )

    def test_a_payment_does_not_count_as_spending(self):
        """RULE: paying someone back is not consuming anything."""
        before = get_spending_summary(self.rohit, date(2026, 11, 12))

        self.create_payment(amount=money("500.00"))

        self.assertEqual(get_spending_summary(self.rohit, date(2026, 11, 12)), before)

    def test_payment_outside_a_report_period_is_not_counted_in_that_period(self):
        """A report counts expenses and payments dated inside its period."""
        self.create_payment(amount=money("200.00"), date=date(2026, 12, 5))
        november = DateRange(date(2026, 11, 1), date(2026, 11, 30))

        self.assertEqual(
            get_settlement_summary(self.rahul, november).owed_to_you,
            [("rohit", "rahul", money("500.00"))],
        )
        self.assertEqual(
            get_settlement_summary(self.rahul).owed_to_you, [("rohit", "rahul", money("300.00"))]
        )

    def test_dashboard_shows_the_updated_balance_and_a_prefilled_link(self):
        """The who-owes-whom card uses the updated numbers and offers to record a payment."""
        self.create_payment(amount=money("200.00"))

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "300.00")
        self.assertContains(
            response,
            f"{reverse('settlement_create')}?direction=they_paid&amp;person=rohit&amp;amount=300.00",
        )


class SettlementPaymentRuleTests(SettlementPaymentTestCase):
    def test_payment_to_yourself_is_refused_by_the_database(self):
        """The constraint holds even when the service is skipped."""
        with self.assertRaisesMessage(IntegrityError, "settlement_payment_between_different_users"):
            with transaction.atomic():
                self.create_payment(from_user=self.rahul, to_user=self.rahul)

    def test_zero_and_negative_amounts_are_refused_by_the_database(self):
        """The amount must be greater than zero."""
        for amount in (money("0.00"), money("-5.00")):
            with self.subTest(amount=amount):
                with self.assertRaisesMessage(
                    IntegrityError, "settlement_payment_amount_greater_than_zero"
                ):
                    with transaction.atomic():
                        self.create_payment(amount=amount)

    def test_recording_a_payment_between_two_other_people_is_refused_by_the_database(self):
        """SAFETY: nobody can change what two other people owe each other."""
        with self.assertRaisesMessage(IntegrityError, "settlement_payment_created_by_a_party"):
            with transaction.atomic():
                self.create_payment(created_by=self.outsider)

    def test_service_refuses_a_payment_to_yourself_with_a_readable_message(self):
        """The rule gives a message before the database is involved."""
        payment = SettlementPayment(
            from_user=self.rahul,
            to_user=self.rahul,
            amount=money("1.00"),
            date=date(2026, 11, 12),
        )

        with self.assertRaisesMessage(ValidationError, "two different people"):
            save_settlement_payment(payment, self.rahul)

    def test_service_refuses_a_payment_between_two_other_people(self):
        """The person recording a payment must be one of its two people."""
        payment = SettlementPayment(
            from_user=self.rohit,
            to_user=self.rahul,
            amount=money("1.00"),
            date=date(2026, 11, 12),
        )

        with self.assertRaisesMessage(ValidationError, "that you made or received"):
            save_settlement_payment(payment, self.outsider)


class SettlementPageTests(SettlementPaymentTestCase):
    def test_urls_are_at_the_expected_addresses(self):
        """The pages live under /settlements/."""
        self.assertEqual(reverse("settlement_list"), "/settlements/")
        self.assertEqual(reverse("settlement_create"), "/settlements/add/")
        self.assertEqual(reverse("settlement_detail", args=[5]), "/settlements/5/")
        self.assertEqual(reverse("settlement_edit", args=[5]), "/settlements/5/edit/")
        self.assertEqual(reverse("settlement_delete", args=[5]), "/settlements/5/delete/")

    def test_every_page_sends_anonymous_visitors_to_login(self):
        """Payments are private."""
        payment = self.create_payment()
        self.client.logout()
        urls = (
            reverse("settlement_list"),
            reverse("settlement_create"),
            reverse("settlement_detail", args=[payment.pk]),
            reverse("settlement_edit", args=[payment.pk]),
            reverse("settlement_delete", args=[payment.pk]),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_they_paid_me_records_the_other_person_as_the_payer(self):
        """Rahul records that Rohit paid him."""
        response = self.client.post(reverse("settlement_create"), self.form_data())

        self.assertRedirects(response, reverse("settlement_list"))
        payment = SettlementPayment.objects.get()
        self.assertEqual(payment.from_user, self.rohit)
        self.assertEqual(payment.to_user, self.rahul)
        self.assertEqual(payment.created_by, self.rahul)
        self.assertEqual(payment.amount, money("200.00"))

    def test_i_paid_them_records_the_logged_in_user_as_the_payer(self):
        """Rahul records that he paid Rohit."""
        self.client.post(reverse("settlement_create"), self.form_data(direction="i_paid"))

        payment = SettlementPayment.objects.get()
        self.assertEqual(payment.from_user, self.rahul)
        self.assertEqual(payment.to_user, self.rohit)

    def test_other_person_can_be_given_by_email(self):
        """Exact email works, as for expense participants."""
        self.client.post(reverse("settlement_create"), self.form_data(person="ROHIT@example.com"))

        self.assertEqual(SettlementPayment.objects.get().from_user, self.rohit)

    def test_submitted_parties_cannot_override_the_logged_in_user(self):
        """A user cannot record a payment between two other people through the form."""
        self.client.post(
            reverse("settlement_create"),
            self.form_data(
                from_user=self.aman.pk, to_user=self.outsider.pk, created_by=self.aman.pk
            ),
        )

        payment = SettlementPayment.objects.get()
        self.assertEqual(payment.to_user, self.rahul)
        self.assertEqual(payment.created_by, self.rahul)

    def test_link_from_the_dashboard_fills_in_the_form(self):
        """Direction, person and amount come from the link."""
        response = self.client.get(
            reverse("settlement_create"),
            {"direction": "they_paid", "person": "rohit", "amount": "300.00"},
        )

        self.assertContains(response, 'value="rohit"')
        self.assertContains(response, 'value="300.00"')

    def test_invalid_input_is_refused_and_saves_nothing(self):
        """Server-side validation covers every field."""
        invalid_inputs = (
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "abc"},
            {"amount": "1.234"},
            {"amount": ""},
            {"date": ""},
            {"date": "nonsense"},
            {"direction": ""},
            {"direction": "sideways"},
            {"person": ""},
            {"person": "nobody"},
            {"person": "roh"},
            {"note": "x" * 201},
        )
        for overrides in invalid_inputs:
            with self.subTest(overrides=overrides):
                response = self.client.post(
                    reverse("settlement_create"), self.form_data(**overrides)
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(SettlementPayment.objects.count(), 0)

    def test_payment_to_yourself_shows_an_error(self):
        """The form shows the service's message instead of a server error."""
        response = self.client.post(reverse("settlement_create"), self.form_data(person="rahul"))

        self.assertContains(response, "two different people")
        self.assertEqual(SettlementPayment.objects.count(), 0)

    def test_form_does_not_list_any_other_user(self):
        """RULE: no user directory."""
        response = self.client.get(reverse("settlement_create"))

        self.assertNotContains(response, "rohit")
        self.assertNotContains(response, "@example.com")

    def test_form_without_a_csrf_token_is_rejected(self):
        """CSRF protection is on for this form."""
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(reverse("settlement_create"), self.form_data())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(SettlementPayment.objects.count(), 0)

    def test_script_in_a_note_is_escaped(self):
        """User input is never rendered as HTML."""
        self.create_payment(note="<script>alert(1)</script>", created_by=self.rahul)

        response = self.client.get(reverse("settlement_list"))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


class SettlementPermissionTests(SettlementPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.payment = self.create_payment(amount=money("200.00"))
        self.detail_url = reverse("settlement_detail", args=[self.payment.pk])
        self.edit_url = reverse("settlement_edit", args=[self.payment.pk])
        self.delete_url = reverse("settlement_delete", args=[self.payment.pk])

    def test_both_people_can_view_a_payment(self):
        """Rohit recorded it, and Rahul received it."""
        for user in (self.rahul, self.rohit):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.detail_url).status_code, 200)

    def test_both_people_see_it_in_their_list(self):
        """The list holds payments made and received."""
        self.assertIn(self.payment, get_payments_for_user(self.rahul))
        self.assertIn(self.payment, get_payments_for_user(self.rohit))

    def test_creator_can_edit_and_delete(self):
        """Rohit recorded the payment, so he can change it."""
        self.client.force_login(self.rohit)

        response = self.client.post(
            self.edit_url,
            self.form_data(direction="i_paid", person="rahul", amount="250.00"),
        )

        self.assertRedirects(response, reverse("settlement_list"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, money("250.00"))
        self.assertEqual(self.payment.from_user, self.rohit)

        self.client.post(self.delete_url)
        self.assertFalse(SettlementPayment.objects.filter(pk=self.payment.pk).exists())

    def test_edit_page_shows_the_current_direction_and_person(self):
        """The form describes the payment from the logged-in user's side."""
        self.client.force_login(self.rohit)

        response = self.client.get(self.edit_url)

        self.assertContains(response, 'value="rahul"')
        self.assertEqual(response.context["form"].initial["direction"], "i_paid")

    def test_other_person_on_the_payment_cannot_edit_or_delete(self):
        """RULE: only the creator changes it. Rahul knows the payment, so it is a 403."""
        self.assertEqual(self.client.get(self.edit_url).status_code, 403)
        self.assertEqual(self.client.post(self.edit_url, self.form_data()).status_code, 403)
        self.assertEqual(self.client.get(self.delete_url).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url).status_code, 403)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, money("200.00"))

    def test_other_person_is_not_offered_edit_or_delete(self):
        """The page does not show actions the user may not take."""
        response = self.client.get(reverse("settlement_list"))

        self.assertNotContains(response, self.edit_url)
        self.assertNotContains(response, self.delete_url)

    def test_outsider_gets_a_404_everywhere(self):
        """IDOR check: an outsider cannot learn that the payment exists."""
        self.client.force_login(self.outsider)

        for url in (self.detail_url, self.edit_url, self.delete_url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(self.delete_url).status_code, 404)
        self.assertTrue(SettlementPayment.objects.filter(pk=self.payment.pk).exists())

    def test_outsider_does_not_see_the_payment_or_the_debt(self):
        """Isolation: a payment between two other people changes nothing for the outsider."""
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("settlement_list"))

        self.assertContains(response, "No payments yet")
        self.assertNotContains(response, "rohit")

    def test_list_query_count_does_not_grow_with_the_number_of_payments(self):
        """No N+1: session, user, expense rows, payment rows, and the payment list."""
        for _ in range(20):
            self.create_payment(amount=money("1.00"))

        with self.assertNumQueries(5):
            self.client.get(reverse("settlement_list"))


class SettlementAdminTests(SettlementPaymentTestCase):
    def test_admin_list_loads_with_search(self):
        """A wrong field name in the admin configuration would fail here."""
        admin_user = create_user("admin_user")
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.save()
        self.create_payment()
        self.client.force_login(admin_user)
        url = reverse("admin:expenses_settlementpayment_changelist")

        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url, {"q": "rohit"}).status_code, 200)
