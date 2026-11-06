"""
Views for the REST API.

What lives here: thin API views that connect URLs, serializers, selectors and services.
Who calls it: expenses/api_urls.py.
What it must never do: hold business rules, or fetch a row without checking who may see it.

REST framework exempts its views from LoginRequiredMiddleware and answers anonymous requests
with a 401 instead. DEFAULT_PERMISSION_CLASSES in config/settings.py requires the login.
swagger_fake_view is set while the API docs are generated, when there is no logged-in user.
"""

from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .forms import ExpenseFilterForm
from .models import Expense
from .selectors import (
    filter_expenses,
    get_dashboard_data,
    get_expenses_for_user,
    get_month_range,
    get_payments_for_user,
    get_report_data,
    get_settlement_summary,
)
from .serializers import (
    DashboardSerializer,
    ExpenseSerializer,
    MonthlyReportQuerySerializer,
    MonthlyReportSerializer,
    SettlementsSerializer,
)


class IsCreatorOrReadOnly(permissions.BasePermission):
    """RULE: participants can view an expense, but only its creator can change it."""

    def has_object_permission(self, request: HttpRequest, view: APIView, obj: Expense) -> bool:
        return request.method in permissions.SAFE_METHODS or obj.created_by_id == request.user.pk


def _visible_expenses(request: HttpRequest) -> QuerySet[Expense]:
    """Return the expenses the user may view, with their participants loaded in one go."""
    # SAFETY: other users' expenses are not in this queryset, so their ids give a 404.
    return get_expenses_for_user(request.user).prefetch_related("participants__user")


@extend_schema(
    parameters=[
        OpenApiParameter("q", str, description="Search in the title and description."),
        OpenApiParameter("date_from", str, description="First date to include, YYYY-MM-DD."),
        OpenApiParameter("date_to", str, description="Last date to include, YYYY-MM-DD."),
        OpenApiParameter("category", int, description="Category id."),
        OpenApiParameter("payment_method", str, description="Payment method."),
        OpenApiParameter("expense_type", str, description="personal or shared."),
    ],
    methods=["GET"],
)
class ExpenseListCreateView(generics.ListCreateAPIView):
    """List the user's expenses with the same filters as the website, or add an expense."""

    serializer_class = ExpenseSerializer

    def get_queryset(self) -> QuerySet[Expense]:
        if getattr(self, "swagger_fake_view", False):
            return Expense.objects.none()
        filter_form = ExpenseFilterForm(self.request.query_params, user=self.request.user)
        if not filter_form.is_valid():
            raise serializers.ValidationError(filter_form.errors)
        return filter_expenses(_visible_expenses(self.request), filter_form.cleaned_data)


class ExpenseDetailView(generics.RetrieveUpdateDestroyAPIView):
    """View, replace or delete one expense. Only its creator can replace or delete it."""

    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, IsCreatorOrReadOnly]
    http_method_names = ["get", "put", "delete", "head", "options"]

    def get_queryset(self) -> QuerySet[Expense]:
        if getattr(self, "swagger_fake_view", False):
            return Expense.objects.none()
        return _visible_expenses(self.request)


class DashboardView(APIView):
    """The same numbers as the dashboard page."""

    @extend_schema(responses=DashboardSerializer)
    def get(self, request: HttpRequest) -> Response:
        data = get_dashboard_data(request.user, timezone.localdate())
        return Response(DashboardSerializer(data).data)


class MonthlyReportView(APIView):
    """The same numbers as the monthly report page, for the month containing a date."""

    @extend_schema(parameters=[MonthlyReportQuerySerializer], responses=MonthlyReportSerializer)
    def get(self, request: HttpRequest) -> Response:
        query = MonthlyReportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date", timezone.localdate())
        date_range = get_month_range(day)
        data = {
            "start": date_range.start,
            "end": date_range.end,
            **get_report_data(request.user, date_range),
        }
        return Response(MonthlyReportSerializer(data).data)


class SettlementsView(APIView):
    """Who owes whom after recorded payments, and the payments themselves."""

    @extend_schema(responses=SettlementsSerializer)
    def get(self, request: HttpRequest) -> Response:
        data = {
            "settlements": get_settlement_summary(request.user),
            "payments": get_payments_for_user(request.user),
        }
        return Response(SettlementsSerializer(data).data)
