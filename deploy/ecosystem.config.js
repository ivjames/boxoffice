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
module.exports = {
  apps: [
    {
      name: "boxoffice",
      cwd: "/var/www/boxoffice",
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
