from django.urls import path

from . import views

urlpatterns = [
    path("webhooks/stripe/", views.stripe_webhook, name="stripe_webhook"),
]
