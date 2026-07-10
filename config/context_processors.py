from config.version import APP_VERSION


def app_version(request):
    """Exposes the resolved deploy stamp to every template as `app_version`
    ({"version": ..., "deployed_at": ...}) -- rendered small in the footer so
    staff can confirm at a glance which build a given device is showing (see
    config/version.py for how it's resolved)."""
    return {"app_version": APP_VERSION}
