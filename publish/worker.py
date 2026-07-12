"""Hourly publish worker: push the last N days of flight data to the public-data branch.

Runs as a daemon thread inside the `watch` collector process (started from
cli.cmd_watch when OGNFLIGHTS_PUBLISH=1). It maintains its OWN git clone/worktree
of the `public-data` branch at a PERSISTENT path on the data volume, because the
container is built with COPY and therefore has no .git of the app repo, and because
the workdir must survive container restarts.

Design constraints (see CLAUDE.md):
  - Publishing is OFF unless OGNFLIGHTS_PUBLISH=1, so local/dev runs never push.
  - The year DB is opened READ-ONLY by the sync, so this never disturbs capture.
  - Every publish is fully exception-guarded: a failure (network, auth, git) MUST
    NEVER crash or stall the capture loop or the webapp. We log a warning and retry
    next interval, leaving the last good branch state intact.
  - First run handles the branch not existing yet by creating it as an ORPHAN branch
    (no history from main) holding only the JSON data + manifest.

Config (all via env):
  OGNFLIGHTS_PUBLISH            "1" to enable (default off)
  OGNFLIGHTS_PUBLISH_REMOTE     git remote, e.g. git@github.com:8none1/ognflights.git
  OGNFLIGHTS_PUBLISH_BRANCH     branch to publish to (default public-data)
  OGNFLIGHTS_DEPLOY_KEY         path to the SSH private deploy key
  OGNFLIGHTS_PUBLISH_DAYS       days back to publish (default 7)
  OGNFLIGHTS_PUBLISH_INTERVAL_S seconds between runs (default 3600)
  OGNFLIGHTS_PUBLISH_WORKDIR    persistent git worktree path (default /app/data/.publish-repo)
"""
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GIT_BOT_NAME = "ognflights bot"
GIT_BOT_EMAIL = "ognflights-bot@users.noreply.github.com"


def _env(name, default=None):
    return os.environ.get(name, default)


def enabled() -> bool:
    return _env("OGNFLIGHTS_PUBLISH") == "1"


def _git_env(deploy_key: str) -> dict:
    """Environment for git so it uses the deploy key and never prompts."""
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {deploy_key} -o StrictHostKeyChecking=accept-new "
        f"-o IdentitiesOnly=yes -o BatchMode=yes"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# Disable git's *detached* background maintenance. After a fetch/commit git otherwise
# double-forks a `git gc/maintenance --auto` that we never spawned directly: it reparents
# to PID 1 (this process, as the container's init) and, because nothing reaps arbitrary
# orphans here, lingers as a <defunct> git zombie forever (hundreds observed in prod).
# With autoDetach off any auto-maintenance runs INLINE in the git command below, which
# subprocess.run() waits on and reaps like every other child. Packing still happens; it
# just no longer escapes into an unreapable background process.
_NO_DETACH = ["-c", "gc.autoDetach=false", "-c", "maintenance.autoDetach=false"]


def _git(workdir, *args, env=None, check=True, capture=False):
    """Run a git command in workdir. Returns CompletedProcess (waited on + reaped)."""
    return subprocess.run(
        ["git", "-C", workdir, *_NO_DETACH, *args],
        env=env, check=check,
        capture_output=capture, text=True,
    )


def _ensure_repo(workdir, remote, branch, env):
    """Ensure `workdir` is a git repo on `branch` tracking `remote`.

    Handles three states:
      - workdir missing / not a repo -> init, add remote, configure identity.
      - remote branch exists         -> fetch + hard-reset onto it (discarding any
                                        local divergence so we always build from the
                                        last published state).
      - remote branch does not exist -> orphan branch (no history from main).
    Returns True if the remote branch already existed.
    """
    os.makedirs(workdir, exist_ok=True)
    if not os.path.isdir(os.path.join(workdir, ".git")):
        _git(workdir, "init", "-q", env=env)
    # Ensure the remote points at the configured URL (idempotent).
    remotes = _git(workdir, "remote", env=env, capture=True).stdout.split()
    if "origin" in remotes:
        _git(workdir, "remote", "set-url", "origin", remote, env=env)
    else:
        _git(workdir, "remote", "add", "origin", remote, env=env)
    _git(workdir, "config", "user.name", GIT_BOT_NAME, env=env)
    _git(workdir, "config", "user.email", GIT_BOT_EMAIL, env=env)

    # Does the branch exist on the remote?
    ls = _git(workdir, "ls-remote", "--heads", "origin", branch, env=env, capture=True)
    branch_exists = bool(ls.stdout.strip())

    if branch_exists:
        _git(workdir, "fetch", "-q", "origin", branch, env=env)
        # Point local branch at the fetched head, discarding any local state.
        _git(workdir, "checkout", "-q", "-B", branch, "FETCH_HEAD", env=env)
        _git(workdir, "reset", "-q", "--hard", "FETCH_HEAD", env=env)
        # Drop any stale untracked files from a previous partial run.
        _git(workdir, "clean", "-fdq", env=env)
    else:
        # Fresh orphan branch: no history from main, only the data files we add.
        _git(workdir, "checkout", "-q", "--orphan", branch, env=env)
        # An orphan checkout stages whatever was in the index/workdir; start clean.
        _git(workdir, "reset", "-q", env=env, check=False)
    return branch_exists


