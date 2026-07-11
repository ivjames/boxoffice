from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path(
        "set-password/<uidb64>/<token>/",
        views.set_password_view,
        name="set_password",
    ),
]
