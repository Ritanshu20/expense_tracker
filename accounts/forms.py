"""
Forms for accounts.

What lives here: the login, registration and profile forms.
Who uses it: accounts/views.py and accounts/urls.py.
What it must never do: reveal anything about other users beyond "this name or email is taken".
"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from config.forms import BootstrapFormMixin


def _clean_unique_email(email: str, current_user: User | None = None) -> str:
    """
    Return the email in lowercase, refusing one that another account already uses.

    Why: participants are added by exact email, so one email must mean one person.
    Django's User model does not make email unique, so the forms have to check it.
    """
    email = email.lower()
    other_accounts = User.objects.filter(email__iexact=email)
    if current_user is not None:
        other_accounts = other_accounts.exclude(pk=current_user.pk)
    if other_accounts.exists():
        raise ValidationError("An account with this email already exists.")
    return email


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    """Django's login form with Bootstrap styling."""


class RegistrationForm(BootstrapFormMixin, UserCreationForm):
    """Sign-up form: username, email and password, with Django's password rules."""

    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        fields = ("username", "email")

    def clean_email(self) -> str:
        return _clean_unique_email(self.cleaned_data["email"])


class ProfileForm(BootstrapFormMixin, forms.ModelForm):
    """Lets a user change their own name and email. The username stays fixed."""

    email = forms.EmailField()

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")

    def clean_email(self) -> str:
        return _clean_unique_email(self.cleaned_data["email"], current_user=self.instance)
