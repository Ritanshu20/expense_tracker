"""
Views for expenses.

What lives here: thin views that connect URLs, forms, selectors and services.
Who calls it: expenses/urls.py.
What it must never do: hold business rules, or fetch a row without checking who may see it.
Every view here needs a login, because LoginRequiredMiddleware is on.
"""

from django.contrib import messages
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .forms import (
    BaseParticipantRowFormSet,
    BudgetForm,
    CategoryForm,
    ExpenseFilterForm,
    ExpenseForm,
    ParticipantRowFormSet,
    SettlementPaymentForm,
)
from .models import Budget, Category, Expense, ExpenseType, SettlementPayment
from .selectors import (
    filter_expenses,
    get_budgets_for_user,
    get_categories_for_user,
    get_expenses_for_user,
    get_payments_for_user,
    get_settlement_summary,
)
from .services import (
    SplitType,
    calculate_expense_settlements,
    delete_category,
    save_budget,
    save_category,
    save_personal_expense,
    save_settlement_payment,
    save_shared_expense,
)

EXPENSES_PER_PAGE = 20
PARTICIPANT_FORMS_PREFIX = "participants"


def _get_own_category_or_404(request: HttpRequest, pk: int) -> Category:
    """Return one of the user's own categories, or a 404."""
    # SAFETY: default categories and other users' categories are not in this queryset, so they
    # give a 404. The page does not reveal whether such a category exists.
    return get_object_or_404(Category.objects.owned_by(request.user), pk=pk)


def _save_category_form(form: CategoryForm) -> bool:
    """Save a valid form. Returns False, with an error on the form, if the name is refused."""
    try:
        save_category(form.save(commit=False))
    except ValidationError as error:
        form.add_error("name", error)
        return False
    return True


@require_GET
def category_list(request: HttpRequest) -> HttpResponse:
    """List the default categories and the user's own."""
    categories = get_categories_for_user(request.user)
    return render(request, "expenses/category_list.html", {"categories": categories})


@require_http_methods(["GET", "POST"])
def category_create(request: HttpRequest) -> HttpResponse:
    """Add a custom category for the logged-in user."""
    form = CategoryForm(request.POST or None, owner=request.user)
    if form.is_valid() and _save_category_form(form):
        messages.success(request, "Category added.")
        return redirect("category_list")
    return render(request, "expenses/category_form.html", {"form": form, "title": "Add category"})


@require_http_methods(["GET", "POST"])
def category_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Rename one of the user's own categories."""
    category = _get_own_category_or_404(request, pk)
    form = CategoryForm(request.POST or None, instance=category, owner=request.user)
    if form.is_valid() and _save_category_form(form):
        messages.success(request, "Category updated.")
        return redirect("category_list")
    return render(request, "expenses/category_form.html", {"form": form, "title": "Edit category"})


@require_http_methods(["GET", "POST"])
def category_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Ask for confirmation on GET, and delete on POST unless expenses use the category."""
    category = _get_own_category_or_404(request, pk)
    if request.method == "GET":
        return render(request, "expenses/category_confirm_delete.html", {"category": category})
    try:
        delete_category(category)
    except ValidationError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Category deleted.")
    return redirect("category_list")


def _get_visible_expense_or_404(request: HttpRequest, pk: int) -> Expense:
    """Return an expense the user may view, or a 404."""
    # SAFETY: other users' expenses are not in this queryset, so changing the id in the URL
    # gives a 404. The page does not reveal whether such an expense exists.
    return get_object_or_404(get_expenses_for_user(request.user), pk=pk)


def _get_changeable_expense(request: HttpRequest, pk: int) -> Expense:
    """Return an expense the user may change. Outsiders get a 404, participants a 403."""
    expense = _get_visible_expense_or_404(request, pk)
    # SAFETY: RULE: participants can view an expense, but only its creator can change it.
    if expense.created_by_id != request.user.pk:
        raise PermissionDenied
    return expense


@require_GET
def expense_list(request: HttpRequest) -> HttpResponse:
    """List the user's expenses with search, filters and pagination."""
    filter_form = ExpenseFilterForm(request.GET, user=request.user)
    expenses = get_expenses_for_user(request.user)
    if filter_form.is_valid():
        expenses = filter_expenses(expenses, filter_form.cleaned_data)
    page = Paginator(expenses, EXPENSES_PER_PAGE).get_page(request.GET.get("page"))
    context = {"filter_form": filter_form, "page": page}
    return render(request, "expenses/expense_list.html", context)


