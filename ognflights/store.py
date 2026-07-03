"""SQLite storage for OGN fixes and resolved device metadata."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixes (
    address   TEXT NOT NULL,
    ts        INTEGER NOT NULL,          -- epoch seconds (UTC)
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    alt_ft    REAL NOT NULL,             -- GPS altitude, ft (WGS84 geoid)
    speed_kt  INTEGER,
    climb_fpm INTEGER,
    receiver  TEXT,
    PRIMARY KEY (address, ts)
);
CREATE INDEX IF NOT EXISTS idx_fixes_ts ON fixes(ts);

CREATE TABLE IF NOT EXISTS devices (
    address       TEXT PRIMARY KEY,
    address_type  TEXT,
    aircraft_type TEXT,
    registration  TEXT,
    cn            TEXT,
    model         TEXT,
    last_seen     INTEGER
);
"""


@dataclass
class Fix:
    address: str
    ts: int           # epoch seconds UTC
    lat: float
    lon: float
    alt_ft: float
    speed_kt: int | None
    climb_fpm: int | None


class Store:
    def __init__(self, path: str = "data/ogn.sqlite"):
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        # WAL: lets exports read while the collector writes; NORMAL sync is durable enough.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def add_fix(self, b) -> None:
        """Insert a Beacon (ignores duplicate address+ts)."""
        self.db.execute(
            "INSERT OR IGNORE INTO fixes VALUES (?,?,?,?,?,?,?,?)",
            (b.address, int(b.ts.timestamp()), b.lat, b.lon, b.altitude_ft,
             b.speed_kt, b.climb_fpm, b.receiver),
        )

    def upsert_device(self, b, dev) -> None:
        reg = dev.registration if dev else None
        cn = dev.cn if dev else None
        model = dev.model if dev else None
        self.db.execute(
            """INSERT INTO devices (address, address_type, aircraft_type, registration, cn, model, last_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET
                 address_type=excluded.address_type, aircraft_type=excluded.aircraft_type,
                 registration=COALESCE(excluded.registration, devices.registration),
                 cn=COALESCE(excluded.cn, devices.cn),
                 model=COALESCE(excluded.model, devices.model),
                 last_seen=excluded.last_seen""",
            (b.address, b.address_type, b.aircraft_type, reg, cn, model, int(b.ts.timestamp())),
        )

    def commit(self) -> None:
        self.db.commit()

    def day_bounds(self, day: datetime) -> tuple[int, int]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(start.timestamp()), int(start.timestamp()) + 86400

    def addresses_on_day(self, day: datetime) -> list[tuple[str, str, str, int]]:
        """(address, label, aircraft_type, fix_count) for aircraft seen on `day`."""
        lo, hi = self.day_bounds(day)
        rows = self.db.execute(
            """SELECT f.address,
                      COALESCE(d.registration || COALESCE(' ['||d.cn||']',''), f.address) AS label,
                      COALESCE(d.aircraft_type, '?'), COUNT(*)
               FROM fixes f LEFT JOIN devices d ON d.address = f.address
               WHERE f.ts >= ? AND f.ts < ?
               GROUP BY f.address ORDER BY 4 DESC""",
            (lo, hi),
        ).fetchall()
        return rows

    def fixes_for(self, address: str, lo: int, hi: int) -> list[Fix]:
        rows = self.db.execute(
            """SELECT address, ts, lat, lon, alt_ft, speed_kt, climb_fpm
               FROM fixes WHERE address = ? AND ts >= ? AND ts < ? ORDER BY ts""",
            (address, lo, hi),
        ).fetchall()
        return [Fix(*r) for r in rows]

    def addresses_for_reg(self, reg: str) -> list[str]:
        """Device addresses whose registration or CN matches `reg` (substring, case-insensitive)."""
        like = f"%{reg.upper()}%"
        rows = self.db.execute(
            "SELECT address FROM devices WHERE UPPER(registration) LIKE ? OR UPPER(cn) LIKE ?",
            (like, like),
        ).fetchall()
        return [r[0] for r in rows]

    def device_label(self, address: str) -> tuple[str, str]:
        row = self.db.execute(
            "SELECT registration, cn, model FROM devices WHERE address = ?", (address,)
        ).fetchone()
        if row and row[0]:
            label = row[0] + (f" [{row[1]}]" if row[1] else "")
            return label, (row[2] or "")
        return address, ""


# --- year-partitioned storage: one SQLite file per calendar year, kept indefinitely ---

def year_file(year: int, data_dir: str | None = None) -> str:
    import os
    from .config import DATA_DIR
    return os.path.join(data_dir or DATA_DIR, f"ogn-{year}.sqlite")


def store_for_day(day: datetime, data_dir: str | None = None) -> "Store":
    """Open the SQLite file for the calendar year containing `day`."""
    return Store(year_file(day.year, data_dir))
