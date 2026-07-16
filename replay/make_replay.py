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
import argparse, json, math, os, re, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ognflights.config import GRANSDEN, GROUND_AGL_FT, MIN_FLIGHT_PEAK_AGL_FT
from ognflights.flights import Flight, segment
from ognflights.store import Store, store_for_day
from ognflights.theme import MAP_HELP_BTN, MAP_HELP_HTML, MAP_HELP_JS, THEME_CSS

FT_TO_M = 0.3048
GLIDERISH = {"glider", "tow", "motorglider"}
# Despike (display-layer): drop isolated out-and-back spike points from a track before it is
# drawn. A point is a spike when it juts far from BOTH neighbours while the neighbours themselves
# stay close together (an outlier fix that snaps back on the next fix). Raw data is untouched.
SPIKE_MIN_M = 80.0    # both neighbour hops must exceed this for a point to count as a spike
SPIKE_RATIO = 2.5     # ...and the out-and-back detour must be this much longer than the direct hop
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
<style>__THEMECSS__
html,body,#c{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000}
#legend{position:absolute;top:10px;left:10px;z-index:10;color:var(--text);
font:12px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:9px 11px;max-height:88vh;overflow:auto}
@media(max-width:640px){#legend{top:72px;max-height:70vh}
.cesium-viewer-toolbar{display:none}}
#legend:empty{display:none}
#legend b{font-size:14px} .hint{color:var(--dim);font-size:11px}
#legend a{color:var(--blue)}
.sw{display:inline-block;width:12px;height:12px;margin-right:6px;border-radius:3px;vertical-align:middle}
#empty{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:15;display:none;
font:15px system-ui,-apple-system,"Segoe UI",sans-serif;padding:14px 18px}</style>
</head><body><div id="c"></div><div id="legend" class="of-panel"></div>__DAYPICKER__<div id="empty" class="of-panel">No flights recorded for this day.</div>
__HELPHTML__
<script>
const INLINE_DATA=__PAYLOAD__;   // inlined DATA (inline mode) or null (external-data mode)
const EXTERNAL=__EXTERNAL__;     // true = fetch DATA (and manifest, in public build) at runtime
const DATABASE=__DATABASE__;     // base URL for external <day>.json + manifest.json (may be relative/empty)
const DAYPICKER=__DAYPICKER__;   // true = show the day-picker control and load manifest.json
const HOME=__HOME__;
const MYAW=__MYAW__;             // {modelKey: radians} yaw offset per model; tune with number keys + [ ]
const TRAILMODE="__TRAILMODE__";
const COLOURMODE="__COLOURMODE__";   // initial trail colouring: "off" | "speed" | "climb"
const SINGLELINK=__SINGLELINK__; // day string to link each aircraft to its single-aircraft view, or null
const PATHRES=__PATHRES__;
const TAILSECS=__TAILSECS__;  // sliding "tail" trail: seconds of track kept behind the aircraft
let tailSecs=TAILSECS;        // live-adjustable via the settings slider
const MULT=__MULT__;
const M_TO_FT=1/0.3048, MS_TO_KT=1/0.514444;  // metres->feet, vertical m/s -> knots
const FIELD_ELEV_FT=__FIELDELEV__;  // field elevation (ft AMSL); readout shows true altitude AMSL
const VARIO_WIN_S=18;               // vario smoothing window (s): least-squares slope of alt vs time
let readoutsOn=true;                // settings toggle: show/hide the per-aircraft readout labels
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
const SPD_LO=30, SPD_HI=110;   // knots mapped across the speed colour ramp
function speedColor(kt){
  const t=Math.max(0,Math.min(1,(kt-SPD_LO)/(SPD_HI-SPD_LO)));
  const st=[[0.12,0.12,0.5],[0.0,0.8,0.5],[1.0,1.0,0.5]];  // dim blue -> green -> bright yellow
  const seg=t*2, i=Math.min(1,Math.floor(seg)), f=seg-i, a=st[i], b=st[i+1]||st[i];
  return new Cesium.Color(a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f, a[2]+(b[2]-a[2])*f, 0.95);
}
const CLB_LO=-6, CLB_HI=6;   // knots (sink..climb) across the climb colour ramp
function climbColor(kt){
  const t=Math.max(0,Math.min(1,(kt-CLB_LO)/(CLB_HI-CLB_LO)));
  const st=[[0.15,0.45,1.0],[0.80,0.80,0.80],[1.0,0.28,0.10]];  // cold blue (sink) -> neutral -> hot red (climb)
  const seg=t*2, i=Math.min(1,Math.floor(seg)), f=seg-i, a=st[i], b=st[i+1]||st[i];
  return new Cesium.Color(a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f, a[2]+(b[2]-a[2])*f, 0.95);
}

