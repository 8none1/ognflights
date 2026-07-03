#!/usr/bin/env python3
"""Generate a self-contained Cesium 3D flight-replay HTML.

Examples:
  # my 4 KFY flights
  python3 replay/make_replay.py --out out/G-CKFY_2026-07-01_replay.html \
      --day 2026-07-01 --title "G-CKFY 2026-07-01 (my flights)" \
      --reg "G-CKFY:1,2,3,6" --mult 30

  # every glider/tug that flew
  python3 replay/make_replay.py --out out/all-gliders_2026-07-01_replay.html \
      --day 2026-07-01 --title "All gliders 2026-07-01" --gliders --mult 60

The opening camera is set with --home "lon,lat,height,heading,pitch" (degrees, metres).
Each aircraft gets a 3D model chosen from its CGC model string (gliders vs DR-400 tugs, etc).
In the viewer: C copies the current camera as --home; number keys pick a model and [ ] yaw it
(logged so the value can be baked in via --yaw "glider=0,dr400=90").
"""
import argparse, json, math, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ognflights.config import GRANSDEN
from ognflights.flights import segment
from ognflights.store import Store, store_for_day

FT_TO_M = 0.3048
GLIDERISH = {"glider", "tow", "motorglider"}
CES = "https://cesium.com/downloads/cesiumjs/releases/1.143/Build/Cesium"
PALETTE = ["#1e90ff", "#32cd32", "#ff4500", "#ff00ff", "#00ffff", "#ffd700", "#ff1493",
           "#7cfc00", "#ff8c00", "#9370db", "#00fa9a", "#dc143c", "#40e0d0", "#ffa07a"]

# Per-type 3D models (glb files in replay/models/). "match" = substrings of the CGC model string.
# Order matters: first match wins; DEFAULT_MODEL is the fallback. All from FlightAirMap (GPLv2).
DEFAULT_MODEL = "glider"
MODELS = {
    "dr400":  dict(file="DR40.glb", label="DR-400 (tug)", yaw=0.0,
                   match=["dr-400", "dr400", "dr 400", "robin"]),
    "glider": dict(file="AS21.glb", label="glider (ASK-21)", yaw=0.0, match=[]),
}


def classify_model(model_str, ac_type):
    s = (model_str or "").lower()
    for key, spec in MODELS.items():
        if key != DEFAULT_MODEL and any(m in s for m in spec["match"]):
            return key
    if ac_type == "tow":        # tugs get the powered model even if the string is odd
        return "dr400"
    return DEFAULT_MODEL


# default opening view: ~10 km south of Gransden, 6 km up, looking north, tilted down 30 deg
DEFAULT_HOME = dict(lon=GRANSDEN.lon, lat=GRANSDEN.lat - 0.09, height=6000.0, heading=0.0, pitch=-30.0)

TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__ replay</title>
<script src="__CES__/Cesium.js"></script>
<link href="__CES__/Widgets/widgets.css" rel="stylesheet">
<style>html,body,#c{margin:0;padding:0;width:100%;height:100%;overflow:hidden}
#legend{position:absolute;top:8px;left:8px;z-index:10;background:rgba(0,0,0,.6);color:#fff;
font:12px sans-serif;padding:8px 10px;border-radius:6px;max-height:90vh;overflow:auto}
#legend b{font-size:14px} .hint{opacity:.6;font-size:11px}
.sw{display:inline-block;width:12px;height:12px;margin-right:6px;border-radius:2px;vertical-align:middle}</style>
</head><body><div id="c"></div><div id="legend"></div>
<script>
const DATA=__PAYLOAD__;       // title, flights, legend, models keyed to {url,label}
const HOME=__HOME__;
const MYAW=__MYAW__;          // {modelKey: radians} yaw offset per model; tune with number keys + [ ]
const TRAILMODE="__TRAILMODE__";
const SPEEDCOL=__SPEEDCOL__;
Cesium.Ion.defaultAccessToken="";
const viewer=new Cesium.Viewer("c",{
  baseLayer:Cesium.ImageryLayer.fromProviderAsync(Cesium.ArcGisMapServerImageryProvider.fromUrl(
    "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer")),
  baseLayerPicker:false,geocoder:false,homeButton:false,navigationHelpButton:false,
  infoBox:false,selectionIndicator:false,animation:true,timeline:true,
  requestRenderMode:true});  // only redraw when something changes (idle/paused = ~0 CPU)
