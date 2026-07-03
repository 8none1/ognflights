"""Tiny stdlib web server for the collector container.

  /            -> today's all-gliders 3D replay (generated from the live DB, cached)
  /stats,/status -> health + live capture statistics (auto-refreshing)
  /models/*.glb  -> aircraft models referenced by the replay page

Runs in a thread alongside the `watch` collector, sharing a status dict.
"""
import glob
import http.server
import os
import re
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .config import GRANSDEN
from .flights import segment
from .store import Store, year_file

REPLAY_TTL = 60  # seconds to cache the generated replay page
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _parse_day(query: str) -> datetime:
    """?day=YYYY-MM-DD from the query string, defaulting to today (UTC). Validated."""
    vals = parse_qs(query).get("day", [])
    if vals and _DAY_RE.match(vals[0]):
        try:
            return datetime.strptime(vals[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _today()


def _days_with_flights(data_dir: str, limit: int = 21) -> list[str]:
    """Recent days (YYYY-MM-DD) that have stored fixes, newest first, across year files."""
    days: set[str] = set()
    for yf in glob.glob(os.path.join(data_dir, "ogn-*.sqlite")):
        try:
            s = Store(yf)
            days.update(r[0] for r in s.db.execute(
                "SELECT DISTINCT strftime('%Y-%m-%d', ts, 'unixepoch') FROM fixes"))
            s.close()
        except Exception:
            pass
    return sorted(days, reverse=True)[:limit]


def _nav_html(day: datetime) -> str:
    """A small fixed date-picker (prev / date / next) injected into the replay page."""
    d = day.strftime("%Y-%m-%d")
    prev = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    nxt = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    return (
        '<div style="position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:20;'
        'background:rgba(0,0,0,.6);color:#fff;padding:5px 9px;border-radius:6px;font:13px sans-serif">'
        f'<a href="/?day={prev}" style="color:#8cf;text-decoration:none">&#9664;</a> '
        f'<input type="date" value="{d}" onchange="location=\'/?day=\'+this.value" '
        'style="font:13px sans-serif;background:#222;color:#fff;border:1px solid #555;border-radius:3px"> '
        f'<a href="/?day={nxt}" style="color:#8cf;text-decoration:none">&#9654;</a>'
        ' <a href="/stats" style="color:#8cf;margin-left:8px">stats</a></div>')


def _aircraft_count(day: datetime, data_dir: str) -> int:
    lo = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    yf = year_file(day.year, data_dir)
    if not os.path.exists(yf):
        return 0
    s = Store(yf)
    try:
        return s.db.execute("SELECT COUNT(DISTINCT address) FROM fixes WHERE ts>=? AND ts<?",
                            (lo, lo + 86400)).fetchone()[0]
    finally:
        s.close()


def _render_replay(day: datetime, replay_script: str, data_dir: str) -> str | None:
    key = day.strftime("%Y-%m-%d")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[1] < REPLAY_TTL:
            return hit[0]
    # Simplify trails harder when more gliders are on screen (keeps it responsive).
    # A handful of aircraft render full-fidelity; a busy day is thinned (turns preserved).
    n_ac = _aircraft_count(day, data_dir)
    simplify = max(0, min(60, (n_ac - 4) * 4))
    tmp = tempfile.mktemp(suffix=".html")
    cmd = ["python3", replay_script, "--out", tmp, "--day", key,
           "--title", f"All gliders {key}", "--gliders", "--mult", "60"]
    if simplify:
        cmd += ["--simplify", str(simplify)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
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
    out["days"] = _days_with_flights(data_dir)
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
    days = st.get("days", [])
    daylinks = ("".join(f'<li><a href="/?day={d}">{d}</a></li>' for d in days)
                if days else "<li class='hint'>none captured yet</li>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><title>ognflights status</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;max-width:520px;margin:2.5rem auto;padding:0 1rem;color:#222}}
h1{{font-size:1.3rem}} .dot{{display:inline-block;width:12px;height:12px;border-radius:50%;background:{dot};vertical-align:middle;margin-right:8px}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}} th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
th{{color:#666;font-weight:600;width:45%}} a{{color:#1e6fd0}} .hint{{color:#999;font-size:.85rem}} ul{{columns:2;padding-left:1.1rem}}</style></head>
<body><h1><span class="dot"></span>ognflights collector, {st['day']}</h1>
<table>{body}</table>
<p><a href="/">today's all-gliders replay &rarr;</a></p>
<h2 style="font-size:1rem">Days with flights</h2>
<ul>{daylinks}</ul>
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
                day = _parse_day(urlparse(self.path).query)
                nav = _nav_html(day)
                html = _render_replay(day, replay_script, data_dir)
                if html is None:
                    label = "today" if day.date() == _today().date() else day.strftime("%Y-%m-%d")
                    self._send(200, "<!DOCTYPE html><meta charset=utf-8>"
                               "<body style='font:15px system-ui;margin:0;color:#eee;background:#111'>"
                               + nav +
                               "<div style='margin:6rem auto;max-width:32rem;text-align:center'>"
                               f"<h1>No flights stored for {label}.</h1>"
                               "<p>Pick another day above, or <a style='color:#8cf' href='/stats'>see status &rarr;</a></p>"
                               "</div></body>")
                else:
                    self._send(200, html.replace("</body>", nav + "</body>", 1))
            else:
                self._send(404, "not found")

        do_HEAD = do_GET

    return Handler


def serve(port, status, data_dir, replay_script, models_dir):
    httpd = socketserver.ThreadingTCPServer(("", port), make_handler(status, data_dir, replay_script, models_dir))
    httpd.daemon_threads = True
    httpd.serve_forever()
