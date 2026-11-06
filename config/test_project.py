"""
Project-wide tests. They find every URL by themselves, so a new page is covered automatically.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse
from django.urls.converters import IntConverter

PUBLIC_PAGES = {"home", "register", "login"}
API_ROUTE_PREFIX = "api_"
# Public on purpose: this is where an API client exchanges a password for a token.
PUBLIC_API_ROUTES = {"api_token"}
SAMPLE_ID = 1
SAMPLE_TEXT = "csv"
STATUS_REDIRECT = 302
STATUS_OK = 200
STATUS_NOT_FOUND = 404
STATUS_METHOD_NOT_ALLOWED = 405


def get_named_routes(patterns: list | None = None) -> list[URLPattern]:
    """Return every named route of the project. The Django admin is tested separately."""
    if patterns is None:
        patterns = get_resolver().url_patterns
    routes = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.app_name != "admin":
                routes.extend(get_named_routes(pattern.url_patterns))
        elif pattern.name:
            routes.append(pattern)
    return routes


def url_of(route: URLPattern) -> str:
    """Build a real URL for a route, with a sample value for each part of the path."""
    kwargs = {}
    for name, converter in route.pattern.converters.items():
        kwargs[name] = SAMPLE_ID if isinstance(converter, IntConverter) else SAMPLE_TEXT
    return reverse(route.name, kwargs=kwargs)


def is_api_route(route: URLPattern) -> bool:
    return route.name.startswith(API_ROUTE_PREFIX)


class UrlResolutionTests(TestCase):
    def test_the_project_has_the_expected_number_of_routes_to_check(self):
        """If route discovery silently broke, the tests below would pass with nothing to do."""
        names = {route.name for route in get_named_routes()}

        for name in ("home", "dashboard", "expense_list", "report_custom", "api_expense_list"):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_every_route_resolves_back_to_its_own_view(self):
        """No URL is shadowed by an earlier pattern."""
        for route in get_named_routes():
            with self.subTest(route=route.name):
                self.assertEqual(resolve(url_of(route)).url_name, route.name)


class LoginCoverageTests(TestCase):
    def test_every_page_sends_anonymous_visitors_to_login_except_the_public_ones(self):
        """SAFETY: a new page that forgets about login fails this test."""
        for route in get_named_routes():
            if is_api_route(route):
                continue
            with self.subTest(route=route.name):
                url = url_of(route)
                response = self.client.get(url)
                if route.name in PUBLIC_PAGES:
                    self.assertEqual(response.status_code, STATUS_OK)
                else:
                    self.assertRedirects(
                        response, f"{reverse('login')}?next={url}", fetch_redirect_response=False
                    )

    def test_every_api_route_refuses_anonymous_requests_except_the_token_one(self):
        """SAFETY: a new API view that forgets about login fails this test."""
        for route in get_named_routes():
            if not is_api_route(route) or route.name in PUBLIC_API_ROUTES:
                continue
            with self.subTest(route=route.name):
                self.assertEqual(self.client.get(url_of(route)).status_code, 401)


class NoCrashTests(TestCase):
    def test_no_page_crashes_for_a_new_user_with_no_data(self):
        """Every page copes with empty data. An unhandled error would fail the test."""
        self.client.force_login(User.objects.create_user("new_user"))
        allowed = {STATUS_OK, STATUS_REDIRECT, STATUS_NOT_FOUND, STATUS_METHOD_NOT_ALLOWED}

        for route in get_named_routes():
            with self.subTest(route=route.name):
                self.assertIn(self.client.get(url_of(route)).status_code, allowed)


class ProjectHealthTests(TestCase):
    def test_every_model_change_has_a_migration(self):
        """makemigrations --check exits with an error if a migration is missing."""
        call_command("makemigrations", check=True, dry_run=True, stdout=StringIO())

    def test_django_system_checks_pass(self):
        """The same checks as python manage.py check."""
        call_command("check", stdout=StringIO())
