from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token

from . import api_views

urlpatterns = [
    # Public on purpose: this is where an API client exchanges a password for a token.
    path("token/", obtain_auth_token, name="api_token"),
    path("expenses/", api_views.ExpenseListCreateView.as_view(), name="api_expense_list"),
    path("expenses/<int:pk>/", api_views.ExpenseDetailView.as_view(), name="api_expense_detail"),
    path("dashboard/", api_views.DashboardView.as_view(), name="api_dashboard"),
    path("reports/monthly/", api_views.MonthlyReportView.as_view(), name="api_report_monthly"),
    path("settlements/", api_views.SettlementsView.as_view(), name="api_settlements"),
    path("schema/", SpectacularAPIView.as_view(), name="api_schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api_schema"), name="api_docs"),
]
