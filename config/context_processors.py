from django.conf import settings

from config.version import APP_VERSION


def app_version(request):
    """Exposes the resolved deploy stamp to every template as `app_version`
    ({"version": ..., "deployed_at": ...}) -- rendered small in the footer so
    staff can confirm at a glance which build a given device is showing (see
    config/version.py for how it's resolved)."""
    return {"app_version": APP_VERSION}


def show_admin_link(request):
    """Exposes settings.SHOW_ADMIN_LINK to templates as `show_admin_link`.
    Off by default; the staging/beta deploy sets SHOW_ADMIN_LINK=true so its
    platform-host landing page carries a convenience link to /admin/. Prod
    leaves it off so the public marketing page never advertises the superuser
    surface (see the setting's docstring in config/settings/base.py)."""
    return {"show_admin_link": settings.SHOW_ADMIN_LINK}
