"""Form helpers shared by the apps."""

from django import forms


class BootstrapFormMixin:
    """Adds Bootstrap's CSS classes to every field of a form."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", css_class)
