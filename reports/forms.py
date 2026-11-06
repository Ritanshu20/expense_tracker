"""Forms that choose the period of a report. Used by reports/views.py."""

from django import forms
from django.core.exceptions import ValidationError

from config.forms import BootstrapFormMixin
from expenses.forms import HTML_DATE_FORMAT


def _date_input() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format=HTML_DATE_FORMAT)


class ReportDateForm(BootstrapFormMixin, forms.Form):
    """Any day in the period. An empty value means today."""

    date = forms.DateField(required=False, label="Any day in the period", widget=_date_input())


class CustomReportForm(BootstrapFormMixin, forms.Form):
    """A start and an end date. Both are included in the report."""

    date_from = forms.DateField(label="From", widget=_date_input())
    date_to = forms.DateField(label="To", widget=_date_input())

    def clean(self) -> dict:
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise ValidationError("The start date cannot be after the end date.")
        return cleaned_data
