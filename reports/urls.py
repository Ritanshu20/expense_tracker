from django.urls import path

from . import views

urlpatterns = [
    path("reports/", views.report_index, name="report_index"),
    path("reports/daily/", views.daily_report, name="report_daily"),
    path("reports/weekly/", views.weekly_report, name="report_weekly"),
    path("reports/monthly/", views.monthly_report, name="report_monthly"),
    path("reports/yearly/", views.yearly_report, name="report_yearly"),
    path("reports/custom/", views.custom_report, name="report_custom"),
    path("reports/export/<str:file_format>/", views.export_expenses, name="export_expenses"),
]
