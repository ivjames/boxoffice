from django.urls import path

from . import views

urlpatterns = [
    path("scan/", views.scan_home, name="scan_home"),
    # Deliberately short + UPPERCASE: this path is what a Ticket's QR encodes,
    # and an uppercase, ?sig=-free URL is what keeps the QR in alphanumeric
    # mode (see orders/tokens.py). The signature is a path segment, not a
    # query param. reverse('scan_redeem', args=[token, sig]) builds it.
    path("S/<slug:token>/<slug:sig>/", views.scan_redeem, name="scan_redeem"),
]
