from django.http import JsonResponse
from django.shortcuts import render


def healthz(request):
    return JsonResponse({"status": "ok"})


def home(request):
    """
    Root URL. Renders the tenant storefront placeholder when
    request.organization is set (a tenant subdomain), otherwise the platform
    landing placeholder (reserved subdomain / bare host).
    """
    if request.organization:
        return render(request, "tenants/storefront_home.html")
    return render(request, "tenants/platform_landing.html")
