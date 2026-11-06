"""Security tests: settings per environment, response headers, unsafe code and hostile input."""

import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from expenses.models import Expense
from expenses.tests.helpers import create_personal_expense, create_user

SETTINGS_FILE = Path(settings.BASE_DIR) / "config" / "settings.py"
PROJECT_PACKAGES = ("accounts", "config", "dashboard", "expenses", "reports")
TEST_SECRET_KEY = "a-key-used-only-by-this-test"
HOSTILE_TEXT = "<script>alert(1)</script>"
ESCAPED_HOSTILE_TEXT = "&lt;script&gt;alert(1)&lt;/script&gt;"


def load_settings(**environment) -> dict:
    """Run config/settings.py with exactly these environment variables and no .env file."""
    with mock.patch("dotenv.load_dotenv"), mock.patch.dict(os.environ, environment, clear=True):
        return runpy.run_path(str(SETTINGS_FILE))


class SecretAndDebugSettingsTests(SimpleTestCase):
    def test_app_refuses_to_start_without_a_secret_key(self):
        """There is no default that could end up public."""
        with self.assertRaisesMessage(ImproperlyConfigured, "SECRET_KEY is not set"):
            load_settings()

    def test_debug_is_off_when_the_variable_is_missing(self):
        """A forgotten variable fails safely."""
        self.assertFalse(load_settings(SECRET_KEY=TEST_SECRET_KEY)["DEBUG"])

    def test_only_the_word_true_turns_debug_on(self):
        """Values like 1 or yes do not switch debug pages on by accident."""
        for value, expected in (("True", True), ("true", True), ("1", False), ("yes", False)):
            with self.subTest(value=value):
                loaded = load_settings(SECRET_KEY=TEST_SECRET_KEY, DEBUG=value)
                self.assertEqual(loaded["DEBUG"], expected)

    def test_settings_file_contains_no_generated_secret_key(self):
        """The key Django generates for new projects must not be in the code."""
        self.assertNotIn("django-insecure", SETTINGS_FILE.read_text(encoding="utf-8"))


class ProductionSettingsTests(SimpleTestCase):
    def test_production_requires_https_and_secure_cookies(self):
        """With debug off, cookies are never sent over plain HTTP."""
        loaded = load_settings(SECRET_KEY=TEST_SECRET_KEY, DEBUG="False")

        self.assertTrue(loaded["SECURE_SSL_REDIRECT"])
        self.assertTrue(loaded["SESSION_COOKIE_SECURE"])
        self.assertTrue(loaded["CSRF_COOKIE_SECURE"])
        self.assertGreater(loaded["SECURE_HSTS_SECONDS"], 0)

    def test_development_leaves_https_settings_off(self):
        """The development server has no HTTPS, so these would lock the developer out."""
        loaded = load_settings(SECRET_KEY=TEST_SECRET_KEY, DEBUG="True")

        self.assertFalse(loaded["SECURE_SSL_REDIRECT"])
        self.assertFalse(loaded["SESSION_COOKIE_SECURE"])
        self.assertFalse(loaded["CSRF_COOKIE_SECURE"])
        self.assertEqual(loaded["SECURE_HSTS_SECONDS"], 0)

    def test_https_redirect_and_hsts_can_be_set_from_the_environment(self):
        """CI turns the redirect off, and HSTS can be raised once HTTPS works."""
        loaded = load_settings(
            SECRET_KEY=TEST_SECRET_KEY,
            DEBUG="False",
            SECURE_SSL_REDIRECT="False",
            SECURE_HSTS_SECONDS="31536000",
        )

        self.assertFalse(loaded["SECURE_SSL_REDIRECT"])
        self.assertEqual(loaded["SECURE_HSTS_SECONDS"], 31536000)
        self.assertTrue(loaded["SESSION_COOKIE_SECURE"])


