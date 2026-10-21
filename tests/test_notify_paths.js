// Confirms the embedded Notify Stash function passes Windows paths through verbatim.
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const axios = (cfg) => new Promise((resolve, reject) => {
  const u = new URL(cfg.url);
  const req = http.request({
    hostname: u.hostname, port: u.port, path: u.pathname,
    method: (cfg.method || 'get').toUpperCase(), headers: cfg.headers, timeout: cfg.timeout,
  }, (res) => {
    let b = '';
    res.on('data', (c) => { b += c; });
    res.on('end', () => resolve({ status: res.statusCode, data: JSON.parse(b || '{}') }));
  });
  req.on('error', reject);
  if (cfg.data) req.write(JSON.stringify(cfg.data));
  req.end();
});

const FLOW = path.join(__dirname, '..', 'tdarr-flow', 'Stash-HEVC-Flow.json');
const flow = JSON.parse(fs.readFileSync(FLOW, 'utf8'));
const raw = flow.flowPlugins.find((p) => p.pluginName === 'customFunction').inputsDB.code;

// Real Windows paths, written as JS literals so nothing can eat the separators.
const ORIGINAL = 'Z:\\Media\\Movies\\Foo (2009)\\Foo.avi';
const NEWPATH = 'Z:\\Media\\Movies\\Foo (2009)\\Foo.mkv';

let received = null;
const srv = http.createServer((req, res) => {
  let b = '';
  req.on('data', (c) => { b += c; });
  req.on('end', () => {
    received = JSON.parse(b);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ data: { runPluginTask: 'job-42' } }));
  });
});

srv.listen(0, '127.0.0.1', async () => {
  const code = raw.replace('http://STASH-HOST:9999', `http://127.0.0.1:${srv.address().port}`);
  const tmp = path.join(os.tmpdir(), `notify_${Date.now()}.js`);
  fs.writeFileSync(tmp, code);
  const fn = require(tmp);

  const logs = [];
  await fn({
    inputFileObj: { _id: NEWPATH },
    originalLibraryFile: { _id: ORIGINAL },
    jobLog: (m) => logs.push(m),
    variables: {},
    deps: { axios },
  });

  const sent = received.variables.argsMap;
  const checks = [
    ['originalPath verbatim', sent.originalPath === ORIGINAL, sent.originalPath],
    ['newPath verbatim', sent.newPath === NEWPATH, sent.newPath],
    ['mode is sync', sent.mode === 'sync', sent.mode],
    ['description is basename only',
      received.variables.description === 'Tdarr sync: Foo.mkv',
      received.variables.description],
  ];

  let failed = 0;
  checks.forEach(([label, ok, actual]) => {
    if (!ok) failed += 1;
    console.log(`  ${ok ? 'PASS' : 'FAIL'} ${label}\n       -> ${JSON.stringify(actual)}`);
  });

  fs.unlinkSync(tmp);
  srv.close();
  console.log(`\n${checks.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
});
