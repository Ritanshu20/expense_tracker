from django.urls import path

from . import views

urlpatterns = [
    path("categories/", views.category_list, name="category_list"),
    path("categories/add/", views.category_create, name="category_create"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category_delete"),
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/add/", views.expense_create, name="expense_create"),
    path("expenses/<int:pk>/", views.expense_detail, name="expense_detail"),
    path("expenses/<int:pk>/edit/", views.expense_edit, name="expense_edit"),
    path("expenses/<int:pk>/delete/", views.expense_delete, name="expense_delete"),
    path("budget/", views.budget_list, name="budget_list"),
    path("budget/add/", views.budget_create, name="budget_create"),
    path("budget/<int:pk>/edit/", views.budget_edit, name="budget_edit"),
    path("budget/<int:pk>/delete/", views.budget_delete, name="budget_delete"),
    path("settlements/", views.settlement_list, name="settlement_list"),
    path("settlements/add/", views.settlement_create, name="settlement_create"),
    path("settlements/<int:pk>/", views.settlement_detail, name="settlement_detail"),
    path("settlements/<int:pk>/edit/", views.settlement_edit, name="settlement_edit"),
    path("settlements/<int:pk>/delete/", views.settlement_delete, name="settlement_delete"),
]
