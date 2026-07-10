from django import forms


class GuestEmailForm(forms.Form):
    """Single email field for the portal's "email me a sign-in link" step.
    The view normalizes (guests.models.normalize_email) before looking a
    guest up, so this only needs to validate that it's a well-formed
    address."""

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"autofocus": True, "autocomplete": "email", "placeholder": "you@example.com"}
        )
    )
