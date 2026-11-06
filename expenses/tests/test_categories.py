"""Tests for the category pages: CRUD, validation, duplicates and ownership isolation."""

from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from expenses.forms import CategoryForm
from expenses.models import Category
from expenses.selectors import get_categories_for_user
from expenses.services import save_category

from .helpers import create_expense, create_user, default_category

DEFAULT_CATEGORY_COUNT = 11


class CategoryTestCase(TestCase):
    """Two users, each with one custom category, and Rahul logged in."""

    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.rahul_category = Category.objects.create(name="Pets", owner=cls.rahul)
        cls.aman_category = Category.objects.create(name="Gym", owner=cls.aman)
        cls.food = default_category()

    def setUp(self):
        self.client.force_login(self.rahul)

    def assert_form_error(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)


class CategoryUrlTests(CategoryTestCase):
    def test_urls_match_the_agreed_addresses(self):
        """The list lives at /categories/, as the project URLs require."""
        self.assertEqual(reverse("category_list"), "/categories/")
        self.assertEqual(reverse("category_create"), "/categories/add/")
        self.assertEqual(reverse("category_edit", args=[5]), "/categories/5/edit/")
        self.assertEqual(reverse("category_delete", args=[5]), "/categories/5/delete/")

    def test_every_category_page_sends_anonymous_visitors_to_login(self):
        """Login is required for all four pages."""
        self.client.logout()
        urls = (
            reverse("category_list"),
            reverse("category_create"),
            reverse("category_edit", args=[self.rahul_category.pk]),
            reverse("category_delete", args=[self.rahul_category.pk]),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(response, f"{reverse('login')}?next={url}")

    def test_anonymous_post_cannot_delete_a_category(self):
        """The login check also covers POST, not only opening the page."""
        self.client.logout()

        self.client.post(reverse("category_delete", args=[self.rahul_category.pk]))

        self.assertTrue(Category.objects.filter(pk=self.rahul_category.pk).exists())


class CategoryVisibilityTests(CategoryTestCase):
    def test_visible_to_returns_defaults_and_own_but_not_other_users(self):
        """This query is the visibility rule for categories."""
        visible = Category.objects.visible_to(self.rahul)

        self.assertEqual(visible.count(), DEFAULT_CATEGORY_COUNT + 1)
        self.assertIn(self.food, visible)
        self.assertIn(self.rahul_category, visible)
        self.assertNotIn(self.aman_category, visible)

    def test_owned_by_returns_only_the_users_own_categories(self):
        """Defaults are visible to users but are not theirs to change."""
        self.assertEqual(list(Category.objects.owned_by(self.rahul)), [self.rahul_category])

    def test_categories_are_sorted_by_name_ignoring_letter_case(self):
        """A lowercase name must not be pushed to the end of the list."""
        Category.objects.create(name="apples", owner=self.rahul)

        names = [category.name for category in get_categories_for_user(self.rahul)]

        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertEqual(names[0], "apples")


class CategoryListTests(CategoryTestCase):
    def test_list_shows_defaults_and_own_categories_but_not_other_users(self):
        """A user never sees another user's category names."""
        response = self.client.get(reverse("category_list"))

        self.assertContains(response, "Food")
        self.assertContains(response, "Pets")
        self.assertNotContains(response, "Gym")

    def test_only_custom_categories_have_edit_and_delete_links(self):
        """Default categories are read-only, so they offer no actions."""
        response = self.client.get(reverse("category_list"))

        self.assertContains(response, reverse("category_edit", args=[self.rahul_category.pk]))
        self.assertNotContains(response, reverse("category_edit", args=[self.food.pk]))
        self.assertNotContains(response, reverse("category_delete", args=[self.food.pk]))

    def test_script_in_a_category_name_is_escaped_on_the_page(self):
        """User input is never rendered as HTML."""
        Category.objects.create(name="<script>alert(1)</script>", owner=self.rahul)

        response = self.client.get(reverse("category_list"))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_list_only_accepts_get(self):
        """The list page changes nothing, so other methods are refused."""
        self.assertEqual(self.client.post(reverse("category_list")).status_code, 405)

    def test_query_count_does_not_grow_with_the_number_of_categories(self):
        """No N+1: session, user and one category query, however long the list is."""
        Category.objects.bulk_create(
            Category(name=f"Custom {number}", owner=self.rahul) for number in range(30)
        )

        with self.assertNumQueries(3):
            response = self.client.get(reverse("category_list"))

        self.assertContains(response, "Custom 29")


class CategoryCreateTests(CategoryTestCase):
    def create(self, name, **extra):
        return self.client.post(reverse("category_create"), {"name": name, **extra})

    def test_valid_name_creates_a_category_owned_by_the_logged_in_user(self):
        """The new category belongs to the person who added it."""
        response = self.create("Books")

        self.assertRedirects(response, reverse("category_list"))
        self.assertEqual(Category.objects.get(name="Books").owner, self.rahul)

    def test_owner_in_the_submitted_data_is_ignored(self):
        """A user cannot create a category for someone else, or a default one."""
        self.create("Books", owner=self.aman.pk)

        self.assertEqual(Category.objects.get(name="Books").owner, self.rahul)

    def test_spaces_around_the_name_are_removed(self):
        """' Books ' and 'Books' are the same name."""
        self.create("  Books  ")

        self.assertTrue(Category.objects.filter(name="Books", owner=self.rahul).exists())

    def test_empty_and_blank_names_are_rejected(self):
        """A name is required, and spaces alone do not count."""
        for name in ("", "    "):
            with self.subTest(name=name):
                self.assert_form_error(self.create(name), "This field is required")

    def test_name_longer_than_100_characters_is_rejected(self):
        """The limit gives a form error instead of a database error."""
        self.assert_form_error(self.create("x" * 101), "at most 100 characters")

    def test_duplicate_of_own_category_in_any_case_is_rejected(self):
        """'Pets' and 'pETS' are the same category."""
        self.assert_form_error(self.create("pETS"), "already exists")
        self.assertEqual(Category.objects.filter(owner=self.rahul).count(), 1)

    def test_duplicate_of_a_default_category_in_any_case_is_rejected(self):
        """A second 'Food' in the dropdown could not be told apart from the default one."""
        self.assert_form_error(self.create("fOOD"), "already exists")
        self.assertEqual(Category.objects.filter(owner=self.rahul).count(), 1)

    def test_same_name_as_another_users_category_is_allowed(self):
        """Other users' categories stay private, so they must not block a name."""
        response = self.create("Gym")

        self.assertRedirects(response, reverse("category_list"))
        self.assertTrue(Category.objects.filter(name="Gym", owner=self.rahul).exists())

    def test_duplicate_that_gets_past_the_form_check_gives_an_error_not_a_crash(self):
        """If two requests race, the database refuses one and the user sees a message."""
        with mock.patch.object(CategoryForm, "clean_name", lambda form: form.cleaned_data["name"]):
            response = self.create("pets")

        self.assert_form_error(response, "already exists")
        self.assertEqual(Category.objects.filter(owner=self.rahul).count(), 1)

    def test_form_without_a_csrf_token_is_rejected(self):
        """CSRF protection is on for this form."""
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(reverse("category_create"), {"name": "Books"})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Category.objects.filter(name="Books").exists())