// Precompute, once per flight, two Cesium.SampledProperty(Number): altitude in feet AMSL and
// a smoothed rate of climb in knots. Both are keyed on the same JulianDates as the position
// samples, so the label just reads them at the clock time (linear interpolation, cheap).
// The vario is a least-squares slope of altitude(m) vs time over a trailing ~VARIO_WIN_S window,
// which tames the very noisy raw OGN GPS altitude (same noise that killed instantaneous pitch).
function buildReadout(samples){
  const n=samples.length;
  const times=new Array(n), secs=new Array(n), altM=new Array(n);
  for(let i=0;i<n;i++){
    times[i]=Cesium.JulianDate.fromIso8601(samples[i][0]);
    secs[i]=Cesium.JulianDate.secondsDifference(times[i],times[0]);  // seconds from first sample
    altM[i]=samples[i][3];   // height above field, metres
  }
  const altFt=new Cesium.SampledProperty(Number);
  const varioKt=new Cesium.SampledProperty(Number);
  altFt.setInterpolationOptions({interpolationDegree:1,interpolationAlgorithm:Cesium.LinearApproximation});
  varioKt.setInterpolationOptions({interpolationDegree:1,interpolationAlgorithm:Cesium.LinearApproximation});
  let j=0;  // trailing window start index; advances monotonically
  for(let i=0;i<n;i++){
    altFt.addSample(times[i], altM[i]*M_TO_FT + FIELD_ELEV_FT);   // altitude AMSL
    while(secs[i]-secs[j] > VARIO_WIN_S) j++;
    // least-squares slope of altM vs secs over [j..i]
    let cnt=0,sx=0,sy=0,sxx=0,sxy=0;
    for(let k=j;k<=i;k++){const x=secs[k],y=altM[k]; cnt++; sx+=x; sy+=y; sxx+=x*x; sxy+=x*y;}
    const denom=cnt*sxx - sx*sx;
    const slope=(cnt>=2 && Math.abs(denom)>1e-9)?(cnt*sxy - sx*sy)/denom:0;  // m/s
    varioKt.addSample(times[i], slope*MS_TO_KT);
  }
  return {altFt, varioKt};
}
// three lines: short callsign, height above the field in feet, then signed climb rate in knots.
function fmtReadout(cs, ft, kt){
  const ftStr=Math.round(ft).toLocaleString("en-GB");
  const sign=kt>=0?"+":"-";
  return (cs?cs+"\n":"")+ftStr+" ft\n"+sign+Math.abs(kt).toFixed(1)+" kt";
}

// --- per-day render state: everything built from a DATA dict, torn down on day change ---
let trails=[], planes=[], colFlights=[];   // colFlights: per-flight progressive colour trail
let aircraftOn=[], flightAi=[];

function teardown(){
  // remove all entities + primitives added for the previous day so switching days leaks nothing
  planes.forEach(p=>viewer.entities.remove(p));
  trails.forEach(t=>viewer.entities.remove(t));
  colFlights.forEach(cf=>{ if(cf.prim) viewer.scene.primitives.remove(cf.prim); });
  trails=[]; planes=[]; colFlights=[];
  aircraftOn=[]; flightAi=[];
  viewer.scene.requestRender();
}

