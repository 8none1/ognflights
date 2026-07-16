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
    course    INTEGER,                  -- heading, degrees 0-359 (from the APRS CSE field)
    rot       REAL,                     -- rate of turn, half-turns/min (OGN `rot` field)
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
    course: int | None = None    # heading, degrees; None on older rows / CGC backfill
    rot: float | None = None     # rate of turn, half-turns/min; None when not reported


class Store:
    def __init__(self, path: str = "data/ogn.sqlite", read_only: bool = False):
        import os
        self.path = path
        self.read_only = read_only
        if read_only:
            # Open read-only (immutable=0 so WAL commits from the live collector are still
            # visible). Never creates the file, never writes: safe against the running daemon.
            uri = f"file:{os.path.abspath(path)}?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        # WAL: lets exports read while the collector writes; NORMAL sync is durable enough.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate_fixes()
        self._dedup_fixes()
        self.db.commit()

    def _migrate_fixes(self) -> None:
        """Add columns introduced after a DB file was first created (idempotent).

        CREATE TABLE IF NOT EXISTS never alters an existing table, so year files
        made before a column existed need it added in place. ALTER ... ADD COLUMN
        appends nullable columns cheaply (no row rewrite); safe to run every open.
        """
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(fixes)").fetchall()}
        if "course" not in cols:
            self.db.execute("ALTER TABLE fixes ADD COLUMN course INTEGER")
        if "rot" not in cols:
            self.db.execute("ALTER TABLE fixes ADD COLUMN rot REAL")

    def _fixes_index_cols(self, name: str) -> list[str]:
        return [r[2] for r in self.db.execute(f"PRAGMA index_info({name})").fetchall()]

    def _dedup_fixes(self) -> None:
        """Guarantee exactly one row per (address, ts), backed by a UNIQUE index.

        The same beacon reaches us once per ground receiver that heard it, so a
        DB must dedup on (address, ts). The current schema does this with the
        fixes PRIMARY KEY, whose autoindex is UNIQUE on (address, ts) - in that
        case this is a no-op and we must NOT add a second, redundant unique index
        (which would double per-insert B-tree maintenance). A legacy DB created
        without that constraint could hold duplicates and needs bringing into
        line. Idempotent and cheap once a suitable unique index exists, so it is
        safe to run on every open, including at container startup.
        """
        idx = self.db.execute("PRAGMA index_list(fixes)").fetchall()
        # PRAGMA index_list columns: seq, name, unique, origin, partial
        for _seq, name, uniq, *_ in idx:
            if uniq and self._fixes_index_cols(name) == ["address", "ts"]:
                return   # a unique index on exactly (address, ts) already exists
        # No unique (address, ts) index yet. Dedup first (a non-unique index on
        # those columns, or none at all, may have allowed duplicate rows), then
        # add the unique index. If a NON-UNIQUE index on exactly (address, ts)
        # exists, drop it so we upgrade in place rather than keeping two indexes
        # on identical columns.
        self.db.execute(
            "DELETE FROM fixes WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM fixes GROUP BY address, ts)"
        )
        for _seq, name, uniq, *_ in idx:
            if not uniq and not name.startswith("sqlite_") \
                    and self._fixes_index_cols(name) == ["address", "ts"]:
                self.db.execute(f"DROP INDEX {name}")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_fixes_addr_ts ON fixes(address, ts)"
        )

    def close(self) -> None:
        self.db.close()

    def add_fix(self, b) -> bool:
        """Insert a Beacon, ignoring a duplicate (address, ts).

        The same fix reaches us once per ground receiver that heard the aircraft;
        only the first is stored. Returns True if this call actually stored a new
        row, False if it was a duplicate that was ignored. Callers use this to
        avoid re-publishing an already-seen fix to the live view.
        """
        before = self.db.total_changes
        # Explicit column list (not positional VALUES) so it stays correct whether the
        # course/rot columns sit where the fresh schema puts them or where ALTER appended
        # them. getattr keeps the dormant CGC backfill record (no course/rot) working.
        self.db.execute(
            "INSERT OR IGNORE INTO fixes "
            "(address, ts, lat, lon, alt_ft, speed_kt, climb_fpm, receiver, course, rot) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (b.address, int(b.ts.timestamp()), b.lat, b.lon, b.altitude_ft,
             b.speed_kt, b.climb_fpm, b.receiver,
             getattr(b, "course", None), getattr(b, "turn_rate", None)),
        )
        return self.db.total_changes > before

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
            """SELECT address, ts, lat, lon, alt_ft, speed_kt, climb_fpm, course, rot
               FROM fixes WHERE address = ? AND ts >= ? AND ts < ? ORDER BY ts""",
            (address, lo, hi),
        ).fetchall()
        return [Fix(*r) for r in rows]

    def recent_airborne(self, now_ts: int, window_s: int, min_alt_ft: float
                         ) -> list[tuple[str, str, int, float]]:
        """Aircraft to re-acquire after a restart.

        Returns (address, address_type, last_ts, last_alt_ft) for each address whose
        most recent fix is within `window_s` of `now_ts` and was above `min_alt_ft`
        (a clear-flight altitude, so aircraft that have landed are not re-followed).
        address_type comes from the devices table so the caller can reconstruct the
        APRS source callsign for the buddy filter.
        """
        since = now_ts - window_s
        rows = self.db.execute(
            """SELECT f.address, COALESCE(d.address_type, ''), m.mts, f.alt_ft
               FROM (SELECT address, MAX(ts) AS mts FROM fixes
                     WHERE ts >= ? GROUP BY address) m
               JOIN fixes f ON f.address = m.address AND f.ts = m.mts
               LEFT JOIN devices d ON d.address = f.address
               WHERE f.alt_ft > ?""",
            (since, min_alt_ft),
        ).fetchall()
        return [(a, t, ts, alt) for (a, t, ts, alt) in rows]

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


def store_for_day(day: datetime, data_dir: str | None = None, read_only: bool = False) -> "Store":
    """Open the SQLite file for the calendar year containing `day`."""
    return Store(year_file(day.year, data_dir), read_only=read_only)
