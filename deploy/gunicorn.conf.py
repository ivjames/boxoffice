"""Gunicorn config for the boxoffice web app, loaded by `bin/boxoffice serve`
(via --config). `--bind` and `--workers` still come from the CLI there (off the
app-dir .env); this file only sets what the CLI doesn't.

Two settings, both for the optional logo background-removal feature
(tenants/logo_bg.py), whose first use in a fresh worker pays a big ONE-TIME cost
-- importing rembg (numba + onnxruntime, ~20s on a 2-core droplet) and loading
its ~170MB model (~4s). The actual removal of a normalized <=512px logo is ~2s.

- timeout: raised from gunicorn's default 30s. That cold start ran past 30s
  inside the request, so the arbiter SIGABRT'd the worker mid-import and the
  request 500'd. 120s is ample headroom; a warmed removal stays ~2s.
- post_worker_init: warm rembg in a BACKGROUND thread as each worker boots, so
  the one-time cost is paid at startup off the request path -- the first user
  click then hits an already-warm, cached session. Backgrounded so it never
  blocks the worker from serving and a slow/absent model can't stall boot. No-op
  when rembg isn't installed (warm() swallows that).
"""

import threading

timeout = 120


def post_worker_init(worker):
    def _warm():
        try:
            from tenants.logo_bg import warm

            warm()
        except Exception:
            # Warming is best-effort: never let it crash or hang a worker. A
            # cold first request still works (just slower), covered by timeout.
            pass

    threading.Thread(target=_warm, name="rembg-warm", daemon=True).start()
