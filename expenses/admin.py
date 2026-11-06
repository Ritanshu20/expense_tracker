"""
Django admin for expenses.

What lives here: how staff see and edit categories, expenses and budgets.
Who uses it: superusers, at /admin/.
What it must never do: let staff save an expense whose money does not add up.
"""

from django.contrib import admin
from django.forms import BaseInlineFormSet

from .models import Budget, Category, Expense, ExpenseParticipant, SettlementPayment
from .services import validate_participant_amounts


class ExpenseParticipantInlineFormSet(BaseInlineFormSet):
    """Checks the money rules across all participant rows of one expense."""

    def clean(self) -> None:
        super().clean()
        expense = self.instance
        # If the expense form or a row already has errors, there is nothing reliable to compare.
        if any(self.errors) or expense.amount is None or expense.created_by_id is None:
            return
        rows = [
            form.cleaned_data
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        validate_participant_amounts(
            amount=expense.amount,
            expense_type=expense.expense_type,
            created_by=expense.created_by_id,
            paid_amounts={row["user"].pk: row["paid_amount"] for row in rows},
            share_amounts={row["user"].pk: row["share_amount"] for row in rows},
        )


class ExpenseParticipantInline(admin.TabularInline):
    model = ExpenseParticipant
    formset = ExpenseParticipantInlineFormSet
    fields = ("user", "paid_amount", "share_amount")
    extra = 2


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    list_filter = (("owner", admin.EmptyFieldListFilter),)
    search_fields = ("name", "owner__username", "owner__email")
    ordering = ("name",)
    list_select_related = ("owner",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "amount",
        "date",
        "category",
        "payment_method",
        "expense_type",
        "created_by",
    )
    list_filter = ("expense_type", "payment_method", "category", "date")
    search_fields = ("title", "description", "created_by__username", "created_by__email")
    ordering = ("-date", "-id")
    date_hierarchy = "date"
    list_select_related = ("category", "created_by")
    inlines = (ExpenseParticipantInline,)

    def get_deleted_objects(self, objs, request):
        """Allow deleting a whole expense, even though its rows cannot be deleted alone."""
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        perms_needed.discard(ExpenseParticipant._meta.verbose_name)
        return to_delete, model_count, perms_needed, protected


@admin.register(ExpenseParticipant)
class ExpenseParticipantAdmin(admin.ModelAdmin):
    """View-only list. Rows change on the expense page, where their sums are checked together."""

    list_display = ("expense", "user", "paid_amount", "share_amount")
    list_filter = ("expense__expense_type",)
    search_fields = ("expense__title", "user__username", "user__email")
    ordering = ("-expense__date", "id")
    list_select_related = ("expense", "user")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ("user", "year", "month", "amount")
    list_filter = ("year", "month")
    search_fields = ("user__username", "user__email")
    ordering = ("-year", "-month")
    list_select_related = ("user",)


@admin.register(SettlementPayment)
class SettlementPaymentAdmin(admin.ModelAdmin):
    list_display = ("date", "from_user", "to_user", "amount", "note", "created_by")
    list_filter = ("date",)
    search_fields = ("from_user__username", "to_user__username", "note")
    ordering = ("-date", "-id")
    date_hierarchy = "date"
    list_select_related = ("from_user", "to_user", "created_by")