viewer.scene.globe.enableLighting=true;
// transparent place-names / boundaries overlay (toggled off by default).
// NB: imageryLayers.add() returns void, so keep the layer ref from fromProviderAsync.
const labelLayer=Cesium.ImageryLayer.fromProviderAsync(
  Cesium.ArcGisMapServerImageryProvider.fromUrl(
    "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer"));
viewer.imageryLayers.add(labelLayer);
labelLayer.show=false;
let tmin=null,tmax=null;
const trails=[], planes=[];
const speedInstances={};   // aircraft index -> [GeometryInstance]; batched into one primitive per aircraft
const speedPrims={};       // aircraft index -> Cesium.Primitive
const SPD_LO=30, SPD_HI=110;   // knots mapped across the speed colour ramp
function speedColor(kt){
  const t=Math.max(0,Math.min(1,(kt-SPD_LO)/(SPD_HI-SPD_LO)));
  const st=[[0.12,0.12,0.5],[0.0,0.8,0.5],[1.0,1.0,0.5]];  // dim blue -> green -> bright yellow
  const seg=t*2, i=Math.min(1,Math.floor(seg)), f=seg-i, a=st[i], b=st[i+1]||st[i];
  return new Cesium.Color(a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f, a[2]+(b[2]-a[2])*f, 0.95);
}
DATA.flights.forEach(fl=>{
  const pos=new Cesium.SampledPositionProperty();
  fl.samples.forEach(s=>pos.addSample(Cesium.JulianDate.fromIso8601(s[0]),
      Cesium.Cartesian3.fromDegrees(s[1],s[2],s[3])));
  pos.setInterpolationOptions({interpolationDegree:2,interpolationAlgorithm:Cesium.LagrangePolynomialApproximation});
  const t0=Cesium.JulianDate.fromIso8601(fl.samples[0][0]);
  const t1=Cesium.JulianDate.fromIso8601(fl.samples[fl.samples.length-1][0]);
  if(!tmin||Cesium.JulianDate.lessThan(t0,tmin))tmin=t0;
  if(!tmax||Cesium.JulianDate.greaterThan(t1,tmax))tmax=t1;
  const col=Cesium.Color.fromCssColorString(fl.color);
  const velProp=new Cesium.VelocityOrientationProperty(pos);
  const mk=fl.mk;
  planes.push(viewer.entities.add({
    name:fl.name,
    availability:new Cesium.TimeIntervalCollection([new Cesium.TimeInterval({start:t0,stop:t1})]),
    position:pos,
    orientation:new Cesium.CallbackProperty(function(time,result){
      const base=velProp.getValue(time,result);
      if(!base) return undefined;
      const corr=Cesium.Quaternion.fromAxisAngle(Cesium.Cartesian3.UNIT_Z, MYAW[mk]||0);
      return Cesium.Quaternion.multiply(base, corr, base);
    }, false),
    model:{uri:DATA.models[mk].url, minimumPixelSize:64, maximumScale:20000, scale:1,
      color:col, colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.5,
      silhouetteColor:col, silhouetteSize:1.5},
    path:{resolution:__PATHRES__, material:col, width:3, leadTime:0, trailTime:100000}
  }));
  const flat=fl.samples.flatMap(s=>[s[1],s[2],s[3]]);
  trails.push(viewer.entities.add({name:fl.name+" trail",
    polyline:{positions:Cesium.Cartesian3.fromDegreesArrayHeights(flat),
    width:1, material:col.withAlpha(0.35)}}));
  (speedInstances[fl.ai]=speedInstances[fl.ai]||[]).push(new Cesium.GeometryInstance({geometry:new Cesium.PolylineGeometry({
      positions:Cesium.Cartesian3.fromDegreesArrayHeights(flat), width:3,
      vertexFormat:Cesium.PolylineColorAppearance.VERTEX_FORMAT,
      colors:(fl.spd||[]).map(speedColor), colorsPerVertex:true, arcType:Cesium.ArcType.NONE})}));
});
// one batched speed-trail primitive per aircraft: draw calls drop from ~1/flight to ~1/aircraft,
// while still allowing per-aircraft show/hide.
Object.keys(speedInstances).forEach(ai=>{
  speedPrims[ai]=viewer.scene.primitives.add(new Cesium.Primitive({
    geometryInstances:speedInstances[ai],
    appearance:new Cesium.PolylineColorAppearance({translucent:true}), show:false}));
});
const leg=[`<b>${DATA.title}</b><br><span class="hint">${DATA.flights.length} flights, ${DATA.legend.length} aircraft</span><br>`,
  `<div style="margin:4px 0;user-select:none">trails:
    <label><input type="radio" name="tm" value="full"> full</label>
    <label><input type="radio" name="tm" value="active"> active</label>
    <label><input type="radio" name="tm" value="off"> off</label>
    <label style="cursor:pointer;margin-left:8px"><input type="checkbox" id="speedcol"> by speed</label>
    <div id="speedscale" style="display:none;margin:3px 0">
      <span style="display:inline-block;width:130px;height:9px;border-radius:2px;background:linear-gradient(90deg,#1f1f80,#00cc80,#ffff80)"></span>
      <br><span class="hint">slow ${SPD_LO} &rarr; ${SPD_HI} kt fast</span></div>
    <br><label style="cursor:pointer"><input type="checkbox" id="nightsky" checked> night sky</label>
    <label style="cursor:pointer;margin-left:8px"><input type="checkbox" id="placenames"> place names</label>
    <br><button id="resetview" style="cursor:pointer;margin-top:4px">reset view</button></div>`];
