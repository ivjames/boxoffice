// pm2 ecosystem file for boxoffice. Started automatically by
// `boxoffice deploy` the first time (when no pm2 app named "boxoffice"
// exists yet); afterwards `boxoffice deploy`/`restart` just restart it.
//
// Manual equivalent, if you'd rather start it by hand:
//   cd /var/www/boxoffice && pm2 start deploy/ecosystem.config.js && pm2 save
//
// gunicorn itself reads PORT/WEB_CONCURRENCY from the app-dir .env (via
// `bin/boxoffice serve`), so this file does not need an `env:` block for
// those -- .env is the single source of truth, matching every other
// lab980 site.
//
// cwd and the pm2 app name are derived from this file's own location (its
// parent dir) rather than hard-coded, so the SAME file works unchanged for a
// second install: the beta/staging deploy at /var/www/boxoffice-beta comes up
// as the pm2 app "boxoffice-beta" (matching bin/boxoffice's PM2_APP_NAME),
// with no collision against prod's "boxoffice".
const path = require("path");
const appDir = path.resolve(__dirname, "..");

module.exports = {
  apps: [
    {
      name: path.basename(appDir),
      cwd: appDir,
      script: "bin/boxoffice",
      args: "serve",
      interpreter: "none",
      autorestart: true,
      // gunicorn manages its own worker processes; pm2 just supervises the
      // one master process bin/boxoffice serve execs into.
      instances: 1,
      exec_mode: "fork",
    },
  ],
};
