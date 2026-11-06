"""Tests for registration, login, logout, the profile page and login protection."""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

STRONG_PASSWORD = "correct-horse-battery-42"
EVIL_SITE = "https://evil.example.com/steal"


def registration_data(**overrides) -> dict:
    data = {
        "username": "rahul",
        "email": "rahul@example.com",
        "password1": STRONG_PASSWORD,
        "password2": STRONG_PASSWORD,
    }
    data.update(overrides)
    return data


class RegistrationTests(TestCase):
    def test_registration_page_is_public(self):
        """A visitor without an account can open the page."""
        response = self.client.get(reverse("register"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/register.html")

    def test_valid_registration_creates_the_user_and_logs_them_in(self):
        """After signing up the user is logged in and sent to the dashboard."""
        response = self.client.post(reverse("register"), registration_data())

        self.assertRedirects(response, reverse("dashboard"))
        user = User.objects.get(username="rahul")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_password_is_stored_hashed_not_as_plain_text(self):
        """The database never holds the real password."""
        self.client.post(reverse("register"), registration_data())

        user = User.objects.get(username="rahul")
        self.assertNotIn(STRONG_PASSWORD, user.password)
        self.assertTrue(user.check_password(STRONG_PASSWORD))

    def test_new_account_is_not_staff_or_superuser(self):
        """Signing up never gives access to the admin."""
        self.client.post(reverse("register"), registration_data(is_staff="on", is_superuser="on"))

        user = User.objects.get(username="rahul")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_email_is_stored_in_lowercase(self):
        """Participants are matched by email later, so one format is stored."""
        self.client.post(reverse("register"), registration_data(email="Rahul@Example.COM"))

        self.assertEqual(User.objects.get(username="rahul").email, "rahul@example.com")

    def test_duplicate_username_is_rejected(self):
        """Usernames are unique."""
        User.objects.create_user("rahul", "first@example.com")

        response = self.client.post(reverse("register"), registration_data())

        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.count(), 1)

    def test_same_username_in_a_different_case_is_rejected(self):
        """'Rahul' and 'rahul' would look like the same person to everyone else."""
        User.objects.create_user("rahul", "first@example.com")

        response = self.client.post(reverse("register"), registration_data(username="RAHUL"))

        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.count(), 1)

    def test_duplicate_email_in_any_case_is_rejected(self):
        """One email means one person, because participants are added by email."""
        User.objects.create_user("first", "rahul@example.com")

        response = self.client.post(
            reverse("register"), registration_data(email="RAHUL@example.com")
        )

        self.assertContains(response, "An account with this email already exists")
        self.assertEqual(User.objects.count(), 1)

    def test_invalid_input_is_rejected_and_creates_no_account(self):
        """Missing email, mismatched passwords and weak passwords are all refused."""
        invalid_inputs = (
            {"email": ""},
            {"email": "not-an-email"},
            {"username": ""},
            {"password2": "something-else-99"},
            {"password1": "12345", "password2": "12345"},
        )
        for overrides in invalid_inputs:
            with self.subTest(overrides=overrides):
                response = self.client.post(reverse("register"), registration_data(**overrides))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(User.objects.count(), 0)

    def test_registration_follows_a_safe_next_address(self):
        """A user sent here from a protected page goes back to it."""
        response = self.client.post(reverse("register"), registration_data(next=reverse("profile")))

        self.assertRedirects(response, reverse("profile"))

    def test_registration_ignores_a_next_address_on_another_site(self):
        """A crafted link cannot send a new user to a fake site."""
        response = self.client.post(reverse("register"), registration_data(next=EVIL_SITE))

        self.assertRedirects(response, reverse("dashboard"))

    def test_registration_page_does_not_echo_a_next_address_on_another_site(self):
        """An unsafe address never reaches the hidden form field."""
        response = self.client.get(reverse("register"), {"next": EVIL_SITE})

        self.assertNotContains(response, EVIL_SITE)

    def test_logged_in_user_opening_registration_is_sent_to_the_dashboard(self):
        """There is no reason to sign up while logged in."""
        self.client.force_login(User.objects.create_user("rahul"))

        response = self.client.get(reverse("register"))

        self.assertRedirects(response, reverse("dashboard"))

    def test_registration_without_a_csrf_token_is_rejected(self):
        """CSRF protection is on for this form."""
        strict_client = Client(enforce_csrf_checks=True)

        response = strict_client.post(reverse("register"), registration_data())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 0)


