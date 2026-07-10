"""Deploy/version stamp -- shown in the site footer and handy for reasoning
about which build is actually live (e.g. confirming a phone/iPad isn't serving
a stale page after a deploy).

Resolved ONCE at import, cheapest-reliable source first:

  1. ``APP_VERSION`` env var -- set by the process manager, if the operator
     wants to pin it explicitly (pairs with optional ``APP_DEPLOYED_AT``).
  2. a ``VERSION`` file at the repo root -- written by ``bin/boxoffice deploy``
     (format: ``<short-sha> <iso8601>`` on one line). Survives a .git-less
     checkout, so it's the normal production source.
  3. ``git rev-parse --short HEAD`` -- runtime fallback when neither is present
     (local runs, or a deploy that predates the VERSION file).
  4. ``"dev"`` -- last resort when nothing above resolves.

Static assets are already content-hashed by WhiteNoise's manifest storage
(see STORAGES in config/settings/base.py), so this stamp is for humans, not
for busting the CSS/JS cache -- those bust themselves on content change.
"""

import os
import subprocess
from pathlib import Path

# config/version.py -> config -> repo root
BASE_DIR = Path(__file__).resolve().parent.parent


def _from_version_file():
    try:
        text = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not text:
        return None, None
    # First line only; "<version> <iso8601-deploy-time>" (time optional).
    first = text.splitlines()[0].strip()
    parts = first.split(maxsplit=1)
    version = parts[0] or None
    deployed_at = parts[1].strip() if len(parts) > 1 else None
    return version, deployed_at


def _git(args):
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _from_git():
    sha = _git(["rev-parse", "--short", "HEAD"])
    if not sha:
        return None, None
    return sha, _git(["show", "-s", "--format=%cI", "HEAD"])


def resolve():
    """Return {"version": str, "deployed_at": str|None}. Never raises."""
    env_version = os.environ.get("APP_VERSION")
    if env_version and env_version.strip():
        return {
            "version": env_version.strip(),
            "deployed_at": (os.environ.get("APP_DEPLOYED_AT") or "").strip() or None,
        }
    for source in (_from_version_file, _from_git):
        version, deployed_at = source()
        if version:
            return {"version": version, "deployed_at": deployed_at}
    return {"version": "dev", "deployed_at": None}


# Computed once per process; the underlying sources only change on redeploy
# (which restarts the process), so there's no reason to re-resolve per request.
APP_VERSION = resolve()
