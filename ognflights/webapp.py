"""Tiny stdlib web server for the collector container.

  /            -> landing page linking to Live / Daily replay / Stats
  /replay      -> all-gliders 3D replay for a day (?day=, ?address= for single aircraft)
  /live        -> real-time 3D view of currently-airborne aircraft (SSE-driven)
  /live.json   -> JSON feed of aircraft active in the last ~2 min (initial snapshot)
  /live.stream -> Server-Sent Events: one fix per followed aircraft as it arrives
  /stats,/status -> health + live capture statistics (auto-refreshing)
  /models/*.glb  -> aircraft models referenced by the replay page

Runs in a thread alongside the `watch` collector, sharing a status dict + a live hub.
"""
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


def live_color(address: str) -> str:
    """Stable palette colour for an aircraft, deterministic on its address so the
    initial /live.json snapshot and the /live.stream events always agree."""
    h = 0
    for ch in address:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PALETTE[h % len(PALETTE)]


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
    extra = (f'<a href="/replay?day={d}" style="color:#8cf;margin-left:8px">all gliders</a>'
             if address else '<a href="/" style="color:#8cf;margin-left:8px">home</a>'
                             ' <a href="/stats" style="color:#8cf;margin-left:8px">stats</a>')
    return (
        '<div style="position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:20;'
        'background:rgba(0,0,0,.6);color:#fff;padding:5px 9px;border-radius:6px;font:13px sans-serif">'
        f'<a href="/replay?day={prev}{q}" style="color:#8cf;text-decoration:none">&#9664;</a> '
        f'<input type="date" value="{d}" onchange="location=\'/replay?day=\'+this.value+\'{q}\'" '
        'style="font:13px sans-serif;background:#222;color:#fff;border:1px solid #555;border-radius:3px"> '
        f'<a href="/replay?day={nxt}{q}" style="color:#8cf;text-decoration:none">&#9654;</a>'
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
# The page paints an initial /live.json snapshot then follows /live.stream (SSE).
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
.hint{opacity:.6;font-size:11px}
#legend label{cursor:pointer}</style>
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

const GRACE_MS=60000;    // remove an aircraft this long after its last event
const MAX_TRAIL=600;     // bounded recent-points trail per aircraft
const ORIENT_MIN_M=30;   // walk back through the trail until at least this far behind
const ORIENT_STATIONARY_M=10; // below this displacement, keep the last-good heading (no spin)
const ac={};             // address -> {plane, trail, color, name, model, pts[], lastSeen, _ori}
let trailsOn=true;       // toggled by the "Trail" checkbox in the legend

// create-or-update an aircraft entity from a position (lon,lat,height_m)
function ensure(addr,name,color,model){
  let e=ac[addr];
  if(e){
    if(name){ e.name=name; e.plane.name=name; e.trail.name=name+" trail"; }
    return e;
  }
  const col=Cesium.Color.fromCssColorString(color);
  e=ac[addr]={color:color,name:name,model:model,pts:[],maxTs:0,_pitch:0};
  e.plane=viewer.entities.add({
    name:name,
    position:new Cesium.CallbackProperty(()=>e._pos,false),
    // nose-forward: driven by the smoothed lookback vector (see updateOrientation);
    // undefined until the aircraft has moved enough, then holds the last-good heading.
    orientation:new Cesium.CallbackProperty(()=>e._ori,false),
    model:{uri:MODELS[model]||MODELS.glider, minimumPixelSize:64, maximumScale:20000, scale:1,
      color:col, colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.5,
      silhouetteColor:col, silhouetteSize:1.5}
  });
  e.trail=viewer.entities.add({
    name:name+" trail", show:trailsOn,
    polyline:{positions:new Cesium.CallbackProperty(()=>e._trail,false),
      width:2, material:col.withAlpha(0.55)}
  });
  return e;
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
  const n=e.pts.length;
  if(n<2) return;
  const last=e.pts[n-1];
  const cur=Cesium.Cartesian3.fromDegrees(last[0],last[1],last[2]);
  const curTs=last[3];
  // HEADING baseline: walk back until at least ORIENT_MIN_M behind (else oldest point).
  // This short, responsive lookback sets which way the nose points.
  let lookback=null;
  for(let i=n-2;i>=0;i--){
    const p=e.pts[i];
    const c=Cesium.Cartesian3.fromDegrees(p[0],p[1],p[2]);
    lookback=c;
    if(Cesium.Cartesian3.distance(cur,c)>=ORIENT_MIN_M) break;
  }
  if(!lookback) return;
  const vel=Cesium.Cartesian3.subtract(cur,lookback,new Cesium.Cartesian3());
  if(Cesium.Cartesian3.magnitude(vel)<ORIENT_STATIONARY_M) return; // keep last-good
  // Orient the model straight along the track velocity: heading and pitch both simply
  // match the direction of travel (the simple, stable behaviour).
  const m=Cesium.Transforms.rotationMatrixFromPositionVelocity(cur,vel,Cesium.Ellipsoid.WGS84);
  const q=Cesium.Quaternion.fromRotationMatrix(m);
  // Guard: a near-vertical velocity makes the matrix degenerate, so fromRotationMatrix can
  // return a non-unit or NaN quaternion. Cesium builds the model matrix via
  // Matrix3.fromQuaternion WITHOUT normalising, so a non-unit quaternion SCALES the model
  // (it balloons in size). Normalise, and keep the last-good heading if anything is not finite.
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

// append one [lon,lat,height_m,ts] point, keeping the trail bounded
function pushPoint(e,pt){
  e.pts.push(pt);
  if(e.pts.length>MAX_TRAIL) e.pts.splice(0,e.pts.length-MAX_TRAIL);
  e._pos=Cesium.Cartesian3.fromDegrees(pt[0],pt[1],pt[2]);
  e._trail=trailPositions(e.pts);
  updateOrientation(e);
  e.lastSeen=Date.now();
  if(viewer.scene.requestRenderMode) viewer.scene.requestRender();
}

// initial snapshot from /live.json: paint whole recent tracks (does not duplicate)
function snapshot(a){
  const pts=a.points||[];
  if(!pts.length) return;
  const e=ensure(a.address,a.name,a.color,a.model);
  e.pts=pts.slice(-MAX_TRAIL);
  e.maxTs=a.last_ts||0;   // so streamed duplicates already in this snapshot are dropped
  const last=e.pts[e.pts.length-1];
  e._pos=Cesium.Cartesian3.fromDegrees(last[0],last[1],last[2]);
  e._trail=trailPositions(e.pts);
  updateOrientation(e);   // seed heading from the snapshot trail if it has moved enough
  e.lastSeen=Date.now();
}

// a single streamed fix event
function onEvent(ev){
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
  renderLegend();
}

function renderLegend(){
  const items=Object.values(ac);
  const rows=items.map(e=>`<div><span class="sw" style="background:${e.color}"></span>${e.name}</div>`);
  const n=items.length;
  const head=`<b>Live - Gransden</b><br><span class="hint">${n} aircraft airborne</span>`;
  const ctrl=`<div style="margin:4px 0;user-select:none"><label>`
    +`<input type="checkbox" id="traillbl"${trailsOn?" checked":""}> Trail</label></div>`;
  document.getElementById("legend").innerHTML=head+ctrl+(rows.length?rows.join(""):"");
  const cb=document.getElementById("traillbl");
  if(cb) cb.addEventListener("change",e=>{ trailsOn=e.target.checked; applyTrails(); });
}

// 1) paint the current picture once, then 2) open the live event stream.
async function start(){
  try{
    const r=await fetch("live.json",{cache:"no-store"});
    const d=await r.json();
    (d.aircraft||[]).forEach(snapshot);
  }catch(e){ /* no snapshot; the stream will fill in */ }
  renderLegend();
  const es=new EventSource("live.stream");
  es.onmessage=function(m){
    try{ onEvent(JSON.parse(m.data)); }catch(e){}
  };
  // EventSource auto-reconnects; ensure()/snapshot() are idempotent so no dupes.
}
start();
setInterval(prune, 5000);

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


def _live_page() -> str:
    models = {k: f"models/{v}" for k, v in MODEL_FILES.items()}
    return LIVE_HTML.replace("__CES__", LIVE_CES).replace("__MODELS__", json.dumps(models))


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
        {"href": "/live", "title": "Live",
         "desc": "Real-time 3D view of aircraft airborne right now."},
        {"href": "/replay", "title": "Daily replay",
         "desc": "3D replay of a day's flights. Pick any day with the date picker."},
        {"href": "/stats", "title": "Stats",
         "desc": "Collector health and today's capture statistics."},
    ]
    card_html = "".join(
        f'<a class="card" href="{c["href"]}"><h2>{c["title"]}</h2>'
        f'<p>{c["desc"]}</p></a>'
        for c in cards)

    noun = "aircraft airborne now"
    status_html = (
        f'<div class="strip"><a href="/live"><b>{airborne}</b> {noun}</a>'
        f'<span class="sep">|</span>'
        f'<a href="/replay"><b>{flights_today}</b> flights today</a></div>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ognflights - Gransden</title>
<style>
:root{{color-scheme:dark}}
body{{font:16px/1.5 system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3;
min-height:100vh;display:flex;flex-direction:column;align-items:center}}
.wrap{{max-width:760px;width:100%;padding:2.5rem 1.25rem 3rem;box-sizing:border-box}}
h1{{font-size:1.7rem;margin:.2rem 0 .1rem}}
.sub{{color:#8b949e;margin:0 0 1.4rem}}
.strip{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:.7rem 1rem;
margin-bottom:1.6rem;font-size:.95rem}}
.strip a{{color:#e6edf3;text-decoration:none}} .strip b{{color:#58a6ff}}
.strip .sep{{color:#30363d;margin:0 .8rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1rem}}
.card{{display:block;background:#161b22;border:1px solid #30363d;border-radius:10px;
padding:1.1rem 1.2rem;text-decoration:none;color:inherit;transition:border-color .15s,background .15s}}
.card:hover{{border-color:#58a6ff;background:#1b222b}}
.card h2{{font-size:1.15rem;margin:0 0 .35rem;color:#58a6ff}}
.card p{{margin:0;color:#8b949e;font-size:.92rem}}
.foot{{color:#484f58;font-size:.8rem;margin-top:2rem}}
</style></head>
<body><div class="wrap">
<h1>ognflights</h1>
<p class="sub">Glider tracking for Gransden Lodge (Cambridge Gliding Centre).</p>
{status_html}
<div class="grid">{card_html}</div>
<p class="foot">Data from the Open Glider Network. Times are UTC.</p>
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
    daylinks = ("".join(f'<li><a href="/replay?day={d}">{d}</a></li>' for d in days)
                if days else "<li class='hint'>none captured yet</li>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><title>ognflights status</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;max-width:520px;margin:2.5rem auto;padding:0 1rem;color:#222}}
h1{{font-size:1.3rem}} .dot{{display:inline-block;width:12px;height:12px;border-radius:50%;background:{dot};vertical-align:middle;margin-right:8px}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}} th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
th{{color:#666;font-weight:600;width:45%}} a{{color:#1e6fd0}} .hint{{color:#999;font-size:.85rem}} ul{{columns:2;padding-left:1.1rem}}</style></head>
<body><h1><span class="dot"></span>ognflights collector, {st['day']}</h1>
<table>{body}</table>
<p><a href="/">home</a> &middot; <a href="/replay">today's all-gliders replay &rarr;</a></p>
<h2 style="font-size:1rem">Days with flights</h2>
<ul>{daylinks}</ul>
<p class="hint">auto-refreshes every 10s. "Following now" = aircraft launched from the field being tracked live.</p>
</body></html>"""


def make_handler(status, data_dir, replay_script, models_dir, hub):
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

        def _redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _replay(self):
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
                           "<p>Pick another day above, or <a style='color:#8cf' href='/stats'>see status &rarr;</a>, "
                           "or <a style='color:#8cf' href='/'>home &rarr;</a></p>"
                           "</div></body>")
            else:
                self._send(200, html.replace("</body>", nav + "</body>", 1))

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
                self._send(200, _stats_html(_stats(status, data_dir)))
            elif path == "/live.json":
                self._send(200, json.dumps(_live_feed(data_dir)),
                           "application/json; charset=utf-8")
            elif path == "/live.stream":
                self._stream()
            elif path == "/live":
                self._send(200, _live_page())
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
