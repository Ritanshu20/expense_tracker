"""Unit tests for the split and settlement engine. No database is involved."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from expenses.services import (
    Settlement,
    SplitType,
    calculate_balances,
    calculate_expense_split,
    calculate_pairwise_debts,
    calculate_settlements,
    to_money,
    validate_paid_amounts,
    validate_participant_amounts,
)


def money(value: str) -> Decimal:
    return Decimal(value)


class MoneyConversionTests(SimpleTestCase):
    def test_half_paisa_rounds_up(self):
        """The rounding policy is ROUND_HALF_UP, not banker's rounding."""
        self.assertEqual(to_money("10.005"), money("10.01"))
        self.assertEqual(to_money("10.015"), money("10.02"))

    def test_result_always_has_two_decimal_places(self):
        """Whole numbers become 2-place money values."""
        self.assertEqual(str(to_money(5)), "5.00")

    def test_float_is_refused_for_money(self):
        """Floats cannot represent money exactly, so they are not accepted at all."""
        with self.assertRaisesMessage(ValidationError, "not float"):
            to_money(10.5)

    def test_values_that_are_not_finite_numbers_are_refused(self):
        """NaN, Infinity and text must not reach any calculation."""
        for bad_value in ("NaN", "sNaN", "Infinity", "-Infinity", "abc", "", None, True):
            with self.subTest(value=bad_value):
                with self.assertRaisesMessage(ValidationError, "valid number"):
                    to_money(bad_value)

    def test_value_too_large_for_the_database_column_is_refused(self):
        """Values above DecimalField(max_digits=12, decimal_places=2) are rejected."""
        self.assertEqual(to_money("9999999999.99"), money("9999999999.99"))
        with self.assertRaisesMessage(ValidationError, "too large"):
            to_money("10000000000")
        with self.assertRaisesMessage(ValidationError, "too large"):
            to_money("1E+30")


class EqualSplitTests(SimpleTestCase):
    def test_1000_between_2_people_gives_500_each(self):
        """The simplest equal split has no remainder."""
        shares = calculate_expense_split("1000", ["A", "B"], SplitType.EQUAL)

        self.assertEqual(shares, {"A": money("500.00"), "B": money("500.00")})

    def test_1500_between_3_people_gives_500_each(self):
        """A split that divides evenly has no remainder."""
        shares = calculate_expense_split("1500", ["A", "B", "C"], SplitType.EQUAL)

        self.assertEqual(shares, {"A": money("500.00"), "B": money("500.00"), "C": money("500.00")})

    def test_100_between_3_people_gives_the_extra_paisa_to_the_first_person(self):
        """100.00 / 3 leaves 1 paisa, which goes to the first participant."""
        shares = calculate_expense_split("100", ["A", "B", "C"], SplitType.EQUAL)

        self.assertEqual(shares, {"A": money("33.34"), "B": money("33.33"), "C": money("33.33")})

    def test_two_leftover_paise_go_to_the_first_two_people(self):
        """Leftover paise are handed out one by one, in participant order."""
        shares = calculate_expense_split("100.01", ["A", "B", "C"], SplitType.EQUAL)

        self.assertEqual(shares, {"A": money("33.34"), "B": money("33.34"), "C": money("33.33")})

    def test_participant_order_decides_who_gets_the_extra_paisa(self):
        """The result depends only on the input order, so it is deterministic."""
        shares = calculate_expense_split("100", ["C", "A", "B"], SplitType.EQUAL)

        self.assertEqual(shares["C"], money("33.34"))
        self.assertEqual(shares["A"], money("33.33"))

    def test_one_paisa_between_3_people_gives_it_to_the_first_person(self):
        """An amount smaller than the group still adds up exactly."""
        shares = calculate_expense_split("0.01", ["A", "B", "C"], SplitType.EQUAL)

        self.assertEqual(shares, {"A": money("0.01"), "B": money("0.00"), "C": money("0.00")})