if(DATA.legend.length>1) leg.push(`<div class="hint" style="margin-top:4px">show: <a href="#" id="acAll" style="color:#8cf">all</a> / <a href="#" id="acNone" style="color:#8cf">none</a></div>`);
DATA.legend.forEach((a,i)=>leg.push(`<label style="cursor:pointer;display:block"><input type="checkbox" class="acft" data-ai="${i}" checked> <span class="sw" style="background:${a.color}"></span>${a.label} (${a.n})</label>`));
leg.push(`<div id="models" class="hint" style="margin-top:6px"></div>`);
leg.push(`<div class="hint" style="margin-top:6px">3D models: <a style="color:#8cf" href="https://github.com/Ysurac/FlightAirMap-3dmodels">FlightAirMap</a> (GPLv2)</div>`);
document.getElementById("legend").innerHTML=leg.join("");

// per-aircraft visibility (index matches DATA.legend); each flight knows its aircraft via fl.ai
const aircraftOn=DATA.legend.map(()=>true);
const flightAi=DATA.flights.map(f=>f.ai);
// trail modes: full = whole track; active = flown tail of airborne gliders; off = none.
// "by speed" recolours the full track (slow=dim blue, fast=bright yellow); active tail stays per-aircraft.
function applyTrails(){
  const m=document.querySelector('input[name=tm]:checked').value;
  const speed=document.getElementById("speedcol").checked;
  planes.forEach((p,i)=>{const on=aircraftOn[flightAi[i]]; p.show=on; p.path.show=on && m!=="off";});
  trails.forEach((t,i)=>{t.show=aircraftOn[flightAi[i]] && m==="full" && !speed;});
  Object.keys(speedPrims).forEach(ai=>{speedPrims[ai].show=aircraftOn[+ai] && m==="full" && speed;});
  document.getElementById("speedscale").style.display=(m==="full"&&speed)?"block":"none";
  viewer.scene.requestRender();   // requestRenderMode is on, so ask for a redraw after toggling
}
document.querySelectorAll('input.acft').forEach(cb=>cb.addEventListener("change",e=>{
  aircraftOn[+e.target.dataset.ai]=e.target.checked; applyTrails();
}));
function setAllAircraft(on){
  aircraftOn.fill(on);
  document.querySelectorAll('input.acft').forEach(cb=>{cb.checked=on;});
  applyTrails();
}
if(document.getElementById("acAll")) document.getElementById("acAll").addEventListener("click",e=>{e.preventDefault();setAllAircraft(true);});
if(document.getElementById("acNone")) document.getElementById("acNone").addEventListener("click",e=>{e.preventDefault();setAllAircraft(false);});
document.querySelectorAll('input[name=tm]').forEach(r=>r.addEventListener("change",applyTrails));
document.getElementById("speedcol").addEventListener("change",function(){
  if(this.checked) document.querySelector('input[name=tm][value="full"]').checked=true;  // speed colours the full track
  applyTrails();
});
document.querySelector('input[name=tm][value="'+TRAILMODE+'"]').checked=true;
document.getElementById("speedcol").checked=SPEEDCOL;
applyTrails();

