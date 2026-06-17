"""Spike: connect to OGN APRS-IS, filter around Gransden, dump aircraft beacons."""
import socket, time, sys

HOST, PORT = "aprs.glidernet.org", 14580
LAT, LON, RADIUS_KM = 52.18, -0.11, 60
LOGIN = f"user OGNSPK pass -1 vers ogn-spike 0.1 filter r/{LAT}/{LON}/{RADIUS_KM}\r\n"
RUN_SECONDS = 30

def main():
    print(f"connecting to {HOST}:{PORT} ...")
    s = socket.create_connection((HOST, PORT), timeout=15)
    s.settimeout(5)
    f = s.makefile("rwb")
    banner = f.readline().decode("utf-8", "replace").strip()
    print("banner:", banner)
    f.write(LOGIN.encode()); f.flush()
    print("sent login, listening", RUN_SECONDS, "s ...\n")

    start = time.time()
    aircraft, receivers, lines = {}, set(), 0
    while time.time() - start < RUN_SECONDS:
        try:
            raw = f.readline()
        except socket.timeout:
            continue
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip()
        lines += 1
        if line.startswith("#") or not line:
            continue
        src = line.split(">", 1)[0]
        if ":/" in line or ":!" in line:        # position packet
            payload = line.split(":", 1)[1]
            if src.startswith(("FLR", "ICA", "OGN", "PAW", "FNT", "FLD")):
                alt = ""
                if "A=" in payload:
                    alt = payload.split("A=", 1)[1][:6]
                if src not in aircraft:
                    print(f"  AIRCRAFT {src:12} alt={alt}  {payload[:50]}")
                aircraft[src] = alt
            else:
                receivers.add(src)
    print(f"\n--- {RUN_SECONDS}s: {lines} lines, {len(aircraft)} aircraft, {len(receivers)} receiver/other stations ---")
    print("aircraft IDs:", sorted(aircraft))
    print("sample receivers:", sorted(receivers)[:10])

if __name__ == "__main__":
    main()
