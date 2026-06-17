"""OGN Device Database (DDB) lookup: device hex id -> registration / model / type.

The DDB is an opt-in registry mapping FLARM/ICAO/OGN device ids to aircraft
details. We download it once and cache to disk. Aircraft not in the DDB (or
flagged no-track) simply resolve to None and are shown by raw id.

Source: https://ddb.glidernet.org/download/?j=1  (JSON)
"""
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DDB_URL = "https://ddb.glidernet.org/download/?j=1"
CACHE_TTL_SECONDS = 24 * 3600


@dataclass
class Device:
    registration: str | None
    cn: str | None            # competition number
    model: str | None
    aircraft_type: str | None
    tracked: bool
    identified: bool


class DDB:
    def __init__(self, cache_path: str = "ddb_cache.json"):
        self._cache_path = cache_path
        self._by_id: dict[str, Device] = {}

    def load(self, force: bool = False) -> int:
        """Load the DDB from cache (or download if stale/missing). Returns count."""
        data = None
        if not force and os.path.exists(self._cache_path):
            age = time.time() - os.path.getmtime(self._cache_path)
            if age < CACHE_TTL_SECONDS:
                with open(self._cache_path) as f:
                    data = json.load(f)
                logger.info("DDB from cache (%d s old)", int(age))
        if data is None:
            logger.info("downloading DDB from %s", DDB_URL)
            req = urllib.request.Request(DDB_URL, headers={"User-Agent": "ognflights/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            with open(self._cache_path, "w") as f:
                json.dump(data, f)
        self._index(data)
        return len(self._by_id)

    def _index(self, data: dict) -> None:
        self._by_id.clear()
        for d in data.get("devices", []):
            dev_id = (d.get("device_id") or "").upper()
            if not dev_id:
                continue
            self._by_id[dev_id] = Device(
                registration=d.get("registration") or None,
                cn=d.get("cn") or None,
                model=d.get("aircraft_model") or None,
                aircraft_type=d.get("aircraft_type") or None,
                tracked=str(d.get("tracked", "Y")).upper() != "N",
                identified=str(d.get("identified", "Y")).upper() != "N",
            )

    def lookup(self, address: str) -> Device | None:
        return self._by_id.get(address.upper())

    def label(self, address: str) -> str:
        """Human label: registration (CN) if known, else raw id."""
        d = self.lookup(address)
        if d and d.registration:
            return f"{d.registration}" + (f" [{d.cn}]" if d.cn else "")
        return address
