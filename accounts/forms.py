from django import forms


class StaffLoginForm(forms.Form):
    """Deliberately plain (not Django's AuthenticationForm): the custom User
    logs in by email, and membership-in-this-org is checked by the view
    (accounts/views.login_view) AFTER authenticate() succeeds, so this form
    only needs to collect + do basic validation of the two fields."""

    email = forms.EmailField(widget=forms.EmailInput(attrs={"autofocus": True}))
    password = forms.CharField(widget=forms.PasswordInput)
