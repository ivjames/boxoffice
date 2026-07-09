from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("tenants.urls")),
    path("", include("accounts.urls")),
    path("", include("events.urls")),
    path("", include("orders.urls")),
    path("", include("payments.urls")),
    path("", include("dashboard.urls")),
    path("", include("scanning.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
