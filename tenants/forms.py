from django import forms


class PlatformContactForm(forms.Form):
    """The landing page's "Get in touch" form (platform host only). Replaces
    the old mailto:hello@boxo.show CTAs so the live site never publishes a
    raw email address -- submissions land in the DB (tenants.ContactInquiry)
    and work with zero SMTP dependency.

    `website` is a honeypot: visually hidden (and tab-skipped) in the
    template, so a human never fills it while naive form bots do. The view
    answers a filled honeypot with the normal success redirect -- but stores
    nothing -- so a bot can't tell it was caught.
    """

    name = forms.CharField(
        max_length=120,
        label="Your name",
        widget=forms.TextInput(attrs={"autocomplete": "name", "placeholder": "Alex Rivera"}),
    )
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "placeholder": "you@example.com"}
        ),
    )
    venue = forms.CharField(
        max_length=200,
        required=False,
        label="Venue or theater (optional)",
        widget=forms.TextInput(
            attrs={"autocomplete": "organization", "placeholder": "The Roxy Theater"}
        ),
    )
    message = forms.CharField(
        max_length=5000,
        label="What do you need?",
        widget=forms.Textarea(
            attrs={
                "rows": 5,
                "placeholder": "Tell us about your venue, your shows, and when you'd like to be live.",
            }
        ),
    )
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))

    def is_spam(self):
        """True when the honeypot was filled. Only meaningful after
        is_valid() -- the field is optional, so a bot submission still
        validates and cleaned_data carries whatever it typed."""
        return bool(self.cleaned_data.get("website"))
