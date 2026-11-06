"""
Forms for expenses.

What lives here: validation of what users type.
Who uses it: expenses/views.py.
What it must never do: take an owner or any other permission-related value from submitted data.
"""

from django import forms
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.dates import MONTHS

from config.forms import BootstrapFormMixin

from .models import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    ZERO_AMOUNT,
    Budget,
    Category,
    Expense,
    ExpenseType,
    PaymentMethod,
    SettlementPayment,
)
from .selectors import find_user_by_username_or_email, get_categories_for_user
from .services import ParticipantRow, SplitType

# The only format a browser's date input can show.
HTML_DATE_FORMAT = "%Y-%m-%d"
MAX_PARTICIPANTS = 50
EARLIEST_BUDGET_YEAR = 2000
LATEST_BUDGET_YEAR = 2100


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    """Add or rename a custom category. Only the name can be typed."""

    class Meta:
        model = Category
        fields = ("name",)

    def __init__(self, *args, owner: AbstractBaseUser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.owner = owner
        # SAFETY: the owner is the logged-in user and is never read from the submitted data.
        self.instance.owner = owner

    def clean_name(self) -> str:
        name = self.cleaned_data["name"]
        # RULE: a name cannot repeat one of the user's categories or a default category, in any
        # letter case, because two entries called "Food" in a dropdown could not be told apart.
        same_name = Category.objects.visible_to(self.owner).filter(name__iexact=name)
        if same_name.exclude(pk=self.instance.pk).exists():
            raise ValidationError("A category with this name already exists.")
        return name


class ExpenseForm(BootstrapFormMixin, forms.ModelForm):
    """The details of an expense. Who created it is set by the view, never by the form."""

    # Only used to calculate the shares. The result is stored, not the choice.
    split_type = forms.ChoiceField(
        choices=SplitType.choices, initial=SplitType.EQUAL, required=False, label="How to split"
    )

    class Meta:
        model = Expense
        fields = (
            "title",
            "description",
            "amount",
            "date",
            "category",
            "payment_method",
            "expense_type",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "date": forms.DateInput(attrs={"type": "date"}, format=HTML_DATE_FORMAT),
        }

    def __init__(self, *args, user: AbstractBaseUser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # SAFETY: a user can only pick a default category or one of their own.
        self.fields["category"].queryset = get_categories_for_user(user)
        self.fields["category"].empty_label = "Choose a category"
        self.fields["payment_method"].choices = [
            ("", "Choose a payment method"),
            *PaymentMethod.choices,
        ]


class ParticipantRowForm(BootstrapFormMixin, forms.Form):
    """One person on a shared expense: who they are, what they paid and their share."""

    person = forms.CharField(label="Username or email", max_length=254)
    paid_amount = forms.DecimalField(
        label="Paid",
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        min_value=ZERO_AMOUNT,
        required=False,
    )
    share_value = forms.DecimalField(
        label="Share",
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        min_value=ZERO_AMOUNT,
        required=False,
    )

    def clean_person(self) -> AbstractBaseUser:
        # RULE: no user directory. An exact match is the only way to reach another account.
        user = find_user_by_username_or_email(self.cleaned_data["person"])
        if user is None:
            raise ValidationError("No user found with this exact username or email.")
        return user


class BaseParticipantRowFormSet(forms.BaseFormSet):
    """All the participant rows of one shared expense."""

    def get_rows(self) -> list[ParticipantRow]:
        """Return the rows that were filled in. A blank amount counts as zero."""
        return [
            ParticipantRow(
                user=form.cleaned_data["person"],
                paid_amount=form.cleaned_data["paid_amount"] or ZERO_AMOUNT,
                share_value=form.cleaned_data["share_value"] or ZERO_AMOUNT,
            )
            for form in self.forms
            if form.cleaned_data
        ]


ParticipantRowFormSet = forms.formset_factory(
    ParticipantRowForm,
    formset=BaseParticipantRowFormSet,
    extra=1,
    min_num=1,
    max_num=MAX_PARTICIPANTS,
    validate_max=True,
)


class BudgetForm(BootstrapFormMixin, forms.ModelForm):
    """A monthly budget. The owner is the logged-in user, and is never read from the form."""

    year = forms.IntegerField(min_value=EARLIEST_BUDGET_YEAR, max_value=LATEST_BUDGET_YEAR)
    month = forms.TypedChoiceField(choices=list(MONTHS.items()), coerce=int)

    class Meta:
        model = Budget
        fields = ("year", "month", "amount")

    def __init__(self, *args, owner: AbstractBaseUser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.owner = owner
        # SAFETY: the owner is the logged-in user and is never read from the submitted data.
        self.instance.user = owner

    def clean(self) -> dict:
        cleaned_data = super().clean()
        year = cleaned_data.get("year")
        month = cleaned_data.get("month")
        if year and month:
            # RULE: one budget per user per month. Django skips the database constraint here,
            # because the owner is not a field on this form.
            same_month = Budget.objects.filter(user=self.owner, year=year, month=month)
            if same_month.exclude(pk=self.instance.pk).exists():
                raise ValidationError("You already have a budget for this month.")
        return cleaned_data


class PaymentDirection(models.TextChoices):
    I_PAID = "i_paid", "I paid them"
    THEY_PAID = "they_paid", "They paid me"


class SettlementPaymentForm(BootstrapFormMixin, forms.ModelForm):
    """Record that the logged-in user paid someone, or that someone paid them."""

    direction = forms.ChoiceField(choices=PaymentDirection.choices, label="Who paid")
    person = forms.CharField(label="Other person's username or email", max_length=254)

    field_order = ("direction", "person", "amount", "date", "note")

    class Meta:
        model = SettlementPayment
        fields = ("amount", "date", "note")
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format=HTML_DATE_FORMAT)}

    def __init__(self, *args, user: AbstractBaseUser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        if self.instance.pk:
            self._fill_in_the_parties_of_an_existing_payment()

    def _fill_in_the_parties_of_an_existing_payment(self) -> None:
        i_paid = self.instance.from_user_id == self.user.pk
        other_person = self.instance.to_user if i_paid else self.instance.from_user
        self.initial["direction"] = (
            PaymentDirection.I_PAID if i_paid else PaymentDirection.THEY_PAID
        )
        self.initial["person"] = other_person.username

    def clean_person(self) -> AbstractBaseUser:
        # RULE: no user directory. An exact match is the only way to reach another account.
        user = find_user_by_username_or_email(self.cleaned_data["person"])
        if user is None:
            raise ValidationError("No user found with this exact username or email.")
        return user

    def get_payment(self) -> SettlementPayment:
        """Return the unsaved payment, with its parties worked out from the direction."""
        payment = self.save(commit=False)
        other_person = self.cleaned_data["person"]
        # SAFETY: the logged-in user is always one of the two parties.
        if self.cleaned_data["direction"] == PaymentDirection.I_PAID:
            payment.from_user, payment.to_user = self.user, other_person
        else:
            payment.from_user, payment.to_user = other_person, self.user
        return payment


class ExpenseFilterForm(BootstrapFormMixin, forms.Form):
    """The search box and filters above the expense list. Every field is optional."""

    q = forms.CharField(required=False, label="Search")
    date_from = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}, format=HTML_DATE_FORMAT),
    )
    date_to = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}, format=HTML_DATE_FORMAT),
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(), required=False, empty_label="All"
    )
    payment_method = forms.ChoiceField(
        choices=[("", "All"), *PaymentMethod.choices], required=False, label="Payment"
    )
    expense_type = forms.ChoiceField(
        choices=[("", "All"), *ExpenseType.choices], required=False, label="Type"
    )

    def __init__(self, *args, user: AbstractBaseUser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = get_categories_for_user(user)

    def clean(self) -> dict:
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise ValidationError("The start date cannot be after the end date.")
        return cleaned_data
