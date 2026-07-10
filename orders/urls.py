from django.urls import path

from . import views

urlpatterns = [
    path("performances/<int:pk>/", views.performance_detail, name="performance_detail"),
    path("performances/<int:pk>/hold/", views.hold_create, name="hold_create"),
    path("cart/", views.cart_view, name="cart"),
    path("cart/release/", views.cart_release, name="cart_release"),
    path("checkout/", views.checkout_view, name="checkout"),
    # STUB CHECKOUT: the simulated hosted-payment page create_checkout_session
    # redirects to when a tenant has no Stripe keys -- see checkout_stub's
    # docstring.
    path("checkout/stub/", views.checkout_stub, name="checkout_stub"),
    # TEST CHECKOUT: always routed, but 404s per-request unless
    # settings.ENABLE_TEST_CHECKOUT is True -- see checkout_test's docstring.
    path("checkout/test/", views.checkout_test, name="checkout_test"),
    path("checkout/success/", views.checkout_success, name="checkout_success"),
    path("checkout/cancel/", views.checkout_cancel, name="checkout_cancel"),
    path("tickets/<slug:token>/", views.ticket_detail, name="ticket_detail"),
]
