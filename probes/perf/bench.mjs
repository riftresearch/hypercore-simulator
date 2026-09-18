import http from 'node:http';
import {readFileSync, writeFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {createHash} from 'node:crypto';

const [root, base, pidText, profile, casesText] = process.argv.slice(2);
const url = new URL(base);
if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:') throw new Error('Literal loopback only');
const pid = Number(pidText);
const fixture = JSON.parse(readFileSync(`${root}/payloads.json`, 'utf8'));
const {metadata, users} = fixture;
const cases = JSON.parse(casesText);
const setupAgent = new http.Agent({keepAlive: true, maxSockets: 32, maxFreeSockets: 32});
const errors = [];
function request(agent, path, body) {
  const wire = typeof body === 'string' ? body : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = http.request({hostname: url.hostname, port: url.port, path, method: 'POST', agent,
      headers: {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(wire)}}, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('error', reject);
      res.on('end', () => {
        try {
          const text = Buffer.concat(chunks).toString();
          if (res.statusCode !== 200) throw new Error(`HTTP ${res.statusCode}: ${text}`);
          resolve(JSON.parse(text));
        } catch (error) {reject(error);}
      });
    });
    req.setTimeout(120000, () => req.destroy(new Error('120 second request timeout')));
    req.on('error', reject);
    req.end(wire);
  });
}
async function parallelItems(items, concurrency, fn) {
  let next = 0;
  await Promise.all(Array.from({length: concurrency}, async () => {
    while (next < items.length) {
      const i = next++;
      await fn(items[i], i);
    }
  }));
}
function serverStats() {
  const stat = readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ')[1].split(' ');
  const status = readFileSync(`/proc/${pid}/status`, 'utf8');
  return {cpu_ticks: Number(stat[11]) + Number(stat[12]),
    rss_kib: Number(status.match(/^VmRSS:\s+(\d+)/m)[1]),
    peak_rss_kib: Number(status.match(/^VmHWM:\s+(\d+)/m)[1])};
}
function percentile(sorted, p) {
  return sorted[Math.max(0, Math.ceil(sorted.length * p) - 1)];
}
const report = {
  profile, base_url: base, server_pid: pid, node: process.version,
  workload: metadata,
  payload_sha256: createHash('sha256').update(readFileSync(`${root}/payloads.json`)).digest('hex'),
  qualifications: [
    'Loopback HTTP/1.1 keep-alive; one outstanding order per active user; distinct signed users',
    'Pre-signed successful single-order IOC buys; signing, funding, connection warmup and reconciliation excluded',
    'One shared market, one ask level, two-token portfolios, fixed clock (no minute-boundary dust sweeps)',
    'Closed-loop saturated load; latency measured before http.request until complete parsed response',
    'Server RSS high-water includes setup and prior cases in this process; not unbounded-history capacity',
  ],
  cases: [],
};
for (const trial of cases) {
  const {name, concurrency, per_user} = trial;
  if (concurrency > users.length || per_user > metadata.orders_per_user) throw new Error('Unsupported case');
  await request(setupAgent, '/_test/reset', {});
  await request(setupAgent, '/_test/time', {now_ms: metadata.fixed_time_ms});
  await request(setupAgent, '/_test/book', {coin: metadata.market, markPx: '10',
    bids: [{px: '9', sz: '1000000', n: 1}], asks: [{px: '10', sz: '1000000', n: 1}]});
  await parallelItems(users, 32, async user => {
    const result = await request(setupAgent, '/_test/fund', {address: user.address, token: 'USDC', amount: '100000', mode: 'transfer'});
    if (!result.ok) throw new Error(`Funding failed: ${JSON.stringify(result)}`);
  });
  const agent = new http.Agent({keepAlive: true, maxSockets: concurrency, maxFreeSockets: concurrency, scheduling: 'fifo'});
  await Promise.all(Array.from({length: concurrency}, () => request(agent, '/info', {type: 'userRole', user: users[0].address})));
  const warmConnections = Object.values(agent.freeSockets).reduce((sum, value) => sum + value.length, 0);
  const latencies = [];
  const oids = new Set();
  const caseErrors = [];
  let success = 0;
  const serverBefore = serverStats();
  const clientBefore = process.cpuUsage();
  const started = performance.now();
  await parallelItems(users, concurrency, async (user, userIndex) => {
    for (let i = 0; i < per_user; i++) {
      const begin = performance.now();
      try {
        const response = await request(agent, '/exchange', user.requests[i]);
        const filled = response.response?.data?.statuses?.[0]?.filled;
        if (response.status !== 'ok' || Number(filled?.totalSz) !== 2 || Number(filled?.avgPx) !== 10
            || filled?.cloid !== `0x${(userIndex + 1).toString(16).padStart(16, '0')}${(i + 1).toString(16).padStart(16, '0')}`
            || oids.has(filled?.oid)) throw new Error(`Unexpected order result: ${JSON.stringify(response)}`);
        oids.add(filled.oid);
        success++;
      } catch (error) {
        caseErrors.push(String(error));
      }
      latencies.push(performance.now() - begin);
    }
  });
  const elapsed = (performance.now() - started) / 1000;
  const clientCpu = process.cpuUsage(clientBefore);
  const serverAfter = serverStats();
  agent.destroy();
  latencies.sort((a, b) => a - b);
  const accountChecks = [];
  await parallelItems(users, 32, async user => {
    const state = await request(setupAgent, '/info', {type: 'spotClearinghouseState', user: user.address});
    const usdc = state.balances.find(balance => balance.token === 0);
    const purr = state.balances.find(balance => balance.token === 1);
    if (Number(usdc?.total) !== 100000 - per_user * 20
        || Math.round(Number(purr?.total) * 100000) !== per_user * 199860
        || Number(purr?.entryNtl) !== per_user * 20) accountChecks.push(user.address);
  });
  const book = await request(setupAgent, '/info', {type: 'l2Book', coin: metadata.market});
  const remainingDepthMatches = Number(book.levels[1][0].sz) === 1000000 - users.length * per_user * 2;
  const result = {
    name, concurrency, users: users.length, orders_per_user: per_user, warm_connections: warmConnections,
    attempted_orders: latencies.length, filled_orders: success, errors: caseErrors.length,
    first_errors: caseErrors.slice(0, 3), elapsed_seconds: elapsed, filled_orders_per_second: success / elapsed,
    latency_ms: {mean: latencies.reduce((a,b) => a+b, 0) / latencies.length,
      p50: percentile(latencies, 0.50), p95: percentile(latencies, 0.95), p99: percentile(latencies, 0.99), max: latencies.at(-1)},
    server_cpu_cores: (serverAfter.cpu_ticks - serverBefore.cpu_ticks) / metadata.clock_ticks_per_second / elapsed,
    client_cpu_cores: (clientCpu.user + clientCpu.system) / 1e6 / elapsed,
    server_rss_kib: serverAfter.rss_kib, server_process_peak_rss_kib: serverAfter.peak_rss_kib,
    reconciled_accounts: users.length - accountChecks.length, remaining_depth_matches: remainingDepthMatches,
  };
  report.cases.push(result);
  writeFileSync(`${root}/${profile}.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(result));
  if (caseErrors.length || accountChecks.length || !remainingDepthMatches || warmConnections !== concurrency) {
    errors.push({name, caseErrors: caseErrors.slice(0,3), accountChecks: accountChecks.slice(0,3), remainingDepthMatches});
    break;
  }
}
setupAgent.destroy();
if (errors.length) {console.error(JSON.stringify(errors)); process.exitCode = 1;}