// night sky: drop the bright atmosphere + ground haze, revealing the star skybox (ground stays lit)
function setNight(on){
  viewer.scene.skyAtmosphere.show=!on;
  viewer.scene.globe.showGroundAtmosphere=!on;
  viewer.scene.backgroundColor=Cesium.Color.BLACK;
  viewer.scene.requestRender();
}
document.getElementById("nightsky").addEventListener("change",e=>setNight(e.target.checked));
setNight(true);

// place names overlay + reset-view button
document.getElementById("placenames").addEventListener("change",e=>{labelLayer.show=e.target.checked; viewer.scene.requestRender();});
document.getElementById("resetview").addEventListener("click",function(){goHome(); this.blur();});

// hover tooltip: show the aircraft name/registration under the cursor
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

// opening camera (position = X/Y/Z, angle = heading/pitch)
function goHome(){
  viewer.camera.setView({
    destination:Cesium.Cartesian3.fromDegrees(HOME.lon,HOME.lat,HOME.height),
    orientation:{heading:Cesium.Math.toRadians(HOME.heading),
                 pitch:Cesium.Math.toRadians(HOME.pitch),
                 roll:Cesium.Math.toRadians(HOME.roll||0)}
  });
}
goHome();

// model yaw tuning: number keys pick a model, [ / ] rotate its nose; value logged for --yaw
const MK=Object.keys(DATA.models);
let selM=MK[0];
function renderModels(){
  document.getElementById("models").innerHTML="models (press 1-9 to pick, [ ] to yaw):<br>"+
    MK.map((k,i)=>`<span style="${k===selM?'color:#8cf;font-weight:bold':''}">${i+1}. ${DATA.models[k].label}: ${Math.round(Cesium.Math.toDegrees(MYAW[k]||0))}&deg;</span>`).join("<br>");
}
renderModels();
window.addEventListener("keydown",e=>{
  if(/^[1-9]$/.test(e.key)){const i=+e.key-1; if(i<MK.length){selM=MK[i]; renderModels();} return;}
  if(e.key==="["||e.key==="]"){
    MYAW[selM]=(MYAW[selM]||0)+(e.key==="]"?1:-1)*Cesium.Math.toRadians(5);
    renderModels();
    console.log(`--yaw "${MK.map(k=>k+"="+Math.round(Cesium.Math.toDegrees(MYAW[k]||0))).join(",")}"`);
  }
});

// press C to capture the current view as a --home value
window.addEventListener("keydown",e=>{
  if(e.key!=="c"&&e.key!=="C")return;
  const c=Cesium.Cartographic.fromCartesian(viewer.camera.positionWC);
  const v=[Cesium.Math.toDegrees(c.longitude).toFixed(6),
           Cesium.Math.toDegrees(c.latitude).toFixed(6),
           c.height.toFixed(0),
           Cesium.Math.toDegrees(viewer.camera.heading).toFixed(1),
           Cesium.Math.toDegrees(viewer.camera.pitch).toFixed(1)].join(",");
  console.log("--home \""+v+"\"");
  if(navigator.clipboard)navigator.clipboard.writeText(v);
});