def _has_staged_changes(workdir, env) -> bool:
    return _git(workdir, "diff", "--cached", "--quiet", env=env, check=False).returncode != 0


def publish_once() -> bool:
    """Run one publish cycle. Returns True if it pushed a new commit, else False.

    Fully self-contained and exception-guarded by the caller. Raises on failure so
    the caller can log; callers in the worker loop swallow the exception.
    """
    remote = _env("OGNFLIGHTS_PUBLISH_REMOTE")
    if not remote:
        raise RuntimeError("OGNFLIGHTS_PUBLISH_REMOTE is not set")
    deploy_key = _env("OGNFLIGHTS_DEPLOY_KEY")
    if not deploy_key or not os.path.exists(deploy_key):
        raise RuntimeError(f"deploy key not found: {deploy_key!r}")
    branch = _env("OGNFLIGHTS_PUBLISH_BRANCH", "public-data")
    workdir = _env("OGNFLIGHTS_PUBLISH_WORKDIR", "/app/data/.publish-repo")
    days = int(_env("OGNFLIGHTS_PUBLISH_DAYS", "7"))

    # Import here so a headless collector without publishing needn't import replay/etc.
    from ognflights.config import DATA_DIR
    from publish.sync_public import sync

    env = _git_env(deploy_key)
    _ensure_repo(workdir, remote, branch, env)

    # Build the JSONs + manifest into the worktree, reading the year DB read-only.
    written, manifest = sync(workdir, data_dir=DATA_DIR, days=days)
    logger.info("publish: %d day(s) with flights; changed: %s",
                len(manifest["days"]),
                ", ".join(written) if written else "(none)")

    _git(workdir, "add", "-A", env=env)
    if not _has_staged_changes(workdir, env):
        logger.info("publish: no changes to push")
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    _git(workdir, "commit", "-q", "-m", f"public-data: refresh {stamp}", env=env)
    _git(workdir, "push", "-q", "origin", f"HEAD:{branch}", env=env)
    logger.info("publish: pushed to %s", branch)
    return True


def _loop(interval_s):
    """Worker loop: bootstrap publish on startup, then every interval_s."""
    while True:
        try:
            publish_once()
        except subprocess.CalledProcessError as e:
            logger.warning("publish failed (git): %s%s", e,
                           f"\n{e.stderr}" if getattr(e, "stderr", None) else "")
        except Exception as e:  # never let a publish error touch capture
            logger.warning("publish failed: %s", e)
        time.sleep(interval_s)


def start_worker():
    """Start the hourly publish worker as a daemon thread, if enabled.

    Returns the Thread, or None if publishing is off. Never raises: any setup error
    is logged so it can never take down the collector.
    """
    if not enabled():
        logger.info("publish worker disabled (set OGNFLIGHTS_PUBLISH=1 to enable)")
        return None
    interval_s = int(_env("OGNFLIGHTS_PUBLISH_INTERVAL_S", "3600"))
    logger.info("publish worker enabled: every %ds -> %s (%s)",
                interval_s,
                _env("OGNFLIGHTS_PUBLISH_BRANCH", "public-data"),
                _env("OGNFLIGHTS_PUBLISH_REMOTE"))
    t = threading.Thread(target=_loop, args=(interval_s,), daemon=True)
    t.start()
    return t
