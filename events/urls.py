from django.urls import path

from . import views

urlpatterns = [
    path("events/<slug:slug>/", views.event_detail, name="event_detail"),
]
