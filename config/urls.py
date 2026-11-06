"""Root URL configuration. Each app owns its own URLs; this file only includes them."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("", include("dashboard.urls")),
    path("", include("expenses.urls")),
    path("", include("reports.urls")),
    path("api/", include("expenses.api_urls")),
]
