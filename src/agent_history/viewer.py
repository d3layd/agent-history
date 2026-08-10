"""Local web viewer — `agent-history serve`.

A single-file page over the same search used by the CLI. Standard library
only, and the server binds strictly to loopback: the index holds private
conversations (and routinely, credentials pasted into them), so it is never
exposed to the network. There is deliberately no flag to change the bind
address.
"""
from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import search, show, store

HOST = "127.0.0.1"
DEFAULT_PORT = 8127

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-history</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1d21; --muted: #6b7280; --line: #e5e7eb;
    --accent: #2563eb; --card: #f8fafc; --mark: #fde68a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101317; --fg: #e6e8ea; --muted: #8b939e; --line: #262b31;
      --accent: #60a5fa; --card: #171b20; --mark: #7c5e10;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, sans-serif;
  }
  main { max-width: 60rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
  h1 { font-size: 1.1rem; margin: 0 0 1rem; }
  h1 span { color: var(--muted); font-weight: normal; }
  form { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  input, select, button {
    font: inherit; color: inherit; background: var(--card);
    border: 1px solid var(--line); border-radius: 6px; padding: .45rem .6rem;
  }
  input#q { flex: 1 1 20rem; }
  button { cursor: pointer; border-color: var(--accent); color: var(--accent); }
  .hit {
    border: 1px solid var(--line); border-radius: 8px; background: var(--card);
    padding: .75rem .9rem; margin-bottom: .75rem;
  }
  .hit header {
    display: flex; gap: .75rem; flex-wrap: wrap; align-items: baseline;
    color: var(--muted); font-size: .8rem; margin-bottom: .35rem;
  }
  .hit header .agent { color: var(--accent); }
  .hit pre {
    margin: 0; white-space: pre-wrap; word-break: break-word;
    font: .85rem/1.45 ui-monospace, monospace; max-height: 12rem; overflow: auto;
  }
  .hit .origin {
    font-size: .75rem; color: var(--muted); margin-top: .4rem;
    word-break: break-all;
  }
  .hit a { color: var(--accent); cursor: pointer; text-decoration: underline; }
  #status { color: var(--muted); margin: 1rem 0; }
  dialog {
    width: min(56rem, 92vw); max-height: 85vh; border: 1px solid var(--line);
    border-radius: 10px; background: var(--bg); color: var(--fg); padding: 1rem;
  }
  dialog::backdrop { background: rgba(0,0,0,.45); }
  dialog .msg { margin-bottom: .9rem; }
  dialog .msg .who { color: var(--accent); font-size: .8rem; }
  dialog pre { white-space: pre-wrap; word-break: break-word;
    font: .85rem/1.45 ui-monospace, monospace; margin: .2rem 0 0; }
</style>
</head>
<body>
<main>
  <h1>agent-history <span id="stats"></span></h1>
  <form id="f">
    <input id="q" placeholder="what did we decide about ..." autofocus>
    <select id="mode">
      <option value="hybrid">hybrid</option>
      <option value="vector">vector</option>
      <option value="text">text</option>
    </select>
    <select id="sort">
      <option value="relevance">relevance</option>
      <option value="newest">newest</option>
    </select>
    <select id="agent"><option value="">all agents</option></select>
    <input id="cwd" placeholder="cwd filter" style="flex:0 1 10rem">
    <button>search</button>
  </form>
  <div id="status"></div>
  <div id="results"></div>
  <dialog id="dlg"><div id="dlgBody"></div></dialog>
</main>
<script>
const $ = id => document.getElementById(id);

fetch('/api/stats').then(r => r.json()).then(s => {
  $('stats').textContent = '— ' + s.total.toLocaleString() + ' chunks';
  for (const name of Object.keys(s.per_agent)) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name + ' (' + s.per_agent[name] + ')';
    $('agent').appendChild(o);
  }
});

$('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  const q = $('q').value.trim();
  if (!q) return;
  $('status').textContent = 'searching…';
  $('results').textContent = '';
  const params = new URLSearchParams({q, mode: $('mode').value,
                                      sort: $('sort').value, k: '20'});
  if ($('agent').value) params.set('agent', $('agent').value);
  if ($('cwd').value) params.set('cwd', $('cwd').value);
  try {
    const res = await fetch('/api/search?' + params);
    if (!res.ok) throw new Error(await res.text());
    render(await res.json());
  } catch (e) {
    $('status').textContent = 'error: ' + e.message;
  }
});

