from django.urls import path

from . import views

urlpatterns = [
    path("passes/", views.pass_list, name="passes"),
    path("passes/<int:pk>/", views.pass_detail, name="pass_detail"),
    # STUB pass payment: the simulated hosted-payment page
    # create_pass_checkout_session redirects to when a tenant hasn't finished
    # Stripe Connect onboarding -- see pass_stub's docstring.
    path("passes/stub/", views.pass_stub, name="pass_stub"),
    # -- redemption (spend an owned pass) ------------------------------------
    path("passes/redeem/start/", views.pass_redeem_start, name="pass_redeem_start"),
    path("passes/redeem/exit/", views.pass_redeem_exit, name="pass_redeem_exit"),
    path("passes/redeem/", views.pass_redeem, name="pass_redeem"),
]
