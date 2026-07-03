"""Tiny stdlib web server for the collector container.

  /            -> today's all-gliders 3D replay (generated from the live DB, cached)
  /stats,/status -> health + live capture statistics (auto-refreshing)
  /models/*.glb  -> aircraft models referenced by the replay page

Runs in a thread alongside the `watch` collector, sharing a status dict.
"""
import http.server
import os
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from .config import GRANSDEN
from .flights import segment
from .store import Store, year_file

REPLAY_TTL = 60  # seconds to cache the generated replay page
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _render_replay(day: datetime, replay_script: str) -> str | None:
    key = day.strftime("%Y-%m-%d")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[1] < REPLAY_TTL:
            return hit[0]
    tmp = tempfile.mktemp(suffix=".html")
    try:
        subprocess.run(
            ["python3", replay_script, "--out", tmp, "--day", key,
             "--title", f"All gliders {key}", "--gliders", "--mult", "60"],
            check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError:
        return None            # no flights yet today
    except Exception:
        return None
    with open(tmp) as fh:
        html = fh.read()
    os.remove(tmp)
    with _cache_lock:
        _cache[key] = (html, time.time())
    return html


def _stats(status: dict, data_dir: str) -> dict:
    day = _today()
    lo = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    hi = lo + 86400
    yf = year_file(day.year, data_dir)
    out = {"day": day.strftime("%Y-%m-%d"), "fixes_today": 0, "aircraft_today": 0,
           "flights_today": 0, "db_bytes": 0}
    if os.path.exists(yf):
        out["db_bytes"] = os.path.getsize(yf)
        s = Store(yf)
        try:
            out["fixes_today"] = s.db.execute(
                "SELECT COUNT(*) FROM fixes WHERE ts>=? AND ts<?", (lo, hi)).fetchone()[0]
            addrs = [r[0] for r in s.db.execute(
                "SELECT DISTINCT address FROM fixes WHERE ts>=? AND ts<?", (lo, hi)).fetchall()]
            out["aircraft_today"] = len(addrs)
            out["flights_today"] = sum(
                len(segment(a, s.fixes_for(a, lo, hi), GRANSDEN)) for a in addrs)
        finally:
            s.close()
    now = time.time()
    out["following"] = status.get("following", 0)
    out["stored_session"] = status.get("stored", 0)
    out["connected"] = status.get("connected", False)
    out["uptime_s"] = int(now - status["started"]) if status.get("started") else 0
    lb = status.get("last_beacon")
    out["last_beacon_age_s"] = int(now - lb) if lb else None
    # healthy = connected and heard a beacon within the last 5 min
    out["healthy"] = bool(out["connected"] and lb and (now - lb) < 300)
    return out


def _fmt_dur(s: int) -> str:
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m {s}s"


def _stats_html(st: dict) -> str:
    ok = st["healthy"]
    dot = "#2ecc71" if ok else "#e74c3c"
    age = "never" if st["last_beacon_age_s"] is None else f"{st['last_beacon_age_s']}s ago"
    rows = [
        ("Status", ("HEALTHY" if ok else "CHECK") + (" (connected)" if st["connected"] else " (disconnected)")),
        ("Uptime", _fmt_dur(st["uptime_s"])),
        ("Last beacon", age),
        ("Following now", st["following"]),
        ("Fixes today", f"{st['fixes_today']:,}"),
        ("Aircraft today", st["aircraft_today"]),
        ("Flights today", st["flights_today"]),
        ("Stored this session", f"{st['stored_session']:,}"),
        ("DB size", f"{st['db_bytes']/1_048_576:.1f} MB"),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><title>ognflights status</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;max-width:520px;margin:2.5rem auto;padding:0 1rem;color:#222}}
h1{{font-size:1.3rem}} .dot{{display:inline-block;width:12px;height:12px;border-radius:50%;background:{dot};vertical-align:middle;margin-right:8px}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}} th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
th{{color:#666;font-weight:600;width:45%}} a{{color:#1e6fd0}} .hint{{color:#999;font-size:.85rem}}</style></head>
<body><h1><span class="dot"></span>ognflights collector, {st['day']}</h1>
<table>{body}</table>
<p><a href="/">all-gliders replay &rarr;</a></p>
<p class="hint">auto-refreshes every 10s. "Following now" = aircraft launched from the field being tracked live.</p>
</body></html>"""


def make_handler(status, data_dir, replay_script, models_dir):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/stats", "/status"):
                self._send(200, _stats_html(_stats(status, data_dir)))
            elif path.startswith("/models/"):
                fn = os.path.basename(path)
                fp = os.path.join(models_dir, fn)
                if fn.endswith(".glb") and os.path.exists(fp):
                    with open(fp, "rb") as fh:
                        self._send(200, fh.read(), "model/gltf-binary")
                else:
                    self._send(404, "not found")
            elif path == "/":
                html = _render_replay(_today(), replay_script)
                if html is None:
                    self._send(200, "<!DOCTYPE html><meta charset=utf-8>"
                               "<body style='font:15px system-ui;margin:3rem auto;max-width:32rem'>"
                               "<h1>No flights captured yet today.</h1>"
                               "<p>The collector is running; this page will show today's flights once "
                               "an aircraft launches from the field. "
                               "<a href='/stats'>See status &rarr;</a></p>")
                else:
                    self._send(200, html)
            else:
                self._send(404, "not found")

        do_HEAD = do_GET

    return Handler


def serve(port, status, data_dir, replay_script, models_dir):
    httpd = socketserver.ThreadingTCPServer(("", port), make_handler(status, data_dir, replay_script, models_dir))
    httpd.daemon_threads = True
    httpd.serve_forever()
