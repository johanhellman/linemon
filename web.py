#!/usr/bin/env python3
"""linemon web - a read-only status page for the linemon data.

  web.py [--data /var/lib/linemon] [--port 8080]

Serves / (the page) and /api/status (JSON). The page refreshes itself every
10 seconds. Uses the same outage logic as analyze.py.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analyze

TARGETS = ['link', 'gateway', 'isp_hop1', 'isp_hop2'] + analyze.INTERNET_HOSTS + ['dns_gateway', 'dns_1.1.1.1']
LABELS = {
    'link': 'Cable (link)', 'gateway': 'ISP router', 'isp_hop1': 'ISP hop 1', 'isp_hop2': 'ISP hop 2',
    'dns_gateway': 'DNS via router', 'dns_1.1.1.1': 'DNS 1.1.1.1',
}


def iso(t):
    return t.isoformat(timespec='seconds') if t else None


# The monitor's health.json, as the page may show it. The page has no login, so nothing the monitor wrote
# reaches it unchecked: only known fields, fixed wording for the reasons, and values that look like plain text.
HEALTH_REASONS = {
    'probe-stalled': 'a measurement thread stopped making progress',
    'probe-error': 'an error on the monitor itself while probing',
    'write-failing': 'data could not be written to disk',
    'clock': 'the clock is unreliable',
    'power': "the Pi's power supply is under-voltage",
    'disk': 'the disk is nearly full',
}
HEALTH_SIGNALS = [
    ('threads', 'Measurement threads'), ('probe_errors', 'Errors while probing'), ('writes', 'Writing data'),
    ('clock', 'Clock'), ('power', 'Power and temperature'), ('disk', 'Disk space'), ('router_capture', 'Router capture'),
]
HEALTH_STALE_S = 60  # health.json is rewritten every 15 s: older than this and the monitor isn't reporting
PLAIN_TEXT = re.compile(r'^[\w .,:+\-()]{0,120}$')  # no slashes, quotes, '=' or markup: no paths, no snippets of code or replies


def read_health(data_dir, now):
    """The monitor's view of its own health for the page, or None if it has never written one."""
    path = os.path.join(data_dir, 'health.json')
    nothing = {'state': 'unknown', 'reporting': False, 'time': None, 'age_s': None,
               'unhealthy_since': None, 'reasons': [], 'signals': []}
    try:
        if os.path.getsize(path) > 20000:
            return nothing
        with open(path) as f:
            doc = json.load(f)
        written, state = analyze.parse_time(doc['time']), doc['state']
    except FileNotFoundError:
        return None  # an older monitor, or one that hasn't sampled yet
    except (OSError, ValueError, KeyError, TypeError):
        return nothing
    if state not in ('healthy', 'degraded', 'unhealthy', 'stopped'):
        return nothing
    age = (now - written).total_seconds()
    reasons = doc.get('reasons') if isinstance(doc.get('reasons'), list) else []
    signals, raw = [], doc.get('signals') if isinstance(doc.get('signals'), dict) else {}
    for key, label in HEALTH_SIGNALS:
        sig = raw.get(key)
        if isinstance(sig, dict) and sig.get('state') in ('ok', 'degraded', 'unhealthy'):
            value = sig.get('value')
            signals.append({'name': label, 'state': sig['state'],
                            'value': value if isinstance(value, str) and PLAIN_TEXT.match(value) else ''})
    try:
        since = analyze.parse_time(doc['unhealthy_since']) if doc.get('unhealthy_since') else None
    except (ValueError, TypeError):
        since = None
    return {'state': state, 'reporting': state == 'stopped' or age <= HEALTH_STALE_S, 'time': iso(written),
            'age_s': round(age), 'unhealthy_since': iso(since),
            'reasons': [HEALTH_REASONS[r] for r in reasons if isinstance(r, str) and r in HEALTH_REASONS],
            'signals': signals}


def minute_series(data_dir, since):
    """Per minute: share of probes lost to the Livebox, ISP hop 1 and the internet.

    'internet' is the lowest loss of the three internet hosts, i.e. loss they
    all had at once.
    """
    by_minute = {}
    for r in analyze.minute_rows(data_dir, since):
        try:
            sent, lost = int(r['sent']), int(r['lost'])
        except (ValueError, TypeError):
            continue  # a line cut short by a power cut
        if sent:
            by_minute.setdefault(r['minute'], {})[r['target']] = lost / sent
    series = []
    for minute in sorted(by_minute):
        m = by_minute[minute]
        hosts = [m[h] for h in analyze.INTERNET_HOSTS if h in m]
        series.append({
            't': minute,
            'gateway': m.get('gateway'),
            'isp_hop1': m.get('isp_hop1'),
            'internet': min(hosts) if len(hosts) == len(analyze.INTERNET_HOSTS) else None,
        })
    return series