class PercentageSplitTests(SimpleTestCase):
    def test_60_40_split_of_1000_gives_600_and_400(self):
        """Shares follow the given percentages."""
        shares = calculate_expense_split(
            "1000", ["A", "B"], SplitType.PERCENTAGE, {"A": "60", "B": "40"}
        )

        self.assertEqual(shares, {"A": money("600.00"), "B": money("400.00")})

    def test_leftover_paisa_goes_to_the_largest_remainder_not_the_first_person(self):
        """10.00 at 33.33/33.33/33.34 gives C the paisa because C has the largest remainder."""
        shares = calculate_expense_split(
            "10",
            ["A", "B", "C"],
            SplitType.PERCENTAGE,
            {"A": "33.33", "B": "33.33", "C": "33.34"},
        )

        self.assertEqual(shares, {"A": money("3.33"), "B": money("3.33"), "C": money("3.34")})

    def test_equal_remainders_are_resolved_by_participant_order(self):
        """0.01 at 50/50 is a tie, so the first participant gets the paisa."""
        shares = calculate_expense_split(
            "0.01", ["A", "B"], SplitType.PERCENTAGE, {"A": "50", "B": "50"}
        )

        self.assertEqual(shares, {"A": money("0.01"), "B": money("0.00")})

    def test_zero_percent_gives_a_zero_share(self):
        """Someone can take part without consuming anything."""
        shares = calculate_expense_split(
            "1000", ["A", "B"], SplitType.PERCENTAGE, {"A": "100", "B": "0"}
        )

        self.assertEqual(shares, {"A": money("1000.00"), "B": money("0.00")})

    def test_percentages_that_do_not_sum_to_100_are_rejected(self):
        """Both under and over 100 are refused."""
        for percentages in ({"A": "60", "B": "30"}, {"A": "60", "B": "50"}):
            with self.subTest(percentages=percentages):
                with self.assertRaisesMessage(ValidationError, "exactly 100"):
                    calculate_expense_split("1000", ["A", "B"], SplitType.PERCENTAGE, percentages)

    def test_percentages_that_only_nearly_sum_to_100_are_rejected(self):
        """33.33 three times is 99.99, and close is not enough."""
        with self.assertRaisesMessage(ValidationError, "exactly 100"):
            calculate_expense_split(
                "1000",
                ["A", "B", "C"],
                SplitType.PERCENTAGE,
                {"A": "33.33", "B": "33.33", "C": "33.33"},
            )

    def test_negative_percentage_is_rejected_even_if_the_total_is_100(self):
        """120 and -20 sum to 100 but a negative share is never valid."""
        with self.assertRaisesMessage(ValidationError, "cannot be negative"):
            calculate_expense_split(
                "1000", ["A", "B"], SplitType.PERCENTAGE, {"A": "120", "B": "-20"}
            )

    def test_percentages_must_cover_exactly_the_participants(self):
        """Missing values, extra people and no values at all are refused."""
        for percentages in ({"A": "100"}, {"A": "50", "B": "25", "C": "25"}, None):
            with self.subTest(percentages=percentages):
                with self.assertRaisesMessage(ValidationError, "one value for each"):
                    calculate_expense_split("1000", ["A", "B"], SplitType.PERCENTAGE, percentages)


