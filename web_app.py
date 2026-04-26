"""Flask dashboard for the Job Hunt AI Agent.

Starts a local web UI that can:
- Run the agent as a subprocess and tail logs live.
- Browse / filter / update the scraped jobs table.
- Show a kanban of statuses.
- Read and (carefully) write `config.yaml`.
- Show recent run history + stats.

All state shared between threads goes through a `threading.Lock` (ring-buffer
logs per task, running process registry). The DB is shared safely with the CLI
because `JobDatabase` uses WAL + 30s busy_timeout.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, abort, jsonify, render_template, request

from job_db import VALID_STATUSES, JobDatabase, resolve_db_path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

logger = logging.getLogger("web_app")

app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
)

# --- Ring-buffer log store per task --------------------------------------- #

_RUN_LOCK = threading.Lock()
_running_tasks: dict[str, list[str]] = {}
_running_procs: dict[str, subprocess.Popen] = {}
_task_status: dict[str, dict[str, Any]] = {}  # {task_id: {state, started_at, finished_at, returncode}}


def _append_log(task_id: str, line: str) -> None:
    with _RUN_LOCK:
        buf = _running_tasks.setdefault(task_id, [])
        buf.append(line.rstrip("\r\n"))
        # Keep last 2000 lines
        if len(buf) > 2000:
            del buf[: len(buf) - 2000]


def _reader_thread(task_id: str, proc: subprocess.Popen) -> None:
    """Drain stdout of `proc` into the task's ring buffer.

    Robust against:
    - child dying without flushing / closing stdout cleanly on Windows,
    - exceptions from the readline iterator itself,
    - pipe being kept open by orphaned grandchildren (chromedriver etc).
    """
    assert proc.stdout is not None
    try:
        while True:
            try:
                raw = proc.stdout.readline()
            except (ValueError, OSError) as e:
                _append_log(task_id, f"[web_app] reader: stdout read failed: {e}")
                break
            if raw:
                _append_log(task_id, raw)
                continue
            # empty string -> EOF. But on Windows the pipe can also hand us ""
            # briefly while the child is still alive; only treat it as EOF if
            # the child has actually exited.
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    except Exception as e:
        _append_log(task_id, f"[web_app] reader: unexpected error: {e}")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            rc = proc.wait()
        # Shred the per-run config tempfile (may contain Naukri password etc).
        with _RUN_LOCK:
            status = _task_status.get(task_id, {})
            cfg_path = status.get("run_cfg_path")
            _task_status[task_id] = {
                **status,
                "state": "done",
                "finished_at": time.time(),
                "returncode": rc,
            }
        if cfg_path:
            try:
                Path(cfg_path).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not delete run config tempfile %s: %s", cfg_path, e)
        _append_log(task_id, f"--- process exited with code {rc} ---")


def _reap_zombie_tasks() -> None:
    """Flip any 'running' tasks whose child is actually dead to 'done'.

    Called on every /api/logs read as a safety net in case the reader thread
    itself dies without flipping state (e.g. handle leak).
    """
    with _RUN_LOCK:
        for tid, proc in list(_running_procs.items()):
            status = _task_status.get(tid, {})
            if status.get("state") == "running" and proc.poll() is not None:
                status.update(
                    state="done",
                    finished_at=time.time(),
                    returncode=proc.returncode,
                )
                _task_status[tid] = status
                _running_tasks.setdefault(tid, []).append(
                    f"--- process exited with code {proc.returncode} (reaped) ---"
                )


# --- DB helpers ----------------------------------------------------------- #


def _db() -> JobDatabase:
    return JobDatabase(db_path=resolve_db_path())


def _load_config_dict() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _enabled_platforms_count() -> int:
    cfg = _load_config_dict()
    plats = cfg.get("platforms", {}) or {}
    return sum(1 for v in plats.values() if (v or {}).get("enabled"))


def _enabled_platforms_list() -> list[str]:
    cfg = _load_config_dict()
    plats = cfg.get("platforms", {}) or {}
    return sorted(k for k, v in plats.items() if (v or {}).get("enabled"))


# --- Routes --------------------------------------------------------------- #


@app.route("/")
def index() -> Any:
    return render_template("web_app_template.html")


@app.route("/api/stats")
def api_stats() -> Any:
    stats = _db().get_stats()
    stats["platforms_enabled"] = _enabled_platforms_count()
    stats["platforms_with_jobs"] = len(stats.get("by_platform", {}) or {})
    stats["enabled_platforms"] = _enabled_platforms_list()
    return jsonify(stats)


@app.route("/api/jobs")
def api_jobs() -> Any:
    q = request.args.get("q", "", type=str).strip()
    status = request.args.get("status", "", type=str).strip() or None
    platform = request.args.get("platform", "", type=str).strip() or None
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    rows = _db().search_jobs(
        query=q, status=status, platform=platform, limit=limit, offset=offset
    )
    return jsonify({"count": len(rows), "jobs": rows})


@app.route("/api/jobs/<int:job_id>/status", methods=["POST"])
def api_update_status(job_id: int) -> Any:
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    notes = data.get("notes")
    if status not in VALID_STATUSES:
        return (
            jsonify({"error": f"invalid status; must be one of {sorted(VALID_STATUSES)}"}),
            400,
        )
    try:
        rowcount = _db().update_status_by_id(job_id, status, notes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"rowcount": rowcount, "status": status})


@app.route("/api/runs")
def api_runs() -> Any:
    limit = request.args.get("limit", 20, type=int)
    return jsonify({"runs": _db().recent_runs(limit=limit)})


@app.route("/api/config", methods=["GET", "POST"])
def api_config() -> Any:
    if request.method == "GET":
        if not CONFIG_PATH.exists():
            return jsonify({"error": "config.yaml not found"}), 404
        return jsonify({"config": _load_config_dict(), "path": str(CONFIG_PATH)})

    # POST — write config. Keep a .bak backup before overwriting.
    data = request.get_json(silent=True) or {}
    new_cfg = data.get("config")
    if not isinstance(new_cfg, dict):
        return jsonify({"error": "'config' must be a JSON object"}), 400
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(".yaml.bak")
        try:
            backup.write_bytes(CONFIG_PATH.read_bytes())
        except OSError as e:
            logger.warning("Could not write config backup: %s", e)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_cfg, f, default_flow_style=False, sort_keys=False, width=120)
    except Exception as e:
        return jsonify({"error": f"write failed: {e}"}), 500

    return jsonify(
        {
            "ok": True,
            "path": str(CONFIG_PATH),
            "warning": (
                "Secrets should live in .env (referenced via ${VAR}); avoid saving raw keys here."
            ),
        }
    )


@app.route("/api/config/raw", methods=["POST"])
def api_config_raw() -> Any:
    """Accept raw YAML text (Content-Type: text/yaml) and write to config.yaml."""
    raw = request.get_data(as_text=True) or ""
    if not raw.strip():
        return jsonify({"error": "empty body"}), 400
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return jsonify({"error": f"YAML parse error: {e}"}), 400
    if not isinstance(parsed, dict):
        return jsonify({"error": "top-level YAML must be a mapping"}), 400

    if CONFIG_PATH.exists():
        try:
            CONFIG_PATH.with_suffix(".yaml.bak").write_bytes(CONFIG_PATH.read_bytes())
        except OSError as e:
            logger.warning("Could not write config backup: %s", e)

    try:
        CONFIG_PATH.write_text(raw, encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"write failed: {e}"}), 500

    return jsonify(
        {
            "ok": True,
            "path": str(CONFIG_PATH),
            "warning": (
                "Secrets should live in .env (referenced via ${VAR}); avoid saving raw keys here."
            ),
        }
    )


def _write_override_config(base_cfg: dict[str, Any], override: dict[str, Any]) -> Path:
    """Write a merged YAML config to a tempfile in the results dir.

    `override` comes from the browser localStorage settings form. We deep-merge
    it over the on-disk `config.yaml` so anything the user didn't fill out
    falls back to sensible defaults.

    The caller is responsible for cleaning up the returned path. We try to
    delete it ourselves once the subprocess is running (Windows keeps the open
    file valid even after unlink on a best-effort basis).
    """
    merged = _deep_merge(base_cfg, override)
    tmp_dir = PROJECT_ROOT / "results"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Use a predictable prefix so stray files are easy to spot + clean up.
    fd, path = tempfile.mkstemp(prefix="run_cfg_", suffix=".yaml", dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False, width=120)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return Path(path)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict where `override` is recursively merged over `base`.

    Lists are replaced wholesale (not concatenated) so the browser-provided
    values win for e.g. `platforms`, `job_titles`, etc.
    """
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@app.route("/api/run", methods=["POST"])
def api_run() -> Any:
    """Spawn `python job_hunter.py` with the selected flags and stream stdout.

    If the POST body includes a `config` object, it is merged with the on-disk
    `config.yaml`, written to a per-run tempfile, and passed to the subprocess
    via `--config <tempfile>`. The tempfile is deleted after the subprocess
    exits (in `_reader_thread`), which keeps secrets off-disk for only as long
    as the run actually needs them.
    """
    data = request.get_json(silent=True) or {}
    platforms = data.get("platforms") or []
    new_only = bool(data.get("new_only", False))
    no_apply = bool(data.get("no_apply", True))
    verbose = bool(data.get("verbose", False))
    override_cfg = data.get("config")

    cmd: list[str] = [sys.executable, "-u", str(PROJECT_ROOT / "job_hunter.py")]

    # Per-run temp config (from browser localStorage override).
    run_cfg_path: Path | None = None
    if isinstance(override_cfg, dict) and override_cfg:
        try:
            run_cfg_path = _write_override_config(_load_config_dict(), override_cfg)
            cmd += ["--config", str(run_cfg_path)]
        except Exception as e:
            logger.warning("Failed to write override config: %s", e)

    if isinstance(platforms, list) and platforms:
        cmd += ["--platforms", *[str(p) for p in platforms]]
    if new_only:
        cmd += ["--new-only"]
    if no_apply:
        cmd += ["--no-apply"]
    if verbose:
        cmd += ["--verbose"]

    task_id = uuid.uuid4().hex[:12]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
    except Exception as e:
        if run_cfg_path:
            try:
                run_cfg_path.unlink(missing_ok=True)
            except OSError:
                pass
        return jsonify({"error": f"failed to spawn: {e}"}), 500

    with _RUN_LOCK:
        _running_tasks[task_id] = []
        _running_procs[task_id] = proc
        _task_status[task_id] = {
            "state": "running",
            "started_at": time.time(),
            "cmd": cmd,
            "run_cfg_path": str(run_cfg_path) if run_cfg_path else None,
        }

    # Don't echo the override config path (or any paths with secrets) into the
    # log buffer visible to the UI — replace the per-run tempfile path with a
    # placeholder in the command line we show.
    if run_cfg_path:
        safe_cmd = [("<per-run>" if c == str(run_cfg_path) else c) for c in cmd]
    else:
        safe_cmd = list(cmd)
    _append_log(task_id, f"$ {' '.join(safe_cmd)}")

    threading.Thread(target=_reader_thread, args=(task_id, proc), daemon=True).start()

    return jsonify({"task_id": task_id, "cmd": safe_cmd})


