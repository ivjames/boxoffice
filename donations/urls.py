from django.urls import path

from . import views

urlpatterns = [
    path("donate/", views.donate, name="donate"),
    # STUB donation payment: the simulated hosted-payment page
    # create_donation_checkout_session redirects to when a tenant hasn't
    # finished Stripe Connect onboarding -- see donate_stub's docstring.
    path("donate/stub/", views.donate_stub, name="donate_stub"),
]
