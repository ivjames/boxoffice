from django.urls import path

from . import views

urlpatterns = [
    path("scan/", views.scan_home, name="scan_home"),
    path("scan/redeem/<uuid:token>/", views.scan_redeem, name="scan_redeem"),
]
