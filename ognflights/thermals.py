"""Thermal-hotspot detection from historic fixes, with precomputed storage.

A "hotspot" is a place where gliders *repeatedly* climb: a fix counts as thermalling
when the aircraft is a glider, climbing (>= CLIMB_FLOOR), circling (|rot| >= ROT_MIN) and
NOT under aerotow (no tug within TUG_R_M / TUG_DZ_FT / TUG_DT_S). Thermalling fixes are
grid-binned; a cell is a hotspot when enough distinct aircraft-days climbed there AND
gliders climb there disproportionately often (lift ratio), which de-biases the "everyone
is near the field" effect. Contiguous hot cells are clustered; each cluster yields a
centroid, radius, altitude band, mean climb, and a base(low-alt)->top(high-alt) drift
vector (the prevailing downwind lean of the column).

Detection is stdlib-only. Results are cached in a small separate SQLite (data/thermals.sqlite)
so the map overlays can look them up instantly; recomputed on a schedule (see publish.worker).
"""
import glob
import math
import os
import sqlite3
import time

from . import config

# Tuned against a week of Gransden data (see the Phase-1 exploration).
DEFAULTS = dict(
    radius_m=40000,      # ~25 miles; the recurrence rule keeps far one-off climbs out anyway
    climb_floor=100,     # fpm (~1 kt) to count as climbing
    climb_cap=3000,      # fpm; above this is noise/sentinel
    rot_min=4.0,         # |rot| to count as circling (feed p90 ~5.6)
    tug_r_m=250, tug_dz_ft=300, tug_dt_s=12,   # aerotow exclusion window
    cell_m=250,          # grid cell size
    min_ac_days=5,       # >= this many distinct (aircraft, day) climbed in the cell
    min_ratio=0.12,      # ...and gliders climb here in >= this fraction of their passes
    k_sigma=1.8,         # circle radius = this many std-devs of the cluster spread
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS thermals (
    computed_at  INTEGER,
    window_start INTEGER,
    window_end   INTEGER,
    lat          REAL,      -- hotspot centroid
    lon          REAL,
    radius_m     REAL,
    base_lat     REAL,      -- low-altitude centroid (column base)
    base_lon     REAL,
    base_ft      REAL,      -- AMSL
    top_lat      REAL,      -- high-altitude centroid (column top)
    top_lon      REAL,
    top_ft       REAL,
    climb_kt     REAL,
    ac_days      INTEGER,
    fixes        INTEGER,
    drift_m      REAL,      -- horizontal base->top offset
    drift_deg    REAL       -- bearing the top leans toward (downwind)
);
"""


def _pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


def compute(data_dir, site, start_ts, end_ts, params=None):
    """Detect thermal hotspots in [start_ts, end_ts) around `site`. Returns list of dicts.

    Reads every year DB in `data_dir` (read-only) that could overlap the window.
    """
    p = dict(DEFAULTS, **(params or {}))
    LAT, LON = site.lat, site.lon
    cosl = math.cos(math.radians(LAT))
    elev = site.elevation_ft
    R = p["radius_m"]

    def to_xy(lat, lon):
        return ((lon - LON) * 111320.0 * cosl, (lat - LAT) * 111320.0)

    def to_ll(x, y):
        return (LAT + y / 111320.0, LON + x / (111320.0 * cosl))

    dlat = R / 111320.0
    dlon = dlat / cosl
    rows = []
    for f in sorted(glob.glob(os.path.join(data_dir, "ogn-*.sqlite"))):
        db = sqlite3.connect("file:" + os.path.abspath(f) + "?mode=ro", uri=True)
        try:
            rows += db.execute(
                """SELECT fx.lat, fx.lon, fx.alt_ft, fx.climb_fpm, fx.rot, fx.address, fx.ts,
                          COALESCE(d.aircraft_type,'?')
                   FROM fixes fx LEFT JOIN devices d ON d.address = fx.address
                   WHERE fx.ts >= ? AND fx.ts < ?
                     AND fx.lat BETWEEN ? AND ? AND fx.lon BETWEEN ? AND ?""",
                (start_ts, end_ts, LAT - dlat, LAT + dlat, LON - dlon, LON + dlon)).fetchall()
        finally:
            db.close()

    # tug positions bucketed by time for the aerotow exclusion
    dt = p["tug_dt_s"]
    tugs = {}
    for lat, lon, alt, cfpm, rot, addr, ts, typ in rows:
        if typ == "tow":
            x, y = to_xy(lat, lon)
            tugs.setdefault(ts // dt, []).append((x, y, alt, ts))

    def near_tug(x, y, alt, ts):
        b0 = ts // dt
        for b in (b0 - 1, b0, b0 + 1):
            for tx, ty, ta, tts in tugs.get(b, ()):
                if abs(tts - ts) <= dt and abs(ta - alt) <= p["tug_dz_ft"] \
                        and (tx - x) ** 2 + (ty - y) ** 2 <= p["tug_r_m"] ** 2:
                    return True
        return False

    cell_m = p["cell_m"]
    exposure = {}                 # cell -> all glider fixes through it
    acdays = {}                   # cell -> set of (address, day) that thermalled
    cellpts = {}                  # cell -> [(x, y, alt, climb)]
    for lat, lon, alt, cfpm, rot, addr, ts, typ in rows:
        if typ != "glider":
            continue
        x, y = to_xy(lat, lon)
        if x * x + y * y > R * R:
            continue
        c = (round(x / cell_m), round(y / cell_m))
        exposure[c] = exposure.get(c, 0) + 1
        if cfpm is None or rot is None or not (p["climb_floor"] <= cfpm <= p["climb_cap"]) \
                or abs(rot) < p["rot_min"] or near_tug(x, y, alt, ts):
            continue
        acdays.setdefault(c, set()).add((addr, ts // 86400))
        cellpts.setdefault(c, []).append((x, y, alt, cfpm))

    hot = {c for c in cellpts
           if len(acdays[c]) >= p["min_ac_days"]
           and len(cellpts[c]) / max(exposure.get(c, 1), 1) >= p["min_ratio"]}

    # flood-fill contiguous hot cells (8-neighbour) into clusters
    seen, clusters = set(), []
    for c in hot:
        if c in seen:
            continue
        stack, comp = [c], []
        seen.add(c)
        while stack:
            cc = stack.pop()
            comp.append(cc)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cc[0] + dx, cc[1] + dy)
                    if nb in hot and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        clusters.append(comp)

    hotspots = []
    for comp in clusters:
        pts, ad = [], set()
        for cell in comp:
            pts += cellpts[cell]
            ad |= acdays[cell]
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        al = [q[2] for q in pts]
        cl = [q[3] for q in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = (sum((x - mx) ** 2 for x in xs) + sum((y - my) ** 2 for y in ys)) / (2 * len(xs))
        radius = max(150.0, p["k_sigma"] * math.sqrt(var))
        lo_t, hi_t = _pct(al, .33), _pct(al, .66)
        lo = [q for q in pts if q[2] <= lo_t] or pts
        hi = [q for q in pts if q[2] >= hi_t] or pts
        lcx = sum(q[0] for q in lo) / len(lo)
        lcy = sum(q[1] for q in lo) / len(lo)
        hcx = sum(q[0] for q in hi) / len(hi)
        hcy = sum(q[1] for q in hi) / len(hi)
        blat, blon = to_ll(lcx, lcy)
        tlat, tlon = to_ll(hcx, hcy)
        clat, clon = to_ll(mx, my)
        drift = math.hypot(hcx - lcx, hcy - lcy)
        bearing = (math.degrees(math.atan2(hcx - lcx, hcy - lcy)) + 360) % 360
        hotspots.append(dict(
            lat=round(clat, 6), lon=round(clon, 6), radius_m=round(radius, 1),
            base_lat=round(blat, 6), base_lon=round(blon, 6), base_ft=round(_pct(al, .10), 1),
            top_lat=round(tlat, 6), top_lon=round(tlon, 6), top_ft=round(_pct(al, .90), 1),
            climb_kt=round(sum(cl) / len(cl) / 101.3, 2), ac_days=len(ad), fixes=len(cl),
            drift_m=round(drift, 1), drift_deg=round(bearing)))
    hotspots.sort(key=lambda h: -h["ac_days"])
    return hotspots


# --- storage (a small separate SQLite so it never contends with the live collector) -------
def store_path(data_dir=None):
    return os.path.join(data_dir or config.DATA_DIR, "thermals.sqlite")


def open_store(data_dir=None):
    db = sqlite3.connect(store_path(data_dir))
    db.executescript(SCHEMA)
    return db


def save(db, start_ts, end_ts, hotspots):
    """Replace all stored hotspots with this computation (the rolling 'recent' set)."""
    now = int(time.time())
    db.execute("DELETE FROM thermals")
    db.executemany(
        """INSERT INTO thermals (computed_at, window_start, window_end, lat, lon, radius_m,
             base_lat, base_lon, base_ft, top_lat, top_lon, top_ft,
             climb_kt, ac_days, fixes, drift_m, drift_deg)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(now, start_ts, end_ts, h["lat"], h["lon"], h["radius_m"],
          h["base_lat"], h["base_lon"], h["base_ft"], h["top_lat"], h["top_lon"], h["top_ft"],
          h["climb_kt"], h["ac_days"], h["fixes"], h["drift_m"], h["drift_deg"])
         for h in hotspots])
    db.commit()


def load(db):
    """Stored hotspots as a list of dicts (newest computation), plus window metadata."""
    cur = db.execute(
        """SELECT lat, lon, radius_m, base_lat, base_lon, base_ft, top_lat, top_lon, top_ft,
                  climb_kt, ac_days, fixes, drift_m, drift_deg, window_start, window_end, computed_at
           FROM thermals ORDER BY ac_days DESC""")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_cached(data_dir=None):
    """Stored hotspots (read-only; never creates/writes), or [] if none computed yet."""
    p = store_path(data_dir)
    if not os.path.exists(p):
        return []
    db = sqlite3.connect("file:" + os.path.abspath(p) + "?mode=ro", uri=True)
    try:
        return load(db)
    finally:
        db.close()


def recompute(data_dir, site, days=7, params=None):
    """Compute the last `days` days' hotspots and store them. Returns the hotspot list."""
    end = int(time.time())
    start = end - days * 86400
    hotspots = compute(data_dir, site, start, end, params=params)
    db = open_store(data_dir)
    try:
        save(db, start, end, hotspots)
    finally:
        db.close()
    return hotspots
