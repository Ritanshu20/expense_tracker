"""
Business logic for expenses.

What lives here: splitting an amount between participants, validating who paid
what, net balances, settlement suggestions, budget usage, saving expenses, budgets and
settlement payments, and saving or deleting categories.
Who calls it: views, the admin and forms, never the other way round.
What it must never do: use float for money, or know about requests and HTML.
The money functions are plain calculations and do not touch the database.
"""

from collections.abc import Hashable, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError

from .models import (
    Budget,
    Category,
    Expense,
    ExpenseParticipant,
    ExpenseType,
    SettlementPayment,
)

# A participant can be anything hashable: a user id in the app, a name in the tests.
Participant = Hashable
MoneyInput = Decimal | int | str

PAISA = Decimal("0.01")
PAISE_PER_RUPEE = 100
ZERO_MONEY = Decimal("0.00")
ONE_HUNDRED_PERCENT = Decimal("100")
# The largest value a DecimalField(max_digits=12, decimal_places=2) column can store.
MAX_MONEY_AMOUNT = Decimal("9999999999.99")


class SplitType(models.TextChoices):
    EQUAL = "equal", "Equal"
    CUSTOM = "custom", "Custom amount"
    PERCENTAGE = "percentage", "Percentage"


class Settlement(NamedTuple):
    """One suggested payment: from_user pays to_user this amount."""

    from_user: Participant
    to_user: Participant
    amount: Decimal


class BudgetUsage(NamedTuple):
    """How much of a budget has been used."""

    spent: Decimal
    remaining: Decimal
    usage_percent: Decimal
    is_warning: bool
    is_over: bool

    @property
    def over_amount(self) -> Decimal:
        """How far over the budget the spending is, as a positive number."""
        return -self.remaining


class ParticipantRow(NamedTuple):
    """What the user entered for one person on a shared expense."""

    user: AbstractBaseUser
    paid_amount: MoneyInput
    # An amount for a custom split, a percentage for a percentage split, unused for an equal split.
    share_value: MoneyInput


def to_money(value: MoneyInput) -> Decimal:
    """
    Convert a value to a Decimal with exactly 2 decimal places.

    Why: every money value in the project must follow one rounding policy.

    Args:
        value: A Decimal, int or numeric string. Floats are refused.

    Returns:
        The value quantized to 0.01 using ROUND_HALF_UP.

    Raises:
        ValidationError: If the value is not a finite number or is too large to store.

    Example:
        to_money("10.005") -> Decimal("10.01")
    """
    number = _to_decimal(value)
    if abs(number) > MAX_MONEY_AMOUNT:
        raise ValidationError("Amount is too large.")
    return _round_to_paisa(number)


def calculate_expense_split(
    amount: MoneyInput,
    participants: Sequence[Participant],
    split_type: str,
    values: Mapping[Participant, MoneyInput] | None = None,
) -> dict[Participant, Decimal]:
    """
    Work out how much of an expense each participant consumed (their share).

    Why: shares must always add up to the expense amount exactly, so money is
    never created or lost.

    Args:
        amount: The expense total. Must be greater than zero.
        participants: Distinct participants. Their order decides who gets a leftover
            paisa when there is a tie.
        split_type: A SplitType value.
        values: One value per participant. Percentages for PERCENTAGE, amounts for
            CUSTOM. Not used for EQUAL.

    Returns:
        A dict of participant to share, in participant order.

    Raises:
        ValidationError: If the amount, participants, split type or values are invalid.

    Example:
        calculate_expense_split("100", ["A", "B", "C"], SplitType.EQUAL)
        -> {"A": Decimal("33.34"), "B": Decimal("33.33"), "C": Decimal("33.33")}
    """
    total = _validated_amount(amount)
    _validate_participants(participants)
    if split_type == SplitType.EQUAL:
        return _split_equally(total, participants)
    if split_type == SplitType.PERCENTAGE:
        return _split_by_percentage(total, participants, values)
    if split_type == SplitType.CUSTOM:
        return _split_by_custom_amount(total, participants, values)
    raise ValidationError("Unknown split type.")


