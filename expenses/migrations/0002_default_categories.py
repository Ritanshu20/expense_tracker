"""Creates the default categories, which have no owner and are visible to every user."""

from django.db import migrations

DEFAULT_CATEGORY_NAMES = [
    "Food",
    "Travel",
    "Shopping",
    "Bills",
    "Rent",
    "Entertainment",
    "Medical",
    "Education",
    "Fuel",
    "Groceries",
    "Other",
]


def create_default_categories(apps, schema_editor):
    category_model = apps.get_model("expenses", "Category")
    for name in DEFAULT_CATEGORY_NAMES:
        category_model.objects.get_or_create(name=name, owner=None)


class Migration(migrations.Migration):
    dependencies = [
        ("expenses", "0001_initial"),
    ]

    operations = [
        # Reversing keeps the rows, because expenses may already be using these categories.
        migrations.RunPython(create_default_categories, migrations.RunPython.noop),
    ]