class CustomSplitTests(SimpleTestCase):
    def test_custom_shares_that_sum_to_the_total_are_accepted(self):
        """Different people can consume different amounts."""
        shares = calculate_expense_split(
            "1000", ["A", "B"], SplitType.CUSTOM, {"A": "700", "B": "300"}
        )

        self.assertEqual(shares, {"A": money("700.00"), "B": money("300.00")})

    def test_custom_shares_that_do_not_sum_to_the_total_are_rejected(self):
        """Both under and over the total are refused."""
        for custom_shares in ({"A": "700", "B": "200"}, {"A": "700", "B": "400"}):
            with self.subTest(custom_shares=custom_shares):
                with self.assertRaisesMessage(ValidationError, "add up to the total"):
                    calculate_expense_split("1000", ["A", "B"], SplitType.CUSTOM, custom_shares)

    def test_negative_custom_share_is_rejected_even_if_the_total_matches(self):
        """1200 and -200 sum to 1000 but a negative share is never valid."""
        with self.assertRaisesMessage(ValidationError, "cannot be negative"):
            calculate_expense_split(
                "1000", ["A", "B"], SplitType.CUSTOM, {"A": "1200", "B": "-200"}
            )

    def test_tiny_negative_custom_share_is_rejected_instead_of_rounding_to_zero(self):
        """-0.001 would round to 0.00, so the sign is checked before rounding."""
        with self.assertRaisesMessage(ValidationError, "cannot be negative"):
            calculate_expense_split(
                "1000", ["A", "B"], SplitType.CUSTOM, {"A": "1000", "B": "-0.001"}
            )

    def test_custom_shares_must_cover_exactly_the_participants(self):
        """Every participant needs a value and nobody else can have one."""
        for custom_shares in ({"A": "1000"}, {"A": "1000", "B": "0", "C": "0"}, None):
            with self.subTest(custom_shares=custom_shares):
                with self.assertRaisesMessage(ValidationError, "one value for each"):
                    calculate_expense_split("1000", ["A", "B"], SplitType.CUSTOM, custom_shares)


class SplitInputValidationTests(SimpleTestCase):
    def test_zero_and_negative_amounts_are_rejected(self):
        """An expense amount must be greater than zero."""
        for amount in ("0", "0.00", "-1", "-100.50", "0.004"):
            with self.subTest(amount=amount):
                with self.assertRaisesMessage(ValidationError, "greater than zero"):
                    calculate_expense_split(amount, ["A", "B"], SplitType.EQUAL)

    def test_empty_participant_list_is_rejected(self):
        """A split needs at least one participant."""
        with self.assertRaisesMessage(ValidationError, "At least one participant"):
            calculate_expense_split("100", [], SplitType.EQUAL)

    def test_repeated_participant_is_rejected(self):
        """Participants must be distinct."""
        with self.assertRaisesMessage(ValidationError, "only once"):
            calculate_expense_split("100", ["A", "A"], SplitType.EQUAL)

    def test_unknown_split_type_is_rejected(self):
        """Only equal, custom and percentage exist."""
        with self.assertRaisesMessage(ValidationError, "Unknown split type"):
            calculate_expense_split("100", ["A", "B"], "shares")

    def test_split_type_can_be_given_as_a_plain_string(self):
        """Forms submit plain strings, which must work like the enum."""
        shares = calculate_expense_split("100", ["A", "B"], "equal")

        self.assertEqual(shares, {"A": money("50.00"), "B": money("50.00")})


class MoneyConservationTests(SimpleTestCase):
    AMOUNTS = ("0.01", "0.02", "0.99", "1", "1.01", "9.99", "100", "100.01", "333.33", "1000")
    LARGEST_AMOUNT = "9999999999.99"

    def test_equal_shares_always_add_up_to_the_exact_amount(self):
        """Money is never created or lost, for any amount and any group size."""
        for amount in (*self.AMOUNTS, self.LARGEST_AMOUNT):
            for group_size in range(1, 13):
                with self.subTest(amount=amount, group_size=group_size):
                    shares = calculate_expense_split(amount, range(group_size), SplitType.EQUAL)

                    self.assertEqual(sum(shares.values()), to_money(amount))

    def test_equal_shares_never_differ_by_more_than_one_paisa(self):
        """An equal split stays as fair as paise allow."""
        for amount in self.AMOUNTS:
            for group_size in range(1, 13):
                with self.subTest(amount=amount, group_size=group_size):
                    shares = calculate_expense_split(amount, range(group_size), SplitType.EQUAL)

                    self.assertLessEqual(max(shares.values()) - min(shares.values()), money("0.01"))

    def test_percentage_shares_always_add_up_to_the_exact_amount(self):
        """The largest remainder step keeps percentage splits exact."""
        percentage_sets = (
            ("100",),
            ("50", "50"),
            ("33.33", "33.33", "33.34"),
            ("12.5", "12.5", "25", "50"),
            ("0.01", "0.01", "99.98"),
            ("14.28", "14.28", "14.29", "14.29", "14.29", "14.28", "14.29"),
        )
        for amount in (*self.AMOUNTS, self.LARGEST_AMOUNT):
            for percentages in percentage_sets:
                with self.subTest(amount=amount, percentages=percentages):
                    values = dict(enumerate(percentages))
                    shares = calculate_expense_split(
                        amount, list(values), SplitType.PERCENTAGE, values
                    )

                    self.assertEqual(sum(shares.values()), to_money(amount))