def validate_paid_amounts(
    amount: MoneyInput, paid_amounts: Mapping[Participant, MoneyInput]
) -> dict[Participant, Decimal]:
    """
    Check that the amounts people actually paid add up to the expense amount.

    Why: one expense can be paid by several people, and the cash paid must equal
    the bill. A participant who paid nothing has a paid amount of zero.

    Args:
        amount: The expense total. Must be greater than zero.
        paid_amounts: Participant to the amount they paid.

    Returns:
        The same mapping with every value converted by to_money.

    Raises:
        ValidationError: If a value is negative or the total does not match.

    Example:
        validate_paid_amounts("3000", {"Rahul": "1500", "Aman": "1000", "Rohit": "500"})
        passes, while {"Rahul": "1500"} is rejected because 1500 is not 3000.
    """
    total = _validated_amount(amount)
    validated = {
        participant: _non_negative_money(paid_amount, "Paid amount")
        for participant, paid_amount in paid_amounts.items()
    }
    # RULE: sum(paid_amount) == expense.amount
    if sum(validated.values()) != total:
        raise ValidationError("Paid amounts must add up to the total amount.")
    return validated


def validate_participant_amounts(
    amount: MoneyInput,
    expense_type: str,
    created_by: Participant,
    paid_amounts: Mapping[Participant, MoneyInput],
    share_amounts: Mapping[Participant, MoneyInput],
) -> None:
    """
    Check that all participant rows of one expense obey the money rules together.

    Why: a single row can look fine while the expense as a whole does not add up.
    RULE: sum(paid_amount) and sum(share_amount) must both equal the expense amount.
    RULE: a personal expense has exactly one participant, its creator.

    Args:
        amount: The expense total.
        expense_type: An ExpenseType value.
        created_by: The participant who created the expense.
        paid_amounts: Participant to the amount they paid.
        share_amounts: Participant to their share.

    Raises:
        ValidationError: If any rule above is broken.

    Example:
        A shared 3000 paid as 1500/1000/500 with shares of 1000 each passes.
        A personal 500 with a row for someone other than the creator is rejected.
    """
    calculate_expense_split(amount, list(share_amounts), SplitType.CUSTOM, share_amounts)
    validate_paid_amounts(amount, paid_amounts)
    if set(paid_amounts) != set(share_amounts):
        raise ValidationError("Paid amounts and shares must be for the same participants.")
    if expense_type == ExpenseType.PERSONAL and set(share_amounts) != {created_by}:
        raise ValidationError("A personal expense must have exactly one participant: its creator.")


def calculate_balances(
    entries: Iterable[tuple[Participant, MoneyInput, MoneyInput]],
) -> dict[Participant, Decimal]:
    """
    Calculate each participant's net balance across one or many expenses.

    Why: the balance shows who should receive money and who owes it.
    RULE: net_balance = total_paid - total_share. Positive means should receive,
    negative means owes.

    Args:
        entries: Rows of (participant, paid_amount, share_amount). A participant can
            appear many times, once per expense.

    Returns:
        A dict of participant to net balance. Zero balances are kept.

    Raises:
        ValidationError: If a value is not a valid amount.

    Example:
        [("Rahul", "1500", "1000"), ("Aman", "1000", "1000"), ("Rohit", "500", "1000")]
        -> Rahul +500.00, Aman 0.00, Rohit -500.00
    """
    balances: dict[Participant, Decimal] = {}
    for participant, paid_amount, share_amount in entries:
        net_amount = to_money(paid_amount) - to_money(share_amount)
        balances[participant] = balances.get(participant, ZERO_MONEY) + net_amount
    return balances