function renderData(DATA){
  teardown();
  const empty=document.getElementById("empty");
  if(!DATA||!DATA.flights||!DATA.flights.length){
    document.getElementById("legend").innerHTML=`<b>${(DATA&&DATA.title)||""}</b>`;
    if(empty)empty.style.display="block";
    return;
  }
  if(empty)empty.style.display="none";
  let tmin=null,tmax=null;
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
    // altitude + vario readout properties (precomputed once, read per frame by the label callback)
    const ro=buildReadout(fl.samples);
    const plane=viewer.entities.add({
      name:fl.name,
      availability:new Cesium.TimeIntervalCollection([new Cesium.TimeInterval({start:t0,stop:t1})]),
      position:pos,
      orientation:new Cesium.CallbackProperty(function(time,result){
        const base=velProp.getValue(time,result);
        if(!base) return undefined;
        const corr=Cesium.Quaternion.fromAxisAngle(Cesium.Cartesian3.UNIT_Z, MYAW[mk]||0);
        return Cesium.Quaternion.multiply(base, corr, base);
      }, false),
      model:{uri:DATA.models[mk].url, minimumPixelSize:40, maximumScale:20000, scale:1,
        color:col, colorBlendMode:Cesium.ColorBlendMode.MIX, colorBlendAmount:0.5,
        silhouetteColor:col, silhouetteSize:1.5},
      path:{resolution:PATHRES, material:col, width:3, leadTime:0, trailTime:100000},
      // floating altitude + rate-of-climb readout, hovering above the model. The callback only
      // reads two precomputed SampledProperties, so it is cheap even with many gliders; far-away
      // gliders drop out via distanceDisplayCondition to keep the scene uncluttered and light.
      label:{
        text:new Cesium.CallbackProperty(function(time){
          const ft=ro.altFt.getValue(time), kt=ro.varioKt.getValue(time);
          if(ft===undefined||kt===undefined) return "";
          return fmtReadout(fl.cs, ft, kt);
        }, false),
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
    planes.push(plane);
    const flat=fl.samples.flatMap(s=>[s[1],s[2],s[3]]);
    trails.push(viewer.entities.add({name:fl.name+" trail",
      polyline:{positions:Cesium.Cartesian3.fromDegreesArrayHeights(flat),
      width:1, material:col.withAlpha(0.35)}}));
    // Colour trails are drawn PROGRESSIVELY (revealed up to the playback clock) by
    // updateColourTrails(), so the coloured line is the single trail that grows behind the
    // glider - no static full-track overlay and no second solid comet-path. Here we just
    // precompute each flight's geometry, the per-vertex colours (speed + climb), and the
    // sample times the reveal indexes into.
    colFlights.push({
      ai:fl.ai,
      times:fl.samples.map(s=>Cesium.JulianDate.fromIso8601(s[0])),
      pos:Cesium.Cartesian3.fromDegreesArrayHeights(flat),
      spdCol:(fl.spd||[]).map(speedColor),
      climbCol:(fl.climb||[]).map(climbColor),
      prim:null, key:null});
  });
  const leg=[`<b>${DATA.title}</b><br><span class="hint">${DATA.flights.length} flights, ${DATA.legend.length} aircraft</span><br>`,
    `<div style="margin:4px 0;user-select:none">trails:
      <label><input type="radio" name="tm" value="all"> all flights</label>
      <label><input type="radio" name="tm" value="current"> current flight</label>
      <label><input type="radio" name="tm" value="tail"> tail</label>
      <label><input type="radio" name="tm" value="off"> off</label>
      <span style="margin-left:8px">colour:</span>
      <label><input type="radio" name="cm" value="off"> none</label>
      <label><input type="radio" name="cm" value="speed"> speed</label>
      <label><input type="radio" name="cm" value="climb"> climb</label>
      <div id="speedscale" style="display:none;margin:3px 0">
        <span style="display:inline-block;width:130px;height:9px;border-radius:2px;background:linear-gradient(90deg,#1f1f80,#00cc80,#ffff80)"></span>
        <br><span class="hint">slow ${SPD_LO} &rarr; ${SPD_HI} kt fast</span></div>
      <div id="climbscale" style="display:none;margin:3px 0">
        <span style="display:inline-block;width:130px;height:9px;border-radius:2px;background:linear-gradient(90deg,#2673ff,#cccccc,#ff481a)"></span>
        <br><span class="hint">sink ${CLB_LO} &rarr; +${CLB_HI} kt climb</span></div>
      <br><label style="cursor:pointer"><input type="checkbox" id="placenames"> place names</label>
      <br><button id="resetview" style="cursor:pointer;margin-top:4px">reset view</button>
      <details id="settings" style="margin-top:6px">
        <summary style="cursor:pointer;user-select:none;opacity:.8">settings</summary>
        <div style="margin:4px 0 2px 2px">
          <label style="cursor:pointer;display:block"><input type="checkbox" id="nightsky" checked> night sky</label>
          <label style="cursor:pointer;display:block;margin-top:4px"><input type="checkbox" id="readouts" checked> altitude / climb readouts</label>
          <label style="display:block;margin-top:4px">tail length: <span id="tailval">${TAILSECS}</span>s<br>
            <input type="range" id="tailrange" min="10" max="300" step="5" value="${TAILSECS}" style="width:150px;vertical-align:middle">
          </label>
        </div>
      </details></div>`];
  if(DATA.legend.length>1) leg.push(`<div class="hint" style="margin-top:4px">show: <a href="#" id="acAll">all</a> / <a href="#" id="acNone">none</a></div>`);
  DATA.legend.forEach((a,i)=>leg.push(`<div style="display:block"><label style="cursor:pointer"><input type="checkbox" class="acft" data-ai="${i}" checked> <span class="sw" style="background:${a.color}"></span>${a.label} (${a.n})</label>${SINGLELINK&&a.key?` <a href="#" class="single" data-key="${encodeURIComponent(a.key)}" style="font-size:11px">single &rarr;</a>`:""}</div>`));
  leg.push(`<div id="models" class="hint" style="margin-top:6px"></div>`);
  leg.push(`<div class="hint" style="margin-top:6px">3D models: <a href="https://github.com/Ysurac/FlightAirMap-3dmodels">FlightAirMap</a> (GPLv2)</div>`);
  document.getElementById("legend").innerHTML=leg.join("");

  // per-aircraft visibility (index matches DATA.legend); each flight knows its aircraft via fl.ai
  aircraftOn=DATA.legend.map(()=>true);
  flightAi=DATA.flights.map(f=>f.ai);
  // trail modes: all = every flight that day (accumulates as the day plays); current = only the
  // flight in progress at the playback time (hidden once it lands); tail = sliding window behind
  // the current flight; off = none. colour "speed"/"climb" replaces the trail with a single
  // progressive coloured line that draws as the glider flies (speed: slow blue->fast yellow;
  // climb: sink blue->climb red); "off" keeps the per-aircraft solid trail.
  document.querySelectorAll('input.acft').forEach(cb=>cb.addEventListener("change",e=>{
    aircraftOn[+e.target.dataset.ai]=e.target.checked; applyTrails();
  }));
  if(document.getElementById("acAll")) document.getElementById("acAll").addEventListener("click",e=>{e.preventDefault();setAllAircraft(true);});
  if(document.getElementById("acNone")) document.getElementById("acNone").addEventListener("click",e=>{e.preventDefault();setAllAircraft(false);});
  document.querySelectorAll('input[name=tm]').forEach(r=>r.addEventListener("change",applyTrails));
  // settings: live tail-length slider (switches to the "tail" mode so the change is visible)
  const tailRange=document.getElementById("tailrange");
  if(tailRange) tailRange.addEventListener("input",e=>{
    tailSecs=+e.target.value; document.getElementById("tailval").textContent=tailSecs;
    document.querySelector('input[name=tm][value="tail"]').checked=true;
    applyTrails();
  });
  document.querySelectorAll('input[name=cm]').forEach(r=>r.addEventListener("change",applyTrails));
  document.querySelector('input[name=tm][value="'+TRAILMODE+'"]').checked=true;
  document.querySelector('input[name=cm][value="'+COLOURMODE+'"]').checked=true;
  applyTrails();
  document.getElementById("nightsky").addEventListener("change",e=>setNight(e.target.checked));
  setNight(true);
  // readouts toggle: the load escape hatch. Flip label visibility on every aircraft and redraw.
  const readoutsCb=document.getElementById("readouts");
  readoutsCb.checked=readoutsOn;
  readoutsCb.addEventListener("change",e=>{
    readoutsOn=e.target.checked;
    planes.forEach((p,i)=>{if(p.label) p.label.show=readoutsOn && aircraftOn[flightAi[i]];});
    viewer.scene.requestRender();
  });
  document.getElementById("placenames").addEventListener("change",e=>{labelLayer.show=e.target.checked; viewer.scene.requestRender();});
  document.getElementById("resetview").addEventListener("click",function(){goHome(); this.blur();});
  // single-aircraft click-through: stay on this page, just re-render one aircraft (query-param based)
  document.querySelectorAll('a.single').forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault();
    const u=new URL(location.href);
    if(SINGLELINK)u.searchParams.set("day",SINGLELINK);
    u.searchParams.set("address",decodeURIComponent(a.dataset.key));
    location.href=u.toString();
  }));

  renderModels(DATA);

  viewer.clock.startTime=tmin.clone();viewer.clock.stopTime=tmax.clone();viewer.clock.currentTime=tmin.clone();
  viewer.clock.clockRange=Cesium.ClockRange.LOOP_STOP;viewer.clock.multiplier=MULT;viewer.clock.shouldAnimate=true;
  viewer.timeline.zoomTo(tmin,tmax);
  viewer.scene.requestRender();
}

