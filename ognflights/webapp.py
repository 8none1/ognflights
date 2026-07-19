"""Tiny stdlib web server for the collector container.

  /            -> landing page linking to Live / Daily replay / Stats
  /my-flights  -> public "Watch your flight" finder (date + rough time -> replay links)
  /replay      -> all-gliders 3D replay for a day (?day=, ?address= for single aircraft,
                  ?t= to keep only the flight airborne at that moment)
  /download    -> one flight as a file: ?day=&address=&t=&fmt=kml|gpx|igc (same day/
                  address/t identifiers as the replay links; KML opens in Google Earth)
  /branding/*  -> club logo etc, served from <data_dir>/branding/ (drop-in, no rebuild)
  /live        -> real-time 3D view of currently-airborne aircraft (SSE-driven)
  /live.json   -> JSON feed of aircraft active in the last ~2 min (initial snapshot)
  /live.stream -> Server-Sent Events: one fix per followed aircraft as it arrives
  /stats,/status -> health + live capture statistics (auto-refreshing)
  /models/*.glb  -> aircraft models referenced by the replay page

Runs in a thread alongside the `watch` collector, sharing a status dict + a live hub.
"""
import colorsys
import glob
import http.server
import json
import os
import queue
import re
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from . import export
from .config import GRANSDEN
from .flights import segment
from .store import Store, year_file
from .theme import (CES, MAP_HELP_BTN, MAP_HELP_HTML, MAP_HELP_JS, THEME_CSS,
                    THERMALS_JS, header_html, nav_html)

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


def live_color(address: str) -> str:
    """A distinct, vivid colour per aircraft, deterministic on its address so the initial
    /live.json snapshot and the /live.stream events always agree. Uses a hue derived from
    the address hash (360 hues) rather than a small fixed palette, so different aircraft
    (e.g. two flying together) don't collide onto the same colour."""
    h = 0
    for ch in address:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.6, 0.75)  # bright, saturated
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def live_height_m(alt_ft: float) -> float:
    """Height above the airfield in metres, floored at 0 (matches the replay)."""
    return round(max(0.0, (alt_ft - GRANSDEN.elevation_ft) * FT_TO_M), 1)


class LiveHub:
    """In-process pub/sub between the collector and the webapp.

    Each subscriber gets its own bounded queue. publish() is non-blocking: on a
    full queue the event is dropped for that subscriber, so a slow or dead browser
    can never stall the collector loop.
    """
    def __init__(self, maxsize: int = 200):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # drop for this slow subscriber; never block the collector


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
        for addr, fixes in by_addr.items():
            if not fixes or (now - fixes[-1][0]) > LIVE_ACTIVE:
                continue
            label, model_str = s.device_label(addr)
            row = s.db.execute(
                "SELECT aircraft_type FROM devices WHERE address=?", (addr,)
            ).fetchone()
            ac_type = row[0] if row else ""
            mk = _live_model(model_str, ac_type)
            pts = [[round(lon, 6), round(lat, 6), live_height_m(alt), int(_ts)]
                   for (_ts, lat, lon, alt) in fixes]
            out["aircraft"].append({
                "address": addr,
                "name": label,
                "label": label,
                "color": live_color(addr),
                "model": mk,
                "points": pts,
                "last_ts": fixes[-1][0],
            })
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


# --- /download: give one flight away as a file (Google Earth KML by default) -----------
# The flight identifier mirrors the replay links: day + device address + ?t= (the moment
# the flight was airborne, epoch seconds or HH:MM UTC), so the same day/address/t that
# builds a "/replay?..." link also builds a working "/download?..." link.
_T_RE = re.compile(r"^(\d{9,}|\d{1,2}:\d{2})$")
_ADDR_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DL_CTYPES = {"kml": "application/vnd.google-earth.kml+xml",
             "gpx": "application/gpx+xml",
             "igc": "text/plain; charset=utf-8"}


def _parse_t(tval: str | None, day: datetime) -> int | None:
    """?t= as epoch seconds, from '<epoch>' or 'HH:MM' (UTC, on `day`). None if absent."""
    if not tval:
        return None
    if re.match(r"^\d{9,}$", tval):
        return int(tval)
    m = re.match(r"^(\d{1,2}):(\d{2})$", tval)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(day.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=0, microsecond=0).timestamp())
    raise ValueError(tval)


def _select_flight(flights: list, t: int | None):
    """Pick the flight `t` refers to, mirroring the replay page's filterTime(): the flight
    airborne at that moment, else the nearest take-off within 15 minutes. Without a t the
    choice is only well-defined when the aircraft flew exactly once that day."""
    if t is None:
        return flights[0] if len(flights) == 1 else None
    for fl in flights:
        if fl.start <= t <= fl.end:
            return fl
    best, bd = None, 15 * 60 + 1
    for fl in flights:
        d = abs(fl.start - t)
        if d < bd:
            bd, best = d, fl
    return best


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


def _nav_html(day: datetime, address: str | None = None, logo: str = "",
              t: str | None = None, help_btn: bool = True) -> str:
    """Fixed date-picker (prev / date / next) + shared nav links, injected into the replay
    page (which carries THEME_CSS, so the .of-topbar classes resolve). In single-aircraft
    view it keeps the aircraft across dates, offers a link back to all gliders, and adds a
    "Google Earth" KML download for the flight (`t` = the replay's ?t= flight selector).
    Server-side only: the static public replay never gets this bar, so it never gets a
    download link its host cannot serve."""
    d = day.strftime("%Y-%m-%d")
    prev = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    nxt = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"&address={address}" if address else ""
    logo_html = f'<img class="nlogo" src="{logo}" alt="">' if logo else ""
    if address:
        dl_href = f"/download?day={d}&address={address}&fmt=kml" + (f"&t={t}" if t else "")
        extra = (f'<a href="/replay?day={d}">all gliders</a> <a href="/">home</a>'
                 f' <a class="of-btn-secondary" href="{dl_href}"'
                 ' title="Download this flight as KML for Google Earth">Google Earth &#8595;</a>')
    else:
        extra = ('<a href="/">home</a> <a href="/live">live</a>'
                 ' <a href="/my-flights">my&nbsp;flights</a> <a href="/stats">stats</a>')
    if help_btn:
        # reopen control for the map-controls help overlay (wired by delegation in
        # MAP_HELP_JS, so it works even though this bar is injected after the script).
        extra += " " + MAP_HELP_BTN
    return (
        f'<div class="of-topbar">{logo_html}'
        f'<a href="/replay?day={prev}{q}">&#9664;</a>'
        f'<input type="date" value="{d}" onchange="location=\'/replay?day=\'+this.value+\'{q}\'">'
        f'<a href="/replay?day={nxt}{q}">&#9654;</a>'
        f'{extra}</div>')


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
# The page paints an initial /live.json snapshot then follows /live.stream (SSE).
LIVE_CES = "https://cesium.com/downloads/cesiumjs/releases/1.143/Build/Cesium"
LIVE_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Live - Gransden</title>
<script src="__CES__/Cesium.js"></script>
<link href="__CES__/Widgets/widgets.css" rel="stylesheet">
<style>__THEMECSS__
html,body,#c{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000}
#legend{position:absolute;top:10px;left:10px;z-index:10;color:var(--text);
font:12px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:9px 11px;
max-height:88vh;overflow:auto;min-width:160px}
@media(max-width:640px){#legend{top:72px;max-height:70vh}
.cesium-viewer-toolbar{display:none}}
#legend b{font-size:14px}
#legend a{color:var(--blue)}
.sw{display:inline-block;width:12px;height:12px;margin-right:6px;border-radius:3px;vertical-align:middle}
.hint{color:var(--dim);font-size:11px}
#legend label{cursor:pointer}</style>
</head><body>
<div class="of-topbar">__NAVLOGO__<b>Live</b>
<a href="/">home</a>
<a href="/replay">replay</a>
<a href="/my-flights">my&nbsp;flights</a>
<a href="/stats">stats</a> __HELPBTN__</div>
<div id="c"></div><div id="legend" class="of-panel"><div id="legdyn"><b>Live - Gransden</b><br><span class="hint">connecting...</span></div></div>
__HELPHTML__
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
// transparent place-names / boundaries overlay (toggled off by default), mirroring the replay.
// NB: imageryLayers.add() returns void, so keep the layer ref from fromProviderAsync.
const labelLayer=Cesium.ImageryLayer.fromProviderAsync(
  Cesium.ArcGisMapServerImageryProvider.fromUrl(
    "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer"));
viewer.imageryLayers.add(labelLayer);
labelLayer.show=false;

// opening camera over Gransden, oblique looking north
const HOME={lon:-0.111,lat:52.10,height:9000,heading:0,pitch:-35,roll:0};
function goHome(){
  viewer.camera.setView({
    destination:Cesium.Cartesian3.fromDegrees(HOME.lon,HOME.lat,HOME.height),
    orientation:{heading:Cesium.Math.toRadians(HOME.heading),
                 pitch:Cesium.Math.toRadians(HOME.pitch),roll:Cesium.Math.toRadians(HOME.roll)}
  });
}
goHome();

const GRACE_MS=60000;    // remove an aircraft this long after its last event
let maxTrail=600;        // bounded recent-points trail per aircraft (tuned by the settings slider)
const ORIENT_MIN_M=30;   // walk back through the trail until at least this far behind
const ORIENT_STATIONARY_M=10; // below this displacement, keep the last-good heading (no spin)
// Pitch: OGN altitude is noisy, so a single short baseline gives a wildly exaggerated
// angle. Fit the flight-path angle over a LONG window of many points so the nose matches
// the real track slope, then smooth and clamp. Tune these if it looks too lively/sluggish.
const PITCH_WINDOW_S=30;  // seconds of track for the least-squares climb-rate fit (this IS the smoothing)
const PITCH_MAX_DEG=45;   // safety clamp (real steep climbs/descents still show)
const M_TO_FT=1/0.3048, MS_TO_KT=1/0.514444;  // metres->feet, vertical m/s -> knots
const FIELD_ELEV_FT=__FIELDELEV__;  // field elevation (ft AMSL); readout shows true altitude AMSL
const VARIO_WIN_S=18;     // vario smoothing window (s): least-squares slope of height vs time
// Despike: some aircraft (notably ADS-B tugs) occasionally report a single position
// ~150-300 m off that snaps back on the next fix ("out-and-back" spike). It draws a stray
// spur on the trail, flips the nose for a frame and jumps the model. We drop such isolated
// interior points at the DISPLAY layer only (raw data is untouched). A point is a spike when
// it juts far from BOTH neighbours while the neighbours themselves are close together.
const SPIKE_MIN_M=80;    // both neighbour hops must exceed this for a point to count as a spike
const SPIKE_RATIO=2.5;   // ...and the out-and-back detour must be this much longer than the direct hop
const ac={};             // address -> {plane, trail, color, name, model, pts[], lastSeen, _ori}
// Parked/ground overlay: aircraft sitting at the field, from ground events (ev.g). A separate
// entity set keyed by address, so a parked glider is never confused with an airborne one. These
// are LIVE-ONLY (never stored/segmented server-side) - the map just mirrors the beacon stream.
const parked={};         // address -> {plane, label, color, cs, lastSeen}
const PARKED_GRACE_MS=300000; // parked gliders beacon slowly: keep a marker 5 min after its last ground event
let parkedOn=true;       // toggled by the "parked aircraft" checkbox in the settings panel
function parkedPos(lon,lat){ return Cesium.Cartesian3.fromDegrees(lon,lat,0); } // sit ON the ground
let trailsOn=true;       // toggled by the "Trail" checkbox in the legend
// URL-controllable comet trail (for the demo/kiosk big screen). Works on plain /live and /live?demo=1.
//   ?trail=comet|full   full (default) = the whole bounded point-count trail (current behaviour)
//   ?trailsecs=N        comet tail length in seconds (default 60, clamped >=5)
// Comet mode only FILTERS which retained points are DRAWN (by time); it never changes storage
// (e.pts stays bounded by maxTrail) nor the model position/heading/vario/despike.
const _tp=new URLSearchParams(location.search);
const DEMO=_tp.has("demo");   // demo/kiosk mode: comet trail, day sky and shallow pitch by default
window.OF_HELP_SUPPRESS=DEMO; // kiosk screens are unattended: never auto-show the help overlay
const _trail=_tp.get("trail");
const trailMode=_trail?(_trail==="comet"?"comet":"full"):(DEMO?"comet":"full");
let trailSecs=parseInt(_tp.get("trailsecs"),10);
if(!Number.isFinite(trailSecs)) trailSecs=60;
if(trailSecs<5) trailSecs=5;
let readoutsOn=true;     // toggled by the "altitude / climb readouts" checkbox in the settings panel

