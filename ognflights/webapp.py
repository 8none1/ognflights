"""Tiny stdlib web server for the collector container.

  /            -> today's all-gliders 3D replay (generated from the live DB, cached)
  /live        -> real-time 3D view of currently-airborne aircraft (polls /live.json)
  /live.json   -> JSON feed of aircraft active in the last ~2 min (recent tracks + latest pos)
  /stats,/status -> health + live capture statistics (auto-refreshing)
  /models/*.glb  -> aircraft models referenced by the replay page

Runs in a thread alongside the `watch` collector, sharing a status dict.
"""
import glob
import http.server
import json
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

FT_TO_M = 0.3048
LIVE_WINDOW = 300     # seconds of recent track to include per aircraft
LIVE_ACTIVE = 120     # an aircraft is "airborne now" if its last fix is within this
# Colour palette + model glb files, kept in sync with replay/make_replay.py.
PALETTE = ["#1e90ff", "#32cd32", "#ff4500", "#ff00ff", "#00ffff", "#ffd700", "#ff1493",
           "#7cfc00", "#ff8c00", "#9370db", "#00fa9a", "#dc143c", "#40e0d0", "#ffa07a"]
DR400_MATCH = ("dr-400", "dr400", "dr 400", "robin")
MODEL_FILES = {"glider": "AS21.glb", "dr400": "DR40.glb"}


def _live_model(model_str: str, ac_type: str) -> str:
    """glider vs dr400 (tug), replicating make_replay.classify_model's simple rule."""
    s = (model_str or "").lower()
    if any(m in s for m in DR400_MATCH):
        return "dr400"
    if ac_type == "tow":
        return "dr400"
    return "glider"


def _live_feed(data_dir: str) -> dict:
    """Aircraft active in the last LIVE_ACTIVE seconds, each with its recent track."""
    now = int(time.time())
    day = _today()
    out = {"now": now, "aircraft": []}
    yf = year_file(day.year, data_dir)
    if not os.path.exists(yf):
        return out
    s = Store(yf)
    try:
        rows = s.db.execute(
            """SELECT address, ts, lat, lon, alt_ft FROM fixes
               WHERE ts >= ? ORDER BY address, ts""",
            (now - LIVE_WINDOW,),
        ).fetchall()
        by_addr: dict[str, list] = {}
        for addr, ts, lat, lon, alt in rows:
            by_addr.setdefault(addr, []).append((ts, lat, lon, alt))
        idx = 0
        for addr, fixes in by_addr.items():
            if not fixes or (now - fixes[-1][0]) > LIVE_ACTIVE:
                continue
            label, model_str = s.device_label(addr)
            row = s.db.execute(
                "SELECT aircraft_type FROM devices WHERE address=?", (addr,)
            ).fetchone()
            ac_type = row[0] if row else ""
            mk = _live_model(model_str, ac_type)
            pts = [[round(lon, 6), round(lat, 6),
                    round(max(0.0, (alt - GRANSDEN.elevation_ft) * FT_TO_M), 1)]
                   for (_ts, lat, lon, alt) in fixes]
            out["aircraft"].append({
                "address": addr,
                "name": label,
                "label": label,
                "color": PALETTE[idx % len(PALETTE)],
                "model": mk,
                "points": pts,
                "last_ts": fixes[-1][0],
            })
            idx += 1
    finally:
        s.close()
    return out


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


def _nav_html(day: datetime, address: str | None = None) -> str:
    """Fixed date-picker (prev / date / next) injected into the replay page. In single-aircraft
    view it keeps the aircraft across dates and offers a link back to all gliders."""
    d = day.strftime("%Y-%m-%d")
    prev = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    nxt = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"&address={address}" if address else ""
    extra = (f'<a href="/?day={d}" style="color:#8cf;margin-left:8px">all gliders</a>'
             if address else '<a href="/stats" style="color:#8cf;margin-left:8px">stats</a>')
    return (
        '<div style="position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:20;'
        'background:rgba(0,0,0,.6);color:#fff;padding:5px 9px;border-radius:6px;font:13px sans-serif">'
        f'<a href="/?day={prev}{q}" style="color:#8cf;text-decoration:none">&#9664;</a> '
        f'<input type="date" value="{d}" onchange="location=\'/?day=\'+this.value+\'{q}\'" '
        'style="font:13px sans-serif;background:#222;color:#fff;border:1px solid #555;border-radius:3px"> '
        f'<a href="/?day={nxt}{q}" style="color:#8cf;text-decoration:none">&#9654;</a>'
        f' {extra}</div>')


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