function applyTrails(){
  const m=document.querySelector('input[name=tm]:checked').value;
  const cm=document.querySelector('input[name=cm]:checked').value;   // off | speed | climb
  const coloured=cm!=="off";
  // Colour on: the single progressive coloured trail (updateColourTrails) IS the trail, so
  // hide the solid comet-path and the static per-aircraft line to avoid a double trail.
  // "tail" = short sliding window behind the aircraft; "active"/"full" = whole flown tail.
  planes.forEach((p,i)=>{const on=aircraftOn[flightAi[i]]; p.show=on;
    p.path.show=on && m!=="off" && !coloured;
    p.path.trailTime=(m==="tail")?tailSecs:100000;
    if(p.label) p.label.show=readoutsOn;});   // entity.show already gates a hidden aircraft's label
  trails.forEach((t,i)=>{t.show=aircraftOn[flightAi[i]] && m==="all" && !coloured;});
  document.getElementById("speedscale").style.display=(coloured&&cm==="speed"&&m!=="off")?"block":"none";
  document.getElementById("climbscale").style.display=(coloured&&cm==="climb"&&m!=="off")?"block":"none";
  updateColourTrails(true);         // apply the colour-trail visibility/window immediately
  viewer.scene.requestRender();     // requestRenderMode is on, so ask for a redraw after toggling
}

// binary search over an ascending JulianDate[] ----------------------------------------------
function jdCountLE(times, t){       // number of samples with time <= t
  let lo=0, hi=times.length;
  while(lo<hi){const m=(lo+hi)>>1; if(Cesium.JulianDate.lessThanOrEquals(times[m],t)) lo=m+1; else hi=m;}
  return lo;
}
function jdFirstGE(times, t){       // first index with time >= t
  let lo=0, hi=times.length;
  while(lo<hi){const m=(lo+hi)>>1; if(Cesium.JulianDate.lessThan(times[m],t)) lo=m+1; else hi=m;}
  return lo;
}
// Progressive colour trails: reveal each flight's coloured track up to the playback clock, so a
// single trail draws behind the glider as it flies (full/active persist from the start; tail keeps
// the last tailSecs). Per-vertex colour needs the static primitive, which cannot itself follow the
// clock, so we rebuild a flight's primitive only when its visible [start,end) sample window changes,
// throttled to ~120ms wall time so a fast multiplier does not thrash the GPU. The rebuilt geometry
// is created SYNCHRONOUSLY (asynchronous:false) so the new primitive is ready the instant the old
// one is removed - otherwise the async build leaves a frame or two drawing nothing, i.e. flicker.
let _colLastWall=0;
function updateColourTrails(force){
  const cmEl=document.querySelector('input[name=cm]:checked');
  if(!cmEl) return;                 // legend not built yet (initial load)
  const wall=Date.now();
  if(!force && wall-_colLastWall<120) return;
  _colLastWall=wall;
  const cm=cmEl.value, m=document.querySelector('input[name=tm]:checked').value;
  const now=viewer.clock.currentTime;
  let changed=false;
  colFlights.forEach(cf=>{
    let on = cm!=="off" && m!=="off" && aircraftOn[cf.ai];
    if(on && (m==="current" || m==="tail")){
      // "current"/"tail" show only the flight in progress at the playback time; "all" accumulates
      const t0=cf.times[0], t1=cf.times[cf.times.length-1];
      on = Cesium.JulianDate.lessThanOrEquals(t0,now) && Cesium.JulianDate.lessThanOrEquals(now,t1);
    }
    if(!on){ if(cf.prim) cf.prim.show=false; return; }
    const end=jdCountLE(cf.times, now);
    const start=(m==="tail")
      ? jdFirstGE(cf.times, Cesium.JulianDate.addSeconds(now,-tailSecs,new Cesium.JulianDate()))
      : 0;
    const key=cm+":"+start+":"+end;
    if(key===cf.key){ if(cf.prim) cf.prim.show=true; return; }
    cf.key=key;
    if(cf.prim){ viewer.scene.primitives.remove(cf.prim); cf.prim=null; }
    if(end-start<2) return;         // need at least one segment to draw
    const cols=(cm==="climb"?cf.climbCol:cf.spdCol).slice(start,end);
    cf.prim=viewer.scene.primitives.add(new Cesium.Primitive({
      geometryInstances:new Cesium.GeometryInstance({geometry:new Cesium.PolylineGeometry({
        positions:cf.pos.slice(start,end), width:3,
        vertexFormat:Cesium.PolylineColorAppearance.VERTEX_FORMAT,
        colors:cols, colorsPerVertex:true, arcType:Cesium.ArcType.NONE})}),
      appearance:new Cesium.PolylineColorAppearance({translucent:true}),
      asynchronous:false}));   // build now so it renders the instant the old primitive is removed
    changed=true;
  });
  if(changed) viewer.scene.requestRender();
}
// drive the reveal off the playback clock (one listener for the whole page)
viewer.clock.onTick.addEventListener(function(){ updateColourTrails(false); });
function setAllAircraft(on){
  aircraftOn.fill(on);
  document.querySelectorAll('input.acft').forEach(cb=>{cb.checked=on;});
  applyTrails();
}