class PaidAmountValidationTests(SimpleTestCase):
    def test_unequal_payments_that_sum_to_the_amount_are_accepted(self):
        """Three people can pay different parts of one bill."""
        paid = validate_paid_amounts("3000", {"Rahul": "1500", "Aman": "1000", "Rohit": "500"})

        self.assertEqual(
            paid, {"Rahul": money("1500.00"), "Aman": money("1000.00"), "Rohit": money("500.00")}
        )

    def test_one_person_paying_everything_is_accepted(self):
        """The others have a paid amount of zero."""
        paid = validate_paid_amounts("1000", {"A": "1000", "B": "0"})

        self.assertEqual(paid["B"], money("0.00"))

    def test_payments_that_do_not_sum_to_the_amount_are_rejected(self):
        """Both under and over the bill are refused."""
        for paid_amounts in ({"A": "500", "B": "400"}, {"A": "500", "B": "600"}, {}):
            with self.subTest(paid_amounts=paid_amounts):
                with self.assertRaisesMessage(ValidationError, "add up to the total"):
                    validate_paid_amounts("1000", paid_amounts)

    def test_negative_payment_is_rejected_even_if_the_total_matches(self):
        """1200 and -200 sum to 1000 but nobody can pay a negative amount."""
        with self.assertRaisesMessage(ValidationError, "cannot be negative"):
            validate_paid_amounts("1000", {"A": "1200", "B": "-200"})

    def test_zero_expense_amount_is_rejected(self):
        """The amount rule also applies when validating payments."""
        with self.assertRaisesMessage(ValidationError, "greater than zero"):
            validate_paid_amounts("0", {"A": "0"})


class ParticipantAmountValidationTests(SimpleTestCase):
    def validate(self, paid_amounts, share_amounts, amount="1000", expense_type="shared"):
        validate_participant_amounts(amount, expense_type, "A", paid_amounts, share_amounts)

    def test_valid_shared_expense_passes(self):
        """Paid amounts and shares both add up to the amount."""
        self.validate({"A": "1000", "B": "0"}, {"A": "500", "B": "500"})

    def test_paid_amounts_that_do_not_add_up_are_rejected(self):
        """Paid amounts must equal the amount."""
        with self.assertRaisesMessage(ValidationError, "Paid amounts must add up"):
            self.validate({"A": "900", "B": "0"}, {"A": "500", "B": "500"})

    def test_shares_that_do_not_add_up_are_rejected(self):
        """Shares must equal the amount."""
        with self.assertRaisesMessage(ValidationError, "Custom shares must add up"):
            self.validate({"A": "1000", "B": "0"}, {"A": "500", "B": "400"})

    def test_no_participants_are_rejected(self):
        """An expense must have at least one participant."""
        with self.assertRaisesMessage(ValidationError, "At least one participant"):
            self.validate({}, {})

    def test_someone_who_paid_but_is_not_a_participant_is_rejected(self):
        """Paid amounts and shares must be for the same people."""
        with self.assertRaisesMessage(ValidationError, "same participants"):
            self.validate({"C": "1000"}, {"A": "500", "B": "500"})

    def test_personal_expense_for_its_creator_passes(self):
        """One row, for the creator, with paid = share = amount."""
        self.validate({"A": "1000"}, {"A": "1000"}, expense_type="personal")

    def test_personal_expense_with_two_participants_is_rejected(self):
        """A personal expense has exactly one participant."""
        with self.assertRaisesMessage(ValidationError, "exactly one participant"):
            self.validate(
                {"A": "1000", "B": "0"}, {"A": "500", "B": "500"}, expense_type="personal"
            )

    def test_personal_expense_for_someone_else_is_rejected(self):
        """That one participant must be the creator."""
        with self.assertRaisesMessage(ValidationError, "exactly one participant"):
            self.validate({"B": "1000"}, {"B": "1000"}, expense_type="personal")


