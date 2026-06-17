"""Export a flight's fixes to GPX, KML (Google Earth Web friendly) or IGC."""
import re
from datetime import datetime, timezone

from .flights import Flight

FT_TO_M = 0.3048


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def filename(flight: Flight, label: str, ext: str) -> str:
    t0 = _utc(flight.start).strftime("%Y-%m-%d_%H%M%S")
    return _safe(f"{label}_{t0}") + "." + ext


def gpx(flight: Flight, label: str, model: str = "") -> str:
    t0 = _utc(flight.start).strftime("%Y-%m-%d %H:%M")
    name = f"{label} {t0}"
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="ognflights" xmlns="http://www.topografix.com/GPX/1/1">',
           f'  <metadata><name>{name}</name><desc>{model}</desc></metadata>',
           '  <trk>', f'    <name>{name}</name>', '    <trkseg>']
    for f in flight.fixes:
        t = _utc(f.ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(f'      <trkpt lat="{f.lat}" lon="{f.lon}">'
                   f'<ele>{round(f.alt_ft * FT_TO_M, 1)}</ele><time>{t}</time></trkpt>')
    out += ['    </trkseg>', '  </trk>', '</gpx>']
    return "\n".join(out)


def kml(flight: Flight, label: str, model: str = "") -> str:
    """Plain LineString KML (Google Earth Web supports this; gx:Track is desktop-only)."""
    t0 = _utc(flight.start).strftime("%Y-%m-%d %H:%M")
    name = f"{label} {t0}"
    coords = " ".join(f"{f.lon},{f.lat},{round(f.alt_ft * FT_TO_M, 1)}" for f in flight.fixes)
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '  <Document>', f'    <name>{name}</name>',
        '    <Style id="track"><LineStyle><color>ff1e90ff</color><width>3</width></LineStyle></Style>',
        '    <Placemark>', f'      <name>{name}</name>', f'      <description>{model}</description>',
        '      <styleUrl>#track</styleUrl>',
        '      <LineString>', '        <altitudeMode>absolute</altitudeMode>',
        '        <tessellate>1</tessellate>',
        f'        <coordinates>{coords}</coordinates>',
        '      </LineString>', '    </Placemark>', '  </Document>', '</kml>',
    ])


def _igc_latlon(lat: float, lon: float) -> str:
    def fmt(v, deg_width, hemis):
        h = hemis[0] if v >= 0 else hemis[1]
        v = abs(v)
        d = int(v)
        mmm = int(round((v - d) * 60000))
        return f"{d:0{deg_width}d}{mmm:05d}{h}"
    return fmt(lat, 2, "NS") + fmt(lon, 3, "EW")


def igc(flight: Flight, label: str, model: str = "") -> str:
    """Minimal IGC. OGN-derived (no flight-recorder security), GPS altitude only.

    Fine for viewing/analysis (SeeYou, WeGlide replay); not valid for badge claims.
    """
    d = _utc(flight.start)
    lines = [
        f"AOGN{_safe(label)[:3].upper():>3}",
        f"HFDTEDATE:{d.strftime('%d%m%y')}",
        f"HFGTYGLIDERTYPE:{model}",
        f"HFGIDGLIDERID:{label}",
        "HFFTYFRTYPE:OGN/ognflights",
        "HFALGALTGPS:GEO",
    ]
    for f in flight.fixes:
        t = _utc(f.ts).strftime("%H%M%S")
        gps_alt = max(0, int(round(f.alt_ft * FT_TO_M)))
        # B record: time, lat/lon, fix validity, pressure alt (unknown=00000), gps alt
        lines.append(f"B{t}{_igc_latlon(f.lat, f.lon)}A00000{gps_alt:05d}")
    return "\n".join(lines) + "\n"


WRITERS = {"gpx": gpx, "kml": kml, "igc": igc}


# Distinct KML line colours (aabbggrr) for cycling per aircraft.
_PALETTE = [
    "ff1e90ff", "ff32cd32", "ff0000ff", "ffff9000", "ffff00ff",
    "ff00ffff", "ff8000ff", "ff00a5ff", "ff808000", "ffcb3aff",
]


def _linestring(coords: str, style_id: str, name: str, desc: str) -> list[str]:
    return [
        '    <Placemark>', f'      <name>{name}</name>', f'      <description>{desc}</description>',
        f'      <styleUrl>#{style_id}</styleUrl>',
        '      <LineString><altitudeMode>absolute</altitudeMode><tessellate>1</tessellate>',
        f'        <coordinates>{coords}</coordinates>',
        '      </LineString>', '    </Placemark>',
    ]


def kml_tracks(tracks: list[tuple[str, str, list]], doc_name: str) -> str:
    """One KML Document containing every track as a colour-coded folder.

    tracks: list of (label, model, fixes). Each becomes a folder with a track
    placemark (and a start pin), so the whole capture opens at once.
    """
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2">', '  <Document>',
           f'    <name>{doc_name}</name>']
    for i in range(len(_PALETTE)):
        out.append(f'    <Style id="c{i}"><LineStyle><color>{_PALETTE[i]}</color>'
                   f'<width>2</width></LineStyle><IconStyle><scale>0.7</scale></IconStyle></Style>')
    for idx, (label, model, fixes) in enumerate(tracks):
        if not fixes:
            continue
        sid = f"c{idx % len(_PALETTE)}"
        coords = " ".join(f"{f.lon},{f.lat},{round(f.alt_ft * FT_TO_M, 1)}" for f in fixes)
        t0 = _utc(fixes[0].ts).strftime("%H:%M")
        t1 = _utc(fixes[-1].ts).strftime("%H:%M")
        desc = f"{model} | {len(fixes)} fixes | {t0}-{t1} UTC"
        out.append(f'    <Folder><name>{label}</name>')
        out += _linestring(coords, sid, label, desc)
        out += ['      <Placemark><name>start</name>',
                f'        <styleUrl>#{sid}</styleUrl>',
                f'        <Point><coordinates>{fixes[0].lon},{fixes[0].lat},'
                f'{round(fixes[0].alt_ft * FT_TO_M, 1)}</coordinates></Point></Placemark>',
                '    </Folder>']
    out += ['  </Document>', '</kml>']
    return "\n".join(out)
