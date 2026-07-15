from django.urls import path

from . import views

urlpatterns = [
    path("account/", views.guest_portal, name="guest_portal"),
    path("account/link/", views.guest_request_link, name="guest_request_link"),
    path("account/verify/", views.guest_verify, name="guest_verify"),
    path("account/logout/", views.guest_logout, name="guest_logout"),
    # -- marketing preferences (Phase 4 CRM) --------------------------------
    path("account/preferences/", views.guest_preferences, name="guest_preferences"),
    path("account/unsubscribe/", views.guest_unsubscribe, name="guest_unsubscribe"),
]