class BalanceTests(SimpleTestCase):
    def test_dinner_example_gives_plus_500_zero_and_minus_500(self):
        """Paid 1500/1000/500 with 1000 each: Rahul +500, Aman 0, Rohit -500."""
        balances = calculate_balances(
            [("Rahul", "1500", "1000"), ("Aman", "1000", "1000"), ("Rohit", "500", "1000")]
        )

        self.assertEqual(
            balances, {"Rahul": money("500.00"), "Aman": money("0.00"), "Rohit": money("-500.00")}
        )

    def test_balances_add_up_across_many_expenses(self):
        """A participant's rows from different expenses are summed."""
        balances = calculate_balances(
            [("A", "100", "50"), ("B", "0", "50"), ("A", "0", "30"), ("B", "30", "0")]
        )

        self.assertEqual(balances, {"A": money("20.00"), "B": money("-20.00")})

    def test_balances_of_a_valid_expense_always_sum_to_zero(self):
        """What some people are owed is exactly what the others owe."""
        shares = calculate_expense_split("100", ["A", "B", "C"], SplitType.EQUAL)
        paid = {"A": money("100.00"), "B": money("0.00"), "C": money("0.00")}

        balances = calculate_balances((person, paid[person], shares[person]) for person in shares)

        self.assertEqual(sum(balances.values()), money("0.00"))

    def test_no_entries_give_no_balances(self):
        """With no expenses nobody has a balance."""
        self.assertEqual(calculate_balances([]), {})


