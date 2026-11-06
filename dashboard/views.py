"""
Views for the home page and the dashboard.

What lives here: thin views. All numbers come from expenses/selectors.py.
Who calls it: dashboard/urls.py.
What it must never do: calculate money here, or show data from anyone but the logged-in user.
"""

from django.contrib.auth.decorators import login_not_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from expenses.selectors import get_dashboard_data


@login_not_required
def home(request: HttpRequest) -> HttpResponse:
    """Show the public landing page, or send logged-in users to their dashboard."""
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "dashboard/home.html")


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """Show the logged-in user's spending and who owes whom."""
    context = get_dashboard_data(request.user, timezone.localdate())
    return render(request, "dashboard/dashboard.html", context)