// short callsign = the bit in square brackets in the aircraft name (e.g. "G-ELSB [SB]" -> "SB",
// "G-CKFY [KFY]" -> "KFY"); fall back to the full name/registration if there are no brackets.
function shortCallsign(name){
  if(!name) return "";
  const m=name.match(/\[([^\]]+)\]/);
  return m?m[1].trim():name.trim();
}

// same format as the replay's fmtReadout: short callsign, then height above the field in feet,
// then signed climb rate in knots (three lines).
function fmtReadout(cs, ft, kt){
  const ftStr=Math.round(ft).toLocaleString("en-GB");
  const sign=kt>=0?"+":"-";
  return (cs?cs+"\n":"")+ftStr+" ft\n"+sign+Math.abs(kt).toFixed(1)+" kt";
}

// Remove isolated out-and-back spike points from a track. A point p[i] is a spike if it is far
// from BOTH neighbours AND the two hops out+back are much longer than the direct neighbour-to-
// neighbour hop (so the point juts out and comes straight back while the neighbours stay close).
// Turn-safe: a genuine turn point sits near at least one neighbour, or the neighbours are far
// apart, so the ratio test fails. Horizontal distance only (altitude ignored). First/last kept.
function haversineM(a,b){
  const R=6371000, toR=Math.PI/180;
  const dLat=(b[1]-a[1])*toR, dLon=(b[0]-a[0])*toR;
  const la1=a[1]*toR, la2=b[1]*toR;
  const h=Math.sin(dLat/2)**2 + Math.cos(la1)*Math.cos(la2)*Math.sin(dLon/2)**2;
  return 2*R*Math.asin(Math.min(1,Math.sqrt(h)));
}
function despike(pts){
  if(pts.length<3) return pts;
  const out=[pts[0]];
  for(let i=1;i<pts.length-1;i++){
    const prev=out[out.length-1], p=pts[i], next=pts[i+1];
    const d0=haversineM(prev,p), d1=haversineM(p,next), dd=haversineM(prev,next);
    if(d0>SPIKE_MIN_M && d1>SPIKE_MIN_M && (d0+d1)>SPIKE_RATIO*dd){
      continue;  // isolated out-and-back spike: drop it
    }
    out.push(p);
  }
  out.push(pts[pts.length-1]);
  return out;
}

// create-or-update an aircraft entity from a position (lon,lat,height_m)
function ensure(addr,name,color,model){
  let e=ac[addr];
  if(e){
    if(name){ e.name=name; e.plane.name=name; e.trail.name=name+" trail"; e._cs=shortCallsign(name); }
    return e;
  }
  const col=Cesium.Color.fromCssColorString(color);
  e=ac[addr]={color:color,name:name,model:model,pts:[],maxTs:0,_pitch:0,_cs:shortCallsign(name)};
  e.plane=viewer.entities.add({
    name:name,
    position:new Cesium.CallbackProperty(()=>e._pos,false),
    // nose-forward: driven by the smoothed lookback vector (see updateOrientation);
    // undefined until the aircraft has moved enough, then holds the last-good heading.
    orientation:new Cesium.CallbackProperty(()=>e._ori,false),
    model:{uri:MODELS[model]||MODELS.glider, minimumPixelSize:40, maximumScale:20000, scale:1,
      color:col, colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.5,
      silhouetteColor:col, silhouetteSize:1.5},
    // floating altitude + rate-of-climb readout, hovering above the model. Both values are
    // computed once per fix (see updateOrientation) and stored on e, so the callback only
    // formats them; far-away gliders fade/drop via distanceDisplayCondition to declutter.
    label:{
      text:new Cesium.CallbackProperty(()=>{
        if(e._alt==null||e._vario==null) return "";
        return fmtReadout(e._cs, e._alt, e._vario);
      },false),
      show:readoutsOn,
      font:"12px sans-serif",
      fillColor:Cesium.Color.WHITE,
      showBackground:true,
      backgroundColor:new Cesium.Color(0,0,0,0.6),
      backgroundPadding:new Cesium.Cartesian2(6,4),
      verticalOrigin:Cesium.VerticalOrigin.BOTTOM,
      horizontalOrigin:Cesium.HorizontalOrigin.CENTER,
      pixelOffset:new Cesium.Cartesian2(0,-28),
      disableDepthTestDistance:Number.POSITIVE_INFINITY,
      distanceDisplayCondition:new Cesium.DistanceDisplayCondition(0.0, 60000.0),
      translucencyByDistance:new Cesium.NearFarScalar(15000,1.0,60000,0.25),
      scaleByDistance:new Cesium.NearFarScalar(15000,1.0,60000,0.75)
    }
  });
  e.trail=viewer.entities.add({
    name:name+" trail", show:trailsOn,
    polyline:{positions:new Cesium.CallbackProperty(()=>e._trail,false),
      width:2, material:col.withAlpha(0.55)}
  });
  return e;
}

// create-or-update a PARKED marker: the aircraft model sitting on the ground, dimmed and
// translucent, with just a short-callsign label. No trail, no altitude/vario readout. Kept
// visually distinct from the flying aircraft so a parked glider reads as "on the ground".
function ensureParked(addr,cs,color,model,lon,lat){
  let p=parked[addr];
  const pos=parkedPos(lon,lat);
  if(p){
    p._pos=pos; p.lastSeen=Date.now();
    if(cs){ p.cs=cs; p.label.name=cs+" (parked)"; }
    if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
    return p;
  }
  const col=Cesium.Color.fromCssColorString(color);
  p=parked[addr]={color:color,cs:cs,model:model,lastSeen:Date.now(),_pos:pos};
  p.plane=viewer.entities.add({
    name:(cs||addr)+" (parked)",
    show:parkedOn,
    position:new Cesium.CallbackProperty(()=>p._pos,false),
    model:{uri:MODELS[model]||MODELS.glider, minimumPixelSize:28, maximumScale:20000, scale:1,
      // dimmed + translucent so it clearly reads as parked/inactive vs the flying models.
      color:col.withAlpha(0.55), colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.85,
      silhouetteColor:col.withAlpha(0.5), silhouetteSize:1.0}
  });
  p.label=viewer.entities.add({
    name:(cs||addr)+" (parked)",
    show:parkedOn,
    position:new Cesium.CallbackProperty(()=>p._pos,false),
    label:{
      text:new Cesium.CallbackProperty(()=>p.cs||"",false),
      font:"11px sans-serif",
      fillColor:Cesium.Color.WHITE.withAlpha(0.85),
      showBackground:true,
      backgroundColor:new Cesium.Color(0,0,0,0.45),
      backgroundPadding:new Cesium.Cartesian2(5,3),
      verticalOrigin:Cesium.VerticalOrigin.BOTTOM,
      horizontalOrigin:Cesium.HorizontalOrigin.CENTER,
      pixelOffset:new Cesium.Cartesian2(0,-16),
      disableDepthTestDistance:Number.POSITIVE_INFINITY,
      distanceDisplayCondition:new Cesium.DistanceDisplayCondition(0.0, 20000.0),
      translucencyByDistance:new Cesium.NearFarScalar(8000,1.0,20000,0.2)
    }
  });
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
  return p;
}

// remove a parked marker (both its model + label entities) and forget it
function removeParked(addr){
  const p=parked[addr];
  if(!p) return;
  viewer.entities.remove(p.plane);
  viewer.entities.remove(p.label);
  delete parked[addr];
}