class SettlementTests(SimpleTestCase):
    def assert_settlements_clear_all_balances(self, balances, settlements):
        """Apply every payment and check that nobody has anything left."""
        remaining = dict(balances)
        for settlement in settlements:
            remaining[settlement.from_user] += settlement.amount
            remaining[settlement.to_user] -= settlement.amount
        self.assertTrue(all(balance == 0 for balance in remaining.values()), remaining)

    def test_two_people_gives_one_payment(self):
        """The person who owes pays the person who should receive."""
        settlements = calculate_settlements({"A": money("500"), "B": money("-500")})

        self.assertEqual(settlements, [Settlement("B", "A", money("500.00"))])

    def test_three_people_with_one_creditor(self):
        """Spec example: A +500, B -300, C -200 gives B pays A 300 and C pays A 200."""
        settlements = calculate_settlements({"A": "500", "B": "-300", "C": "-200"})

        self.assertEqual(
            settlements,
            [Settlement("B", "A", money("300.00")), Settlement("C", "A", money("200.00"))],
        )

    def test_four_people_with_two_creditors_and_two_debtors(self):
        """Spec example: A +700, B +300, C -600, D -400, always largest with largest."""
        balances = {"A": money("700"), "B": money("300"), "C": money("-600"), "D": money("-400")}

        settlements = calculate_settlements(balances)

        self.assertEqual(
            settlements,
            [
                Settlement("C", "A", money("600.00")),
                Settlement("D", "B", money("300.00")),
                Settlement("D", "A", money("100.00")),
            ],
        )
        self.assert_settlements_clear_all_balances(balances, settlements)

    def test_many_creditors_and_debtors_are_all_cleared(self):
        """The algorithm works for any number of participants."""
        balances = {
            "A": money("1000.50"),
            "B": money("-250.25"),
            "C": money("300.00"),
            "D": money("-1200.00"),
            "E": money("0.00"),
            "F": money("149.75"),
            "G": money("0.01"),
            "H": money("-0.01"),
        }

        settlements = calculate_settlements(balances)

        self.assert_settlements_clear_all_balances(balances, settlements)
        self.assertLessEqual(len(settlements), len(balances) - 1)

    def test_all_zero_balances_need_no_payments(self):
        """Nobody owes anything, so nothing is suggested."""
        self.assertEqual(calculate_settlements({"A": "0", "B": "0.00"}), [])

    def test_no_balances_need_no_payments(self):
        """An empty input is valid and gives an empty result."""
        self.assertEqual(calculate_settlements({}), [])

    def test_person_with_zero_balance_is_left_out_of_every_payment(self):
        """Aman paid exactly his share, so he neither pays nor receives."""
        settlements = calculate_settlements({"Rahul": "500", "Aman": "0", "Rohit": "-500"})

        people_involved = {s.from_user for s in settlements} | {s.to_user for s in settlements}
        self.assertNotIn("Aman", people_involved)

    def test_equal_debts_are_settled_in_balance_order(self):
        """Ties are broken by input order, so the result is deterministic."""
        settlements = calculate_settlements({"A": "200", "C": "-100", "B": "-100"})

        self.assertEqual([settlement.from_user for settlement in settlements], ["C", "B"])

    def test_unbalanced_input_is_rejected(self):
        """Invariant: total credits must equal total debts."""
        for balances in ({"A": "500", "B": "-300"}, {"A": "100"}, {"A": "-100"}):
            with self.subTest(balances=balances):
                with self.assertRaisesMessage(ValidationError, "total credits"):
                    calculate_settlements(balances)

    def test_totals_larger_than_one_expense_can_hold_still_settle(self):
        """Regression: the limit is for one amount, not for a total across many expenses."""
        settlements = calculate_settlements({"A": "20000000000.00", "B": "-20000000000.00"})

        self.assertEqual(settlements, [("B", "A", money("20000000000.00"))])

    def test_every_payment_is_positive_and_between_two_different_people(self):
        """No zero payments and nobody pays themselves."""
        settlements = calculate_settlements({"A": "700", "B": "300", "C": "-600", "D": "-400"})

        for settlement in settlements:
            self.assertGreater(settlement.amount, 0)
            self.assertNotEqual(settlement.from_user, settlement.to_user)


