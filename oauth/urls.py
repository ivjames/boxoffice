from django.urls import path

from . import views

urlpatterns = [
    path("oauth/<slug:provider>/start/", views.oauth_start, name="oauth_start"),
    path("oauth/<slug:provider>/callback/", views.oauth_callback, name="oauth_callback"),
    # No provider in the path: the completion hand-off is provider-agnostic
    # (the identity is already resolved), it just needs the tenant host.
    path("oauth/complete/", views.oauth_complete, name="oauth_complete"),
]