def with_down_durations(events, now):
    """Give each 'down' row the duration of its outage.

    events.csv writes a down row as soon as an outage starts (so it survives a
    crash) and only the matching up row carries the duration. Copy it back, or
    mark the outage as ongoing if the target hasn't come back yet.
    """
    rows = [dict(r) for r in events]
    open_down = {}
    for r in rows:
        if r['target'] == 'monitor':
            if r['event'] in ('start', 'stop'):
                open_down.clear()  # outages open across a restart have no known end
            continue
        if r['event'] == 'down':
            open_down[r['target']] = r
        elif r['event'] == 'up' and r['target'] in open_down:
            open_down.pop(r['target'])['duration_s'] = r['duration_s']
    last_start = max((i for i, r in enumerate(rows) if r['target'] == 'monitor' and r['event'] == 'start'), default=-1)
    for r in open_down.values():
        if rows.index(r) > last_start:  # still down in the current run
            r['ongoing'] = True
            r['duration_s'] = str(round((now - analyze.parse_time(r['time'])).total_seconds()))
    return rows


_sessions_memo = {}


def sessions(data_dir, since, captures):
    """analyze.session_starts, kept until a new capture arrives (they come every few minutes)."""
    key = (data_dir, since, len(captures), captures[-1]['time'] if captures else None)
    if _sessions_memo.get('key') != key:
        _sessions_memo.update(key=key, value=analyze.session_starts(captures))
    return _sessions_memo['value']


