"""Model and database constraint tests. These run on real PostgreSQL."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from expenses.models import Budget, Category, Expense, ExpenseParticipant

from .helpers import create_expense, create_user, default_category

DEFAULT_CATEGORY_NAMES = {
    "Food",
    "Travel",
    "Shopping",
    "Bills",
    "Rent",
    "Entertainment",
    "Medical",
    "Education",
    "Fuel",
    "Groceries",
    "Other",
}


class CategoryModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")

    def test_migration_creates_all_default_categories_without_an_owner(self):
        """The eleven default categories exist and belong to nobody."""
        defaults = Category.objects.filter(owner=None)

        self.assertEqual({category.name for category in defaults}, DEFAULT_CATEGORY_NAMES)

    def test_same_name_in_a_different_case_is_rejected_for_the_same_owner(self):
        """'Food' and 'food' are one category, even though PostgreSQL is case-sensitive."""
        Category.objects.create(name="Pets", owner=self.rahul)

        with self.assertRaisesMessage(IntegrityError, "unique_category_name_per_owner"):
            with transaction.atomic():
                Category.objects.create(name="pETS", owner=self.rahul)

    def test_two_users_can_each_have_a_category_with_the_same_name(self):
        """Uniqueness is per owner, so users do not affect each other."""
        Category.objects.create(name="Pets", owner=self.rahul)
        Category.objects.create(name="Pets", owner=self.aman)

        self.assertEqual(Category.objects.filter(name="Pets").count(), 2)

    def test_duplicate_default_category_is_rejected(self):
        """NULL owners must conflict too, which is what nulls_distinct=False guarantees."""
        with self.assertRaisesMessage(IntegrityError, "unique_category_name_per_owner"):
            with transaction.atomic():
                Category.objects.create(name="FOOD", owner=None)

    def test_full_clean_reports_a_duplicate_name_as_a_validation_error(self):
        """Python validation gives a readable message before the database is involved."""
        Category.objects.create(name="Pets", owner=self.rahul)

        with self.assertRaisesMessage(ValidationError, "already exists"):
            Category(name="pets", owner=self.rahul).full_clean()

    def test_deleting_a_user_deletes_their_custom_categories_but_not_the_defaults(self):
        """Custom categories belong to their owner and default categories belong to nobody."""
        Category.objects.create(name="Pets", owner=self.rahul)

        self.rahul.delete()

        self.assertFalse(Category.objects.filter(name="Pets").exists())
        self.assertTrue(default_category().pk)


class ExpenseModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")

    def test_zero_amount_is_rejected_by_the_database(self):
        """The CHECK constraint holds even when Python validation is skipped."""
        with self.assertRaisesMessage(IntegrityError, "expense_amount_greater_than_zero"):
            with transaction.atomic():
                create_expense(self.rahul, amount=Decimal("0.00"))

    def test_negative_amount_is_rejected_by_the_database(self):
        """A negative expense can never be stored."""
        with self.assertRaisesMessage(IntegrityError, "expense_amount_greater_than_zero"):
            with transaction.atomic():
                create_expense(self.rahul, amount=Decimal("-1.00"))

    def test_full_clean_rejects_an_amount_below_one_paisa(self):
        """The form-level validator gives a readable error for the same rule."""
        expense = Expense(
            title="Tea",
            amount=Decimal("0.00"),
            date="2026-11-01",
            category=default_category(),
            payment_method="cash",
            expense_type="personal",
            created_by=self.rahul,
        )

        with self.assertRaises(ValidationError) as caught:
            expense.full_clean()

        self.assertIn("amount", caught.exception.message_dict)

    def test_full_clean_rejects_an_unknown_payment_method(self):
        """Only the listed payment methods are allowed."""
        expense = Expense(
            title="Tea",
            amount=Decimal("10.00"),
            date="2026-11-01",
            category=default_category(),
            payment_method="bitcoin",
            expense_type="personal",
            created_by=self.rahul,
        )

        with self.assertRaises(ValidationError) as caught:
            expense.full_clean()

        self.assertIn("payment_method", caught.exception.message_dict)

    def test_amount_is_stored_and_read_back_as_an_exact_decimal(self):
        """Money comes back from PostgreSQL exactly as it was saved."""
        expense = create_expense(self.rahul, amount=Decimal("1234567890.12"))

        expense.refresh_from_db()

        self.assertEqual(expense.amount, Decimal("1234567890.12"))
        self.assertIsInstance(expense.amount, Decimal)

    def test_a_category_in_use_cannot_be_deleted(self):
        """Old expenses must never lose their category."""
        category = Category.objects.create(name="Pets", owner=self.rahul)
        create_expense(self.rahul, category=category)

        with self.assertRaises(ProtectedError):
            category.delete()

    def test_a_user_who_created_an_expense_cannot_be_deleted(self):
        """Deleting a user must not silently change other people's balances."""
        create_expense(self.rahul)

        with self.assertRaises(ProtectedError):
            self.rahul.delete()

    def test_expected_indexes_exist_in_the_database(self):
        """The columns used for filtering and sorting are indexed."""
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, Expense._meta.db_table)
        indexed_columns = [item["columns"] for item in constraints.values() if item["index"]]

        for columns in (
            ["created_by_id", "date"],
            ["date", "id"],
            ["category_id"],
            ["payment_method"],
            ["expense_type"],
        ):
            with self.subTest(columns=columns):
                self.assertIn(columns, indexed_columns)

    def test_newest_expense_comes_first_by_default(self):
        """Lists show the most recent expense first."""
        older = create_expense(self.rahul, date="2026-01-01")
        newer = create_expense(self.rahul, date="2026-02-01")

        self.assertEqual(list(Expense.objects.all()), [newer, older])


class ExpenseParticipantModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.expense = create_expense(cls.rahul)

    def test_same_user_cannot_be_added_twice_to_one_expense(self):
        """Each person has one row per expense."""
        ExpenseParticipant.objects.create(expense=self.expense, user=self.rahul)

        with self.assertRaisesMessage(IntegrityError, "unique_participant_per_expense"):
            with transaction.atomic():
                ExpenseParticipant.objects.create(expense=self.expense, user=self.rahul)

    def test_negative_paid_amount_is_rejected_by_the_database(self):
        """Nobody can pay a negative amount."""
        with self.assertRaisesMessage(IntegrityError, "participant_paid_amount_not_negative"):
            with transaction.atomic():
                ExpenseParticipant.objects.create(
                    expense=self.expense, user=self.rahul, paid_amount=Decimal("-0.01")
                )

    def test_negative_share_amount_is_rejected_by_the_database(self):
        """Nobody can consume a negative amount."""
        with self.assertRaisesMessage(IntegrityError, "participant_share_amount_not_negative"):
            with transaction.atomic():
                ExpenseParticipant.objects.create(
                    expense=self.expense, user=self.rahul, share_amount=Decimal("-0.01")
                )

    def test_zero_paid_and_share_amounts_are_allowed(self):
        """A participant can pay nothing, or consume nothing."""
        participant = ExpenseParticipant.objects.create(expense=self.expense, user=self.rahul)

        participant.refresh_from_db()

        self.assertEqual(participant.paid_amount, Decimal("0.00"))
        self.assertEqual(participant.share_amount, Decimal("0.00"))

    def test_deleting_an_expense_deletes_its_participants(self):
        """Participant rows have no meaning without their expense."""
        ExpenseParticipant.objects.create(expense=self.expense, user=self.aman)

        self.expense.delete()

        self.assertFalse(ExpenseParticipant.objects.filter(user=self.aman).exists())

    def test_a_participant_cannot_be_deleted_while_on_an_expense(self):
        """Removing a user would change the balances of everyone else on the expense."""
        ExpenseParticipant.objects.create(expense=self.expense, user=self.aman)

        with self.assertRaises(ProtectedError):
            self.aman.delete()


class BudgetModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")

    def create_budget(self, **overrides) -> Budget:
        values = {"user": self.rahul, "year": 2026, "month": 11, "amount": Decimal("5000.00")}
        values.update(overrides)
        return Budget.objects.create(**values)

    def test_second_budget_for_the_same_user_and_month_is_rejected(self):
        """A user has at most one budget per month."""
        self.create_budget()

        with self.assertRaisesMessage(IntegrityError, "unique_budget_per_user_and_month"):
            with transaction.atomic():
                self.create_budget(amount=Decimal("9000.00"))

    def test_other_months_and_other_users_are_allowed(self):
        """Uniqueness covers the user, year and month together."""
        self.create_budget()
        self.create_budget(month=12)
        self.create_budget(year=2027)
        self.create_budget(user=self.aman)

        self.assertEqual(Budget.objects.count(), 4)

    def test_zero_and_negative_budgets_are_rejected_by_the_database(self):
        """A budget amount must be greater than zero."""
        for amount in (Decimal("0.00"), Decimal("-100.00")):
            with self.subTest(amount=amount):
                with self.assertRaisesMessage(IntegrityError, "budget_amount_greater_than_zero"):
                    with transaction.atomic():
                        self.create_budget(amount=amount)

    def test_month_outside_1_to_12_is_rejected_by_the_database(self):
        """Months 0 and 13 do not exist."""
        for month in (0, 13):
            with self.subTest(month=month):
                with self.assertRaisesMessage(IntegrityError, "budget_month_between_1_and_12"):
                    with transaction.atomic():
                        self.create_budget(month=month)

    def test_full_clean_rejects_a_month_outside_1_to_12(self):
        """The form-level validator gives a readable error for the same rule."""
        budget = Budget(user=self.rahul, year=2026, month=13, amount=Decimal("100.00"))

        with self.assertRaises(ValidationError) as caught:
            budget.full_clean()

        self.assertIn("month", caught.exception.message_dict)