def calculate_settlements(balances: Mapping[Participant, MoneyInput]) -> list[Settlement]:
    """
    Suggest the payments that bring every balance to zero.

    Why: people want a short list of who pays whom, not one payment per expense.
    RULE: greedy algorithm. Repeatedly match the largest debtor with the largest
    creditor and transfer the smaller of the two amounts. Each transfer clears at
    least one person, so n people need at most n - 1 payments.

    Args:
        balances: Participant to net balance, from calculate_balances.

    Returns:
        A list of Settlement(from_user, to_user, amount). Ties are broken by the order
        of the balances, so the same input always gives the same result.

    Raises:
        ValidationError: If total credits do not equal total debts.

    Example:
        {"A": 500, "B": -300, "C": -200} -> B pays A 300, then C pays A 200.
    """
    creditors, debtors = _separate_creditors_and_debtors(balances)
    settlements = []
    while debtors:
        # max() returns the first largest entry, which keeps ties in balance order.
        debtor = max(debtors, key=debtors.get)
        creditor = max(creditors, key=creditors.get)
        transfer = min(debtors[debtor], creditors[creditor])
        settlements.append(Settlement(debtor, creditor, transfer))
        _subtract_and_remove_if_settled(debtors, debtor, transfer)
        _subtract_and_remove_if_settled(creditors, creditor, transfer)
    return settlements


@transaction.atomic
def save_personal_expense(expense: Expense, owner: AbstractBaseUser) -> Expense:
    """
    Save a personal expense and its single participant row together.

    Why: RULE: a personal expense has exactly one participant, its creator, with
    paid_amount = share_amount = amount. Dashboard, reports and budgets can then use the
    same participant queries for personal and shared expenses. The transaction makes sure
    an expense is never left without its row. Used for both adding and editing.

    Args:
        expense: The expense to save, with its details already set.
        owner: The logged-in user, who becomes or remains its creator.

    Returns:
        The saved expense.

    Example:
        A personal expense of 500 gets one row for the owner: paid 500, share 500.
        Editing the amount to 600 changes that row to paid 600, share 600.
    """
    expense.created_by = owner
    expense.expense_type = ExpenseType.PERSONAL
    expense.save()
    _replace_participant_rows(expense, {owner: expense.amount}, {owner: expense.amount})
    return expense


@transaction.atomic
def save_shared_expense(
    expense: Expense,
    owner: AbstractBaseUser,
    split_type: str,
    rows: Sequence[ParticipantRow],
) -> Expense:
    """
    Calculate the shares of a shared expense and save it with all its participant rows.

    Why: RULE: paid amounts and shares must both add up to the amount, and no partial record
    may remain if anything fails. Nothing is written until the numbers are valid, and the
    transaction undoes every write if a later one fails. Used for both adding and editing.

    Args:
        expense: The expense to save, with its details already set.
        owner: The logged-in user, who becomes or remains its creator. They do not have to
            be one of the participants.
        split_type: A SplitType value.
        rows: One row per participant, in the order that decides who gets a leftover paisa.

    Returns:
        The saved expense.

    Raises:
        ValidationError: If the split or the paid amounts are invalid. Nothing is saved.

    Example:
        3000 split equally, with paid amounts of 1500, 1000 and 500, gives three rows with a
        share of 1000 each.
    """
    shares = calculate_expense_split(
        expense.amount,
        [row.user for row in rows],
        split_type,
        {row.user: row.share_value for row in rows},
    )
    paid_amounts = validate_paid_amounts(
        expense.amount, {row.user: row.paid_amount for row in rows}
    )
    expense.created_by = owner
    expense.expense_type = ExpenseType.SHARED
    expense.save()
    _replace_participant_rows(expense, paid_amounts, shares)
    return expense


def calculate_expense_settlements(participants: Iterable[ExpenseParticipant]) -> list[Settlement]:
    """
    Suggest who pays whom to settle one expense, using usernames.

    Args:
        participants: The participant rows of one expense, with their users loaded.

    Returns:
        A list of Settlement. It is empty if nobody owes anything.

    Example:
        Paid 1500, 1000 and 500 with shares of 1000 each gives: Rohit pays Rahul 500.
    """
    entries = [(row.user.username, row.paid_amount, row.share_amount) for row in participants]
    return calculate_settlements(calculate_balances(entries))