// night sky: drop the bright atmosphere + ground haze, revealing the star skybox (ground stays lit)
function setNight(on){
  viewer.scene.skyAtmosphere.show=!on;
  viewer.scene.globe.showGroundAtmosphere=!on;
  viewer.scene.backgroundColor=Cesium.Color.BLACK;
  viewer.scene.requestRender();
}

// hover tooltip: show the aircraft name/registration under the cursor
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
let MK=[], selM=null, curModels={};
function renderModels(DATA){
  curModels=DATA.models; MK=Object.keys(DATA.models);
  if(!selM||MK.indexOf(selM)<0)selM=MK[0];
  const el=document.getElementById("models");
  if(!el)return;
  el.innerHTML="models (press 1-9 to pick, [ ] to yaw):<br>"+
    MK.map((k,i)=>`<span style="${k===selM?'color:var(--accent);font-weight:bold':''}">${i+1}. ${DATA.models[k].label}: ${Math.round(Cesium.Math.toDegrees(MYAW[k]||0))}&deg;</span>`).join("<br>");
}
window.addEventListener("keydown",e=>{
  if(/^[1-9]$/.test(e.key)){const i=+e.key-1; if(i<MK.length){selM=MK[i]; renderModels({models:curModels});} return;}
  if(e.key==="["||e.key==="]"){
    if(!selM)return;
    MYAW[selM]=(MYAW[selM]||0)+(e.key==="]"?1:-1)*Cesium.Math.toRadians(5);
    renderModels({models:curModels});
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

// --- data bootstrap: inline (baked-in DATA) or external (fetch per day) ---
function dataUrl(name){ return (DATABASE||"") + name; }
async function fetchJson(url){
  const r=await fetch(url,{cache:"no-cache"});
  if(!r.ok) throw new Error(url+" -> "+r.status);
  return r.json();
}
// Client-side single-aircraft filter: the per-day JSON has every aircraft, so when
// ?address= is set we keep just that one and re-index its flights' `ai` to 0. This
// mirrors the private --address view but without a server round-trip.
function filterAddress(DATA, addr){
  if(!addr) return DATA;
  const li=DATA.legend.findIndex(a=>a.key===addr);
  if(li<0) return DATA;   // unknown address: fall back to the whole day
  const flights=DATA.flights.filter(f=>f.ai===li).map(f=>Object.assign({}, f, {ai:0}));
  return {title:DATA.legend[li].label+" "+(DATA.title||""),
          flights, legend:[DATA.legend[li]], models:DATA.models};
}
// Client-side single-FLIGHT filter: ?t=<epoch seconds or HH:MM UTC> keeps only the
// flight whose airborne interval contains that moment (the "Watch your flight" finder
// links here so a visitor sees just their flight, not the aircraft's whole day). If no
// interval contains it, the nearest take-off within 15 minutes is used; failing that
// the data is left untouched. Without ?t= behaviour is unchanged.
function filterTime(DATA){
  const tp=new URLSearchParams(location.search).get("t");
  if(!tp||!DATA||!DATA.flights||!DATA.flights.length) return DATA;
  let t=null;
  if(/^\d{9,}$/.test(tp)) t=parseInt(tp,10);
  else if(/^\d{1,2}:\d{2}$/.test(tp)){
    const day=DATA.flights[0].samples[0][0].slice(0,10);
    t=Date.parse(day+"T"+tp.padStart(5,"0")+":00Z")/1000;
  }
  if(t==null||!isFinite(t)) return DATA;
  const bounds=f=>[Date.parse(f.samples[0][0])/1000,
                   Date.parse(f.samples[f.samples.length-1][0])/1000];
  let keep=DATA.flights.filter(f=>{const b=bounds(f); return b[0]<=t&&t<=b[1];});
  if(!keep.length){
    let best=null,bd=15*60+1;
    for(const f of DATA.flights){const d=Math.abs(bounds(f)[0]-t); if(d<bd){bd=d;best=f;}}
    if(best) keep=[best];
  }
  if(!keep.length||keep.length===DATA.flights.length) return DATA;
  return Object.assign({},DATA,{flights:keep});
}
async function loadDay(day){
  const addr=new URLSearchParams(location.search).get("address");
  try{
    const DATA=await fetchJson(dataUrl(day+".json"));
    renderData(filterTime(filterAddress(DATA, addr)));
  }catch(err){
    console.error("failed to load day",day,err);
    renderData({title:day, flights:[], legend:[], models:{}});
  }
}
async function boot(){
  if(!EXTERNAL){ renderData(filterTime(INLINE_DATA)); return; }
  if(!DAYPICKER){ await loadDay(new URLSearchParams(location.search).get("day")||""); return; }
  // public build: manifest.json -> day picker -> newest day (or ?day= override)
  let manifest;
  try{ manifest=await fetchJson(dataUrl("manifest.json")); }
  catch(err){ console.error("manifest load failed",err); renderData({title:"", flights:[], legend:[], models:{}}); return; }
  const days=(manifest.days||[]).map(d=>typeof d==="string"?d:d.day);
  const sel=document.getElementById("daysel");
  sel.innerHTML=days.map(d=>`<option value="${d}">${d}</option>`).join("");
  const params=new URLSearchParams(location.search);
  let want=params.get("day");
  if(!want||days.indexOf(want)<0)want=days[0];
  if(want){ sel.value=want; await loadDay(want); }
  else renderData({title:"", flights:[], legend:[], models:{}});
  sel.addEventListener("change",()=>{
    const p=new URLSearchParams(location.search);
    p.set("day",sel.value); p.delete("address");
    history.replaceState(null,"","?"+p.toString());
    loadDay(sel.value);
  });
}
boot();

__HELPJS__
</script></body></html>"""

# The day-picker control, only emitted in --public builds. Carries the "?" reopen
# button for the map-controls help overlay (the private /replay gets its button from
# the server-injected topbar instead, so there is never a duplicate).
DAYPICKER_HTML = ('<div id="daypick" class="of-topbar">day: '
                  '<select id="daysel"></select> '
                  '<a href="https://github.com/8none1/ognflights" target="_blank" rel="noopener">about</a> '
                  + MAP_HELP_BTN + '</div>')


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


def climb_rates_kt(fixes):
    """Per-fix rate of climb (knots) from the altitude track, lightly smoothed.

    Derived from altitude deltas (central difference), not the raw per-beacon climb_fpm,
    so it matches the smoothed on-screen vario readout and stays clean as a per-vertex
    colour ramp. Positive = climbing, negative = sinking."""
    n = len(fixes)
    if n < 2:
        return [0.0] * n
    ft_to_m, ms_to_kt = 0.3048, 1 / 0.514444
    raw = [0.0] * n
    for i in range(n):
        a = fixes[max(0, i - 1)]; b = fixes[min(n - 1, i + 1)]
        dt = b.ts - a.ts
        if dt <= 0:
            raw[i] = raw[i - 1] if i else 0.0
            continue
        raw[i] = (b.alt_ft - a.alt_ft) * ft_to_m / dt * ms_to_kt  # ft/dt -> m/s -> kt
    out = [0.0] * n
    for i in range(n):
        a, b = max(0, i - 2), min(n, i + 3)
        out[i] = sum(raw[a:b]) / (b - a)
    return out


def _haversine_m(a, b):
    """Horizontal great-circle distance in metres between two (lon, lat) points."""
    R = 6371000.0
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dlat = math.radians(b[1] - a[1]); dlon = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def despike_indices(fixes):
    """Indices of `fixes` to keep after removing isolated out-and-back spike points.

    A point p[i] is a spike when it is far from BOTH neighbours AND the two hops out+back are
    much longer than the direct neighbour-to-neighbour hop (so it juts out and comes straight
    back while the neighbours stay close). Turn-safe: a genuine turn point is close to at least
    one neighbour, or the neighbours are far apart, so the ratio test fails. Horizontal distance
    only (altitude ignored). First and last are always kept. One pass, comparing against the last
    kept point so a spike is not measured against another spike. `fixes` are Flight fixes."""
    n = len(fixes)
    if n < 3:
        return list(range(n))
    keep = [0]
    for i in range(1, n - 1):
        prev = fixes[keep[-1]]
        p, nxt = fixes[i], fixes[i + 1]
        a = (prev.lon, prev.lat); b = (p.lon, p.lat); c = (nxt.lon, nxt.lat)
        d0 = _haversine_m(a, b); d1 = _haversine_m(b, c); dd = _haversine_m(a, c)
        if d0 > SPIKE_MIN_M and d1 > SPIKE_MIN_M and (d0 + d1) > SPIKE_RATIO * dd:
            continue  # isolated out-and-back spike: drop it
        keep.append(i)
    keep.append(n - 1)
    return keep


def short_callsign(label):
    """Short callsign = the bit in square brackets in the label (e.g. "G-ELSB [SB]" -> "SB"),
    falling back to the whole label/registration when there are no brackets."""
    if not label:
        return ""
    m = re.search(r"\[([^\]]+)\]", label)
    return m.group(1).strip() if m else label.strip()


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


def collect(store, day, reg_spec, gliders, simplify=0.0, by_aircraft=False, address=None,
            since=None, until=None):
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
        if address is not None and addr != address:
            continue
        if want_reg is not None and not label.startswith(want_reg):
            continue
        if want_reg is None and gliders and ac_type not in GLIDERISH:
            continue
        raw = store.fixes_for(addr, lo, hi)
        if by_aircraft:
            # one continuous track for the whole day: never vanishes across data gaps or
            # brief ground stops (the aircraft just sits / glides across). Skip never-flew.
            ground = GRANSDEN.elevation_ft + GROUND_AGL_FT
            if not raw or (max(f.alt_ft for f in raw) - ground) < MIN_FLIGHT_PEAK_AGL_FT:
                continue
            fls = [Flight(address=addr, fixes=raw)]
        else:
            fls = segment(addr, raw, GRANSDEN)
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
            # time-window filter: keep flights that overlap [since, until] (unix ts, UTC)
            if since is not None and fl.end < since:
                continue
            if until is not None and fl.start > until:
                continue
            t0 = datetime.fromtimestamp(fl.start, tz=timezone.utc).strftime("%H:%M")
            # Despike (display-layer): drop isolated out-and-back outlier fixes before the track is
            # turned into samples/speeds, so the whole flight (trail, position, readout) is built
            # from the cleaned points. Full look-ahead here, so a single pass is exact.
            fixes = [fl.fixes[i] for i in despike_indices(fl.fixes)]
            # Height ABOVE THE AIRFIELD, not MSL: the replay has no terrain, so Cesium draws
            # the ground at the sea-level ellipsoid. Plotting MSL would float every aircraft
            # ~field-elevation too high. Subtract field elevation so ground level sits on the map.
            samples = [[datetime.fromtimestamp(f.ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        round(f.lon, 6), round(f.lat, 6),
                        round(max(0.0, (f.alt_ft - GRANSDEN.elevation_ft) * FT_TO_M), 1)]
                       for f in fixes]
            spd = [round(v) for v in ground_speeds_kt(fixes)]
            clb = [round(v, 1) for v in climb_rates_kt(fixes)]
            if simplify and len(samples) > 2:
                clat = math.cos(math.radians(GRANSDEN.lat))
                pts = [(s[1] * 111320.0 * clat, s[2] * 111320.0, s[3]) for s in samples]
                keep = _rdp_keep(pts, simplify)
                samples = [samples[i] for i in keep]
                spd = [spd[i] for i in keep]
                clb = [clb[i] for i in keep]
            name = label if by_aircraft else f"{label} F{i} {t0}Z"
            flights.append({"name": name, "color": col, "mk": mk, "cs": short_callsign(label),
                            "ai": aidx, "samples": samples, "spd": spd, "climb": clb})
            used += 1
        if used:
            # "type" = the OGN device-database model string (e.g. "ASK-21", "SZD-50
            # Puchacz", "DR-400"): published in the public per-day JSON so the
            # "Watch your flight" finder can filter by aircraft type.
            legend.append({"label": label, "color": col, "n": used, "key": addr,
                           "type": model_str or ""})
    return flights, legend


PUBLIC_DATA_BASE = "https://raw.githubusercontent.com/8none1/ognflights/public-data/"


def models_for(keys, models_url="models", warn=True):
    """Build the DATA.models mapping {mk: {url, label}} for the given model keys.
    URLs are relative to the served HTML via `models_url`."""
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    base = models_url.rstrip("/")
    out = {}
    for k in keys:
        fn = MODELS[k]["file"]
        if warn and not os.path.exists(os.path.join(src_dir, fn)):
            print(f"warning: model file not found at {os.path.join(src_dir, fn)}", file=sys.stderr)
        out[k] = {"url": f"{base}/{fn}", "label": MODELS[k]["label"]}
    return out


def default_myaw(keys=None, overrides=None):
    """Per-model yaw in radians (registry defaults + optional overrides)."""
    yaw_deg = {k: MODELS[k]["yaw"] for k in MODELS}
    if overrides:
        yaw_deg.update(overrides)
    keys = keys if keys is not None else list(MODELS)
    return {k: math.radians(yaw_deg[k]) for k in keys}


def build_payload(flights, legend, title, models_url="models"):
    """Assemble the DATA dict a page (or a per-day JSON file) consumes."""
    used_keys = sorted({fl["mk"] for fl in flights})
    return {"title": title, "flights": flights, "legend": legend,
            "models": models_for(used_keys, models_url)}


def render_html(*, title, payload, home, myaw, trail, colour_mode="off", single_link,
                path_resolution, mult, tail_seconds=60, external=False, data_base="", daypicker=False):
    """Fill the Cesium template into a complete HTML page.

    `payload` is the inline DATA dict for inline mode, or None in external-data
    mode (the page fetches its DATA at runtime from `data_base`). When `daypicker`
    is set the page loads manifest.json and shows the day-picker control."""
    return (TEMPLATE
            .replace("__DAYPICKER__", DAYPICKER_HTML if daypicker else "", 1)  # the <body> slot (before <script>)
            .replace("__HELPHTML__", MAP_HELP_HTML)
            .replace("__HELPJS__", MAP_HELP_JS)
            .replace("__THEMECSS__", THEME_CSS)
            .replace("__TITLE__", title)
            .replace("__CES__", CES)
            .replace("__PAYLOAD__", json.dumps(payload) if payload is not None else "null")
            .replace("__EXTERNAL__", "true" if external else "false")
            .replace("__DATABASE__", json.dumps(data_base))
            .replace("__DAYPICKER__", "true" if daypicker else "false")  # the JS const slot
            .replace("__HOME__", json.dumps(home))
            .replace("__MYAW__", json.dumps(myaw))
            .replace("__TRAILMODE__", trail)
            .replace("__COLOURMODE__", colour_mode)
            .replace("__SINGLELINK__", json.dumps(single_link))
            .replace("__PATHRES__", repr(path_resolution))
            .replace("__TAILSECS__", str(tail_seconds))
            .replace("__FIELDELEV__", repr(float(GRANSDEN.elevation_ft)))
            .replace("__MULT__", str(mult)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--day", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--reg", help='e.g. "G-CKFY" or "G-CKFY:1,2,3,6"')
    p.add_argument("--address", help="select a single aircraft by exact device address/callsign")
    p.add_argument("--gliders", action="store_true", help="all glider/tug types")
    p.add_argument("--by-aircraft", action="store_true",
                   help="one continuous track per aircraft for the whole day (bridges data gaps and "
                        "ground stops so trails never vanish); vs the default per-flight segmentation")
    p.add_argument("--mult", type=int, default=60, help="playback speed multiplier")
    p.add_argument("--link-single", action="store_true",
                   help="add a 'single ->' link after each legend aircraft (to ?day=<day>&address=<id>), "
                        "for the dashboard all-gliders view")
    p.add_argument("--simplify", type=float, default=0.0,
                   help="RDP trail simplification tolerance in metres (0 = full fidelity); "
                        "drops redundant straight-line points, keeps turns. Used for the busy dashboard view.")
    p.add_argument("--path-resolution", type=float, default=1.0,
                   help="seconds between comet-tail (path) samples; higher = cheaper per frame "
                        "(the dashboard raises this with aircraft count). 1 = smooth, for single-aircraft replays.")
    p.add_argument("--trail", choices=["all", "current", "tail", "off", "full", "active"],
                   default="current",
                   help="initial trail mode: all (every flight that day) / current (the flight in "
                        "progress) / tail (sliding window) / off. full/active are back-compat aliases.")
    p.add_argument("--tail-seconds", type=int, default=60,
                   help='length (seconds of track) of the "tail" sliding trail mode')
    p.add_argument("--colour-mode", choices=["off", "speed", "climb"], default="off",
                   help="initial full-trail colouring: off (per-aircraft colour), speed, or climb rate")
    p.add_argument("--speed-colour", action="store_true",
                   help="alias for --colour-mode speed (kept for back-compat)")
    p.add_argument("--home", help='lon,lat,height,heading,pitch (degrees/metres)')
    p.add_argument("--yaw", help='per-model yaw in degrees, e.g. "glider=0,dr400=90"')
    p.add_argument("--models-url", default="models",
                   help="URL/path (relative to the HTML) where the .glb models are served")
    p.add_argument("--db", default=os.environ.get("OGNFLIGHTS_DB"),
                   help="explicit DB file (default: the year-partitioned file for --day)")
    p.add_argument("--since", help='only flights overlapping from this UTC time, "HH:MM" or "HH:MM:SS"')
    p.add_argument("--until", help='only flights overlapping up to this UTC time, "HH:MM" or "HH:MM:SS"')
    p.add_argument("--external-data", action="store_true",
                   help="the page fetches its DATA at runtime instead of inlining it "
                        "(the private /replay keeps the default inline path)")
    p.add_argument("--data-base", default="",
                   help="base URL for external <day>.json + manifest.json (default: relative/empty, "
                        "for local testing next to the HTML)")
    p.add_argument("--day-picker", action="store_true",
                   help="show the day picker (loads manifest.json). Use with --external-data "
                        "--data-base for local testing of the public UI.")
    p.add_argument("--public", action="store_true",
                   help="public build: external-data + day picker + the raw.githubusercontent "
                        "public-data base URL. Overrides --external-data/--data-base.")
    a = p.parse_args()

    day = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    def _tod(s):
        if not s:
            return None
        fmt = "%H:%M:%S" if s.count(":") == 2 else "%H:%M"
        t = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        return int(day.replace(hour=t.hour, minute=t.minute, second=t.second).timestamp())
    since = _tod(a.since)
    until = _tod(a.until)

    # --public turns on external-data + day picker + the raw.githubusercontent base.
    external = a.external_data or a.public
    daypicker = a.day_picker or a.public
    data_base = PUBLIC_DATA_BASE if a.public else a.data_base

    home = dict(DEFAULT_HOME)
    if a.home:
        lon, lat, h, hd, pt = (float(x) for x in a.home.split(","))
        home = dict(lon=lon, lat=lat, height=h, heading=hd, pitch=pt, roll=0.0)

    # per-model yaw (registry defaults, overridable via --yaw)
    overrides = None
    if a.yaw:
        overrides = {}
        for pair in a.yaw.split(","):
            k, v = pair.split("=")
            overrides[k.strip()] = float(v)

    if external:
        # Shell page: no DB read; DATA (incl. its own models map) is fetched at runtime.
        # Bake yaw for every known model so any day's data can render.
        myaw = default_myaw(overrides=overrides)
        payload = None
        flights = legend = ()  # for the summary line only
        single_link = a.day if a.link_single else None
    else:
        store = Store(a.db) if a.db else store_for_day(day)
        flights, legend = collect(store, day, a.reg, a.gliders, simplify=a.simplify,
                                  by_aircraft=a.by_aircraft, address=a.address,
                                  since=since, until=until)
        if not flights:
            raise SystemExit("no flights matched")
        used_keys = sorted({fl["mk"] for fl in flights})
        myaw = default_myaw(used_keys, overrides)
        payload = build_payload(flights, legend, a.title, a.models_url)
        single_link = a.day if a.link_single else None

    colour_mode = "speed" if a.speed_colour else a.colour_mode
    trail = {"full": "all", "active": "current"}.get(a.trail, a.trail)
    html = render_html(title=a.title, payload=payload, home=home, myaw=myaw,
                       trail=trail, colour_mode=colour_mode, single_link=single_link,
                       path_resolution=a.path_resolution, mult=a.mult, tail_seconds=a.tail_seconds,
                       external=external, data_base=data_base, daypicker=daypicker)
    with open(a.out, "w") as f:
        f.write(html)
    if external:
        print(f"wrote {a.out}  ({len(html)} bytes; external-data mode, "
              f"data-base={data_base or '(relative)'}, day-picker={'on' if daypicker else 'off'})")
    else:
        print(f"wrote {a.out}  ({len(html)} bytes; {len(flights)} flights, {len(legend)} aircraft)")


if __name__ == "__main__":
    main()