class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("rahul", "rahul@example.com", STRONG_PASSWORD)

    def login(self, **overrides):
        data = {"username": "rahul", "password": STRONG_PASSWORD}
        data.update(overrides)
        return self.client.post(reverse("login"), data)

    def assert_not_logged_in(self):
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_is_public(self):
        """A logged-out visitor can open the page."""
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/login.html")

    def test_correct_password_logs_the_user_in(self):
        """A successful login goes to the dashboard."""
        response = self.login()

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_wrong_password_shows_an_error_and_does_not_log_in(self):
        """A wrong password keeps the user out."""
        response = self.login(password="wrong-password-1")

        self.assertContains(response, "Please enter a correct username and password")
        self.assert_not_logged_in()

    def test_unknown_username_gives_the_same_error_as_a_wrong_password(self):
        """The login form must not reveal which usernames exist."""
        response = self.login(username="nobody")

        self.assertContains(response, "Please enter a correct username and password")
        self.assert_not_logged_in()

    def test_login_follows_a_safe_next_address(self):
        """After logging in, the user returns to the page they wanted."""
        response = self.login(next=reverse("profile"))

        self.assertRedirects(response, reverse("profile"))

    def test_login_ignores_a_next_address_on_another_site(self):
        """A crafted link cannot send users to a fake site after login."""
        response = self.login(next=EVIL_SITE)

        self.assertRedirects(response, reverse("dashboard"))

    def test_next_address_from_the_url_is_kept_in_the_login_form(self):
        """The form carries the address, so it survives the POST."""
        response = self.client.get(reverse("login"), {"next": reverse("profile")})

        self.assertContains(response, f'name="next" value="{reverse("profile")}"')

    def test_logged_in_user_opening_login_is_sent_to_the_dashboard(self):
        """There is no reason to log in twice."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("login"))

        self.assertRedirects(response, reverse("dashboard"))


class LogoutTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("rahul"))

    def test_post_logs_the_user_out_and_goes_to_the_home_page(self):
        """Logging out ends the session."""
        response = self.client.post(reverse("logout"))

        self.assertRedirects(response, reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_get_does_not_log_the_user_out(self):
        """A link or image on another site cannot log a user out."""
        response = self.client.get(reverse("logout"))

        self.assertEqual(response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)


class LoginProtectionTests(TestCase):
    def test_profile_sends_anonymous_visitors_to_login_with_a_next_address(self):
        """The protected page is remembered so the user can return to it."""
        response = self.client.get(reverse("profile"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('profile')}")


class LoginRequiredByDefaultTests(SimpleTestCase):
    def test_login_required_middleware_is_switched_on(self):
        """Every view needs a login unless it is explicitly marked as public."""
        self.assertIn("django.contrib.auth.middleware.LoginRequiredMiddleware", settings.MIDDLEWARE)


class ProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = User.objects.create_user("rahul", "rahul@example.com")
        cls.aman = User.objects.create_user("aman", "aman@example.com")

    def setUp(self):
        self.client.force_login(self.rahul)

    def update_profile(self, **overrides):
        data = {"first_name": "Rahul", "last_name": "Sharma", "email": "rahul@example.com"}
        data.update(overrides)
        return self.client.post(reverse("profile"), data)

    def test_profile_shows_the_users_own_details_and_nobody_elses(self):
        """The page is about the logged-in user only."""
        response = self.client.get(reverse("profile"))

        self.assertContains(response, "rahul@example.com")
        self.assertNotContains(response, "aman@example.com")

    def test_user_can_update_their_name_and_email(self):
        """A valid update is saved and redirects back to the profile."""
        response = self.update_profile(email="New.Rahul@example.com")

        self.assertRedirects(response, reverse("profile"))
        self.rahul.refresh_from_db()
        self.assertEqual(self.rahul.first_name, "Rahul")
        self.assertEqual(self.rahul.email, "new.rahul@example.com")

    def test_keeping_your_own_email_is_allowed(self):
        """Saving the profile without changing the email is not a duplicate."""
        response = self.update_profile(email="RAHUL@example.com")

        self.assertRedirects(response, reverse("profile"))

    def test_email_used_by_another_account_is_rejected(self):
        """One email means one person."""
        response = self.update_profile(email="AMAN@example.com")

        self.assertContains(response, "An account with this email already exists")
        self.rahul.refresh_from_db()
        self.assertEqual(self.rahul.email, "rahul@example.com")

    def test_empty_email_is_rejected(self):
        """Every account keeps an email, because participants are added by it."""
        response = self.update_profile(email="")

        self.assertEqual(response.status_code, 200)
        self.rahul.refresh_from_db()
        self.assertEqual(self.rahul.email, "rahul@example.com")

    def test_profile_form_cannot_change_username_or_permissions(self):
        """Fields that are not on the form are ignored even if they are posted."""
        self.update_profile(username="hacked", is_staff="on", is_superuser="on")

        self.rahul.refresh_from_db()
        self.assertEqual(self.rahul.username, "rahul")
        self.assertFalse(self.rahul.is_staff)
        self.assertFalse(self.rahul.is_superuser)

    def test_script_in_a_name_is_escaped_on_the_page(self):
        """User input is never rendered as HTML."""
        self.update_profile(first_name="<script>alert(1)</script>")

        response = self.client.get(reverse("profile"))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