def calculate_pairwise_debts(
    expenses: Iterable[Iterable[tuple[Participant, MoneyInput, MoneyInput]]],
    payments: Iterable[tuple[Participant, Participant, Decimal]] = (),
) -> list[Settlement]:
    """
    Work out what each pair of people owe each other across many expenses, after payments.

    Why: "who owes whom" is about pairs. Each expense is settled on its own, so a debt only
    ever links two people who shared that expense. Debts between the same two people are
    then netted, so the result never suggests a payment between strangers.
    RULE: pairwise balance = what expenses say they owe each other, minus recorded payments.

    Args:
        expenses: One list of (participant, paid_amount, share_amount) rows per expense.
        payments: Rows of (from_user, to_user, amount) for money already paid back.

    Returns:
        A list of Settlement, at most one per pair. Pairs that cancel out are left out.

    Raises:
        ValidationError: If the rows of one expense do not balance.

    Example:
        A owes B 500 from a dinner. If A pays B 200, A still owes 300. If A pays B 500,
        nobody owes anything. If A pays B 700, B now owes A 200.
    """
    owed: dict[tuple[Participant, Participant], Decimal] = {}
    for rows in expenses:
        for debt in calculate_settlements(calculate_balances(rows)):
            _add_debt(owed, debt.from_user, debt.to_user, debt.amount)
    # A payment from A to B counts as a debt from B to A, which cancels what A owed.
    for from_user, to_user, amount in payments:
        _add_debt(owed, to_user, from_user, amount)
    return _net_opposite_debts(owed)


def _add_debt(
    owed: dict[tuple[Participant, Participant], Decimal],
    from_user: Participant,
    to_user: Participant,
    amount: Decimal,
) -> None:
    """Add to what one person owes another."""
    pair = (from_user, to_user)
    owed[pair] = owed.get(pair, ZERO_MONEY) + amount


def _net_opposite_debts(
    owed: Mapping[tuple[Participant, Participant], Decimal],
) -> list[Settlement]:
    """Turn debts in both directions between two people into one debt, or none."""
    debts = []
    for (from_user, to_user), amount in owed.items():
        net_amount = amount - owed.get((to_user, from_user), ZERO_MONEY)
        if net_amount > 0:
            debts.append(Settlement(from_user, to_user, net_amount))
    return debts


def _replace_participant_rows(
    expense: Expense,
    paid_amounts: Mapping[AbstractBaseUser, Decimal],
    share_amounts: Mapping[AbstractBaseUser, Decimal],
) -> None:
    """Make an expense's participant rows match exactly these people and amounts."""
    expense.participants.exclude(user__in=list(share_amounts)).delete()
    for user, share_amount in share_amounts.items():
        ExpenseParticipant.objects.update_or_create(
            expense=expense,
            user=user,
            defaults={"paid_amount": paid_amounts[user], "share_amount": share_amount},
        )


def save_category(category: Category) -> None:
    """
    Save a category, reporting a duplicate name as a normal validation error.

    Why: the form checks for duplicates first, but two requests at the same moment can
    both pass that check. The database constraint then stops the second one, and the user
    should see a message instead of a server error.

    Args:
        category: The category to save, with its name and owner already set.

    Raises:
        ValidationError: If the database refuses the name as a duplicate.
    """
    try:
        # A failed statement would otherwise break any transaction this call is part of.
        with transaction.atomic():
            category.save()
    except IntegrityError as error:
        raise ValidationError("A category with this name already exists.") from error


def delete_category(category: Category) -> None:
    """
    Delete a custom category, unless expenses still use it.

    Why: RULE: old expenses must never lose their category, so a category in use is
    protected instead of being reassigned to something the user did not choose.

    Args:
        category: The category to delete. The caller has already checked ownership.

    Raises:
        ValidationError: If any expense uses this category.

    Example:
        A category used by 2 expenses stays, with the message
        "This category is used by 2 expense(s), so it cannot be deleted."
    """
    try:
        category.delete()
    except ProtectedError as error:
        expense_count = len(error.protected_objects)
        raise ValidationError(
            f"This category is used by {expense_count} expense(s), so it cannot be deleted."
        ) from error