def _render_replay(day: datetime, replay_script: str, data_dir: str, address: str | None = None) -> str | None:
    daystr = day.strftime("%Y-%m-%d")
    key = daystr + ("|" + address if address else "")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[1] < REPLAY_TTL:
            return hit[0]
    tmp = tempfile.mktemp(suffix=".html")
    if address:
        # single-aircraft: full fidelity (no simplify, fine comet-tail), per-flight.
        cmd = ["python3", replay_script, "--out", tmp, "--day", daystr,
               "--title", f"{address} {daystr}", "--address", address, "--mult", "30", "--trail", "full"]
    else:
        # all gliders: simplify + coarser tails scaled to aircraft count; link each to its single view.
        n_ac = _aircraft_count(day, data_dir)
        simplify = max(0, min(60, (n_ac - 4) * 4))
        path_res = min(15, max(1, n_ac // 3))
        cmd = ["python3", replay_script, "--out", tmp, "--day", daystr,
               "--title", f"All gliders {daystr}", "--gliders", "--mult", "60",
               "--link-single", "--path-resolution", str(path_res)]
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


# Camera over Gransden, looking north-ish, oblique. lon/lat/height metres.
LIVE_CES = "https://cesium.com/downloads/cesiumjs/releases/1.143/Build/Cesium"
LIVE_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Live - Gransden</title>
<script src="__CES__/Cesium.js"></script>
<link href="__CES__/Widgets/widgets.css" rel="stylesheet">
<style>html,body,#c{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000}
#legend{position:absolute;top:8px;left:8px;z-index:10;background:rgba(0,0,0,.6);color:#fff;
font:12px sans-serif;padding:8px 10px;border-radius:6px;max-height:90vh;overflow:auto;min-width:150px}
#legend b{font-size:14px}
.sw{display:inline-block;width:12px;height:12px;margin-right:6px;border-radius:2px;vertical-align:middle}
.hint{opacity:.6;font-size:11px}</style>
</head><body><div id="c"></div><div id="legend"><b>Live - Gransden</b><br><span class="hint">connecting...</span></div>
<script>
const MODELS=__MODELS__;      // {glider:"models/AS21.glb", dr400:"models/DR40.glb"}
Cesium.Ion.defaultAccessToken="";
const viewer=new Cesium.Viewer("c",{
  baseLayer:Cesium.ImageryLayer.fromProviderAsync(Cesium.ArcGisMapServerImageryProvider.fromUrl(
    "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer")),
  baseLayerPicker:false,geocoder:false,homeButton:false,navigationHelpButton:false,
  infoBox:false,selectionIndicator:false,animation:false,timeline:false});
viewer.scene.globe.enableLighting=true;
// night-sky style: drop the bright atmosphere + ground haze, black background
viewer.scene.skyAtmosphere.show=false;
viewer.scene.globe.showGroundAtmosphere=false;
viewer.scene.backgroundColor=Cesium.Color.BLACK;

// opening camera over Gransden, oblique looking north
viewer.camera.setView({
  destination:Cesium.Cartesian3.fromDegrees(-0.111, 52.10, 9000),
  orientation:{heading:Cesium.Math.toRadians(0),pitch:Cesium.Math.toRadians(-35),roll:0}
});

const POLL_MS=6000;
const GRACE_MS=20000;    // remove an aircraft this long after it drops from the feed
const ac={};             // address -> {plane, trail, color, name, model, lastSeen}

function upsert(a){
  const pts=a.points||[];
  if(!pts.length) return;
  const last=pts[pts.length-1];
  const pos=Cesium.Cartesian3.fromDegrees(last[0],last[1],last[2]);
  const col=Cesium.Color.fromCssColorString(a.color);
  let e=ac[a.address];
  if(!e){
    e=ac[a.address]={color:a.color,name:a.name,model:a.model};
    e.plane=viewer.entities.add({
      name:a.name,
      position:new Cesium.CallbackProperty(()=>e._pos,false),
      model:{uri:MODELS[a.model]||MODELS.glider, minimumPixelSize:64, maximumScale:20000, scale:1,
        color:col, colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.5,
        silhouetteColor:col, silhouetteSize:1.5}
    });
    e.trail=viewer.entities.add({
      name:a.name+" trail",
      polyline:{positions:new Cesium.CallbackProperty(()=>e._trail,false),
        width:2, material:col.withAlpha(0.55)}
    });
  }
  e._pos=pos;
  e._trail=Cesium.Cartesian3.fromDegreesArrayHeights([].concat(...pts));
  e.name=a.name; e.plane.name=a.name; e.trail.name=a.name+" trail";
  e.lastSeen=Date.now();
}

function prune(){
  const t=Date.now();
  for(const addr of Object.keys(ac)){
    if(t-ac[addr].lastSeen>GRACE_MS){
      viewer.entities.remove(ac[addr].plane);
      viewer.entities.remove(ac[addr].trail);
      delete ac[addr];
    }
  }
}

function renderLegend(){
  const items=Object.values(ac);
  const rows=items.map(e=>`<div><span class="sw" style="background:${e.color}"></span>${e.name}</div>`);
  const n=items.length;
  const head=`<b>Live - Gransden</b><br><span class="hint">${n} aircraft airborne</span>`;
  document.getElementById("legend").innerHTML=head+(rows.length?"<br>"+rows.join(""):"");
}

async function poll(){
  try{
    const r=await fetch("live.json",{cache:"no-store"});
    const d=await r.json();
    (d.aircraft||[]).forEach(upsert);
    prune();
    renderLegend();
  }catch(e){ /* transient; try again next tick */ }
  setTimeout(poll, POLL_MS);
}
poll();

// hover tooltip: aircraft name under the cursor
const _tip=document.createElement("div");
_tip.style.cssText="position:fixed;z-index:30;pointer-events:none;display:none;background:rgba(0,0,0,.8);"
  +"color:#fff;font:12px sans-serif;padding:2px 7px;border-radius:4px;white-space:nowrap";
document.body.appendChild(_tip);
new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas).setInputAction(function(mv){
  const p=viewer.scene.pick(mv.endPosition);
  const name=p&&p.id&&p.id.name;
  if(name){
    _tip.textContent=name.replace(/ trail$/,"");
    _tip.style.left=(mv.endPosition.x+14)+"px"; _tip.style.top=(mv.endPosition.y+10)+"px";
    _tip.style.display="block";
  } else { _tip.style.display="none"; }
}, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
</script></body></html>"""


def _live_page() -> str:
    models = {k: f"models/{v}" for k, v in MODEL_FILES.items()}
    return LIVE_HTML.replace("__CES__", LIVE_CES).replace("__MODELS__", json.dumps(models))


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
            elif path == "/live.json":
                self._send(200, json.dumps(_live_feed(data_dir)),
                           "application/json; charset=utf-8")
            elif path == "/live":
                self._send(200, _live_page())
            elif path.startswith("/models/"):
                fn = os.path.basename(path)
                fp = os.path.join(models_dir, fn)
                if fn.endswith(".glb") and os.path.exists(fp):
                    with open(fp, "rb") as fh:
                        self._send(200, fh.read(), "model/gltf-binary")
                else:
                    self._send(404, "not found")
            elif path == "/":
                q = urlparse(self.path).query
                day = _parse_day(q)
                addr = parse_qs(q).get("address", [None])[0]
                if addr and not re.match(r"^[A-Za-z0-9._-]+$", addr):
                    addr = None
                nav = _nav_html(day, addr)
                html = _render_replay(day, replay_script, data_dir, addr)
                if html is None:
                    label = "today" if day.date() == _today().date() else day.strftime("%Y-%m-%d")
                    what = f"{addr} on {label}" if addr else label
                    self._send(200, "<!DOCTYPE html><meta charset=utf-8>"
                               "<body style='font:15px system-ui;margin:0;color:#eee;background:#111'>"
                               + nav +
                               "<div style='margin:6rem auto;max-width:32rem;text-align:center'>"
                               f"<h1>No flights stored for {what}.</h1>"
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
