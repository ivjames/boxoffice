"""Human-facing text for the ?oauth_error=<code> a failed OAuth flow bounces
back to a sign-in page with (oauth.views). Kept dependency-free so the
accounts and guests sign-in views can turn a code into a message without
importing the rest of the oauth app."""

ERROR_MESSAGES = {
    "state": "Sorry, that sign-in didn't go through. Please try again.",
    "failed": "Sorry, that sign-in didn't go through. Please try again.",
    "denied": "Sign-in was cancelled.",
    "no_email": "We couldn't get a verified email from that account. Try another way to sign in.",
    "no_account": "No account matches that sign-in yet. Ask a theater owner to invite you first.",
    "no_access": "That account doesn't have access to this theater.",
    "inactive": "That account has been deactivated.",
}


def error_message(code):
    """Friendly message for an oauth_error code, or None if the code is
    missing/unknown (so callers can just skip rendering an alert)."""
    if not code:
        return None
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["failed"])
