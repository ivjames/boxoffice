from django.urls import path

from . import views

urlpatterns = [
    path("webhooks/stripe/", views.stripe_webhook, name="stripe_webhook"),
    # Staff-facing Stripe Connect (Express) onboarding, on the tenant subdomain
    # (request.organization is the theater being connected).
    path("dashboard/payments/connect/", views.connect_start, name="connect_start"),
    path("dashboard/payments/connect/return/", views.connect_return, name="connect_return"),
    path("dashboard/payments/connect/refresh/", views.connect_refresh, name="connect_refresh"),
]