def save_settlement_payment(
    payment: SettlementPayment, created_by: AbstractBaseUser
) -> SettlementPayment:
    """
    Record money that one person paid another.

    Why: RULE: a payment is between two different people, and the person recording it must
    be one of them. The database has the same rules as constraints, and this gives a readable
    message first.

    Args:
        payment: The payment to save, with its parties, amount and date already set.
        created_by: The logged-in user recording it.

    Returns:
        The saved payment.

    Raises:
        ValidationError: If a rule above is broken.

    Example:
        Rohit records that he paid Rahul 500. What Rohit owes Rahul goes down by 500.
    """
    if payment.from_user_id == payment.to_user_id:
        raise ValidationError("A payment must be between two different people.")
    if created_by.pk not in (payment.from_user_id, payment.to_user_id):
        raise ValidationError("You can only record a payment that you made or received.")
    payment.created_by = created_by
    payment.save()
    return payment


def save_budget(budget: Budget) -> None:
    """
    Save a budget, reporting a second budget for the same month as a validation error.

    Why: the form checks for duplicates first, but two requests at the same moment can both
    pass that check. The database constraint then stops the second one.

    Args:
        budget: The budget to save, with its user, year, month and amount already set.

    Raises:
        ValidationError: If the database refuses it as a duplicate.
    """
    try:
        # A failed statement would otherwise break any transaction this call is part of.
        with transaction.atomic():
            budget.save()
    except IntegrityError as error:
        raise ValidationError("You already have a budget for this month.") from error


def calculate_budget_usage(
    amount: Decimal, spent: Decimal, warning_threshold_percent: int
) -> BudgetUsage:
    """
    Work out how much of a budget has been used.

    Why: RULE: budget usage is based on "my spending", which is my share of each expense. The
    warning starts when usage reaches the threshold. Remaining goes negative when over budget.

    Args:
        amount: The budget. Always greater than zero, because a constraint enforces it.
        spent: The user's spending in the budget's month.
        warning_threshold_percent: The usage at which the warning starts, such as 80.

    Returns:
        A BudgetUsage with spent, remaining, usage_percent, is_warning and is_over.

    Example:
        A budget of 5000 with 4000 spent and a threshold of 80 gives remaining 1000, usage
        80.00 and a warning. With 5500 spent it gives remaining -500, usage 110.00, and over.
    """
    return BudgetUsage(
        spent=spent,
        remaining=amount - spent,
        usage_percent=_round_to_paisa(spent * ONE_HUNDRED_PERCENT / amount),
        # Compared without dividing, so rounding cannot move anyone across the boundary.
        is_warning=spent * ONE_HUNDRED_PERCENT >= warning_threshold_percent * amount,
        is_over=spent > amount,
    )


def _round_to_paisa(number: Decimal) -> Decimal:
    """Round to 2 decimal places using the project's rounding policy."""
    return number.quantize(PAISA, rounding=ROUND_HALF_UP)


