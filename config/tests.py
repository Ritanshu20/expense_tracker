import os
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TestCase

from config.settings import (
    DB_CONNECT_TIMEOUT_SECONDS,
    build_database_settings,
    get_env_int,
    get_env_list,
)

POSTGRES_ENGINE = "django.db.backends.postgresql"
# PostgreSQL reports its version as a number: 16.0 is 160000.
MINIMUM_POSTGRES_VERSION = 160000


class ProjectSettingsTests(SimpleTestCase):
    def test_timezone_is_india_and_timezone_support_is_on(self):
        """Dates and week boundaries follow Asia/Kolkata with timezone-aware datetimes."""
        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")
        self.assertTrue(settings.USE_TZ)

    def test_default_database_engine_is_postgresql(self):
        """The project never runs on SQLite or MySQL."""
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], POSTGRES_ENGINE)

    def test_only_the_four_project_apps_are_installed(self):
        """The project has exactly the apps accounts, expenses, dashboard and reports."""
        third_party_prefixes = ("django.", "rest_framework", "drf_spectacular")
        project_apps = [
            app for app in settings.INSTALLED_APPS if not app.startswith(third_party_prefixes)
        ]

        self.assertEqual(project_apps, ["accounts", "expenses", "dashboard", "reports"])


class DatabaseSettingsTests(SimpleTestCase):
    def test_database_url_is_parsed_into_postgresql_settings(self):
        """DATABASE_URL is the preferred way to configure the database."""
        url = "postgres://tracker:secret@db.example.com:5433/expenses"

        with mock.patch.dict(os.environ, {"DATABASE_URL": url}):
            database = build_database_settings()

        self.assertEqual(database["ENGINE"], POSTGRES_ENGINE)
        self.assertEqual(database["NAME"], "expenses")
        self.assertEqual(database["USER"], "tracker")
        self.assertEqual(database["HOST"], "db.example.com")
        self.assertEqual(database["PORT"], 5433)

    def test_separate_db_variables_are_used_when_database_url_is_empty(self):
        """DB_NAME, DB_USER, DB_HOST and DB_PORT work when there is no DATABASE_URL."""
        variables = {
            "DATABASE_URL": "",
            "DB_NAME": "mydb",
            "DB_USER": "me",
            "DB_PASSWORD": "pw",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "5432",
        }

        with mock.patch.dict(os.environ, variables):
            database = build_database_settings()

        self.assertEqual(database["ENGINE"], POSTGRES_ENGINE)
        self.assertEqual(database["NAME"], "mydb")
        self.assertEqual(database["USER"], "me")
        self.assertEqual(database["HOST"], "127.0.0.1")

    def test_connection_attempts_have_a_short_timeout_for_both_configuration_styles(self):
        """A stopped database must fail in seconds instead of hanging for minutes."""
        for database_url in ("postgres://tracker:secret@localhost/expenses", ""):
            with self.subTest(database_url=database_url):
                with mock.patch.dict(os.environ, {"DATABASE_URL": database_url}):
                    database = build_database_settings()

                self.assertEqual(database["OPTIONS"]["connect_timeout"], DB_CONNECT_TIMEOUT_SECONDS)

    def test_non_postgres_database_url_is_rejected(self):
        """A SQLite URL in the environment must fail loudly instead of being used."""
        with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///db.sqlite3"}):
            with self.assertRaises(ImproperlyConfigured):
                build_database_settings()


class DatabaseConnectionTests(TestCase):
    def test_tests_run_on_postgresql_16_or_newer(self):
        """The test database is real PostgreSQL in a supported version."""
        self.assertEqual(connection.vendor, "postgresql")
        self.assertGreaterEqual(connection.pg_version, MINIMUM_POSTGRES_VERSION)

    def test_test_database_was_created_and_migrated_so_it_can_store_rows(self):
        """The test runner created its own database and applied the migrations."""
        User.objects.create_user(username="rahul")

        self.assertTrue(User.objects.filter(username="rahul").exists())


class EnvironmentListTests(SimpleTestCase):
    def test_comma_separated_value_is_split_and_trimmed(self):
        """ALLOWED_HOSTS style values become a clean list."""
        with mock.patch.dict(os.environ, {"EXAMPLE_LIST": " a.com , b.com ,, "}):
            self.assertEqual(get_env_list("EXAMPLE_LIST"), ["a.com", "b.com"])

    def test_missing_variable_gives_an_empty_list(self):
        """An unset variable means no entries, not an error."""
        self.assertEqual(get_env_list("THIS_VARIABLE_IS_NOT_SET"), [])


class EnvironmentNumberTests(SimpleTestCase):
    def test_whole_number_is_read_from_the_environment(self):
        """The budget warning threshold can be configured this way."""
        with mock.patch.dict(os.environ, {"EXAMPLE_NUMBER": "65"}):
            self.assertEqual(get_env_int("EXAMPLE_NUMBER", 80), 65)

    def test_missing_variable_gives_the_default(self):
        """An unset variable uses the default, which is 80 for the budget warning."""
        self.assertEqual(get_env_int("THIS_VARIABLE_IS_NOT_SET", 80), 80)
