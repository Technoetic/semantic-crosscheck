'use strict';
// semantic-crosscheck adapter: Cube Core on 127.0.0.1 serving a generated data model (development mode off).
// Configuration comes only from the environment prepared by the Python runner (cube_run.py):
//   CUBEJS_DB_* connection settings, CUBEJS_API_SECRET (random per run), SXC_PORT (0 = ephemeral),
//   SXC_MODEL_DIR (relative to the working directory, default "model").
// Request logs are dropped because they contain query filter values; errors are kept.
process.env.CUBEJS_TELEMETRY = 'false';
const express = require('express');
const { CubejsServerCore } = require('@cubejs-backend/server-core');

const secret = process.env.CUBEJS_API_SECRET || '';
if (secret.length < 32) {
  console.error('SXC_ERROR CUBEJS_API_SECRET must be set by the runner');
  process.exit(2);
}

const logger = (msg, params) => {
  if (params && params.error) {
    console.error('SXC_CUBE_ERROR ' + msg + ': ' + String(params.error).slice(0, 2000));
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
      console.log('SXC_READY ' + server.address().port);
    });
    // The runner keeps stdin open; when it closes (normal stop or the runner died) the adapter exits.
    process.stdin.on('end', () => process.exit(0));
    process.stdin.resume();
  })
  .catch((e) => {
    console.error('SXC_ERROR ' + (e && e.message));
    process.exitCode = 1;
  });