def status(data_dir):
    now = dt.datetime.now().astimezone()
    events = analyze.load_events(data_dir)
    outages, periods = analyze.load_outages(data_dir)
    last_data = analyze.last_minute_end(data_dir)
    change = analyze.router_change(events)

    targets = []
    for name in TARGETS:
        ivs = outages.get(name, [])
        current = next((s for s, e, _ in ivs if e is None), None)
        closed = [(s, e) for s, e, _ in ivs if e]
        targets.append({
            'name': name, 'label': LABELS.get(name, name),
            'down_since': iso(current),
            'outages': len(closed),
            'downtime_s': round(sum((e - s).total_seconds() for s, e in closed)),
        })

    since = periods[0][0] if periods else None
    stale = bool(last_data and (now - last_data).total_seconds() > 180)
    captures = analyze.captures_since(analyze.load_captures(data_dir), since) if since else []
    # Outages still in progress run until now, or until the last data if the monitor has stopped.
    open_end = last_data if stale else now
    internet = analyze.intersect_all([outages.get(h, []) for h in analyze.INTERNET_HOSTS], open_end=open_end)
    internet_rows = []
    for s, e, ongoing in reversed(internet):
        internet_rows.append({'start': iso(s), 'end': None if ongoing else iso(e), 'ongoing': ongoing,
                              'duration_s': round((e - s).total_seconds()),
                              'layer': analyze.classify(s, e, outages),
                              'router': analyze.router_during(captures, s, e)})
    day_ago = now - dt.timedelta(hours=24)
    last_day = [r for r in internet_rows if analyze.parse_time(r['start']) >= day_ago]
    internet_down_since = next((r['start'] for r in internet_rows if r['ongoing']), None)

    recent = list(reversed(with_down_durations(events, now)))[:60]
    router_status = None
    if captures:
        latest = captures[-1]
        router_status = {
            'time': iso(latest['time']), 'reason': latest.get('reason'), 'ok': latest.get('ok'),
            'summary': latest.get('summary'), 'error': latest.get('error'),
            'sessions': [{'start': iso(s['start']), 'last_seen': iso(s['last_seen'])}
                         for s in reversed(sessions(data_dir, since, captures))][:20],
        }
    return {
        'router_status': router_status,
        'now': iso(now),
        'monitoring_since': iso(periods[0][0]) if periods else None,
        'router': change['detail'].split('->')[-1].strip() if change else None,
        'last_data': iso(last_data),
        'stale': stale,
        'health': read_health(data_dir, now),
        'internet_down_since': internet_down_since,
        'targets': targets,
        'internet': {
            'total': len(internet_rows),
            'downtime_s': sum(r['duration_s'] for r in internet_rows),
            'last_24h': len(last_day),
            'downtime_24h_s': sum(r['duration_s'] for r in last_day),
            'outages': internet_rows[:200],
        },
        'series': minute_series(data_dir, day_ago),
        'events': recent,
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Line monitor</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#6b7280; --line:#e5e7eb;
        --ok:#15803d; --ok-bg:#dcfce7; --bad:#b91c1c; --bad-bg:#fee2e2; --warn:#a16207; --warn-bg:#fef3c7;
        --gw:#2563eb; --hop:#9333ea; --inet:#dc2626; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --card:#181b22; --text:#e5e7eb; --muted:#9ca3af; --line:#2a2f3a;
          --ok:#4ade80; --ok-bg:#12301d; --bad:#f87171; --bad-bg:#3b1515; --warn:#facc15; --warn-bg:#3a2f0b;
          --gw:#60a5fa; --hop:#c084fc; --inet:#f87171; }
}
* { box-sizing:border-box }
body { margin:0; font:14px/1.45 -apple-system, system-ui, sans-serif; background:var(--bg); color:var(--text) }
main { max-width:1100px; margin:0 auto; padding:16px }
h1 { font-size:20px; margin:4px 0 2px } h2 { font-size:15px; margin:0 0 10px }
.muted { color:var(--muted) } .small { font-size:12px }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; margin:12px 0 }
.banner { font-size:22px; font-weight:600; padding:16px; border-radius:10px; margin:12px 0 }
.banner.sub { font-size:15px; padding:10px 14px; margin:-4px 0 12px }
.ok { background:var(--ok-bg); color:var(--ok) } .bad { background:var(--bad-bg); color:var(--bad) }
.warn { background:var(--warn-bg); color:var(--warn) }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:8px }
.tile { border:1px solid var(--line); border-radius:8px; padding:8px 10px }
.tile b { display:block } .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px }
.stats { display:flex; gap:28px; flex-wrap:wrap } .stats div b { display:block; font-size:22px }
table { width:100%; border-collapse:collapse } th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line) }
th { color:var(--muted); font-weight:500; font-size:12px } td.num { text-align:right; font-variant-numeric:tabular-nums }
td.nw { white-space:nowrap } tr.ongoing td { color:var(--bad); font-weight:600 }
.pill { display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; font-weight:600 }
[hidden] { display:none !important }
.scroll { max-height:420px; overflow:auto }
canvas { width:100%; height:150px; display:block }
.legend span { margin-right:14px }
</style>
</head>
<body>
<main>
  <h1>Line monitor</h1>
  <div class="muted small" id="meta">Loading…</div>
  <div class="banner" id="banner">…</div>
  <div class="banner sub" id="health-banner" hidden></div>

  <div class="card"><h2>Internet outages</h2><div class="stats" id="stats"></div></div>

  <div class="card" id="router-card" hidden>
    <h2>What the router reports</h2>
    <p><span class="pill" id="router-pill"></span> <span id="router-summary"></span></p>
    <p class="small muted" id="router-meta"></p>
    <h3 class="small" style="margin:14px 0 6px">Internet session (re)started at</h3>
    <p class="small muted" style="margin:0 0 6px">Worked out from the connection uptime the router reports. A new
      session means the operator's side dropped and re-established the connection.</p>
    <div class="scroll" style="max-height:200px"><table><tbody id="sessions"></tbody></table></div>
  </div>

  <div class="card" id="health-card" hidden>
    <h2>Monitor health <span class="pill" id="health-pill"></span></h2>
    <p class="small muted" id="health-meta"></p>
    <table class="small"><tbody id="health-signals"></tbody></table>
  </div>

  <div class="card">
    <h2>Lost probes, last 24 hours</h2>
    <canvas id="chart"></canvas>
    <div class="legend small muted">
      <span><i class="dot" style="background:var(--gw)"></i>ISP router</span>
      <span><i class="dot" style="background:var(--hop)"></i>ISP hop 1</span>
      <span><i class="dot" style="background:var(--inet)"></i>Internet (all three hosts)</span>
    </div>
  </div>

  <div class="card"><h2>Targets</h2><div class="grid" id="targets"></div></div>

  <div class="card"><h2>Internet outages, newest first</h2>
    <div class="scroll"><table><thead><tr><th>Start</th><th>End</th><th class="num">Duration</th><th>Where it broke</th><th>Router said</th></tr></thead>
    <tbody id="outages"></tbody></table></div></div>

  <div class="card">
    <details>
      <summary><h2 style="display:inline">What is checked</h2> <span class="muted small">(click to expand)</span></summary>
      <p class="small muted">Every target is probed about once a second over the wired port only. A target counts as
        down after 3 failed probes in a row; the outage starts at the first failed probe.</p>
      <table class="small"><thead><tr><th>Target</th><th>What it is</th><th>Check</th><th>If only this layer and beyond fail</th></tr></thead><tbody>
        <tr><td class="nw">Cable (link)</td><td>The monitor's own network port</td><td>Carrier state of the port</td><td>The monitor's cable or port</td></tr>
        <tr><td class="nw">ISP router</td><td>The router it is plugged into (e.g. the Livebox)</td><td>Ping to the default gateway</td><td>The router itself, or the cable to it</td></tr>
        <tr><td class="nw">ISP hop 1</td><td>The first router beyond yours that answers, a few hops into the operator's network</td><td>Ping towards 1.1.1.1 with a limited TTL; the router at that hop must answer "TTL exceeded"</td><td>The access network: fibre/line, or the operator's first equipment</td></tr>
        <tr><td class="nw">ISP hop 2</td><td>The next router after hop 1</td><td>Same, one hop further</td><td>Further into the operator's network</td></tr>
        <tr><td class="nw">1.1.1.1, 8.8.8.8, 9.9.9.9</td><td>Cloudflare, Google and Quad9</td><td>Ping each host</td><td>Internet outage when all three fail at once</td></tr>
        <tr><td class="nw">DNS via router / 1.1.1.1</td><td>Name lookups</td><td>Lookup of a random name (no cache can answer) via the router and directly</td><td>Name resolution</td></tr>
      </tbody></table>
      <p class="small muted">"Where it broke" names the first layer that was also down during an internet outage (for the two ISP hops, down for
        at least half of it: their replies are often rate-limited, so a few seconds of "down" proves nothing).
        "Beyond the ISP hops" means the router and the first hops answered, but the internet hosts did not.</p>
      <p class="small muted">"Monitor health": every 15 seconds the monitor checks that it can trust its own
        measurements: its threads keep making progress, nothing fails on the Pi while probing, data can be written,
        the clock is steady and synchronised, the power supply is steady (on a Raspberry Pi) and the disk isn't
        full. When it can't, the page says so, the internet status is shown as uncertain, and that time counts as
        unknown in the availability figures, never as up.</p>
      <p class="small muted">"Router said" is the ISP router's own status, read from its admin pages when the
        outage starts, every 30 s during it and every 5 minutes otherwise (only if a router capture is set up).
        For fibre: the GPON state (O5 = operational), whether the optical signal is present, and whether the
        router has an internet address.</p>
    </details>
  </div>

  <div class="card"><h2>Latest events</h2>
    <div class="scroll"><table><thead><tr><th>Time</th><th>Target</th><th>Event</th><th class="num">Duration</th><th>Detail</th></tr></thead>
    <tbody id="events"></tbody></table></div></div>
