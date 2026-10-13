'use strict';
// semantic-crosscheck adapter: Cube Core on 127.0.0.1 serving a generated data model (development mode off).
// Configuration comes only from the environment prepared by the Python runner (cube_run.py):
//   CUBEJS_DB_* connection settings, CUBEJS_API_SECRET (random per run), SXC_PORT (0 = ephemeral),
//   SXC_MODEL_DIR (relative to the working directory, default "model"), SXC_CUBE_DEBUG ("1" = full error text).
// Everything printed here lands in a server.log next to the results. Request logs are dropped because they contain
// query filter values. PostgreSQL and Cube error messages quote values too (an id compared with a number column is
// echoed back), in a quoting style that depends on the server's message language ('x', "x", »x«, « x », 「x」, ...),
// so by default no message text of a query error is kept at all: a line names the error class, the SQLSTATE or error
// code when there is one, and a coarse category (timeout). Model compile errors are the exception: they come from the
// generated model (member names and configuration literals, never rows) and are kept so a bad model can be fixed.
// Free-form console output of the Cube packages is replaced by one notice. SXC_CUBE_DEBUG=1 adds the full text for
// local debugging; the log can then hold row values and must not be shared.
process.env.CUBEJS_TELEMETRY = 'false';
const DEBUG = process.env.SXC_CUBE_DEBUG === '1';
const print = { log: console.log.bind(console), error: console.error.bind(console) };

const firstLine = (text) => String(text).split(/\r?\n/, 1)[0];
const scrub = (text) => firstLine(text)
  .replace(/[^A-Za-z ]/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()
  .slice(0, 80);
const messageOf = (err) => String(err instanceof Error ? err.message : err);
const errorClass = (err) => {
  const m = /^\s*([A-Za-z][A-Za-z0-9]*(?:Error|Exception))\b/.exec(err instanceof Error ? err.name : String(err));
  return m ? m[1] : 'Error';
};
const summary = (err) => {
  const code = err && typeof err.code === 'string' && /^[A-Z0-9_]{2,40}$/.test(err.code) ? ` [${err.code}]` : '';
  const text = messageOf(err);
  if (/compile errors/i.test(text)) {
    return `${errorClass(err)}${code} (model): ${text.replace(/\s+/g, ' ').slice(0, 1500)}`;
  }
  const category = /time ?out|timed out/i.test(text) ? ' (timeout)' : '';
  return `${errorClass(err)}${code}${category}`;
};
const report = (tag, msg, err) => {
  print.error(`${tag} ${scrub(msg)}: ${summary(err)}`);
  if (DEBUG) {
    print.error(`SXC_DEBUG ${msg}: ${String((err && err.stack) || err).slice(0, 4000)}`);
  }
};

if (!DEBUG) {
  let noticed = false;
  const hide = () => {
    if (!noticed) {
      noticed = true;
      print.error('SXC_CONSOLE console output of the Cube packages is hidden (SXC_CUBE_DEBUG=1 shows it)');
    }
  };
  for (const name of ['log', 'info', 'warn', 'error', 'debug', 'trace']) {
    console[name] = hide;
  }
}
process.on('uncaughtException', (e) => { report('SXC_ERROR', 'uncaught exception', e); process.exit(1); });
process.on('unhandledRejection', (e) => { report('SXC_ERROR', 'unhandled rejection', e); process.exit(1); });

const express = require('express');
const { CubejsServerCore } = require('@cubejs-backend/server-core');

const secret = process.env.CUBEJS_API_SECRET || '';
if (secret.length < 32) {
  print.error('SXC_ERROR CUBEJS_API_SECRET must be set by the runner');
  process.exit(2);
}

const logger = (msg, params) => {
  if (params && params.error) {
    report('SXC_CUBE_ERROR', msg, params.error);
  }
};

const core = new CubejsServerCore({
  apiSecret: secret,
  schemaPath: process.env.SXC_MODEL_DIR || 'model',
  devServer: false,
  cacheAndQueueDriver: 'memory',
  telemetry: false,
  scheduledRefreshTimer: false,
  logger,
});

const app = express();
app.use(express.json({ limit: '1mb' }));

Promise.resolve(core.initApp(app))
  .then(() => {
    const server = app.listen(Number(process.env.SXC_PORT || 0), '127.0.0.1', () => {
      print.log('SXC_READY ' + server.address().port);
    });
    // The runner keeps stdin open; when it closes (normal stop or the runner died) the adapter exits.
    process.stdin.on('end', () => process.exit(0));
    process.stdin.resume();
  })
  .catch((e) => {
    report('SXC_ERROR', 'startup failed', e);
    process.exitCode = 1;
  });
