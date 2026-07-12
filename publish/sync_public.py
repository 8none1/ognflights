#!/usr/bin/env python3
"""Build the per-day JSON + manifest for the public flight dashboard.

For each of the last N days it builds that day's replay DATA (all followed
aircraft, full registration/CN labels, the same simplification the private
all-gliders view uses) and writes it as <YYYY-MM-DD>.json. It also writes a
manifest.json listing the days that actually have flights, newest first.

The output dir is what perceptron will (in Phase 2) commit to the `public-data`
branch of the ognflights repo; the public page fetches those files from
https://raw.githubusercontent.com/8none1/ognflights/public-data/ .

Reads open the year SQLite READ-ONLY (WAL-safe), so this never disturbs the
live `watch` collector.

Examples:
  # write the last 7 days into ./public-out (idempotent; only changed files touched)
  python3 publish/sync_public.py --data-dir data --out public-out

  # module form
  python3 -m publish.sync_public --data-dir data --out public-out --days 7

  # commit into a public-data worktree (Phase 2; --push actually pushes, off by default)
  python3 publish/sync_public.py --out /path/to/public-data-worktree --commit
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ognflights.store import store_for_day
from replay.make_replay import build_payload, collect

# Match the private all-gliders view (webapp._render_replay): simplify + coarse tails
# scale with aircraft count so busy days stay small.
def _simplify_for(n_ac):
    return max(0, min(60, (n_ac - 4) * 4))


def _write_if_changed(path, text):
    """Write `text` to `path` only if its content differs. Returns True if written."""
    if os.path.exists(path):
        with open(path) as f:
            if f.read() == text:
                return False
    with open(path, "w") as f:
        f.write(text)
    return True


def build_day(day, data_dir, models_url="models"):
    """Build the public DATA dict for a single day, or None if nothing flew.
    All followed aircraft, full reg/CN labels, all-gliders simplification."""
    store = store_for_day(day, data_dir=data_dir, read_only=True)
    try:
        n_ac = len(store.addresses_on_day(day))
        simplify = _simplify_for(n_ac)
        # gliders=False -> every followed aircraft (the watch daemon already stores only ours)
        flights, legend = collect(store, day, reg_spec=None, gliders=False,
                                  simplify=simplify)
    finally:
        store.close()
    if not flights:
        return None
    daystr = day.strftime("%Y-%m-%d")
    return build_payload(flights, legend, f"Gransden {daystr}", models_url=models_url)


def sync(out_dir, data_dir="data", days=7, models_url="models", today=None):
    """Build the last `days` day-JSONs + manifest.json into out_dir.
    Prunes day files older than the window. Returns (written_files, manifest)."""
    os.makedirs(out_dir, exist_ok=True)
    today = today or datetime.now(timezone.utc)
    today = today.replace(hour=0, minute=0, second=0, microsecond=0)
    window = [today - timedelta(days=i) for i in range(days)]
    keep = {d.strftime("%Y-%m-%d") + ".json" for d in window}

    written = []
    entries = []  # newest first
    for day in window:   # window is already newest-first
        daystr = day.strftime("%Y-%m-%d")
        payload = build_day(day, data_dir, models_url=models_url)
        if payload is None:
            # stale file for a day that now has no flights: drop it
            stale = os.path.join(out_dir, daystr + ".json")
            if os.path.exists(stale):
                os.remove(stale)
            continue
        text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        path = os.path.join(out_dir, daystr + ".json")
        if _write_if_changed(path, text):
            written.append(os.path.basename(path))
        entries.append({"day": daystr,
                        "flights": len(payload["flights"]),
                        "aircraft": len(payload["legend"])})

    # prune day files outside the window (e.g. yesterday's slid out of a rolling 7)
    for fn in os.listdir(out_dir):
        if fn.endswith(".json") and fn != "manifest.json" and fn not in keep:
            os.remove(os.path.join(out_dir, fn))
            written.append("(pruned " + fn + ")")

    manifest = {"generated": int(time.time()),
                "days": entries}
    # manifest 'generated' changes every run; only rewrite if the day list changed, so
    # hourly runs with no new day don't churn the file. Compare ignoring 'generated'.
    mpath = os.path.join(out_dir, "manifest.json")
    prev_days = None
    if os.path.exists(mpath):
        try:
            with open(mpath) as f:
                prev_days = json.load(f).get("days")
        except (ValueError, OSError):
            prev_days = None
    if prev_days != entries:
        with open(mpath, "w") as f:
            json.dump(manifest, f, separators=(",", ":"), sort_keys=True)
        written.append("manifest.json")
    return written, manifest


# Never let git spin off a *detached* background gc/maintenance: as a container PID 1 those
# orphans reparent to us and are never reaped (see publish/worker.py for the full story).
# autoDetach=false keeps any auto-maintenance inline, where subprocess.run() reaps it.
_NO_DETACH = ["-c", "gc.autoDetach=false", "-c", "maintenance.autoDetach=false"]


def _git(out_dir, *args):
    subprocess.run(["git", "-C", out_dir, *_NO_DETACH, *args], check=True)


def _commit(out_dir, push=False):
    """Stage + commit the JSON changes in a public-data worktree. Pushes only if `push`."""
    _git(out_dir, "add", "-A")
    # nothing staged -> skip the commit (avoids empty commits on a no-op run)
    if subprocess.run(["git", "-C", out_dir, *_NO_DETACH,
                       "diff", "--cached", "--quiet"]).returncode == 0:
        print("no changes to commit")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    _git(out_dir, "commit", "-m", f"public-data: refresh {stamp}")
    if push:
        _git(out_dir, "push", "origin", "HEAD")
        print("pushed public-data")
    else:
        print("committed (not pushed; pass --push to push)")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output dir (or a public-data git worktree)")
    p.add_argument("--data-dir", default="data", help="dir holding ogn-YYYY.sqlite (default: data)")
    p.add_argument("--days", type=int, default=7, help="how many days back to publish (default 7)")
    p.add_argument("--models-url", default="models",
                   help="relative URL to the .glb models from the public page (default: models)")
    p.add_argument("--today", help="override 'today' as YYYY-MM-DD (for testing)")
    p.add_argument("--commit", action="store_true",
                   help="git add/commit the changes in --out (must be a public-data worktree)")
    p.add_argument("--push", action="store_true",
                   help="with --commit, also push (Phase 2 only; leave off in Phase 1)")
    a = p.parse_args()

    today = None
    if a.today:
        today = datetime.strptime(a.today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    written, manifest = sync(a.out, data_dir=a.data_dir, days=a.days,
                             models_url=a.models_url, today=today)
    print(f"days with flights: {len(manifest['days'])}; files written: "
          f"{', '.join(written) if written else '(none - all up to date)'}")
    for d in manifest["days"]:
        print(f"  {d['day']}  {d['flights']} flights, {d['aircraft']} aircraft")
    if a.commit:
        _commit(a.out, push=a.push)


if __name__ == "__main__":
    main()