function render(hits) {
  $('status').textContent = hits.length ? hits.length + ' result(s)' : 'no matches';
  for (const h of hits) {
    const card = document.createElement('div');
    card.className = 'hit';
    const head = document.createElement('header');
    const rank = h.score != null ? 'score ' + h.score.toFixed(4)
               : h.distance != null ? 'dist ' + h.distance.toFixed(2) : 'text match';
    for (const part of [
        h.agent + '/' + h.role, (h.timestamp || '').slice(0, 16),
        h.cwd || '?', rank]) {
      const s = document.createElement('span');
      if (part === h.agent + '/' + h.role) s.className = 'agent';
      s.textContent = part;
      head.appendChild(s);
    }
    const pre = document.createElement('pre');
    pre.textContent = h.text;
    const origin = document.createElement('div');
    origin.className = 'origin';
    const link = document.createElement('a');
    link.textContent = 'open conversation';
    link.addEventListener('click', () => openSession(h));
    origin.append(h.file + ' — ');
    origin.appendChild(link);
    card.append(head, pre, origin);
    $('results').appendChild(card);
  }
}

async function openSession(h) {
  const params = new URLSearchParams({limit: '60'});
  if (h.session_id) params.set('session', h.session_id);
  else params.set('origin', h.file);
  if (h.timestamp) params.set('around', h.timestamp);
  const res = await fetch('/api/session?' + params);
  const rows = await res.json();
  const body = $('dlgBody');
  body.textContent = '';
  for (const r of rows) {
    const div = document.createElement('div');
    div.className = 'msg';
    const who = document.createElement('div');
    who.className = 'who';
    who.textContent = '[' + (r.timestamp || '').slice(0, 19) + '] ' + r.role;
    const pre = document.createElement('pre');
    pre.textContent = r.text;
    div.append(who, pre);
    body.appendChild(div);
  }
  $('dlg').showModal();
}
$('dlg').addEventListener('click', ev => {
  if (ev.target === $('dlg')) $('dlg').close();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        pass

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/stats":
                # Stats never embed, so a model mismatch must not break the
                # page's initial load.
                self._json(store.stats(store.open_for_read(check_model=False)))
            elif parsed.path == "/api/search":
                self._json(search.search_rows(
                    params.get("q", ""),
                    k=min(int(params.get("k", "20")), 100),
                    cwd=params.get("cwd") or None,
                    agent=params.get("agent") or None,
                    mode=params.get("mode", "hybrid"),
                    sort=params.get("sort", "relevance"),
                ))
            elif parsed.path == "/api/session":
                target = params.get("session") or params.get("origin") or ""
                self._json(show.rows_for(
                    target,
                    around=params.get("around") or None,
                    limit=max(1, min(int(params.get("limit", "60")), 500)),
                ))
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:  # noqa: BLE001 - surface, don't kill the server
            self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, payload) -> None:
        self._send(200, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # No CORS header at all: the bundled page is same-origin, and
        # `Access-Control-Allow-Origin: null` would grant opaque origins
        # (sandboxed iframes, file:// pages) read access to the index.
        self.end_headers()
        self.wfile.write(body)


def run(port: int = DEFAULT_PORT) -> int:
    store.open_for_read(check_model=False)  # fail before binding if no index
    server = ThreadingHTTPServer((HOST, port), Handler)
    # Report the bound port, which differs from the request when port=0.
    print(f"agent-history viewer: http://{HOST}:{server.server_address[1]}  "
          "(Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