class PairwiseDebtTests(SimpleTestCase):
    def test_one_expense_gives_the_same_result_as_settling_it(self):
        """With one expense, nothing needs netting."""
        debts = calculate_pairwise_debts(
            [[("Rahul", "1500", "1000"), ("Aman", "1000", "1000"), ("Rohit", "500", "1000")]]
        )

        self.assertEqual(debts, [("Rohit", "Rahul", money("500.00"))])

    def test_debts_in_the_same_direction_add_up(self):
        """A owes B from two different expenses."""
        debts = calculate_pairwise_debts(
            [
                [("A", "0", "100"), ("B", "100", "0")],
                [("A", "0", "50"), ("B", "50", "0")],
            ]
        )

        self.assertEqual(debts, [("A", "B", money("150.00"))])

    def test_debts_in_opposite_directions_are_netted(self):
        """A owes B 500, and B owes A 200: A pays B 300."""
        debts = calculate_pairwise_debts(
            [
                [("A", "0", "500"), ("B", "500", "0")],
                [("A", "200", "0"), ("B", "0", "200")],
            ]
        )

        self.assertEqual(debts, [("A", "B", money("300.00"))])

    def test_larger_debt_decides_the_direction(self):
        """A owes B 200, and B owes A 500: B pays A 300."""
        debts = calculate_pairwise_debts(
            [
                [("A", "0", "200"), ("B", "200", "0")],
                [("A", "500", "0"), ("B", "0", "500")],
            ]
        )

        self.assertEqual(debts, [("B", "A", money("300.00"))])

    def test_equal_opposite_debts_cancel_out(self):
        """A owes B 500, and B owes A 500: nothing is left."""
        debts = calculate_pairwise_debts(
            [
                [("A", "0", "500"), ("B", "500", "0")],
                [("A", "500", "0"), ("B", "0", "500")],
            ]
        )

        self.assertEqual(debts, [])

    def test_no_payment_is_suggested_between_people_who_never_shared_an_expense(self):
        """A owes B, and B owes C. A and C are strangers, so A never pays C."""
        debts = calculate_pairwise_debts(
            [
                [("A", "0", "100"), ("B", "100", "0")],
                [("B", "0", "100"), ("C", "100", "0")],
            ]
        )

        self.assertCountEqual(debts, [("A", "B", money("100.00")), ("B", "C", money("100.00"))])

    def test_no_expenses_give_no_debts(self):
        """Empty is valid."""
        self.assertEqual(calculate_pairwise_debts([]), [])

    def test_expense_whose_rows_do_not_balance_is_refused(self):
        """A broken expense must not silently produce wrong debts."""
        with self.assertRaisesMessage(ValidationError, "total credits"):
            calculate_pairwise_debts([[("A", "100", "0")]])


class FullFlowTests(SimpleTestCase):
    def test_dinner_of_3000_ends_with_rohit_paying_rahul_500(self):
        """The acceptance scenario, from split to settlement."""
        people = ["Rahul", "Aman", "Rohit"]
        shares = calculate_expense_split("3000", people, SplitType.EQUAL)
        paid = validate_paid_amounts("3000", {"Rahul": "1500", "Aman": "1000", "Rohit": "500"})

        balances = calculate_balances((person, paid[person], shares[person]) for person in people)

        self.assertEqual(calculate_settlements(balances), [("Rohit", "Rahul", money("500.00"))])

    def test_rounding_remainder_still_settles_to_exactly_the_right_amounts(self):
        """100 paid by A and split between 3 gives A +66.66, B -33.33, C -33.33."""
        people = ["A", "B", "C"]
        shares = calculate_expense_split("100", people, SplitType.EQUAL)
        paid = validate_paid_amounts("100", {"A": "100", "B": "0", "C": "0"})

        balances = calculate_balances((person, paid[person], shares[person]) for person in people)

        self.assertEqual(
            calculate_settlements(balances),
            [("B", "A", money("33.33")), ("C", "A", money("33.33"))],
        )

    def test_custom_split_settles_correctly(self):
        """1000 paid by A with shares 700/300 means B pays A 300."""
        people = ["A", "B"]
        shares = calculate_expense_split("1000", people, SplitType.CUSTOM, {"A": "700", "B": "300"})
        paid = validate_paid_amounts("1000", {"A": "1000", "B": "0"})

        balances = calculate_balances((person, paid[person], shares[person]) for person in people)

        self.assertEqual(calculate_settlements(balances), [("B", "A", money("300.00"))])

    def test_percentage_split_settles_correctly(self):
        """1000 paid by B with shares 60/40 means A pays B 600."""
        people = ["A", "B"]
        shares = calculate_expense_split(
            "1000", people, SplitType.PERCENTAGE, {"A": "60", "B": "40"}
        )
        paid = validate_paid_amounts("1000", {"A": "0", "B": "1000"})

        balances = calculate_balances((person, paid[person], shares[person]) for person in people)

        self.assertEqual(calculate_settlements(balances), [("A", "B", money("600.00"))])
