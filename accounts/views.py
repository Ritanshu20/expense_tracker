"""
Views for accounts.

What lives here: registration and the profile page. Login and logout use Django's
built-in views, which are configured in accounts/urls.py.
Who calls it: accounts/urls.py.
What it must never do: redirect to an address it has not checked, or show another user's data.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_not_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from .forms import ProfileForm, RegistrationForm


def _safe_next_url(request: HttpRequest) -> str:
    """Return the requested next address, or an empty string if it points to another site."""
    next_url = request.POST.get("next") or request.GET.get("next", "")
    # SAFETY: without this check, a crafted link could send users to a fake site after signing up.
    is_safe = url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    )
    return next_url if is_safe else ""


@login_not_required
@require_http_methods(["GET", "POST"])
def register(request: HttpRequest) -> HttpResponse:
    """Create an account and log the new user in."""
    next_url = _safe_next_url(request)
    if request.user.is_authenticated:
        return redirect(next_url or settings.LOGIN_REDIRECT_URL)

    form = RegistrationForm(request.POST or None)
    if form.is_valid():
        login(request, form.save())
        messages.success(request, "Your account has been created.")
        return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    return render(request, "accounts/register.html", {"form": form, "next": next_url})


@require_http_methods(["GET", "POST"])
def profile(request: HttpRequest) -> HttpResponse:
    """Show and update the logged-in user's own profile."""
    # SAFETY: the form always edits request.user, so no id in the URL can reach another account.
    form = ProfileForm(request.POST or None, instance=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Your profile has been updated.")
        return redirect("profile")
    return render(request, "accounts/profile.html", {"form": form})