def _to_decimal(value: MoneyInput) -> Decimal:
    """Convert a value to a finite Decimal, refusing floats, NaN and Infinity."""
    # RULE: money never goes through float, because 0.1 + 0.2 != 0.3 in binary floats.
    if isinstance(value, float):
        raise ValidationError("Use Decimal for money, not float.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValidationError("Enter a valid number.") from error
    if not number.is_finite():
        raise ValidationError("Enter a valid number.")
    return number


def _validated_amount(amount: MoneyInput) -> Decimal:
    """Return the expense amount as money, refusing zero and negative amounts."""
    total = to_money(amount)
    # RULE: an expense amount must be greater than zero.
    if total <= 0:
        raise ValidationError("Amount must be greater than zero.")
    return total


def _validate_participants(participants: Sequence[Participant]) -> None:
    """Refuse an empty participant list and repeated participants."""
    if not participants:
        raise ValidationError("At least one participant is required.")
    if len(set(participants)) != len(participants):
        raise ValidationError("Each participant can appear only once.")


def _validate_values_match_participants(
    values: Mapping[Participant, MoneyInput] | None, participants: Sequence[Participant]
) -> None:
    """Refuse values that are missing a participant or name someone else."""
    if values is None or set(values) != set(participants):
        raise ValidationError("Provide exactly one value for each participant.")


def _non_negative_money(value: MoneyInput, label: str) -> Decimal:
    """Return a value as money, refusing anything below zero before it is rounded."""
    if _to_decimal(value) < 0:
        raise ValidationError(f"{label} cannot be negative.")
    return to_money(value)


def _split_equally(
    total: Decimal, participants: Sequence[Participant]
) -> dict[Participant, Decimal]:
    """Give every participant the same share, within one paisa."""
    equal_weights = [Decimal(1)] * len(participants)
    shares = _allocate_by_largest_remainder(total, equal_weights)
    return dict(zip(participants, shares, strict=True))


def _split_by_percentage(
    total: Decimal,
    participants: Sequence[Participant],
    percentages: Mapping[Participant, MoneyInput] | None,
) -> dict[Participant, Decimal]:
    """Give each participant their percentage of the total."""
    _validate_values_match_participants(percentages, participants)
    weights = [_to_decimal(percentages[participant]) for participant in participants]
    if any(weight < 0 for weight in weights):
        raise ValidationError("Percentage cannot be negative.")
    # RULE: percentages must sum to exactly 100.
    if sum(weights) != ONE_HUNDRED_PERCENT:
        raise ValidationError("Percentages must add up to exactly 100.")
    shares = _allocate_by_largest_remainder(total, weights)
    return dict(zip(participants, shares, strict=True))


def _split_by_custom_amount(
    total: Decimal,
    participants: Sequence[Participant],
    custom_amounts: Mapping[Participant, MoneyInput] | None,
) -> dict[Participant, Decimal]:
    """Use the amounts the user typed, after checking they match the total."""
    _validate_values_match_participants(custom_amounts, participants)
    shares = {
        participant: _non_negative_money(custom_amounts[participant], "Custom share")
        for participant in participants
    }
    # RULE: custom shares must sum to the total.
    if sum(shares.values()) != total:
        raise ValidationError("Custom shares must add up to the total amount.")
    return shares


def _allocate_by_largest_remainder(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """
    Split a total in proportion to weights so the parts add up to the total exactly.

    Why: rounding each share on its own can lose or create money. 100.00 / 3 rounds
    to 33.33 three times, which is only 99.99.
    How: work in whole paise, give everyone their share rounded down, then hand out
    the leftover paise one by one.

    Example:
        100.00 with weights 1, 1, 1 is 10000 paise, so 3333 each with 1 left over.
        The first participant gets it: 33.34, 33.33, 33.33.
    """
    total_paise = int(total * PAISE_PER_RUPEE)
    total_weight = sum(weights)
    shares_in_paise = []
    remainders = []
    for weight in weights:
        share, remainder = divmod(total_paise * weight, total_weight)
        shares_in_paise.append(int(share))
        remainders.append(remainder)

    leftover_paise = total_paise - sum(shares_in_paise)
    # RULE: largest remainder first. sorted() is stable, so ties keep participant order.
    order = sorted(range(len(weights)), key=lambda index: remainders[index], reverse=True)
    for index in order[:leftover_paise]:
        shares_in_paise[index] += 1

    return [paise * PAISA for paise in shares_in_paise]


def _separate_creditors_and_debtors(
    balances: Mapping[Participant, MoneyInput],
) -> tuple[dict[Participant, Decimal], dict[Participant, Decimal]]:
    """Return who should receive and who owes, both as positive amounts. Zeros are left out."""
    creditors = {}
    debtors = {}
    for participant, balance in balances.items():
        # Not to_money: a total across many expenses can exceed what one expense may hold.
        net_amount = _round_to_paisa(_to_decimal(balance))
        if net_amount > 0:
            creditors[participant] = net_amount
        elif net_amount < 0:
            debtors[participant] = -net_amount
    # RULE: invariant, total credits must equal total debts.
    if sum(creditors.values()) != sum(debtors.values()):
        raise ValidationError("Balances do not add up: total credits must equal total debts.")
    return creditors, debtors


def _subtract_and_remove_if_settled(
    remaining: dict[Participant, Decimal], participant: Participant, transfer: Decimal
) -> None:
    """Reduce what a participant still has to pay or receive, dropping them at zero."""
    remaining[participant] -= transfer
    if remaining[participant] == 0:
        del remaining[participant]
