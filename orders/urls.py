from django.urls import path

from . import views

urlpatterns = [
    path("performances/<int:pk>/", views.performance_detail, name="performance_detail"),
    path("performances/<int:pk>/hold/", views.hold_create, name="hold_create"),
    path("cart/", views.cart_view, name="cart"),
    path("cart/release/", views.cart_release, name="cart_release"),
    path("checkout/", views.checkout_view, name="checkout"),
]