@app.route("/api/logs/<task_id>")
def api_logs(task_id: str) -> Any:
    since = request.args.get("since", 0, type=int)
    _reap_zombie_tasks()
    with _RUN_LOCK:
        if task_id not in _running_tasks:
            abort(404)
        buf = _running_tasks[task_id]
        new_lines = buf[since:]
        total = len(buf)
        status = _task_status.get(task_id, {})
    elapsed = None
    if status.get("started_at"):
        end = status.get("finished_at") or time.time()
        elapsed = round(end - status["started_at"], 1)
    return jsonify(
        {
            "task_id": task_id,
            "lines": new_lines,
            "next_since": total,
            "state": status.get("state", "unknown"),
            "returncode": status.get("returncode"),
            "elapsed_seconds": elapsed,
        }
    )


@app.route("/api/stop/<task_id>", methods=["POST"])
def api_stop(task_id: str) -> Any:
    with _RUN_LOCK:
        proc = _running_procs.get(task_id)
    if proc is None:
        abort(404)
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            return jsonify({"error": f"terminate failed: {e}"}), 500
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/platforms")
def api_platforms() -> Any:
    """List configured platforms + which scrapers are actually registered."""
    from scrapers import SCRAPERS

    cfg = _load_config_dict()
    plats = cfg.get("platforms", {}) or {}
    rows = []
    for name in sorted(set(list(plats.keys()) + list(SCRAPERS.keys()))):
        rows.append(
            {
                "name": name,
                "enabled": bool((plats.get(name) or {}).get("enabled")),
                "registered": name in SCRAPERS,
            }
        )
    return jsonify({"platforms": rows})


# --- CLI -------------------------------------------------------------------- #


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Job Hunt AI Agent — Flask dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5000, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.info("Starting Flask dashboard on http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
