from django.urls import path

from . import views

urlpatterns = [
    path("performances/<int:pk>/", views.performance_detail, name="performance_detail"),
    path("performances/<int:pk>/hold/", views.hold_create, name="hold_create"),
    path("cart/", views.cart_view, name="cart"),
    path("cart/release/", views.cart_release, name="cart_release"),
    path("cart/promo/apply/", views.promo_apply, name="promo_apply"),
    path("cart/promo/remove/", views.promo_remove, name="promo_remove"),
    path("cart/donation/add/", views.donation_add, name="donation_add"),
    path("cart/donation/remove/", views.donation_remove, name="donation_remove"),
    path("checkout/", views.checkout_view, name="checkout"),
    # STUB CHECKOUT: the simulated hosted-payment page create_checkout_session
    # redirects to when a tenant hasn't finished Stripe Connect onboarding
    # (charges not enabled) -- see checkout_stub's docstring.
    path("checkout/stub/", views.checkout_stub, name="checkout_stub"),
    # TEST CHECKOUT: always routed, but 404s per-request unless
    # settings.ENABLE_TEST_CHECKOUT is True -- see checkout_test's docstring.
    path("checkout/test/", views.checkout_test, name="checkout_test"),
    path("checkout/success/", views.checkout_success, name="checkout_success"),
    path("checkout/cancel/", views.checkout_cancel, name="checkout_cancel"),
    path("tickets/<slug:token>/", views.ticket_detail, name="ticket_detail"),
    path("tickets/<slug:token>/pdf/", views.ticket_pdf, name="ticket_pdf"),
]
