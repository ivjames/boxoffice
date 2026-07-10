from django.urls import path

from . import views

urlpatterns = [
    path("scan/", views.scan_home, name="scan_home"),
    # Internal redeem endpoint. NOT encoded in the QR anymore (the QR carries a
    # bare "<token>.<sig>" code -- see orders/tokens.py); it's hit only by the
    # in-page scanner's fetch() and the manual-entry redirect, both of which
    # build this path from the token + signature. Token and sig are path
    # segments (not ?sig=) so the same slug-based route serves both callers.
    path("S/<slug:token>/<slug:sig>/", views.scan_redeem, name="scan_redeem"),
]