</main>
<script>
const fmtTime = s => s ? new Date(s).toLocaleString([], {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '';
function fmtDur(s) {
  s = Math.round(s); if (s < 60) return s + ' s';
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
  return h ? `${h} h ${m} min` : `${m} min ${r} s`;
}
// classes: per-column class names, e.g. ['nw', 'nw', 'num nw', '']
function row(cells, classes = []) {
  const tr = document.createElement('tr');
  cells.forEach((c, i) => { const td = document.createElement('td'); td.textContent = c ?? '';
    td.className = classes[i] || ''; tr.appendChild(td); });
  return tr;
}
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart(series) {
  const c = document.getElementById('chart'), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight; c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const end = Date.now(), start = end - 24 * 3600e3, x = t => (t - start) / (end - start) * w;
  const lanes = [['gateway', '--gw', 'ISP router'], ['isp_hop1', '--hop', 'ISP hop 1'], ['internet', '--inet', 'Internet']];
  const lh = (h - 18) / 3;
  g.strokeStyle = css('--line'); g.fillStyle = css('--muted'); g.font = '11px system-ui';
  for (let hr = 0; hr <= 24; hr += 3) {   // hour grid
    const t = new Date(end - hr * 3600e3); t.setMinutes(0, 0, 0); const px = x(t.getTime());
    if (px < 0 || px > w - 34) continue;  // label would be cut off at the edge
    g.beginPath(); g.moveTo(px, 0); g.lineTo(px, h - 16); g.stroke();
    g.fillText(t.getHours().toString().padStart(2, '0') + ':00', px + 2, h - 4);
  }
  const bw = Math.max(1, w / 1440);
  lanes.forEach(([key, color, label], i) => {
    const top = i * lh;
    g.fillStyle = css('--muted'); g.fillText(label, 4, top + 12);
    g.strokeStyle = css('--line'); g.beginPath(); g.moveTo(0, top + lh - 1.5); g.lineTo(w, top + lh - 1.5); g.stroke();
    g.fillStyle = css(color);
    for (const p of series) {
      const v = p[key]; if (v == null || v === 0) continue;
      const bh = Math.max(2, v * (lh - 4)); g.fillRect(x(new Date(p.t).getTime()), top + lh - bh - 2, bw, bh);
    }
  });
}

async function refresh() {
  let d;
  try { d = await (await fetch('api/status', {cache: 'no-store'})).json(); }
  catch (e) { document.getElementById('meta').textContent = 'Cannot reach the monitor: ' + e; return; }

  document.getElementById('meta').textContent =
    `Monitoring since ${fmtTime(d.monitoring_since)} · router ${d.router || '?'} · last data ${fmtTime(d.last_data)} · updated ${fmtTime(d.now)}`;
  const b = document.getElementById('banner'), h = d.health;
  if (d.stale) { b.className = 'banner warn'; b.textContent = 'No new measurements for over 3 minutes: is the monitor running?'; }
  else if (h && h.reporting && h.state === 'unhealthy') {
    b.className = 'banner warn'; b.textContent = "Internet status uncertain: the monitor can't trust its own measurements right now";
  }
  else if (d.internet_down_since) { b.className = 'banner bad'; b.textContent = 'Internet DOWN since ' + fmtTime(d.internet_down_since); }
  else { b.className = 'banner ok'; b.textContent = 'Internet OK'; }

  const hb = document.getElementById('health-banner'), hc = document.getElementById('health-card');
  hb.hidden = true; hc.hidden = !h;
  if (h) {
    let text = '', cls = 'warn';
    if (h.state === 'stopped') text = 'linemon stopped at ' + fmtTime(h.time) + '.';
    else if (!h.reporting) text = 'The monitor is not reporting its health' + (h.time ? ' (last at ' + fmtTime(h.time) + ')' : '') + '.';
    else if (h.state === 'unhealthy') {
      cls = 'bad';
      text = 'Measurements may be unreliable since ' + fmtTime(h.unhealthy_since) + (h.reasons.length ? ': ' + h.reasons.join('; ') : '') + '.';
    } else if (h.state === 'degraded') {
      text = 'Worth a look: ' + h.signals.filter(x => x.state === 'degraded').map(x => x.name + (x.value ? ' (' + x.value + ')' : '')).join('; ') + '.';
    }
    if (text) { hb.hidden = false; hb.className = 'banner sub ' + cls; hb.textContent = text; }
    const pill = document.getElementById('health-pill');
    pill.textContent = h.reporting ? h.state : (h.state === 'stopped' ? 'stopped' : 'not reporting');
    pill.className = 'pill ' + (h.reporting && h.state === 'healthy' ? 'ok' : (h.state === 'unhealthy' ? 'bad' : 'warn'));
    document.getElementById('health-meta').textContent = h.time ? 'Last checked ' + fmtTime(h.time) + '.' : 'No readable health report.';
    const hs = document.createDocumentFragment();
    h.signals.forEach(x => {
      const tr = row([x.name, x.value], ['nw', '']);
      const dot = document.createElement('i'); dot.className = 'dot';
      dot.style.background = css(x.state === 'ok' ? '--ok' : (x.state === 'degraded' ? '--warn' : '--bad'));
      tr.firstChild.prepend(dot); hs.appendChild(tr);
    });
    document.getElementById('health-signals').replaceChildren(hs);
  }

  // Each section is built off-screen and swapped in whole, so a refresh never
  // empties the page for a moment (which made it jump when scrolled down).
  const s = d.internet;
  const st = document.createDocumentFragment();
  [['Last 24 h', s.last_24h], ['Downtime 24 h', fmtDur(s.downtime_24h_s)], ['Total', s.total], ['Total downtime', fmtDur(s.downtime_s)]]
    .forEach(([k, v]) => { const el = document.createElement('div'); el.innerHTML = '<b></b><span class="muted small"></span>';
      el.querySelector('b').textContent = v; el.querySelector('span').textContent = k; st.appendChild(el); });
  document.getElementById('stats').replaceChildren(st);

  const tg = document.createDocumentFragment();
  d.targets.forEach(t => {
    const el = document.createElement('div'); el.className = 'tile ' + (t.down_since ? 'bad' : '');
    el.innerHTML = '<b><i class="dot"></i><span></span></b><span class="small muted"></span>';
    el.querySelector('.dot').style.background = t.down_since ? css('--bad') : css('--ok');
    el.querySelector('b span').textContent = t.label;
    el.querySelector('.small').textContent = t.down_since ? 'down since ' + fmtTime(t.down_since)
      : `${t.outages} outages · ${fmtDur(t.downtime_s)}`;
    tg.appendChild(el);
  });
  document.getElementById('targets').replaceChildren(tg);

  const ob = document.createDocumentFragment();
  if (!s.outages.length) ob.appendChild(row(['No internet outages yet.']));
  s.outages.forEach(o => {
    const tr = row([fmtTime(o.start), o.ongoing ? 'ongoing' : fmtTime(o.end), fmtDur(o.duration_s),
      o.ongoing ? 'in progress: ' + o.layer : o.layer, o.router], ['nw', 'nw', 'num nw', '', '']);
    if (o.ongoing) tr.className = 'ongoing';
    ob.appendChild(tr);
  });

  const rc = document.getElementById('router-card');
  rc.hidden = !d.router_status;
  if (d.router_status) {
    const r = d.router_status, pill = document.getElementById('router-pill');
    pill.textContent = r.error ? 'capture failed' : (r.ok ? 'OK' : 'PROBLEM');
    pill.className = 'pill ' + (r.error ? 'warn' : (r.ok ? 'ok' : 'bad'));
    document.getElementById('router-summary').textContent = r.error || r.summary || '';
    document.getElementById('router-meta').textContent = `Last capture ${fmtTime(r.time)} (${r.reason})`;
    const sb = document.createDocumentFragment();
    if (!r.sessions.length) sb.appendChild(row(['No connection uptime reported yet.']));
    r.sessions.forEach(x => sb.appendChild(row([fmtTime(x.start), 'last seen ' + fmtTime(x.last_seen)], ['nw', 'muted'])));
    document.getElementById('sessions').replaceChildren(sb);
  }
  document.getElementById('outages').replaceChildren(ob);

  const eb = document.createDocumentFragment();
  d.events.forEach(e => {
    const dur = e.duration_s ? (e.ongoing ? 'ongoing ' : '') + fmtDur(e.duration_s) : '';
    const tr = row([fmtTime(e.time), e.target, e.event, dur, e.detail], ['nw', 'nw', '', 'num nw', '']);
    if (e.ongoing) tr.className = 'ongoing';
    eb.appendChild(tr);
  });
  document.getElementById('events').replaceChildren(eb);

  drawChart(d.series);
}
refresh(); setInterval(refresh, 10000); addEventListener('resize', refresh);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    data_dir = '/var/lib/linemon'

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            self.send(200, 'text/html; charset=utf-8', PAGE.encode())
        elif path == '/api/status':
            try:
                body = json.dumps(status(self.data_dir)).encode()
                self.send(200, 'application/json', body)
            except FileNotFoundError:
                self.send(503, 'application/json', json.dumps({'error': 'no data yet'}).encode())
            except Exception:
                # The page has no login: exception text (paths, data) goes to the journal only.
                traceback.print_exc()
                self.send(500, 'application/json',
                          json.dumps({'error': 'status unavailable; see journalctl -u linemon-web'}).encode())
        else:
            self.send(404, 'text/plain', b'Not found')

    def send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the journal quiet


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--data', default='/var/lib/linemon')
    p.add_argument('--port', type=int, default=8080)
    args = p.parse_args()
    Handler.data_dir = args.data
    ThreadingHTTPServer(('0.0.0.0', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