@require_GET
def expense_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Show one expense, who took part in it and who pays whom."""
    expense = _get_visible_expense_or_404(request, pk)
    participants = list(expense.participants.select_related("user"))
    context = {
        "expense": expense,
        "participants": participants,
        "settlements": calculate_expense_settlements(participants),
    }
    return render(request, "expenses/expense_detail.html", context)


def _save_expense_forms(
    form: ExpenseForm, participant_forms: BaseParticipantRowFormSet, user: AbstractBaseUser
) -> Expense | None:
    """Save the submitted expense. Returns None, with errors on the forms, if it is refused."""
    if not form.is_valid():
        return None
    expense = form.save(commit=False)
    if form.cleaned_data["expense_type"] == ExpenseType.PERSONAL:
        return save_personal_expense(expense, user)
    if not participant_forms.is_valid():
        return None
    try:
        return save_shared_expense(
            expense, user, form.cleaned_data["split_type"], participant_forms.get_rows()
        )
    except ValidationError as error:
        form.add_error(None, error)
        return None


def _render_expense_form(
    request: HttpRequest,
    form: ExpenseForm,
    participant_forms: BaseParticipantRowFormSet,
    title: str,
) -> HttpResponse:
    context = {"form": form, "participant_forms": participant_forms, "title": title}
    return render(request, "expenses/expense_form.html", context)


@require_http_methods(["GET", "POST"])
def expense_create(request: HttpRequest) -> HttpResponse:
    """Add a personal or shared expense for the logged-in user."""
    form = ExpenseForm(
        request.POST or None,
        user=request.user,
        initial={"date": timezone.localdate(), "expense_type": ExpenseType.PERSONAL},
    )
    # The creator usually takes part, so their row starts filled in. They can remove it.
    participant_forms = ParticipantRowFormSet(
        request.POST or None,
        initial=[{"person": request.user.username}],
        prefix=PARTICIPANT_FORMS_PREFIX,
    )
    if request.method == "POST":
        expense = _save_expense_forms(form, participant_forms, request.user)
        if expense is not None:
            messages.success(request, "Expense added.")
            return redirect("expense_detail", pk=expense.pk)
    return _render_expense_form(request, form, participant_forms, "Add expense")


@require_http_methods(["GET", "POST"])
def expense_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Change an expense. Only its creator can do this."""
    expense = _get_changeable_expense(request, pk)
    # The split type is not stored, so the page starts from the stored shares as custom amounts.
    form = ExpenseForm(
        request.POST or None,
        instance=expense,
        user=request.user,
        initial={"split_type": SplitType.CUSTOM},
    )
    participant_forms = ParticipantRowFormSet(
        request.POST or None,
        initial=[
            {
                "person": row.user.username,
                "paid_amount": row.paid_amount,
                "share_value": row.share_amount,
            }
            for row in expense.participants.select_related("user")
        ],
        prefix=PARTICIPANT_FORMS_PREFIX,
    )
    if request.method == "POST":
        if _save_expense_forms(form, participant_forms, request.user) is not None:
            messages.success(request, "Expense updated.")
            return redirect("expense_detail", pk=expense.pk)
    return _render_expense_form(request, form, participant_forms, "Edit expense")


@require_http_methods(["GET", "POST"])
def expense_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Ask for confirmation on GET, and delete on POST. Only the creator can do this."""
    expense = _get_changeable_expense(request, pk)
    if request.method == "GET":
        return render(request, "expenses/expense_confirm_delete.html", {"expense": expense})
    expense.delete()
    messages.success(request, "Expense deleted.")
    return redirect("expense_list")


def _get_own_budget_or_404(request: HttpRequest, pk: int) -> Budget:
    """Return one of the user's own budgets, or a 404."""
    # SAFETY: other users' budgets are not in this queryset, so changing the id gives a 404.
    return get_object_or_404(Budget.objects.filter(user=request.user), pk=pk)


def _save_budget_form(form: BudgetForm) -> bool:
    """Save a valid form. Returns False, with an error on the form, if it is refused."""
    try:
        save_budget(form.save(commit=False))
    except ValidationError as error:
        form.add_error(None, error)
        return False
    return True


@require_GET
def budget_list(request: HttpRequest) -> HttpResponse:
    """List the user's monthly budgets with how much of each has been used."""
    context = {"budgets": get_budgets_for_user(request.user)}
    return render(request, "expenses/budget_list.html", context)