class SaveCategoryServiceTests(CategoryTestCase):
    def test_duplicate_is_reported_and_later_queries_still_work(self):
        """The failed save must not break the surrounding database transaction."""
        duplicate = Category(name="PETS", owner=self.rahul)

        with self.assertRaisesMessage(ValidationError, "already exists"):
            save_category(duplicate)

        self.assertEqual(Category.objects.filter(owner=self.rahul).count(), 1)


class CategoryEditTests(CategoryTestCase):
    def edit(self, category, name):
        return self.client.post(reverse("category_edit", args=[category.pk]), {"name": name})

    def test_owner_can_rename_their_category(self):
        """A valid rename is saved."""
        response = self.edit(self.rahul_category, "Animals")

        self.assertRedirects(response, reverse("category_list"))
        self.rahul_category.refresh_from_db()
        self.assertEqual(self.rahul_category.name, "Animals")

    def test_edit_page_shows_the_current_name(self):
        """The form starts with the existing name."""
        response = self.client.get(reverse("category_edit", args=[self.rahul_category.pk]))

        self.assertContains(response, 'value="Pets"')

    def test_changing_only_the_letter_case_of_your_own_name_is_allowed(self):
        """A category is not a duplicate of itself."""
        response = self.edit(self.rahul_category, "PETS")

        self.assertRedirects(response, reverse("category_list"))
        self.rahul_category.refresh_from_db()
        self.assertEqual(self.rahul_category.name, "PETS")

    def test_renaming_to_a_duplicate_is_rejected(self):
        """The duplicate rules also apply to renames."""
        Category.objects.create(name="Books", owner=self.rahul)

        for duplicate_name in ("books", "food"):
            with self.subTest(name=duplicate_name):
                response = self.edit(self.rahul_category, duplicate_name)
                self.assert_form_error(response, "already exists")

        self.rahul_category.refresh_from_db()
        self.assertEqual(self.rahul_category.name, "Pets")

    def test_default_category_cannot_be_edited(self):
        """Default categories are read-only for every user."""
        url = reverse("category_edit", args=[self.food.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"name": "Hacked"}).status_code, 404)
        self.food.refresh_from_db()
        self.assertEqual(self.food.name, "Food")

    def test_another_users_category_cannot_be_edited(self):
        """IDOR check: changing the id in the URL gives a 404."""
        url = reverse("category_edit", args=[self.aman_category.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"name": "Hacked"}).status_code, 404)
        self.aman_category.refresh_from_db()
        self.assertEqual(self.aman_category.name, "Gym")


