"""
Views for reports.

What lives here: thin views that choose a period. All numbers come from expenses/selectors.py.
Who calls it: reports/urls.py.
What it must never do: calculate money here, or report on anyone but the logged-in user.
Every view here needs a login, because LoginRequiredMiddleware is on.
"""

from collections.abc import Callable
from datetime import date, timedelta

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from expenses.forms import ExpenseFilterForm
from expenses.selectors import (
    DateRange,
    get_day_range,
    get_expenses_for_export,
    get_month_range,
    get_report_data,
    get_week_range,
    get_year_range,
)

from .exports import build_csv, build_pdf, build_xlsx
from .forms import CustomReportForm, ReportDateForm

# Each file format: the function that builds it, and the content type to send it with.
EXPORT_FORMATS = {
    "csv": (build_csv, "text/csv; charset=utf-8"),
    "xlsx": (build_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pdf": (build_pdf, "application/pdf"),
}


def _render_period_report(
    request: HttpRequest, title: str, get_range: Callable[[date], DateRange]
) -> HttpResponse:
    """Show the report for the period containing the chosen day, or today."""
    form = ReportDateForm(request.GET)
    chosen_day = form.cleaned_data.get("date") if form.is_valid() else None
    date_range = get_range(chosen_day or timezone.localdate())
    context = {
        "title": title,
        "form": form,
        "date_range": date_range,
        "previous_day": date_range.start - timedelta(days=1),
        "next_day": date_range.end + timedelta(days=1),
        **get_report_data(request.user, date_range),
    }
    return render(request, "reports/report.html", context)


@require_GET
def report_index(request: HttpRequest) -> HttpResponse:
    """List the available reports."""
    return render(request, "reports/index.html")


@require_GET
def daily_report(request: HttpRequest) -> HttpResponse:
    """Show one day."""
    return _render_period_report(request, "Daily report", get_day_range)


@require_GET
def weekly_report(request: HttpRequest) -> HttpResponse:
    """Show one week, Monday to Sunday."""
    return _render_period_report(request, "Weekly report", get_week_range)


@require_GET
def monthly_report(request: HttpRequest) -> HttpResponse:
    """Show one calendar month."""
    return _render_period_report(request, "Monthly report", get_month_range)


@require_GET
def yearly_report(request: HttpRequest) -> HttpResponse:
    """Show one calendar year."""
    return _render_period_report(request, "Yearly report", get_year_range)


@require_GET
def custom_report(request: HttpRequest) -> HttpResponse:
    """Show any range of dates. Until a valid range is chosen, only the form appears."""
    form = CustomReportForm(request.GET or None)
    context = {"title": "Custom report", "form": form}
    if form.is_valid():
        date_range = DateRange(form.cleaned_data["date_from"], form.cleaned_data["date_to"])
        context.update(date_range=date_range, **get_report_data(request.user, date_range))
    return render(request, "reports/report.html", context)


@require_GET
def export_expenses(request: HttpRequest, file_format: str) -> HttpResponse:
    """Download the user's expenses as a file, with the same filters as the expense list."""
    if file_format not in EXPORT_FORMATS:
        raise Http404("Unknown export format.")
    build_file, content_type = EXPORT_FORMATS[file_format]
    filter_form = ExpenseFilterForm(request.GET, user=request.user)
    if not filter_form.is_valid():
        messages.error(request, "The export filters are not valid. Please check them and retry.")
        return redirect("expense_list")
    expenses = get_expenses_for_export(request.user, filter_form.cleaned_data)
    response = HttpResponse(build_file(expenses), content_type=content_type)
    filename = f"expenses-{timezone.localdate().isoformat()}.{file_format}"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