@require_http_methods(["GET", "POST"])
def budget_create(request: HttpRequest) -> HttpResponse:
    """Add a monthly budget for the logged-in user, starting from the current month."""
    today = timezone.localdate()
    form = BudgetForm(
        request.POST or None,
        owner=request.user,
        initial={"year": today.year, "month": today.month},
    )
    if form.is_valid() and _save_budget_form(form):
        messages.success(request, "Budget added.")
        return redirect("budget_list")
    return render(request, "expenses/budget_form.html", {"form": form, "title": "Add budget"})


@require_http_methods(["GET", "POST"])
def budget_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Change one of the user's own budgets."""
    budget = _get_own_budget_or_404(request, pk)
    form = BudgetForm(request.POST or None, instance=budget, owner=request.user)
    if form.is_valid() and _save_budget_form(form):
        messages.success(request, "Budget updated.")
        return redirect("budget_list")
    return render(request, "expenses/budget_form.html", {"form": form, "title": "Edit budget"})


@require_http_methods(["GET", "POST"])
def budget_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Ask for confirmation on GET, and delete on POST."""
    budget = _get_own_budget_or_404(request, pk)
    if request.method == "GET":
        return render(request, "expenses/budget_confirm_delete.html", {"budget": budget})
    budget.delete()
    messages.success(request, "Budget deleted.")
    return redirect("budget_list")


def _get_visible_payment_or_404(request: HttpRequest, pk: int) -> SettlementPayment:
    """Return a payment the user may view, or a 404."""
    # SAFETY: payments between other people are not in this queryset, so they give a 404.
    return get_object_or_404(get_payments_for_user(request.user), pk=pk)


def _get_changeable_payment(request: HttpRequest, pk: int) -> SettlementPayment:
    """Return a payment the user may change. Outsiders get a 404, the other person a 403."""
    payment = _get_visible_payment_or_404(request, pk)
    # SAFETY: RULE: both people can view a payment, but only the person who recorded it can
    # change it.
    if payment.created_by_id != request.user.pk:
        raise PermissionDenied
    return payment


def _save_payment_form(form: SettlementPaymentForm, user: AbstractBaseUser) -> bool:
    """Save a valid form. Returns False, with an error on the form, if the payment is refused."""
    try:
        save_settlement_payment(form.get_payment(), user)
    except ValidationError as error:
        form.add_error(None, error)
        return False
    return True


@require_GET
def settlement_list(request: HttpRequest) -> HttpResponse:
    """Show who owes whom, and the payments the user made or received."""
    context = {
        "settlements": get_settlement_summary(request.user),
        "payments": get_payments_for_user(request.user),
    }
    return render(request, "expenses/settlement_list.html", context)


@require_GET
def settlement_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Show one payment. Both people on it can view it."""
    payment = _get_visible_payment_or_404(request, pk)
    return render(request, "expenses/settlement_detail.html", {"payment": payment})


@require_http_methods(["GET", "POST"])
def settlement_create(request: HttpRequest) -> HttpResponse:
    """Record a payment. A "Record payment" link can fill in the person, direction and amount."""
    form = SettlementPaymentForm(
        request.POST or None,
        user=request.user,
        initial={
            "date": timezone.localdate(),
            "direction": request.GET.get("direction"),
            "person": request.GET.get("person"),
            "amount": request.GET.get("amount"),
        },
    )
    if form.is_valid() and _save_payment_form(form, request.user):
        messages.success(request, "Payment recorded.")
        return redirect("settlement_list")
    context = {"form": form, "title": "Record a payment"}
    return render(request, "expenses/settlement_form.html", context)


@require_http_methods(["GET", "POST"])
def settlement_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Change a payment. Only the person who recorded it can do this."""
    payment = _get_changeable_payment(request, pk)
    form = SettlementPaymentForm(request.POST or None, instance=payment, user=request.user)
    if form.is_valid() and _save_payment_form(form, request.user):
        messages.success(request, "Payment updated.")
        return redirect("settlement_list")
    context = {"form": form, "title": "Edit payment"}
    return render(request, "expenses/settlement_form.html", context)


@require_http_methods(["GET", "POST"])
def settlement_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Ask for confirmation on GET, and delete on POST. The balance then goes back up."""
    payment = _get_changeable_payment(request, pk)
    if request.method == "GET":
        return render(request, "expenses/settlement_confirm_delete.html", {"payment": payment})
    payment.delete()
    messages.success(request, "Payment deleted.")
    return redirect("settlement_list")