class CategoryDeleteTests(CategoryTestCase):
    def delete_url(self, category):
        return reverse("category_delete", args=[category.pk])

    def test_get_asks_for_confirmation_and_deletes_nothing(self):
        """Opening the page must not delete anything."""
        response = self.client.get(self.delete_url(self.rahul_category))

        self.assertContains(response, "Pets")
        self.assertTrue(Category.objects.filter(pk=self.rahul_category.pk).exists())

    def test_post_deletes_an_unused_custom_category(self):
        """The owner can delete a category that no expense uses."""
        response = self.client.post(self.delete_url(self.rahul_category))

        self.assertRedirects(response, reverse("category_list"))
        self.assertFalse(Category.objects.filter(pk=self.rahul_category.pk).exists())

    def test_category_used_by_an_expense_is_kept_with_a_clear_message(self):
        """Old expenses must keep their category, and the user is told why."""
        create_expense(self.rahul, category=self.rahul_category)
        create_expense(self.rahul, category=self.rahul_category)

        response = self.client.post(self.delete_url(self.rahul_category), follow=True)

        self.assertContains(response, "used by 2 expense(s)")
        self.assertTrue(Category.objects.filter(pk=self.rahul_category.pk).exists())

    def test_category_can_be_deleted_once_no_expense_uses_it(self):
        """The protection lasts only as long as the category is in use."""
        expense = create_expense(self.rahul, category=self.rahul_category)
        expense.delete()

        self.client.post(self.delete_url(self.rahul_category))

        self.assertFalse(Category.objects.filter(pk=self.rahul_category.pk).exists())

    def test_default_category_cannot_be_deleted(self):
        """Default categories are read-only for every user."""
        url = self.delete_url(self.food)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(Category.objects.filter(pk=self.food.pk).exists())

    def test_another_users_category_cannot_be_deleted(self):
        """IDOR check: changing the id in the URL gives a 404."""
        url = self.delete_url(self.aman_category)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(Category.objects.filter(pk=self.aman_category.pk).exists())

    def test_form_without_a_csrf_token_is_rejected(self):
        """A link on another site cannot delete a category."""
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(self.delete_url(self.rahul_category))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Category.objects.filter(pk=self.rahul_category.pk).exists())


class CategoryNavigationTests(TestCase):
    def test_logged_in_user_sees_a_link_to_categories(self):
        """The page can be reached from the navigation."""
        self.client.force_login(User.objects.create_user("rahul"))

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, reverse("category_list"))