class ResponseHeaderTests(SimpleTestCase):
    def test_security_headers_are_sent(self):
        """Clickjacking, MIME sniffing and referrer leaks are blocked."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_session_cookie_is_hidden_from_javascript(self):
        """A script injected into a page could not read the session."""
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")


class UnsafeCodeTests(SimpleTestCase):
    def project_files(self, pattern: str) -> list[Path]:
        files = []
        for package in PROJECT_PACKAGES:
            files.extend((Path(settings.BASE_DIR) / package).rglob(pattern))
        return files

    def test_no_template_marks_text_as_safe(self):
        """SAFETY: autoescaping stays on everywhere, so user input cannot become HTML."""
        templates = list((Path(settings.BASE_DIR) / "templates").rglob("*.html"))
        self.assertGreater(len(templates), 0)

        for template in templates:
            with self.subTest(template=template.name):
                text = template.read_text(encoding="utf-8")
                self.assertNotIn("|safe", text)
                self.assertNotIn("autoescape off", text)

    def test_no_python_file_bypasses_escaping_or_the_orm(self):
        """SAFETY: no mark_safe and no raw SQL, so there is no opening for XSS or injection."""
        forbidden = ("mark_safe", ".raw(", ".extra(", "connection.cursor")
        python_files = [path for path in self.project_files("*.py") if "test" not in path.name]
        self.assertGreater(len(python_files), 0)

        for path in python_files:
            text = path.read_text(encoding="utf-8")
            for word in forbidden:
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, text)


class HostileInputTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")

    def setUp(self):
        self.client.force_login(self.rahul)

    def test_text_in_the_url_is_escaped_on_every_page_that_shows_it(self):
        """Reflected XSS: values from the query string never become HTML."""
        pages = (
            (reverse("expense_list"), {"q": HOSTILE_TEXT}),
            (reverse("settlement_create"), {"person": HOSTILE_TEXT, "amount": HOSTILE_TEXT}),
            (reverse("report_monthly"), {"date": HOSTILE_TEXT}),
            (reverse("report_custom"), {"date_from": HOSTILE_TEXT, "date_to": HOSTILE_TEXT}),
        )
        for url, query in pages:
            with self.subTest(url=url):
                response = self.client.get(url, query)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, HOSTILE_TEXT)

    def test_next_address_with_script_is_not_echoed_on_the_login_pages(self):
        """Reflected XSS on the pages an attacker would most like to send people to."""
        self.client.logout()

        for name in ("login", "register"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name), {"next": HOSTILE_TEXT})
                self.assertNotContains(response, HOSTILE_TEXT)

    def test_sql_in_the_search_box_is_treated_as_plain_text(self):
        """SQL injection: the search finds nothing, and the table is still there."""
        create_personal_expense(self.rahul, title="Lunch")

        response = self.client.get(
            reverse("expense_list"), {"q": "'; DROP TABLE expenses_expense; --"}
        )

        self.assertContains(response, "No expenses found")
        self.assertIn(Expense._meta.db_table, connection.introspection.table_names())
        self.assertEqual(Expense.objects.count(), 1)

    def test_sql_in_a_filter_value_gives_an_error_and_not_a_crash(self):
        """A non-numeric category id is refused by the form."""
        response = self.client.get(reverse("expense_list"), {"category": "1 OR 1=1"})

        self.assertContains(response, "Select a valid choice")


class CsrfCoverageTests(TestCase):
    def test_logout_without_a_csrf_token_is_rejected(self):
        """Another site cannot log a user out."""
        rahul = User.objects.create_user("rahul")
        client = Client(enforce_csrf_checks=True)
        client.force_login(rahul)

        response = client.post(reverse("logout"))

        self.assertEqual(response.status_code, 403)
        self.assertIn("_auth_user_id", client.session)

    def test_profile_change_without_a_csrf_token_is_rejected(self):
        """Another site cannot change a user's email."""
        rahul = User.objects.create_user("rahul", "rahul@example.com")
        client = Client(enforce_csrf_checks=True)
        client.force_login(rahul)

        response = client.post(reverse("profile"), {"email": "attacker@example.com"})

        self.assertEqual(response.status_code, 403)
        rahul.refresh_from_db()
        self.assertEqual(rahul.email, "rahul@example.com")