// show/hide the whole parked overlay without touching airborne aircraft
function applyParked(){
  for(const addr of Object.keys(parked)){ parked[addr].plane.show=parkedOn; parked[addr].label.show=parkedOn; }
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// show/hide every aircraft's trail without touching planes/positions/legend
function applyTrails(){
  for(const addr of Object.keys(ac)) ac[addr].trail.show=trailsOn;
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// point the model along a SMOOTHED velocity vector: from a lookback point a little
// way back in the retained trail to the current position. Deliberately not the
// immediately-previous fix, to damp GPS jitter. This is the same maths the replay's
// VelocityOrientationProperty uses (both models have zero yaw offset, so no extra
// correction is needed). Below ORIENT_STATIONARY_M we keep the last-good heading so
// parked/slow gliders do not spin randomly.
function updateOrientation(e){
  // Drive heading/pitch/vario/position off the DESPIKED trail so a single outlier fix does not
  // flip the nose or jump the model. e.dpts is set alongside e._trail (see pushPoint/snapshot).
  const dp=e.dpts||e.pts;
  const n=dp.length;
  if(n<1) return;
  const last=dp[n-1];
  const curTs=last[3];
  // Readout altitude: height above the field in feet (height_m is already above-field, see
  // live_height_m). Vario: least-squares slope of height(m) vs time over a trailing VARIO_WIN_S
  // window -> knots. This is the same slope-of-height maths the pitch fit below uses (which
  // tames the noisy OGN GPS altitude), just over the shorter readout window; both are stored on
  // e once per fix and read by the label callback, so nothing is recomputed per frame.
  e._alt=last[2]*M_TO_FT + FIELD_ELEV_FT;   // altitude AMSL (above-field + field elevation)
  if(n>=2 && curTs!=null){
    let vs=n-1;
    for(let i=n-2;i>=0;i--){ vs=i; if(dp[i][3]!=null && curTs-dp[i][3]>=VARIO_WIN_S) break; }
    const vwin=dp.slice(vs);
    let cnt=0,sx=0,sy=0,sxx=0,sxy=0;
    for(const p of vwin){ if(p[3]==null) continue; const x=p[3]-vwin[0][3],y=p[2]; cnt++; sx+=x; sy+=y; sxx+=x*x; sxy+=x*y; }
    const den=cnt*sxx-sx*sx;
    const slope=(cnt>=2 && Math.abs(den)>1e-9)?(cnt*sxy-sx*sy)/den:0;  // m/s
    e._vario=slope*MS_TO_KT;
  } else { e._vario=0; }
  if(n<2) return;
  const cur=Cesium.Cartesian3.fromDegrees(last[0],last[1],last[2]);
  // HEADING baseline: walk back until at least ORIENT_MIN_M behind (else oldest point).
  // This short, responsive lookback sets which way the nose points.
  let lookback=null;
  for(let i=n-2;i>=0;i--){
    const p=dp[i];
    const c=Cesium.Cartesian3.fromDegrees(p[0],p[1],p[2]);
    lookback=c;
    if(Cesium.Cartesian3.distance(cur,c)>=ORIENT_MIN_M) break;
  }
  if(!lookback) return;
  const vel0=Cesium.Cartesian3.subtract(cur,lookback,new Cesium.Cartesian3());
  if(Cesium.Cartesian3.magnitude(vel0)<ORIENT_STATIONARY_M) return; // keep last-good
  // HEADING direction: horizontal part of the short lookback velocity (responsive).
  const upv=Cesium.Ellipsoid.WGS84.geodeticSurfaceNormal(cur,new Cesium.Cartesian3());
  const horiz=Cesium.Cartesian3.subtract(vel0,
    Cesium.Cartesian3.multiplyByScalar(upv,Cesium.Cartesian3.dot(vel0,upv),new Cesium.Cartesian3()),
    new Cesium.Cartesian3());
  if(Cesium.Cartesian3.magnitude(horiz)<1e-6) return;
  const hdir=Cesium.Cartesian3.normalize(horiz,new Cesium.Cartesian3());
  // PITCH: least-squares slope of height vs time over a LONG window (many points) = a
  // noise-robust vertical speed; horizontal speed from the ground-track path length. The
  // flight-path angle atan2(vspeed,hspeed) then matches the real track slope, not noise.
  let rawPitch=0;
  if(curTs!=null){
    let s=n-1;
    for(let i=n-2;i>=0;i--){ s=i; if(dp[i][3]!=null && curTs-dp[i][3]>=PITCH_WINDOW_S) break; }
    const win=dp.slice(s);
    if(win.length>=4 && win[0][3]!=null){
      let sx=0,sy=0,sxx=0,sxy=0,path=0,prev=null; const k=win.length,t0=win[0][3];
      for(const p of win){
        const x=p[3]-t0,y=p[2]; sx+=x; sy+=y; sxx+=x*x; sxy+=x*y;
        const fp=Cesium.Cartesian3.fromDegrees(p[0],p[1],0);
        if(prev) path+=Cesium.Cartesian3.distance(prev,fp); prev=fp;
      }
      const den=k*sxx-sx*sx, dt=curTs-t0, hs=dt>0?path/dt:0;
      if(den>1e-6 && hs>0.5) rawPitch=Math.atan2((k*sxy-sx*sy)/den, hs);
    }
  }
  // Use the fit directly. The window already smooths; an extra EMA accumulator only added
  // lag that persisted across frames (nose stuck up after levelling off, until a reload).
  e._pitch=rawPitch;
  const maxP=Cesium.Math.toRadians(PITCH_MAX_DEG);
  const pitch=Math.max(-maxP,Math.min(maxP,e._pitch));
  // velocity = heading direction tilted by the smoothed pitch, wings level.
  const vel=Cesium.Cartesian3.add(
    Cesium.Cartesian3.multiplyByScalar(hdir,Math.cos(pitch),new Cesium.Cartesian3()),
    Cesium.Cartesian3.multiplyByScalar(upv,Math.sin(pitch),new Cesium.Cartesian3()),
    new Cesium.Cartesian3());
  const m=Cesium.Transforms.rotationMatrixFromPositionVelocity(cur,vel,Cesium.Ellipsoid.WGS84);
  const q=Cesium.Quaternion.fromRotationMatrix(m);
  // Normalise (a non-unit quaternion would scale/balloon the model); keep last-good if not finite.
  if(isFinite(q.x)&&isFinite(q.y)&&isFinite(q.z)&&isFinite(q.w)){
    e._ori=Cesium.Quaternion.normalize(q,new Cesium.Quaternion());
  }
}

// flatten only lon/lat/height for the polyline; points now carry a 4th elem (ts)
// used solely by the pitch window, which must not leak into the trail geometry.
function trailPositions(pts){
  const flat=[];
  for(const p of pts){ flat.push(p[0],p[1],p[2]); }
  return Cesium.Cartesian3.fromDegreesArrayHeights(flat);
}

// In comet mode, keep only the retained despiked points whose ts (elem [3], epoch secs)
// falls within trailSecs of THIS aircraft's most recent point, so the drawn tail slides
// along behind the aircraft. In full mode the array is returned untouched, so the trail is
// byte-for-byte the current whole-bounded-trail behaviour. Arrays are small (<=maxTrail).
function trailWindow(dpts){
  if(trailMode!=="comet" || dpts.length<2) return dpts;
  const last=dpts[dpts.length-1];
  const lastTs=last[3];
  if(lastTs==null) return dpts;   // no timestamps to filter on: fall back to full
  const cut=lastTs-trailSecs;
  let i=dpts.length-1;
  while(i>0 && dpts[i-1][3]!=null && dpts[i-1][3]>=cut) i--;
  return (i===0)?dpts:dpts.slice(i);
}

// append one [lon,lat,height_m,ts] point, keeping the trail bounded, then rebuild the despiked
// view used for drawing/heading/position (see despike). The model sits on the LAST despiked
// point, so a spike at the leading edge does not jump it (at most ~1 fix of lag until the spike
// is confirmed as isolated by the next fix). The trail is bounded by maxTrail, so despike is cheap.
function refresh(e){
  e.dpts=despike(e.pts);
  const dl=e.dpts[e.dpts.length-1];
  e._pos=Cesium.Cartesian3.fromDegrees(dl[0],dl[1],dl[2]);
  e._trail=trailPositions(trailWindow(e.dpts));
  updateOrientation(e);
}
function pushPoint(e,pt){
  e.pts.push(pt);
  if(e.pts.length>maxTrail) e.pts.splice(0,e.pts.length-maxTrail);
  refresh(e);
  e.lastSeen=Date.now();
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// initial snapshot from /live.json: paint whole recent tracks (does not duplicate)
function snapshot(a){
  const pts=a.points||[];
  if(!pts.length) return;
  const e=ensure(a.address,a.name,a.color,a.model);
  e.pts=pts.slice(-maxTrail);
  e.maxTs=a.last_ts||0;   // so streamed duplicates already in this snapshot are dropped
  refresh(e);             // seed despiked trail/position/heading from the snapshot if it has moved
  e.lastSeen=Date.now();
}

// a single streamed fix event
function onEvent(ev){
  if(ev.g){                 // ground/parked event: a distinct on-the-ground marker, not an aircraft
    if(!parkedOn) return;   // overlay toggled off: ignore ground events entirely
    // if this aircraft is currently shown as airborne, it hasn't really parked - ignore the
    // ground event (owned/airborne wins). The collector only sends ground for NOT-owned aircraft,
    // but a just-landed aircraft can still have a live airborne entity mid-prune.
    if(ac[ev.addr]) return;
    ensureParked(ev.addr,ev.cs||ev.name,ev.color,ev.model,ev.lon,ev.lat);
    return;
  }
  // normal airborne event: if this aircraft was shown parked, it has launched - drop the marker
  // so it is never shown twice (parked + flying).
  if(parked[ev.addr]) removeParked(ev.addr);
  const e=ensure(ev.addr,ev.label||ev.name,ev.color,ev.model);
  e.lastSeen=Date.now();   // keep it alive even if this fix is a duplicate
  // OGN aircraft are heard by several ground receivers, so the same fix arrives more
  // than once and late relays arrive out of order. Only accept a strictly-newer fix, so
  // the trail never jumps backwards (which showed as cross-loop / sawtooth artefacts).
  if(ev.ts!=null && ev.ts<=e.maxTs) return;
  if(ev.ts!=null) e.maxTs=ev.ts;
  pushPoint(e,[ev.lon,ev.lat,ev.height_m,ev.ts]);
  renderLegend();
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
  // parked gliders beacon slowly, so give them a longer grace before removal.
  for(const addr of Object.keys(parked)){
    if(t-parked[addr].lastSeen>PARKED_GRACE_MS) removeParked(addr);
  }
  renderLegend();
}

// re-trim every aircraft's retained points to the current maxTrail and rebuild its
// polyline, so a slider change is reflected immediately without waiting for new fixes.
function applyTrailLength(){
  for(const e of Object.values(ac)){
    if(e.pts.length>maxTrail) e.pts.splice(0,e.pts.length-maxTrail);
    e.dpts=despike(e.pts);
    e._trail=trailPositions(trailWindow(e.dpts));
  }
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// day/night styling: mirror the replay's setNight. Night reveals the star skybox by
// dropping the bright atmosphere + ground haze against a black background; day restores
// the default blue atmosphere. The live page opens in night style (see viewer setup).
function setNight(on){
  viewer.scene.skyAtmosphere.show=!on;
  viewer.scene.globe.showGroundAtmosphere=!on;
  viewer.scene.backgroundColor=on?Cesium.Color.BLACK:Cesium.Color.CORNFLOWERBLUE;
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// Build the collapsible settings panel ONCE and wire its listeners ONCE. This lives in a
// static sibling element that renderLegend() never overwrites, so an open <details> stays
// open and the slider stays usable (no reset mid-drag) while fixes stream in and only the
// dynamic aircraft rows re-render. See renderLegend(), which touches only #legdyn.
function buildSettings(){
  const box=document.createElement("details");
  box.id="settings"; box.style.marginTop="6px";
  box.innerHTML=`<summary style="cursor:pointer;user-select:none;opacity:.8">settings</summary>`
    +`<div style="margin:4px 0 2px 2px">`
    +`<label style="display:block"><input type="checkbox" id="traillbl"${trailsOn?" checked":""}> Trail</label>`
    +`<label style="display:block;margin-top:4px">trail length: <span id="tlen">${maxTrail}</span> pts<br>`
    +`<input type="range" id="trailrange" min="20" max="1200" step="20" value="${maxTrail}" style="width:150px"></label>`
    +`<label style="display:block;margin-top:4px"><input type="checkbox" id="readoutlbl" checked> altitude / climb readouts</label>`
    +`<label style="display:block;margin-top:4px"><input type="checkbox" id="parkedlbl" checked> parked aircraft</label>`
    +`<label style="display:block;margin-top:4px"><input type="checkbox" id="nightlbl" checked> Night sky</label>`
    +`<label style="display:block;margin-top:4px"><input type="checkbox" id="thermalslbl"> thermal hotspots</label>`
    +`<label style="display:block;margin-top:4px"><input type="checkbox" id="placelbl"> place names</label>`
    +`<button id="resetview" style="cursor:pointer;margin-top:6px">reset view</button>`
    +`</div>`;
  document.getElementById("legend").appendChild(box);
  document.getElementById("traillbl").addEventListener("change",e=>{ trailsOn=e.target.checked; applyTrails(); });
  document.getElementById("trailrange").addEventListener("input",e=>{
    maxTrail=+e.target.value;
    document.getElementById("tlen").textContent=maxTrail;
    applyTrailLength();
  });
  document.getElementById("readoutlbl").addEventListener("change",e=>{ readoutsOn=e.target.checked; applyReadouts(); });
  document.getElementById("thermalslbl").addEventListener("change",e=>{ setThermals(e.target.checked); });
  document.getElementById("parkedlbl").addEventListener("change",e=>{
    parkedOn=e.target.checked;
    if(!parkedOn){ for(const addr of Object.keys(parked)) removeParked(addr); } // hide + clear so stale ones don't linger
    applyParked();
    renderLegend();
  });
  document.getElementById("nightlbl").addEventListener("change",e=>{ setNight(e.target.checked); });
  document.getElementById("placelbl").addEventListener("change",e=>{
    labelLayer.show=e.target.checked;
    if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
  });
  document.getElementById("resetview").addEventListener("click",function(){ goHome(); this.blur(); });
}

// show/hide every aircraft's floating altitude/climb readout without touching anything else
function applyReadouts(){
  for(const addr of Object.keys(ac)){ if(ac[addr].plane.label) ac[addr].plane.label.show=readoutsOn; }
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// Only the dynamic parts re-render per fix: the airborne count + per-aircraft colour rows.
// The settings panel is a separate static element (buildSettings) so it is never rebuilt here.
function renderLegend(){
  const items=Object.values(ac);
  const rows=items.map(e=>`<div><span class="sw" style="background:${e.color}"></span>${e.name}</div>`);
  const n=items.length;
  const np=Object.keys(parked).length;
  const parkedHint=(parkedOn && np)?`<br><span class="hint">${np} parked on the ground</span>`:"";
  const head=`<b>Live - Gransden</b><br><span class="hint">${n} aircraft airborne</span>`+parkedHint;
  document.getElementById("legdyn").innerHTML=head+(rows.length?rows.join(""):"");
}

// 1) paint the current picture once, then 2) open the live event stream.
async function start(){
  try{
    const r=await fetch("live.json",{cache:"no-store"});
    const d=await r.json();
    (d.aircraft||[]).forEach(snapshot);
  }catch(e){ /* no snapshot; the stream will fill in */ }
  buildSettings();   // static controls, built once so per-fix renders never disturb them
  renderLegend();
  const es=new EventSource("live.stream");
  es.onmessage=function(m){
    try{ onEvent(JSON.parse(m.data)); }catch(e){}
  };
  // EventSource auto-reconnects; ensure()/snapshot() are idempotent so no dupes.
}
start();
setInterval(prune, 5000);

// Initial sky from ?sky=day|night. Lets the demo/kiosk (where the settings toggle is hidden)
// choose day or night on the URL; also works on the normal /live. Default stays night.
(function(){
  const sky=new URLSearchParams(location.search).get("sky");
  let night;
  if(sky==="day") night=false;
  else if(sky==="night") night=true;
  else if(DEMO) night=false;   // demo/kiosk defaults to a day sky
  else return;                 // plain /live, no sky param: leave the default (night)
  setNight(night);
  const cb=document.getElementById("nightlbl");
  if(cb) cb.checked=night;
})();

// hover tooltip: aircraft name under the cursor
const _tip=document.createElement("div");
_tip.style.cssText="position:fixed;z-index:30;pointer-events:none;display:none;background:var(--overlay);"
  +"border:1px solid var(--overlay-line);color:var(--text);font:12px system-ui,sans-serif;padding:2px 7px;border-radius:5px;white-space:nowrap";
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

// --- Demo / kiosk mode (big-screen display) -------------------------------------------
// Opt-in via /live?demo=1. Everything above is untouched: same SSE traffic, models, trails
// and readouts. Demo mode only (a) hides the UI chrome, (b) turns OFF requestRenderMode so
// Cesium renders every frame, and (c) slowly orbits the camera around the airfield forever
// via requestAnimationFrame. Heading is derived from wall-clock ELAPSED TIME, so the orbit
// speed is frame-rate independent. camera.lookAt() locks out manual control, which is what a
// kiosk wants. When ?demo is absent, none of this runs and /live behaves exactly as before.
(function(){
  const qp=new URLSearchParams(location.search);
  if(!qp.has("demo")) return;   // plain /live: leave everything alone
  const num=(k,d)=>{ const v=parseFloat(qp.get(k)); return Number.isFinite(v)?v:d; };
  const SECS=Math.max(5, num("secs", 120));   // seconds per full 360 rotation (gentle by default)
  const PITCH=num("pitch", -10);              // camera tilt in degrees (shallow by default)
  const RANGE=Math.max(200, num("range", 4500)); // camera distance from centre, metres
  const LON=num("lon", __DEMOLON__);          // orbit centre (defaults to the airfield)
  const LAT=num("lat", __DEMOLAT__);
  const CENTRE_H=(FIELD_ELEV_FT*0.3048)+100;  // a little above the field so the ground is in view

  // Hide the chrome for a clean screen: the top nav strip and the legend/settings panel.
  // Aircraft models, trails and readouts are entities in the Cesium scene, so they stay.
  document.querySelectorAll("body > div").forEach(el=>{
    if(el.id!=="c") el.style.display="none";   // nav strip + #legend; keep the canvas host
  });
  const legend=document.getElementById("legend");
  if(legend) legend.style.display="none";

  // Small unobtrusive corner title so the screen has some context.
  const title=document.createElement("div");
  title.textContent="Gransden - live";
  title.style.cssText="position:fixed;bottom:10px;left:50%;transform:translateX(-50%);"
    +"z-index:20;color:#fff;font:14px system-ui,sans-serif;opacity:.6;text-shadow:0 0 4px #000;pointer-events:none";
  document.body.appendChild(title);

  // club logo (drop-in branding from /branding/, see _logo_url): a small corner mark.
  // Empty string = no logo file on the volume, so nothing is added.
  const LOGO="__LOGOURL__";
  if(LOGO){
    const li=document.createElement("img");
    li.src=LOGO; li.alt="";
    // right:56px keeps the wordmark clear of Cesium's fullscreen button in the corner
    li.style.cssText="position:fixed;bottom:12px;right:56px;z-index:21;max-height:60px;"
      +"max-width:220px;opacity:.9;pointer-events:none;filter:brightness(0) invert(1)";
    document.body.appendChild(li);
  }

  // Small clickable link back home (the nav strip is hidden in demo mode).
  const homeLink=document.createElement("a");
  homeLink.href="/"; homeLink.textContent="home";
  homeLink.style.cssText="position:fixed;top:10px;left:12px;z-index:21;color:var(--blue);"
    +"font:13px system-ui,sans-serif;opacity:.85;text-decoration:none;text-shadow:0 0 4px #000";
  document.body.appendChild(homeLink);

  // Continuous rendering for a smooth orbit (kiosk, so power/heat are a non-issue).
  viewer.scene.requestRenderMode=false;

  const centre=Cesium.Cartesian3.fromDegrees(LON, LAT, CENTRE_H);
  const toR=Cesium.Math.toRadians;
  function orbit(now){
    const heading=((now/1000)/SECS)*360.0;   // degrees, time-based so it is frame-rate independent
    viewer.camera.lookAt(centre, new Cesium.HeadingPitchRange(toR(heading), toR(PITCH), RANGE));
    requestAnimationFrame(orbit);
  }
  requestAnimationFrame(orbit);
})();

// --- thermal-hotspots overlay (shared renderer; lazy-loaded on first toggle) ------------
__THERMALSJS__
let liveThermal={layer:null,on:false};
function setThermals(on){
  liveThermal.on=on;
  if(on && !liveThermal.layer){
    fetch("/thermals.json").then(function(r){return r.json();}).then(function(d){
      liveThermal.layer=ognThermalLayer(viewer,d.hotspots,FIELD_ELEV_FT);
      liveThermal.layer.show(liveThermal.on);
    }).catch(function(e){});
  } else if(liveThermal.layer){ liveThermal.layer.show(on); }
}

__HELPJS__
</script></body></html>"""


def _calibrate_page() -> str:
    """One-off model-orientation calibration tool.

    Renders a model oriented exactly as the app does (velocity orientation * a per-model
    correction quaternion) next to a RED arrow along the direction of travel, so the nose
    should point down the red arrow, wings level, upright. Nudge yaw/pitch/roll until it
    lines up; the readout is the value to bake into the app for that model.
    """
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://cesium.com/downloads/cesiumjs/releases/1.143/Build/Cesium/Cesium.js"></script>
<link href="https://cesium.com/downloads/cesiumjs/releases/1.143/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
<style>html,body,#c{width:100%;height:100%;margin:0;overflow:hidden}
#hud{position:fixed;top:8px;left:8px;z-index:10;background:rgba(0,0,0,.8);color:#fff;
font:13px/1.5 monospace;padding:10px 12px;border-radius:6px;white-space:pre}</style></head>
<body><div id="c"></div><div id="hud"></div><script>
Cesium.Ion.defaultAccessToken="";
const v=new Cesium.Viewer("c",{baseLayerPicker:false,geocoder:false,timeline:false,animation:false,
  homeButton:false,sceneModePicker:false,navigationHelpButton:false,fullscreenButton:false,infoBox:false,
  selectionIndicator:false,baseLayer:new Cesium.ImageryLayer(new Cesium.GridImageryProvider())});
v.scene.skyAtmosphere.show=false; v.scene.globe.showGroundAtmosphere=false;
const MODELS={glider:"models/AS21.glb", dr400:"models/DR40.glb"};
const lon=-0.109, lat=52.187, h=400;
const pos=Cesium.Cartesian3.fromDegrees(lon,lat,h);
const enu=Cesium.Transforms.eastNorthUpToFixedFrame(pos);
const east=new Cesium.Cartesian3(enu[0],enu[1],enu[2]);
const up=new Cesium.Cartesian3(enu[8],enu[9],enu[10]);
// per-model correction (degrees), what we're tuning
const corr={glider:{yaw:0,pitch:0,roll:0}, dr400:{yaw:0,pitch:0,roll:0}};
let cur="glider", climb=false, plane=null;
function velVec(){
  const ang=climb?30:0;
  const e=Cesium.Cartesian3.multiplyByScalar(east,Math.cos(Cesium.Math.toRadians(ang)),new Cesium.Cartesian3());
  const u=Cesium.Cartesian3.multiplyByScalar(up,Math.sin(Cesium.Math.toRadians(ang)),new Cesium.Cartesian3());
  return Cesium.Cartesian3.add(e,u,new Cesium.Cartesian3());
}
// reference: BLUE tail -> pos -> RED nose target (down the direction of travel)
let refB=null,refR=null;
function drawRef(){
  const vel=velVec();
  const back=Cesium.Cartesian3.add(pos,Cesium.Cartesian3.multiplyByScalar(vel,-140,new Cesium.Cartesian3()),new Cesium.Cartesian3());
  const fwd=Cesium.Cartesian3.add(pos,Cesium.Cartesian3.multiplyByScalar(vel,300,new Cesium.Cartesian3()),new Cesium.Cartesian3());
  if(refB) v.entities.remove(refB);
  if(refR) v.entities.remove(refR);
  refB=v.entities.add({polyline:{positions:[back,pos],width:5,material:Cesium.Color.DEEPSKYBLUE}});
  refR=v.entities.add({polyline:{positions:[pos,fwd],width:7,material:Cesium.Color.RED}});
}
function orient(){
  const vel=velVec();
  const m=Cesium.Transforms.rotationMatrixFromPositionVelocity(pos,vel,Cesium.Ellipsoid.WGS84);
  const velQ=Cesium.Quaternion.normalize(Cesium.Quaternion.fromRotationMatrix(m),new Cesium.Quaternion());
  const c=corr[cur];
  const cq=Cesium.Quaternion.fromHeadingPitchRoll(new Cesium.HeadingPitchRoll(
    Cesium.Math.toRadians(c.yaw),Cesium.Math.toRadians(c.pitch),Cesium.Math.toRadians(c.roll)));
  return Cesium.Quaternion.multiply(velQ,cq,new Cesium.Quaternion());
}
function rebuild(){
  if(plane) v.entities.remove(plane);
  plane=v.entities.add({position:pos,orientation:orient(),
    model:{uri:MODELS[cur],minimumPixelSize:280,maximumScale:20000,scale:1}});
  drawRef(); hud(); v.scene.requestRender();
}
function hud(){
  const c=corr[cur];
  document.getElementById("hud").textContent=
`MODEL CALIBRATION  (nose should point down the RED line, wings level, upright)
selected: ${cur}     climb: ${climb?"+30 (nose should tilt UP)":"level"}

  ${cur}:  yaw=${c.yaw}   pitch=${c.pitch}   roll=${c.roll}   (degrees)

keys:
  g / t      select glider / tug
  a / d      yaw  - / +        (turn nose left/right)
  w / s      pitch + / -       (nose up/down)
  q / e      roll - / +        (bank)
  hold Shift = 1 degree steps (else 5)
  c          toggle climb (validate nose tilts up)
  r          reset this model to 0

report both models' yaw/pitch/roll to Claude.`;
}
addEventListener("keydown",ev=>{
  const s=ev.shiftKey?1:5, c=corr[cur]; let hit=true;
  switch(ev.key.toLowerCase()){
    case"g":cur="glider";break; case"t":cur="dr400";break;
    case"a":c.yaw-=s;break; case"d":c.yaw+=s;break;
    case"w":c.pitch+=s;break; case"s":c.pitch-=s;break;
    case"q":c.roll-=s;break; case"e":c.roll+=s;break;
    case"c":climb=!climb;break; case"r":corr[cur]={yaw:0,pitch:0,roll:0};break;
    default:hit=false;
  }
  if(hit){ev.preventDefault(); if(ev.key.toLowerCase()==="g"||ev.key.toLowerCase()==="t")rebuild(); else if(plane){plane.orientation=orient(); drawRef(); hud(); v.scene.requestRender();}}
});
rebuild();
v.camera.lookAt(pos,new Cesium.Cartesian3(0,-480,150));  // view from the south, up a bit: east=right, up=up
</script></body></html>"""


def _live_page(data_dir: str = "") -> str:
    models = {k: f"models/{v}" for k, v in MODEL_FILES.items()}
    logo = _logo_url(data_dir) if data_dir else ""
    navlogo = f'<img class="nlogo" src="{logo}" alt="">' if logo else ""
    return (LIVE_HTML.replace("__CES__", LIVE_CES).replace("__MODELS__", json.dumps(models))
            .replace("__THEMECSS__", THEME_CSS)
            .replace("__NAVLOGO__", navlogo)
            .replace("__HELPBTN__", MAP_HELP_BTN)
            .replace("__HELPHTML__", MAP_HELP_HTML)
            .replace("__HELPJS__", MAP_HELP_JS)
            .replace("__THERMALSJS__", THERMALS_JS)
            .replace("__FIELDELEV__", repr(float(GRANSDEN.elevation_ft)))
            .replace("__DEMOLON__", repr(float(GRANSDEN.lon)))
            .replace("__DEMOLAT__", repr(float(GRANSDEN.lat)))
            .replace("__LOGOURL__", logo))


# --- club branding: drop-in, no rebuild ---------------------------------------------
# The club can put a logo at <data_dir>/branding/logo.<ext> (the data dir is already a
# mounted volume) and it appears on the public pages. No file = no logo, no gap.
BRANDING_CTYPES = {"png": "image/png", "svg": "image/svg+xml", "webp": "image/webp",
                   "jpg": "image/jpeg", "jpeg": "image/jpeg"}
CLUB_NAME = os.environ.get("CLUB_NAME", "")


def _logo_url(data_dir: str) -> str:
    """/branding/logo.<ext> for the first logo file found, else "" (render nothing)."""
    bdir = os.path.join(data_dir, "branding")
    for ext in ("svg", "png", "webp", "jpg", "jpeg"):
        if os.path.isfile(os.path.join(bdir, "logo." + ext)):
            return "/branding/logo." + ext
    return ""


# "Watch your flight" finder: a public, non-technical page for trial-flight visitors.
# They know WHEN they flew (roughly) and maybe what the aircraft looked like, never the
# registration. The page fetches the day's flights from the same public-data branch the
# public replay uses (last ~7 days), matches on a forgiving time window, and links each
# candidate to the single-flight replay (/replay?day=&address=&t=). No personal data:
# OGN only ever identifies the aircraft, never who was in it.
MINE_DATA_BASE = "https://raw.githubusercontent.com/8none1/ognflights/public-data/"
MINE_HTML = r"""<!DOCTYPE html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watch your flight - Gransden</title>
<style>__THEMECSS__
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:1.3rem 1.2rem 1.25rem;box-shadow:var(--shadow)}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:480px){.fields{grid-template-columns:1fr}}
label.f{display:block;font-weight:600;font-size:.95rem}
label.f .hint{display:block;font-weight:400;color:var(--dim);font-size:.82rem;margin:.05rem 0 .35rem}
input[type=date],input[type=time]{width:100%;padding:.7rem .8rem;font:inherit;font-size:1.1rem;
  color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:10px;
  min-height:3.1rem}
input:focus{outline:2px solid var(--blue);outline-offset:1px;border-color:transparent}
fieldset.types{border:0;padding:0;margin:1.25rem 0 0}
fieldset.types legend{font-weight:600;font-size:.95rem;padding:0}
fieldset.types .hint{display:block;font-weight:400;color:var(--dim);font-size:.82rem}
.typegrid{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem;margin-top:.5rem}
@media(max-width:560px){.typegrid{grid-template-columns:repeat(2,1fr)}}
button.type{appearance:none;font:inherit;cursor:pointer;background:var(--bg);
  border:1.5px solid var(--line);border-radius:12px;padding:.55rem .3rem .5rem;color:var(--dim);
  display:flex;flex-direction:column;align-items:center;gap:.15rem;transition:border-color .12s,background .12s}
button.type svg{width:100%;max-width:104px;height:auto;display:block}
button.type span{font-size:.82rem;font-weight:600}
button.type .cs{font-size:.7rem;font-weight:600;color:var(--blue);letter-spacing:.02em}
button.type:hover{border-color:var(--faint)}
button.type.sel{border-color:var(--blue);background:rgba(129,213,204,.08);color:var(--text)}
button.go{display:block;width:100%;margin-top:1.25rem;padding:.95rem;font-size:1.1rem;
  border-radius:12px}
#results{margin-top:1.6rem}
.rhead{font-size:.95rem;color:var(--dim);margin:0 0 .7rem;text-align:center}
.state{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:1.5rem 1.2rem;text-align:center;color:var(--dim)}
.state b{color:var(--text)}
.spin{display:inline-block;width:22px;height:22px;border:3px solid var(--line);
  border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite;
  vertical-align:-5px;margin-right:.6rem}
@keyframes sp{to{transform:rotate(360deg)}}
.flight{display:flex;align-items:stretch;background:var(--panel);border:1px solid var(--line);
  border-radius:14px;overflow:hidden;margin-bottom:.8rem}
.flight .bar{width:6px;flex:none}
.flight .body{flex:1;padding:.85rem 1rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
.flight .who{flex:1;min-width:11rem}
.flight .reg{font-size:1.15rem;font-weight:700;letter-spacing:.01em}
.flight .cn{display:inline-block;margin-left:.45rem;padding:.05rem .5rem;font-size:.8rem;
  font-weight:700;color:var(--bg);background:var(--dim);border-radius:99px;vertical-align:2px}
.flight .meta{color:var(--dim);font-size:.9rem;margin-top:.15rem}
.flight .meta b{color:var(--text);font-weight:600}
.flight .acts{flex:none;align-self:center;display:flex;flex-direction:column;gap:.45rem}
.flight .acts a{white-space:nowrap;text-align:center}
@media(max-width:430px){
  .flight .body{gap:.6rem}
  .flight .acts{width:100%}
}
</style></head>
<body class="of-body"><div class="of-wrap narrow">
__NAV__
__HEADER__

<form id="finder" class="panel" autocomplete="off">
  <div class="fields">
    <label class="f">Which day did you fly?
      <span class="hint">we keep about the last week</span>
      <input type="date" id="fdate" required>
    </label>
    <label class="f">Roughly when did you take off?
      <span class="hint">local time - a guess is fine</span>
      <input type="time" id="ftime" required>
    </label>
  </div>
  <fieldset class="types">
    <legend>Which glider were you in?<span class="hint">optional - trial flights are always in a two-seat glider</span></legend>
    <div class="typegrid">
      <button type="button" class="type" data-type="k21" aria-pressed="false" title="Schleicher ASK-21">
        <svg viewBox="0 0 200 92" aria-hidden="true">
          <path d="M88 45 Q140 34 184 25 Q189 24 188 28 Q142 40 90 52 Z" fill="#93a4ba"/>
          <path d="M14 55 Q26 44 52 42 L120 45 Q148 46 164 44 L165 50 Q148 53 120 53 L52 55 Q30 58 14 55 Z" fill="#dbe4ee"/>
          <path d="M14 55 Q17 48 24 45 L23 56 Q17 57 14 55 Z" fill="#d94f3d"/>
          <path d="M42 43 Q52 35 66 37 Q75 39 79 44 Z" fill="#8fc1ee"/>
          <path d="M154 45 L165 20 L172 20 L166 46 Z" fill="#dbe4ee"/>
          <path d="M158 20 L184 17 L183 23 L158 25 Z" fill="#b9c6d6"/>
          <ellipse cx="64" cy="80" rx="36" ry="4" fill="#000" opacity=".25"/>
        </svg>
        <span>K21</span>
        <small class="cs">KFY &middot; HTV</small>
      </button>
      <button type="button" class="type" data-type="perkoz" aria-pressed="false" title="SZD-54 Perkoz">
        <svg viewBox="0 0 200 92" aria-hidden="true">
          <path d="M88 45 Q138 34 180 26 Q185 25 184 29 Q140 40 90 52 Z" fill="#93a4ba"/>
          <path d="M180 26 L185 13 L189 14 L184 28 Z" fill="#93a4ba"/>
          <path d="M14 55 Q26 44 52 42 L120 45 Q148 46 164 44 L165 50 Q148 53 120 53 L52 55 Q30 58 14 55 Z" fill="#dbe4ee"/>
          <path d="M24 50 Q70 47.5 122 48.5 L122 51.5 Q70 51 25 53 Z" fill="#5a9fd6"/>
          <path d="M42 43 Q52 35 66 37 Q75 39 79 44 Z" fill="#8fc1ee"/>
          <path d="M154 45 L165 20 L172 20 L166 46 Z" fill="#dbe4ee"/>
          <path d="M158 20 L184 17 L183 23 L158 25 Z" fill="#b9c6d6"/>
          <ellipse cx="64" cy="80" rx="36" ry="4" fill="#000" opacity=".25"/>
        </svg>
        <span>Perkoz</span>
        <small class="cs">PZ</small>
      </button>
      <button type="button" class="type" data-type="puchacz" aria-pressed="false" title="SZD-50 Puchacz">
        <svg viewBox="0 0 200 92" aria-hidden="true">
          <path d="M88 45 Q140 34 184 25 Q189 24 188 28 Q142 40 90 52 Z" fill="#93a4ba"/>
          <path d="M14 55 Q26 44 52 42 L120 45 Q148 46 164 44 L165 50 Q148 53 120 53 L52 55 Q30 58 14 55 Z" fill="#dbe4ee"/>
          <path d="M14 55 Q19 46 28 44 L27 56 Q19 57 14 55 Z" fill="#d94f3d"/>
          <path d="M40 43 Q51 33 68 35 Q78 38 82 44 Z" fill="#8fc1ee"/>
          <path d="M154 45 L165 20 L172 20 L166 46 Z" fill="#d94f3d"/>
          <path d="M158 20 L184 17 L183 23 L158 25 Z" fill="#b9c6d6"/>
          <ellipse cx="64" cy="80" rx="36" ry="4" fill="#000" opacity=".25"/>
        </svg>
        <span>Puchacz</span>
        <small class="cs">JEC</small>
      </button>
      <button type="button" class="type sel" data-type="" aria-pressed="true">
        <svg viewBox="0 0 200 92" aria-hidden="true">
          <circle cx="100" cy="42" r="27" fill="none" stroke="#93a4ba" stroke-width="4"/>
          <text x="100" y="53" text-anchor="middle" font-size="34" font-weight="700"
                fill="#93a4ba" font-family="system-ui,sans-serif">?</text>
        </svg>
        <span>Not sure</span>
      </button>
    </div>
  </fieldset>
  <button type="submit" class="of-btn-primary go">Find my flight</button>
</form>

<section id="results" aria-live="polite"></section>

<p class="of-foot">We only ever know the aircraft, never who was on board - nothing personal
is stored. Flights from roughly the last week are available.<br>
<a href="/">ognflights home</a> &middot; data from the Open Glider Network</p>
</div>
<script>
"use strict";
const DATA_BASE="__DATABASE__";
// CANDL: the server rendering this page has the flight DB, so /download works. A static
// host (the public CDN replay pipeline) sets this false and the button is simply omitted.
const CANDL=__CANDL__;
// Matching window (seconds). A visitor's remembered time is rough, so be forgiving:
// a flight matches if it was airborne anywhere in [entered-5min, entered+20min], or if
// its take-off is within 15 minutes either side. Results sort by take-off closeness.
const BACK_S=5*60, FWD_S=20*60, TOL_S=15*60;
const MIN_DUR_S=120;   // hide sub-2-minute segments (coverage blips, not real flights)
// Trial-fleet type matching. The published legend carries the OGN device-database model
// string per aircraft (legend[].type, e.g. "ASK-21", "SZD-50 Puchacz", "SZD-54 Perkoz");
// these forgiving patterns map each chooser card onto it, case-insensitive, tolerant of
// "ASK 21" / "K-21" style variants. Tug flights (mk "dr400") are never shown at all:
// a trial-flight passenger is always in a two-seat glider, never the tug.
const TYPE_PATTERNS={
  k21:/ask[\s-]?21|(^|[^a-z])k[\s-]?21/i,
  perkoz:/szd[\s-]?54|perkoz/i,
  puchacz:/szd[\s-]?50|puchacz/i};
function typeName(a){
  const s=(a&&a.type)||"";
  if(TYPE_PATTERNS.k21.test(s))return "K21 two-seat glider";
  if(TYPE_PATTERNS.perkoz.test(s))return "Perkoz two-seat glider";
  if(TYPE_PATTERNS.puchacz.test(s))return "Puchacz two-seat glider";
  return s||"Glider";
}

const results=document.getElementById("results");
const dateEl=document.getElementById("fdate");
const timeEl=document.getElementById("ftime");
let chosenType="";
const dayCache={};   // day string -> parsed JSON (or null after a 404)

// default the date to today (local) and cap it there; the past cap comes from the manifest.
function localISO(d){
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}
dateEl.value=localISO(new Date());
dateEl.max=dateEl.value;
fetch(DATA_BASE+"manifest.json",{cache:"no-cache"}).then(r=>r.ok?r.json():null).then(m=>{
  if(!m||!m.days||!m.days.length) return;
  const days=m.days.map(d=>typeof d==="string"?d:d.day).sort();
  dateEl.min=days[0];
}).catch(()=>{});

// glider / aeroplane / not-sure cards behave as a radio group
document.querySelectorAll("button.type").forEach(b=>b.addEventListener("click",()=>{
  chosenType=b.dataset.type;
  document.querySelectorAll("button.type").forEach(o=>{
    const on=o===b;
    o.classList.toggle("sel",on);
    o.setAttribute("aria-pressed",on?"true":"false");
  });
}));

function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function fmtTime(epoch){
  return new Date(epoch*1000).toLocaleTimeString("en-GB",{hour:"2-digit",minute:"2-digit"});
}
function fmtDur(s){
  const m=Math.round(s/60);
  return m>=60?Math.floor(m/60)+" h "+(m%60)+" min":m+" min";
}
function regAndCn(label){
  const m=/^([^\[]+?)\s*(?:\[([^\]]+)\])?$/.exec(label||"");
  return m?{reg:m[1].trim(),cn:(m[2]||"").trim()}:{reg:label||"",cn:""};
}

function showState(html){results.innerHTML='<div class="state">'+html+"</div>";}

async function loadDay(day){
  if(day in dayCache) return dayCache[day];
  let data=null;
  const r=await fetch(DATA_BASE+day+".json",{cache:"no-cache"});
  if(r.ok) data=await r.json();
  else if(r.status!==404) throw new Error("HTTP "+r.status);
  dayCache[day]=data;
  return data;
}

function matchFlights(data,t,type){
  const legend=data.legend||[];
  // Days published before the legend carried model strings have no .type anywhere;
  // for those, quietly skip the type filter rather than matching nothing.
  const hasTypes=legend.some(a=>a&&a.type);
  const out=[];
  for(const f of data.flights||[]){
    if(!f.samples||f.samples.length<2) continue;
    if(f.mk==="dr400") continue;   // the tug: trial flights are always in a glider
    const s=Date.parse(f.samples[0][0])/1000;
    const e=Date.parse(f.samples[f.samples.length-1][0])/1000;
    if(!(e-s>=MIN_DUR_S)) continue;
    if(type&&hasTypes){
      const ts=(legend[f.ai]||{}).type||"";
      if(!TYPE_PATTERNS[type]||!TYPE_PATTERNS[type].test(ts)) continue;
    }
    const airborneNearby=(s<=t+FWD_S)&&(e>=t-BACK_S);
    const takeoffClose=Math.abs(s-t)<=TOL_S;
    if(airborneNearby||takeoffClose) out.push({f:f,s:s,e:e,d:Math.abs(s-t)});
  }
  out.sort((a,b)=>a.d-b.d);
  return out;
}

function card(day,legend,m){
  const a=legend[m.f.ai]||{};
  const rc=regAndCn(a.label);
  const ident="day="+encodeURIComponent(day)
    +"&address="+encodeURIComponent(a.key||"")
    +"&t="+Math.round(m.s);
  const href="/replay?"+ident;
  // same day/address/t identifier drives the server-side KML export (only when this page
  // is served next to the DB - see CANDL).
  const dl=CANDL?'<a class="of-btn-secondary" href="/download?'+ident+'&fmt=kml">Download for Google Earth</a>':"";
  return '<div class="flight"><div class="bar" style="background:'+esc(m.f.color||"#81d5cc")+'"></div>'
    +'<div class="body"><div class="who">'
    +'<div class="reg">'+esc(rc.reg)+(rc.cn?'<span class="cn">'+esc(rc.cn)+"</span>":"")+"</div>"
    +'<div class="meta">'+esc(typeName(a))
    +' &middot; took off at <b>'+fmtTime(m.s)+"</b>"
    +' &middot; <b>'+fmtDur(m.e-m.s)+"</b> in the air</div>"
    +"</div>"
    +'<div class="acts"><a class="of-btn-primary watch" href="'+href+'">Watch this flight &rarr;</a>'
    +dl+"</div>"
    +"</div></div>";
}

async function find(){
  const day=dateEl.value, tm=timeEl.value;
  if(!day||!tm) return;
  showState('<span class="spin"></span>Looking up that day&rsquo;s flights&hellip;');
  // the entered time is LOCAL (what the visitor remembers); flight samples are UTC.
  const t=new Date(day+"T"+tm).getTime()/1000;
  let data;
  try{ data=await loadDay(day); }
  catch(err){
    showState("Sorry, we couldn&rsquo;t load the flight data just now. Please try again in a minute.");
    return;
  }
  if(!data){
    showState("<b>That day isn&rsquo;t available.</b><br>We keep roughly the last week of flying - "
      +"today&rsquo;s flights usually appear by the evening.");
    return;
  }
  const ms=matchFlights(data,t,chosenType);
  if(!ms.length){
    showState("<b>No flights found around then.</b><br>Try nudging the time by half an hour, "
      +"picking a different day, or choosing &ldquo;Not sure&rdquo; for the aircraft type.");
    return;
  }
  const noun=ms.length===1?"One flight was":ms.length+" flights were";
  results.innerHTML='<p class="rhead">'+noun+" in the air around "+esc(tm)
    +" - tap yours to watch it.</p>"+ms.map(m=>card(day,data.legend,m)).join("");
}

document.getElementById("finder").addEventListener("submit",e=>{e.preventDefault();find();});

// shareable/prefill links: /my-flights?date=YYYY-MM-DD&time=HH:MM&type=glider runs the search
(function(){
  const p=new URLSearchParams(location.search);
  const d=p.get("date"),tm=p.get("time"),ty=p.get("type");
  if(ty!==null){
    const b=document.querySelector('button.type[data-type="'+ty.replace(/[^a-z0-9]/g,"")+'"]');
    if(b) b.click();
  }
  if(d&&/^\d{4}-\d{2}-\d{2}$/.test(d)) dateEl.value=d;
  if(tm&&/^\d{2}:\d{2}$/.test(tm)) timeEl.value=tm;
  if(d&&tm) find();
})();
</script></body></html>"""


def _mine_page(data_dir: str) -> str:
    header = header_html(
        "Watch your flight",
        "Took a trial flight at Gransden? Find it below and watch it back in 3D. "
        "You can also download the trace and open it in Google Earth to explore your flight. "
        "All you need is the day and a rough take-off time.",
        logo_url=_logo_url(data_dir), club_name=CLUB_NAME)
    return (MINE_HTML.replace("__DATABASE__", MINE_DATA_BASE)
            .replace("__THEMECSS__", THEME_CSS)
            .replace("__CANDL__", "true" if data_dir else "false")
            .replace("__NAV__", nav_html("/my-flights"))
            .replace("__HEADER__", header))


def _home_page(status: dict, data_dir: str) -> str:
    """Landing page: a status strip + a grid of feature cards. Robust when empty."""
    # cheap, best-effort status line: airborne now + today's flight count.
    airborne = 0
    try:
        airborne = len(_live_feed(data_dir).get("aircraft", []))
    except Exception:
        pass
    flights_today = 0
    try:
        day = _today()
        lo = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        hi = lo + 86400
        yf = year_file(day.year, data_dir)
        if os.path.exists(yf):
            s = Store(yf)
            try:
                addrs = [r[0] for r in s.db.execute(
                    "SELECT DISTINCT address FROM fixes WHERE ts>=? AND ts<?", (lo, hi)).fetchall()]
                flights_today = sum(
                    len(segment(a, s.fixes_for(a, lo, hi), GRANSDEN)) for a in addrs)
            finally:
                s.close()
    except Exception:
        pass

    cards = [
        {"href": "/my-flights", "title": "Watch your flight",
         "desc": "Took a trial flight? Find it by date and rough take-off time, then watch it back in 3D."},
        {"href": "/live", "title": "Live",
         "desc": "Real-time 3D view of aircraft airborne right now."},
        {"href": "/live?demo=1", "title": "Tour mode",
         "desc": "Big-screen kiosk view: camera slowly orbits the field over live traffic."},
        {"href": "/replay", "title": "Daily replay",
         "desc": "3D replay of a day's flights. Pick any day with the date picker."},
        {"href": "/stats", "title": "Stats",
         "desc": "Collector health and today's capture statistics."},
    ]
    card_html = "".join(
        f'<a class="of-card card" href="{c["href"]}"><h2>{c["title"]}</h2>'
        f'<p>{c["desc"]}</p></a>'
        for c in cards)

    header = header_html(
        "ognflights",
        "Glider tracking for Gransden Lodge (Cambridge Gliding Centre).",
        logo_url=_logo_url(data_dir), club_name=CLUB_NAME)

    status_html = (
        f'<div class="strip"><a href="/live"><b>{airborne}</b> aircraft airborne now</a>'
        f'<span class="sep">|</span>'
        f'<a href="/replay"><b>{flights_today}</b> flights today</a></div>')

    # first-timer help: two compact how-to cards under the action grid.
    help_html = """
<div class="help">
  <div class="of-card helpcard">
    <h2>Moving around the 3D view</h2>
    <p class="sub">Works the same on the Live view and the Daily replay.</p>
    <ul>
      <li><b>Left-click and drag</b> to pan and spin the view around.</li>
      <li><b>Scroll the mouse wheel</b> to zoom in and out. <b>Right-click and drag</b>
          up or down also zooms, or pinch on a trackpad.</li>
      <li><b>Hold Ctrl and left-click and drag</b> (or <b>middle-click and drag</b>)
          to tilt the camera and look around the aircraft.</li>
    </ul>
  </div>
  <div class="of-card helpcard">
    <h2>Open your flight in Google Earth</h2>
    <p class="sub">Take your flight home as a 3D track you can explore.</p>
    <ul>
      <li>Install the free <b>Google Earth Pro</b> desktop app for Windows or Mac from
          google.com/earth, or use Google Earth on the web at earth.google.com.</li>
      <li>Click <b>Download for Google Earth</b> on your flight to save the .kml file.</li>
      <li>In the desktop app choose <b>File &gt; Open</b> and pick the .kml. On the web
          choose <b>New project &gt; Import KML file from computer</b>.</li>
      <li>Your flight path appears as a 3D track line. <b>Double-click</b> it to fly to it,
          then tilt and zoom around it with the same mouse controls as above.</li>
    </ul>
  </div>
</div>"""

    return f"""<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ognflights - Gransden</title>
<style>{THEME_CSS}
.strip{{background:var(--panel);border:1px solid var(--line);border-radius:999px;
padding:.6rem 1.3rem;margin:0 auto 1.6rem;font-size:.95rem;width:max-content;max-width:100%}}
.strip a{{color:var(--text);text-decoration:none}} .strip a:hover{{color:var(--blue)}}
.strip b{{color:var(--accent)}}
.strip .sep{{color:var(--line);margin:0 .8rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1rem}}
.card h2{{font-size:1.1rem;margin:0 0 .35rem;color:var(--text)}}
.card p{{margin:0;color:var(--dim);font-size:.92rem}}
.help{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:1rem;
margin-top:1.4rem;align-items:start}}
.helpcard h2{{font-size:1.05rem;margin:0 0 .2rem;color:var(--text)}}
.helpcard .sub{{margin:0 0 .6rem;color:var(--faint);font-size:.85rem}}
.helpcard ul{{margin:0;padding-left:1.15rem;color:var(--dim);font-size:.9rem}}
.helpcard li{{margin:.4rem 0}}
.helpcard b{{color:var(--blue);font-weight:600}}
</style></head>
<body class="of-body"><div class="of-wrap">
{nav_html("/")}
{header}
{status_html}
<div class="grid">{card_html}</div>
{help_html}
<p class="of-foot">Data from the Open Glider Network. Times are UTC.</p>
</div></body></html>"""


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
    ll = status.get("last_line")
    out["last_line_age_s"] = int(now - ll) if ll else None
    out["publish"] = status.get("publish", {"enabled": False})
    out["data_dir"] = data_dir
    out["_now"] = now
    out["days"] = _days_with_flights(data_dir)
    return out


# --- component health: judge each subsystem independently -------------------------------
LINK_STALE_S = 120       # no line from APRS-IS for this long => backend link unhealthy
TRAFFIC_QUIET_S = 300    # informational: "quiet" once no aircraft beacon for this long

_STATE_RANK = {"info": 0, "ok": 1, "warn": 2, "bad": 3}
_STATE_COLOR = {"ok": "var(--ok)", "warn": "var(--warn)",
                "bad": "var(--bad)", "info": "var(--faint)"}


def _health_components(st: dict) -> list:
    """Break the collector's state into independent, individually-judged components.

    Each is {name, state, detail}. state is ok/warn/bad, or `info` for signals that
    are reported but never mark the system unhealthy (a quiet sky is normal, not a
    fault). Overall health = no component is `bad`.
    """
    comps = []

    # The dashboard itself: if this code is running, the web server answered.
    comps.append({"name": "Dashboard", "state": "ok", "detail": "web server responding"})

    # Link to the OGN backend (APRS-IS). Proven live by ANY line incl. keepalives, so it
    # is independent of whether aircraft happen to be flying.
    ll = st["last_line_age_s"]
    if st["connected"] and ll is not None and ll < LINK_STALE_S:
        comps.append({"name": "OGN backend link", "state": "ok",
                      "detail": f"connected to APRS-IS, last data {ll}s ago"})
    else:
        if not st["connected"]:
            detail = "not connected to APRS-IS"
        elif ll is None:
            detail = "connected, but no data received yet"
        else:
            detail = f"no data from APRS-IS for {ll}s (link may be down)"
        comps.append({"name": "OGN backend link", "state": "bad", "detail": detail})

    # Aircraft traffic in range: informational. A quiet sky is expected, not a fault.
    ba = st["last_beacon_age_s"]
    foll = st["following"]
    if ba is None:
        detail = "quiet - no aircraft in range"
    elif ba < TRAFFIC_QUIET_S or foll:
        detail = f"{foll} following, last beacon {ba}s ago"
    else:
        detail = f"quiet - last aircraft {ba}s ago"
    comps.append({"name": "Aircraft in range", "state": "info", "detail": detail})

    # Storage: can we persist fixes? The data dir must be writable.
    if os.access(st["data_dir"], os.W_OK):
        comps.append({"name": "Storage", "state": "ok",
                      "detail": f"{st['db_bytes']/1_048_576:.1f} MB, writable"})
    else:
        comps.append({"name": "Storage", "state": "bad",
                      "detail": f"data dir not writable: {st['data_dir']}"})

    # Publish worker: auxiliary. A failure is a `warn` (capture is unaffected), not `bad`.
    pub = st.get("publish", {"enabled": False})
    if not pub.get("enabled"):
        comps.append({"name": "Publish worker", "state": "info", "detail": "disabled"})
    else:
        interval = pub.get("interval_s", 3600)
        ts = pub.get("ts")
        if ts is None:
            comps.append({"name": "Publish worker", "state": "ok",
                          "detail": "enabled, first run pending"})
        else:
            age = int(st["_now"] - ts)
            if not pub.get("ok"):
                comps.append({"name": "Publish worker", "state": "warn",
                              "detail": f"last run failed {age}s ago: {pub.get('error') or 'unknown'}"})
            elif age > interval * 2:
                comps.append({"name": "Publish worker", "state": "warn",
                              "detail": f"last success {age}s ago (interval {interval}s)"})
            else:
                comps.append({"name": "Publish worker", "state": "ok",
                              "detail": f"last success {age}s ago"})
    return comps


def _overall_state(comps: list) -> str:
    """Worst state across components (info never worse than ok)."""
    worst = "ok"
    for c in comps:
        if _STATE_RANK[c["state"]] > _STATE_RANK[worst]:
            worst = c["state"]
    return worst


def _health_headline(comps: list, overall: str) -> str:
    """Short human summary for the status header."""
    if overall == "bad":
        bad = next(c for c in comps if c["state"] == "bad")
        return bad["detail"] if bad["name"] == "OGN backend link" else f"{bad['name']}: {bad['detail']}"
    traffic = next(c for c in comps if c["name"] == "Aircraft in range")
    base = "Healthy - link up, no aircraft in range" if traffic["detail"].startswith("quiet") \
        else "Healthy - capturing"
    if overall == "warn":
        warn = next(c for c in comps if c["state"] == "warn")
        base += f" ({warn['name'].lower()} needs a look)"
    return base


def _healthz_payload(status: dict, data_dir: str):
    """(http_code, json_str) for /healthz. 200 when nothing is `bad`, else 503."""
    st = _stats(status, data_dir)
    comps = _health_components(st)
    ok = not any(c["state"] == "bad" for c in comps)
    payload = {
        "ok": ok,
        "state": _overall_state(comps),
        "day": st["day"],
        "components": comps,
        "metrics": {
            "uptime_s": st["uptime_s"],
            "connected": st["connected"],
            "last_line_age_s": st["last_line_age_s"],
            "last_beacon_age_s": st["last_beacon_age_s"],
            "following": st["following"],
            "fixes_today": st["fixes_today"],
            "flights_today": st["flights_today"],
            "stored_session": st["stored_session"],
            "db_bytes": st["db_bytes"],
        },
    }
    return (200 if ok else 503), json.dumps(payload)


def _fmt_dur(s: int) -> str:
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m {s}s"


def _stats_html(st: dict, data_dir: str = "") -> str:
    comps = _health_components(st)
    overall = _overall_state(comps)
    dot = _STATE_COLOR[overall]
    headline = _health_headline(comps, overall)

    comp_rows = "".join(
        f'<tr><td class="cdot"><span class="dot" style="background:{_STATE_COLOR[c["state"]]}"></span></td>'
        f'<th>{c["name"]}</th><td>{c["detail"]}</td></tr>'
        for c in comps)

    beacon_age = "never" if st["last_beacon_age_s"] is None else f"{st['last_beacon_age_s']}s ago"
    line_age = "never" if st["last_line_age_s"] is None else f"{st['last_line_age_s']}s ago"
    rows = [
        ("Uptime", _fmt_dur(st["uptime_s"])),
        ("Last data (any)", line_age),
        ("Last aircraft beacon", beacon_age),
        ("Following now", st["following"]),
        ("Fixes today", f"{st['fixes_today']:,}"),
        ("Aircraft today", st["aircraft_today"]),
        ("Flights today", st["flights_today"]),
        ("Stored this session", f"{st['stored_session']:,}"),
        ("DB size", f"{st['db_bytes']/1_048_576:.1f} MB"),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    days = st.get("days", [])
    daylinks = ("".join(f'<a class="chip" href="/replay?day={d}">{d}</a>' for d in days)
                if days else '<span class="hint">none captured yet</span>')
    header = header_html(
        "Collector status",
        f'<span class="dot" style="background:{dot}"></span>'
        f'{headline} &middot; {st["day"]}',
        logo_url=_logo_url(data_dir) if data_dir else "", club_name=CLUB_NAME)
    return f"""<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10"><title>ognflights status</title>
<style>{THEME_CSS}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:-1px;margin-right:6px}}
table{{border-collapse:collapse;width:100%;font-size:.95rem}}
th,td{{text-align:left;padding:.45rem .3rem;border-bottom:1px solid var(--line)}}
tr:last-child th,tr:last-child td{{border-bottom:0}}
th{{color:var(--dim);font-weight:600;width:45%}}
table.comp th{{color:var(--text);width:38%}}
table.comp td.cdot{{width:20px;padding-right:0}}
table.comp td:last-child{{color:var(--dim)}}
h2{{font-size:1rem;margin:1.6rem 0 .6rem;text-align:center}}
.days{{text-align:center}}
.chip{{display:inline-block;margin:0 .2rem .45rem;padding:.25rem .7rem;font-size:.85rem;
background:var(--panel);border:1px solid var(--line);border-radius:999px;
color:var(--blue);text-decoration:none;transition:border-color .15s}}
.chip:hover{{border-color:var(--accent)}}
.hint{{color:var(--faint);font-size:.85rem}}
.actions{{text-align:center;margin-top:1rem}}
.actions a{{color:var(--blue);text-decoration:none;margin:0 .6rem}}
.actions a:hover{{text-decoration:underline}}
</style></head>
<body class="of-body"><div class="of-wrap narrow">
{nav_html("/stats")}
{header}
<h2>Components</h2>
<div class="of-card"><table class="comp">{comp_rows}</table></div>
<h2>Capture details</h2>
<div class="of-card"><table>{body}</table></div>
<p class="actions"><a href="/live">watch live &rarr;</a><a href="/replay">today's replay &rarr;</a>
<a href="/healthz">healthz &rarr;</a></p>
<h2>Days with flights</h2>
<div class="days">{daylinks}</div>
<p class="of-foot">Auto-refreshes every 10 s. A quiet sky (no aircraft in range) is normal and
does not mean anything is wrong. "Following now" = aircraft launched from the field
being tracked live.</p>
</div></body></html>"""


def _thermals_page(data_dir: str = "") -> str:
    """Standalone map of the cached thermal hotspots (no flight replay). Fetches
    /thermals.json and draws the shared drift-column layer."""
    lat, lon, elev = GRANSDEN.lat, GRANSDEN.lon, GRANSDEN.elevation_ft
    field = CLUB_NAME or "Gransden Lodge"
    nav = ('<div class="of-topbar"><b>Thermals</b>'
           '<a href="/">home</a><a href="/live">live</a><a href="/replay">replay</a>'
           '<a href="/stats">stats</a></div>')
    return f"""<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ognflights - thermals</title>
<script src="{CES}/Cesium.js"></script><link href="{CES}/Widgets/widgets.css" rel="stylesheet">
<style>{THEME_CSS}
html,body,#c{{margin:0;width:100%;height:100%;overflow:hidden;background:#000}}
#tinfo{{position:fixed;left:10px;bottom:10px;z-index:20;max-width:22rem;padding:.5rem .7rem;font-size:.8rem}}
</style></head><body class="of-body"><div id="c"></div>
{nav}
<div id="tinfo" class="of-panel"><b>Thermal hotspots</b> &middot; recurring climbs over the last 7 days.<br>
<span class="hint">Column colour = mean climb rate; the lean is the prevailing drift (base &rarr; top).
Label = mean climb / aircraft-days. <span id="tcount"></span></span></div>
<script>
Cesium.Ion.defaultAccessToken="";
{THERMALS_JS}
const v=new Cesium.Viewer("c",{{baseLayer:Cesium.ImageryLayer.fromProviderAsync(
 Cesium.ArcGisMapServerImageryProvider.fromUrl("https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer")),
 baseLayerPicker:false,geocoder:false,homeButton:false,navigationHelpButton:false,infoBox:false,
 selectionIndicator:false,animation:false,timeline:false,requestRenderMode:true}});
v.scene.globe.enableLighting=true;
v.entities.add({{position:Cesium.Cartesian3.fromDegrees({lon},{lat},0),
 point:{{pixelSize:9,color:Cesium.Color.YELLOW}},
 label:{{text:"{field}",font:"13px sans-serif",fillColor:Cesium.Color.YELLOW,
 pixelOffset:new Cesium.Cartesian2(0,-14)}}}});
v.camera.lookAt(Cesium.Cartesian3.fromDegrees({lon},{lat},0),
 new Cesium.HeadingPitchRange(0,Cesium.Math.toRadians(-45),20000));
v.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);   // centre on the field, then release control
fetch("/thermals.json").then(function(r){{return r.json();}}).then(function(d){{
 ognThermalLayer(v,d.hotspots,{elev}).show(true);
 document.getElementById("tcount").textContent=(d.hotspots&&d.hotspots.length)?
   (d.hotspots.length+" hotspots."):"none computed yet.";
 v.scene.requestRender();
}}).catch(function(e){{document.getElementById("tcount").textContent="no data yet.";}});
</script></body></html>"""


def make_handler(status, data_dir, replay_script, models_dir, hub):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _replay(self):
            q = urlparse(self.path).query
            day = _parse_day(q)
            params = parse_qs(q)
            addr = params.get("address", [None])[0]
            if addr and not _ADDR_RE.match(addr):
                addr = None
            # ?t= selects one flight client-side; echo it into the download link so the
            # KML matches exactly what the visitor is watching.
            tval = params.get("t", [None])[0]
            if tval and not _T_RE.match(tval):
                tval = None
            html = _render_replay(day, replay_script, data_dir, addr)
            # the no-flights fallback has no help overlay, so no reopen "?" button there
            nav = _nav_html(day, addr, _logo_url(data_dir), t=tval,
                            help_btn=html is not None)
            if html is None:
                label = "today" if day.date() == _today().date() else day.strftime("%Y-%m-%d")
                what = f"{addr} on {label}" if addr else label
                # standalone fallback page, so it needs the theme CSS inlined itself
                self._send(200, "<!DOCTYPE html><meta charset=utf-8>"
                           '<meta name="viewport" content="width=device-width,initial-scale=1">'
                           f"<style>{THEME_CSS}</style>"
                           "<body class='of-body'>" + nav +
                           "<div style='margin:7rem auto;max-width:32rem;text-align:center;padding:0 1rem'>"
                           f"<h1 style='font-size:1.4rem'>No flights stored for {what}.</h1>"
                           "<p style='color:var(--dim)'>Pick another day above, or "
                           "<a style='color:var(--blue)' href='/stats'>see status &rarr;</a> or "
                           "<a style='color:var(--blue)' href='/'>home &rarr;</a></p>"
                           "</div></body>")
            else:
                self._send(200, html.replace("</body>", nav + "</body>", 1))

        def _download(self):
            """GET /download?day=YYYY-MM-DD&address=<hex>&t=<flight sel>&fmt=kml|gpx|igc

            Serves ONE segmented flight as a file. Opens the year DB read-only (WAL-safe
            against the live collector), segments that aircraft's day, picks the flight
            the same way the replay's ?t= filter does, and streams it as an attachment.
            Clean 400/404s on bad input; never a stack trace."""
            plain = "text/plain; charset=utf-8"
            try:
                q = parse_qs(urlparse(self.path).query)
                day_s = q.get("day", [""])[0]
                addr = q.get("address", [""])[0]
                fmt = (q.get("fmt", ["kml"])[0] or "kml").lower()
                tval = q.get("t", [""])[0]
                if not _DAY_RE.match(day_s):
                    return self._send(400, "bad or missing ?day=YYYY-MM-DD", plain)
                if not addr or not _ADDR_RE.match(addr):
                    return self._send(400, "bad or missing ?address=<device id>", plain)
                if fmt not in export.WRITERS:
                    return self._send(400, "fmt must be one of: " + ", ".join(export.WRITERS), plain)
                day = datetime.strptime(day_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                try:
                    t = _parse_t(tval, day)
                except ValueError:
                    return self._send(400, "bad ?t= (epoch seconds or HH:MM UTC)", plain)
                yf = year_file(day.year, data_dir)
                if not os.path.exists(yf):
                    return self._send(404, f"no data stored for {day_s}", plain)
                s = Store(yf, read_only=True)
                try:
                    lo, hi = s.day_bounds(day)
                    flights = segment(addr, s.fixes_for(addr, lo, hi), GRANSDEN)
                    label, model = s.device_label(addr)
                finally:
                    s.close()
                if not flights:
                    return self._send(404, f"no flights for {addr} on {day_s}", plain)
                fl = _select_flight(flights, t)
                if fl is None:
                    return self._send(400, f"{label} flew {len(flights)} times on {day_s}: "
                                      "add ?t=<epoch seconds or HH:MM UTC> to pick one", plain)
                doc = export.WRITERS[fmt](fl, label, model)
                fname = export.filename(fl, label, fmt)
                self._send(200, doc, DL_CTYPES[fmt],
                           extra={"Content-Disposition": f'attachment; filename="{fname}"'})
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception:
                self._send(500, "sorry, that export failed", plain)

        def _stream(self):
            """Server-Sent Events: live fixes for followed aircraft, plus heartbeats."""
            q = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
                self.end_headers()
                if self.command == "HEAD":
                    return
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                last_beat = time.time()
                while True:
                    try:
                        ev = q.get(timeout=5)
                        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                        self.wfile.flush()
                    except queue.Empty:
                        pass
                    now = time.time()
                    if now - last_beat >= 15:
                        self.wfile.write(b":\n\n")   # heartbeat comment
                        self.wfile.flush()
                        last_beat = now
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away; fall through to unsubscribe
            finally:
                hub.unsubscribe(q)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/stats", "/status"):
                self._send(200, _stats_html(_stats(status, data_dir), data_dir))
            elif path == "/healthz":
                code, body = _healthz_payload(status, data_dir)
                self._send(code, body, "application/json; charset=utf-8")
            elif path == "/thermals":
                self._send(200, _thermals_page(data_dir))
            elif path == "/thermals.json":
                from . import thermals
                self._send(200, json.dumps({"hotspots": thermals.load_cached(data_dir)}),
                           "application/json; charset=utf-8")
            elif path == "/live.json":
                self._send(200, json.dumps(_live_feed(data_dir)),
                           "application/json; charset=utf-8")
            elif path == "/live.stream":
                self._stream()
            elif path == "/live":
                self._send(200, _live_page(data_dir))
            elif path == "/my-flights":
                self._send(200, _mine_page(data_dir))
            elif path.startswith("/branding/"):
                # club drop-in branding (e.g. logo.png) from the mounted data volume:
                # <data_dir>/branding/<file>. Same sanitising pattern as /models/.
                fn = os.path.basename(path)
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                fp = os.path.join(data_dir, "branding", fn)
                if ext in BRANDING_CTYPES and os.path.isfile(fp):
                    with open(fp, "rb") as fh:
                        self._send(200, fh.read(), BRANDING_CTYPES[ext])
                else:
                    self._send(404, "not found")
            elif path == "/download":
                self._download()
            elif path == "/calibrate":
                self._send(200, _calibrate_page())
            elif path == "/replay":
                self._replay()
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
                params = parse_qs(q)
                # backward-compat: old replay-on-root links keep working via redirect.
                if "day" in params or "address" in params:
                    self._redirect("/replay" + ("?" + q if q else ""))
                else:
                    self._send(200, _home_page(status, data_dir))
            else:
                self._send(404, "not found")

        do_HEAD = do_GET

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port, status, data_dir, replay_script, models_dir, hub):
    httpd = _Server(("", port), make_handler(status, data_dir, replay_script, models_dir, hub))
    httpd.serve_forever()