viewer.clock.startTime=tmin.clone();viewer.clock.stopTime=tmax.clone();viewer.clock.currentTime=tmin.clone();
viewer.clock.clockRange=Cesium.ClockRange.LOOP_STOP;viewer.clock.multiplier=__MULT__;viewer.clock.shouldAnimate=true;
viewer.timeline.zoomTo(tmin,tmax);
</script></body></html>"""


def ground_speeds_kt(fixes):
    """Per-fix ground speed (knots) from consecutive positions, lightly smoothed.
    CGC rarely supplies speed, so we derive it: central-difference distance / time."""
    import math
    n = len(fixes)
    if n < 2:
        return [0.0] * n
    R = 6371000.0
    raw = [0.0] * n
    for i in range(n):
        a = fixes[max(0, i - 1)]; b = fixes[min(n - 1, i + 1)]
        dt = b.ts - a.ts
        if dt <= 0:
            raw[i] = raw[i - 1] if i else 0.0
            continue
        p1, p2 = math.radians(a.lat), math.radians(b.lat)
        dphi = math.radians(b.lat - a.lat); dl = math.radians(b.lon - a.lon)
        h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        d = 2 * R * math.asin(min(1.0, math.sqrt(h)))
        raw[i] = d / dt * 1.94384  # m/s -> kt
    out = [0.0] * n
    for i in range(n):
        a, b = max(0, i - 2), min(n, i + 3)
        out[i] = sum(raw[a:b]) / (b - a)
    return out


def _rdp_keep(pts, eps):
    """Ramer-Douglas-Peucker: indices to keep so the polyline stays within `eps` metres
    of the original. Drops redundant points on straight runs, preserves turns. Iterative."""
    n = len(pts)
    if n < 3 or eps <= 0:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        x1, y1, z1 = pts[s]; x2, y2, z2 = pts[e]
        dx, dy, dz = x2 - x1, y2 - y1, z2 - z1
        seg2 = dx * dx + dy * dy + dz * dz
        dmax, idx = 0.0, -1
        for i in range(s + 1, e):
            x, y, z = pts[i]
            if seg2 == 0:
                d = math.dist(pts[i], pts[s])
            else:
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy + (z - z1) * dz) / seg2))
                d = math.dist((x, y, z), (x1 + t * dx, y1 + t * dy, z1 + t * dz))
            if d > dmax:
                dmax, idx = d, i
        if idx != -1 and dmax > eps:
            keep[idx] = True
            stack.append((s, idx)); stack.append((idx, e))
    return [i for i in range(n) if keep[i]]


def collect(store, day, reg_spec, gliders, simplify=0.0):
    lo, hi = store.day_bounds(day)
    flights, legend, ai = [], [], 0
    want_reg, want_idx = None, None
    if reg_spec:
        if ":" in reg_spec:
            want_reg, idx = reg_spec.split(":", 1)
            want_idx = {int(x) for x in idx.split(",")}
        else:
            want_reg = reg_spec
    for addr, label, ac_type, _ in store.addresses_on_day(day):
        if want_reg is not None and not label.startswith(want_reg):
            continue
        if want_reg is None and gliders and ac_type not in GLIDERISH:
            continue
        fls = segment(addr, store.fixes_for(addr, lo, hi), GRANSDEN)
        if not fls:
            continue
        _, model_str = store.device_label(addr)
        mk = classify_model(model_str, ac_type)
        col = PALETTE[ai % len(PALETTE)]; ai += 1
        aidx = len(legend)   # index this aircraft will take in the legend (for per-aircraft toggles)
        used = 0
        for i, fl in enumerate(fls, 1):
            if want_idx and i not in want_idx:
                continue
            t0 = datetime.fromtimestamp(fl.start, tz=timezone.utc).strftime("%H:%M")
            # Height ABOVE THE AIRFIELD, not MSL: the replay has no terrain, so Cesium draws
            # the ground at the sea-level ellipsoid. Plotting MSL would float every aircraft
            # ~field-elevation too high. Subtract field elevation so ground level sits on the map.
            samples = [[datetime.fromtimestamp(f.ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        round(f.lon, 6), round(f.lat, 6),
                        round(max(0.0, (f.alt_ft - GRANSDEN.elevation_ft) * FT_TO_M), 1)]
                       for f in fl.fixes]
            spd = [round(v) for v in ground_speeds_kt(fl.fixes)]
            if simplify and len(samples) > 2:
                clat = math.cos(math.radians(GRANSDEN.lat))
                pts = [(s[1] * 111320.0 * clat, s[2] * 111320.0, s[3]) for s in samples]
                keep = _rdp_keep(pts, simplify)
                samples = [samples[i] for i in keep]
                spd = [spd[i] for i in keep]
            flights.append({"name": f"{label} F{i} {t0}Z", "color": col, "mk": mk,
                            "ai": aidx, "samples": samples, "spd": spd})
            used += 1
        if used:
            legend.append({"label": label, "color": col, "n": used})
    return flights, legend


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--day", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--reg", help='e.g. "G-CKFY" or "G-CKFY:1,2,3,6"')
    p.add_argument("--gliders", action="store_true", help="all glider/tug types")
    p.add_argument("--mult", type=int, default=60, help="playback speed multiplier")
    p.add_argument("--simplify", type=float, default=0.0,
                   help="RDP trail simplification tolerance in metres (0 = full fidelity); "
                        "drops redundant straight-line points, keeps turns. Used for the busy dashboard view.")
    p.add_argument("--path-resolution", type=float, default=1.0,
                   help="seconds between comet-tail (path) samples; higher = cheaper per frame "
                        "(the dashboard raises this with aircraft count). 1 = smooth, for single-aircraft replays.")
    p.add_argument("--trail", choices=["active", "full", "off"], default="active",
                   help="initial trail mode")
    p.add_argument("--speed-colour", action="store_true",
                   help="start with the full trail coloured by ground speed")
    p.add_argument("--home", help='lon,lat,height,heading,pitch (degrees/metres)')
    p.add_argument("--yaw", help='per-model yaw in degrees, e.g. "glider=0,dr400=90"')
    p.add_argument("--models-url", default="models",
                   help="URL/path (relative to the HTML) where the .glb models are served")
    p.add_argument("--db", default=os.environ.get("OGNFLIGHTS_DB"),
                   help="explicit DB file (default: the year-partitioned file for --day)")
    a = p.parse_args()

    day = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    store = Store(a.db) if a.db else store_for_day(day)
    flights, legend = collect(store, day, a.reg, a.gliders, simplify=a.simplify)
    if not flights:
        raise SystemExit("no flights matched")

    home = dict(DEFAULT_HOME)
    if a.home:
        lon, lat, h, hd, pt = (float(x) for x in a.home.split(","))
        home = dict(lon=lon, lat=lat, height=h, heading=hd, pitch=pt, roll=0.0)

    # per-model yaw (registry defaults, overridable via --yaw)
    yaw_deg = {k: MODELS[k]["yaw"] for k in MODELS}
    if a.yaw:
        for pair in a.yaw.split(","):
            k, v = pair.split("=")
            yaw_deg[k.strip()] = float(v)

    # reference the models actually used by URL (served from --models-url, cached across pages)
    used_keys = sorted({fl["mk"] for fl in flights})
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    base = a.models_url.rstrip("/")
    models = {}
    for k in used_keys:
        fn = MODELS[k]["file"]
        if not os.path.exists(os.path.join(src_dir, fn)):
            print(f"warning: model file not found at {os.path.join(src_dir, fn)}", file=sys.stderr)
        models[k] = {"url": f"{base}/{fn}", "label": MODELS[k]["label"]}
    myaw = {k: math.radians(yaw_deg[k]) for k in used_keys}

    payload = {"title": a.title, "flights": flights, "legend": legend, "models": models}
    html = (TEMPLATE
            .replace("__TITLE__", a.title)
            .replace("__CES__", CES)
            .replace("__PAYLOAD__", json.dumps(payload))
            .replace("__HOME__", json.dumps(home))
            .replace("__MYAW__", json.dumps(myaw))
            .replace("__TRAILMODE__", a.trail)
            .replace("__SPEEDCOL__", "true" if a.speed_colour else "false")
            .replace("__PATHRES__", repr(a.path_resolution))
            .replace("__MULT__", str(a.mult)))
    with open(a.out, "w") as f:
        f.write(html)
    print(f"wrote {a.out}  ({len(html)} bytes; {len(flights)} flights, {len(legend)} aircraft; "
          f"models: {', '.join(used_keys)})")


if __name__ == "__main__":
    main()
