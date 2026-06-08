#!/usr/bin/env python3
"""
Artlist Image-to-Image API  —  with Job Queue & Auto-Retry
===========================================================

Synchronous endpoint (existing, unchanged):
  POST /generate
    Body: { "image_url": "...", "prompt": "...", "aspect_ratio": "auto" }
    Returns immediately with result URL.

Async job endpoints (new):
  POST /job
    Body: { "image_url": "...", "prompt": "...", "aspect_ratio": "auto" }
    Returns: { "job_id": "...", "status": "pending" }

  GET /job/<job_id>
    Returns: { "job_id": "...", "status": "pending|processing|completed|failed|retry_pending",
               "url": "...", "all_urls": [...], "error": "...",
               "attempts": 1, "next_retry_at": "..." }

  GET /jobs
    Returns list of all jobs (newest first, max 200).

Retry schedule (per job):
  Attempt 1 fails  →  retry after  5 minutes
  Attempt 2 fails  →  retry after 10 minutes
  Attempt 3 fails  →  retry after 15 minutes
  Attempt 4 fails  →  permanently failed

Account logic:
  - 1 account = 2 free images
  - Auto-rotate accounts via pool; auto-register new accounts via CapSolver when pool exhausted.
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests as std_requests
from flask import Flask, request, jsonify

from artlist_auto import (
    ToolkitClient,
    pool_add_account,
    solve_turnstile,
    _nextauth_login,
    _pool_load,
    _pool_save,
    _random_email,
    _random_password,
    ARTLIST_BASE,
)

# ─── Config ───────────────────────────────────────────────────────────────────

CONFIG_FILE            = Path("config.json")
STATE_FILE             = Path("api_state.json")
ACCOUNTS_FILE          = Path("accounts.json")
JOBS_FILE              = Path("jobs.json")
TURNSTILE_SITE_KEY     = "0x4AAAAAAA1gJJb7OkkH_gL6"
MAX_IMAGES_PER_ACCOUNT = 2
MIN_IMAGE_BYTES        = 5_000
MAX_IMAGE_BYTES        = 20_000_000
VALID_RATIOS           = {"auto", "1:1", "16:9", "9:16", "4:3", "3:4"}

# Retry delays (seconds) indexed by attempt number (1-based).
# attempt 1 fails → wait RETRY_DELAYS[0] = 300s (5 min)
# attempt 2 fails → wait RETRY_DELAYS[1] = 600s (10 min)
# attempt 3 fails → wait RETRY_DELAYS[2] = 900s (15 min)
# attempt 4 fails → permanently failed (no more retries)
RETRY_DELAYS   = [5 * 60, 10 * 60, 15 * 60]
MAX_ATTEMPTS   = len(RETRY_DELAYS) + 1   # = 4

# Worker polls every this many seconds for ready-to-run retry jobs
WORKER_POLL_S  = 15


def _load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    cfg.setdefault("capsolver_key", os.environ.get("CAPSOLVER_API_KEY", ""))
    cfg.setdefault("api_key",       os.environ.get("API_KEY", ""))
    cfg.setdefault("port",          int(os.environ.get("PORT", 5000)))
    return cfg


CONFIG = _load_config()
app    = Flask(__name__)
_lock  = threading.Lock()   # protects account/state files
_jobs_lock = threading.Lock()   # protects jobs file

# ─── State ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"session": None, "images_used": 0}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── Job storage ──────────────────────────────────────────────────────────────

def _load_jobs() -> dict:
    """Load all jobs as dict {job_id: job_dict}."""
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_jobs(jobs: dict) -> None:
    with open(JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_job(image_url: str, prompt: str, aspect_ratio: str) -> dict:
    """Create a new job record, persist it, and return it."""
    job_id = str(uuid.uuid4())
    job = {
        "job_id":        job_id,
        "image_url":     image_url,
        "prompt":        prompt,
        "aspect_ratio":  aspect_ratio,
        "status":        "pending",       # pending | processing | completed | failed | retry_pending
        "attempts":      0,
        "url":           None,
        "all_urls":      [],
        "error":         None,
        "next_retry_at": None,
        "created_at":    _now_iso(),
        "updated_at":    _now_iso(),
    }
    with _jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = job
        _save_jobs(jobs)
    return job


def _update_job(job_id: str, **kwargs) -> dict:
    """Update fields on a job and persist."""
    with _jobs_lock:
        jobs = _load_jobs()
        job = jobs.get(job_id)
        if not job:
            raise KeyError(f"Job not found: {job_id}")
        kwargs["updated_at"] = _now_iso()
        job.update(kwargs)
        jobs[job_id] = job
        _save_jobs(jobs)
    return job


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _load_jobs().get(job_id)

# ─── Account management ───────────────────────────────────────────────────────

def _relogin_pool_account() -> str | None:
    accounts = _pool_load(str(ACCOUNTS_FILE))
    state    = _load_state()
    current_session = state.get("session")

    for acc in accounts:
        if acc.get("exhausted"):
            continue
        email    = acc.get("email", "")
        password = acc.get("password", "")
        if not email or not password:
            continue
        if acc.get("session_token") == current_session:
            continue
        try:
            print(f"[api] Re-logging pool account: {email}")
            token = _nextauth_login(ARTLIST_BASE, email, password)
            acc["session_token"] = token
            _pool_save(str(ACCOUNTS_FILE), accounts)
            return token
        except Exception as e:
            print(f"[api] Re-login failed for {email}: {e}")
    return None


def _register_new_account() -> str:
    capsolver_key = CONFIG.get("capsolver_key", "")
    if not capsolver_key:
        raise RuntimeError("CAPSOLVER_API_KEY not configured in config.json")

    email    = _random_email()
    password = _random_password()
    print(f"[api] Registering new account: {email}")

    turnstile_token = solve_turnstile(capsolver_key, TURNSTILE_SITE_KEY)
    token = _nextauth_login(
        ARTLIST_BASE, email, password,
        is_registration=True, full_name="Alex Smith",
        turnstile_token=turnstile_token,
    )
    pool_add_account(str(ACCOUNTS_FILE), email, password, session_token=token)
    print(f"[api] Account ready: {email}")
    return token


def _get_fresh_token() -> str:
    token = _relogin_pool_account()
    if token:
        return token
    print("[api] No reusable pool accounts — registering new account ...")
    return _register_new_account()


def _get_session(force_new: bool = False) -> tuple[str, dict]:
    state = _load_state()
    needs_new = (
        force_new
        or not state.get("session")
        or state.get("images_used", 0) >= MAX_IMAGES_PER_ACCOUNT
    )
    if needs_new:
        if state.get("session"):
            _mark_pool_exhausted(state["session"])
        token = _get_fresh_token()
        state = {"session": token, "images_used": 0}
        _save_state(state)
    return state["session"], state


def _mark_pool_exhausted(session_token: str) -> None:
    accounts = _pool_load(str(ACCOUNTS_FILE))
    changed  = False
    for acc in accounts:
        if acc.get("session_token") == session_token:
            acc["exhausted"] = True
            changed = True
    if changed:
        _pool_save(str(ACCOUNTS_FILE), accounts)

# ─── Image download & validation ─────────────────────────────────────────────

def _download_image(url: str) -> str:
    r = std_requests.get(
        url, timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
        allow_redirects=True,
    )
    r.raise_for_status()

    content_type = r.headers.get("Content-Type", "")
    if "text/html" in content_type or "text/plain" in content_type:
        raise ValueError(f"URL returned HTML/text instead of an image (Content-Type: {content_type})")

    data = r.content
    if len(data) < MIN_IMAGE_BYTES:
        raise ValueError(
            f"Image too small ({len(data)} bytes). "
            "Make sure the URL points directly to a JPEG/PNG/WebP image file."
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large ({len(data)/1e6:.1f} MB, max 20 MB)")

    suffix = ".jpg"
    if "png" in content_type:    suffix = ".png"
    elif "webp" in content_type: suffix = ".webp"
    elif "gif" in content_type:  suffix = ".gif"

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.flush()
    tmp.close()
    return tmp.name

# ─── Core generation ──────────────────────────────────────────────────────────

def _run_generation(session: str, image_path: str,
                    prompt: str, aspect_ratio: str) -> list[str]:
    client = ToolkitClient(session)
    file_key, _, presigned_get, mime_type = client.upload_image(image_path)
    quote   = client.get_cost_quote(presigned_get, prompt=prompt, aspect_ratio=aspect_ratio)

    if quote.get("cost", 0) > 0:
        raise RuntimeError("OUT_OF_CREDITS: Account has no free generations left.")

    chat_id = client.create_chat_session()
    client.create_generation(
        prompt=prompt, file_key=file_key, presigned_get=presigned_get,
        mime_type=mime_type, quote=quote, chat_session_id=chat_id,
        aspect_ratio=aspect_ratio,
    )
    return client.poll_generation(chat_id)

# ─── Shared generation logic (used by both sync and job worker) ───────────────

def _attempt_generation(image_url: str, prompt: str, aspect_ratio: str) -> list[str]:
    """
    Download image, try generation up to 3 quick attempts (account rotation).
    Returns list of result URLs on success.
    Raises RuntimeError on all-attempt failure.
    This is the same fast-rotation logic as before — the JOB layer adds
    the delayed retries on top.
    """
    tmp_path = None
    try:
        print(f"[gen] Downloading: {image_url[:80]}")
        tmp_path = _download_image(image_url)

        urls       = None
        last_error = "unknown error"

        for attempt in range(1, 4):
            force_new = attempt > 1
            try:
                with _lock:
                    session, state = _get_session(force_new=force_new)
                print(f"[gen] Attempt {attempt}/3 "
                      f"(credit {state['images_used']}/{MAX_IMAGES_PER_ACCOUNT}) ...")
                urls = _run_generation(session, tmp_path, prompt, aspect_ratio)

                with _lock:
                    state2 = _load_state()
                    state2["images_used"] = state2.get("images_used", 0) + 1
                    _save_state(state2)
                    remaining = MAX_IMAGES_PER_ACCOUNT - state2["images_used"]
                print(f"[gen] Done. {remaining} credit(s) left on current account.")
                break

            except RuntimeError as e:
                last_error = str(e)
                print(f"[gen] Attempt {attempt} failed: {last_error[:120]}")
                if attempt == 3:
                    raise RuntimeError(f"All 3 quick-attempts failed. Last: {last_error}")

        if not urls:
            raise RuntimeError("Generation completed but returned no URLs")

        return urls

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ─── Job worker ───────────────────────────────────────────────────────────────

def _process_job(job: dict) -> None:
    """
    Run one job attempt. On failure, schedules retry or marks permanently failed.
    Called from the background worker thread.
    """
    job_id    = job["job_id"]
    attempts  = job["attempts"] + 1   # this is the attempt we are about to make

    print(f"[job:{job_id[:8]}] Starting attempt {attempts}/{MAX_ATTEMPTS} ...")
    _update_job(job_id, status="processing", attempts=attempts, error=None)

    try:
        urls = _attempt_generation(
            job["image_url"], job["prompt"], job["aspect_ratio"]
        )
        _update_job(
            job_id,
            status   = "completed",
            url      = urls[0] if urls else None,
            all_urls = urls,
            error    = None,
        )
        print(f"[job:{job_id[:8]}] Completed successfully.")

    except Exception as e:
        err_msg = str(e)
        print(f"[job:{job_id[:8]}] Attempt {attempts} failed: {err_msg[:120]}")

        if attempts >= MAX_ATTEMPTS:
            # No more retries — permanently fail
            _update_job(
                job_id,
                status         = "failed",
                error          = err_msg,
                next_retry_at  = None,
            )
            print(f"[job:{job_id[:8]}] All {MAX_ATTEMPTS} attempts exhausted → FAILED.")
        else:
            # Schedule delayed retry
            delay   = RETRY_DELAYS[attempts - 1]   # 5min, 10min, or 15min
            retry_at = time.time() + delay
            retry_at_iso = datetime.fromtimestamp(retry_at, tz=timezone.utc).isoformat()
            _update_job(
                job_id,
                status        = "retry_pending",
                error         = err_msg,
                next_retry_at = retry_at_iso,
            )
            mins = delay // 60
            print(f"[job:{job_id[:8]}] Scheduled retry in {mins} min (at {retry_at_iso}).")


def _worker_loop() -> None:
    """Background thread: pick up pending and due retry jobs and process them."""
    print("[worker] Job queue worker started.")
    while True:
        try:
            now = time.time()
            with _jobs_lock:
                jobs = _load_jobs()

            ready = []
            for job in jobs.values():
                status = job.get("status")
                if status == "pending":
                    ready.append(job)
                elif status == "retry_pending":
                    retry_at = job.get("next_retry_at")
                    if retry_at:
                        retry_ts = datetime.fromisoformat(retry_at).timestamp()
                        if now >= retry_ts:
                            ready.append(job)

            for job in ready:
                try:
                    _process_job(job)
                except Exception:
                    traceback.print_exc()

        except Exception:
            traceback.print_exc()

        time.sleep(WORKER_POLL_S)


# Start background worker
_worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="job-worker")
_worker_thread.start()

# ─── Auth guard ───────────────────────────────────────────────────────────────

def _require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = CONFIG.get("api_key", "")
        if not expected:
            return f(*args, **kwargs)
        auth  = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token != expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    state = _load_state()
    used  = state.get("images_used", 0)
    with _jobs_lock:
        jobs = _load_jobs()
    counts = {}
    for j in jobs.values():
        s = j.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return jsonify({
        "status":                    "ok",
        "account_images_used":       used,
        "account_images_remaining":  MAX_IMAGES_PER_ACCOUNT - used,
        "has_active_session":        bool(state.get("session")),
        "job_counts":                counts,
    })


# ── Synchronous /generate (unchanged behaviour) ───────────────────────────────

@app.route("/generate", methods=["POST"])
@_require_api_key
def generate():
    """Synchronous generation — same as before. Blocks until done or error."""
    data         = request.get_json(silent=True) or {}
    image_url    = (data.get("image_url") or "").strip()
    prompt       = (data.get("prompt") or "").strip()
    aspect_ratio = (data.get("aspect_ratio") or "auto").strip()

    if not image_url:
        return jsonify({"error": "image_url is required"}), 400
    if aspect_ratio not in VALID_RATIOS:
        return jsonify({"error": f"aspect_ratio must be one of: {', '.join(sorted(VALID_RATIOS))}"}), 400

    try:
        print(f"[api] [sync] Downloading: {image_url[:80]}")
        urls = _attempt_generation(image_url, prompt, aspect_ratio)
        state = _load_state()
        used      = state.get("images_used", 0)
        remaining = MAX_IMAGES_PER_ACCOUNT - used
        return jsonify({
            "url":                      urls[0],
            "all_urls":                 urls,
            "account_images_used":      used,
            "account_images_remaining": remaining,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except std_requests.RequestException as e:
        return jsonify({"error": f"Failed to download image: {e}"}), 400
    except TimeoutError as e:
        return jsonify({"error": f"Generation timed out: {e}"}), 504
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Unexpected server error — check logs"}), 500


# ── Async job endpoints ───────────────────────────────────────────────────────

@app.route("/job", methods=["POST"])
@_require_api_key
def job_submit():
    """
    Submit an async image generation job.
    Returns immediately with job_id. Poll /job/<id> for status.

    Body: { "image_url": "...", "prompt": "...", "aspect_ratio": "auto" }
    Returns: { "job_id": "...", "status": "pending" }

    Retry schedule on failure:
      Attempt 1 fails → retry in  5 min
      Attempt 2 fails → retry in 10 min
      Attempt 3 fails → retry in 15 min
      Attempt 4 fails → permanently failed
    """
    data         = request.get_json(silent=True) or {}
    image_url    = (data.get("image_url") or "").strip()
    prompt       = (data.get("prompt") or "").strip()
    aspect_ratio = (data.get("aspect_ratio") or "auto").strip()

    if not image_url:
        return jsonify({"error": "image_url is required"}), 400
    if aspect_ratio not in VALID_RATIOS:
        return jsonify({"error": f"aspect_ratio must be one of: {', '.join(sorted(VALID_RATIOS))}"}), 400

    job = _create_job(image_url, prompt, aspect_ratio)
    print(f"[job] Created job {job['job_id'][:8]} for: {image_url[:60]}")
    return jsonify({
        "job_id": job["job_id"],
        "status": job["status"],
        "message": (
            "Job submitted. Poll GET /job/<job_id> for status. "
            f"On failure retries at: 5 min → 10 min → 15 min → failed."
        ),
    }), 202


@app.route("/job/<job_id>", methods=["GET"])
@_require_api_key
def job_status(job_id: str):
    """
    Get job status.

    Returns:
      { "job_id", "status", "attempts", "url", "all_urls",
        "error", "next_retry_at", "created_at", "updated_at" }

    status values:
      pending        — queued, not yet started
      processing     — currently running
      completed      — finished successfully (url is set)
      retry_pending  — failed, waiting for scheduled retry (next_retry_at is set)
      failed         — all retries exhausted, permanently failed
    """
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": f"Job not found: {job_id}"}), 404

    resp = {
        "job_id":        job["job_id"],
        "status":        job["status"],
        "attempts":      job["attempts"],
        "url":           job.get("url"),
        "all_urls":      job.get("all_urls", []),
        "error":         job.get("error"),
        "next_retry_at": job.get("next_retry_at"),
        "created_at":    job.get("created_at"),
        "updated_at":    job.get("updated_at"),
    }

    # Add human-readable retry info
    if job["status"] == "retry_pending" and job.get("next_retry_at"):
        try:
            retry_ts    = datetime.fromisoformat(job["next_retry_at"]).timestamp()
            secs_left   = max(0, int(retry_ts - time.time()))
            resp["retry_in_seconds"] = secs_left
        except Exception:
            pass

    http_code = 200
    if job["status"] == "failed":
        http_code = 200    # still 200 — client should read the status field

    return jsonify(resp), http_code


@app.route("/jobs", methods=["GET"])
@_require_api_key
def job_list():
    """
    List all jobs (newest first, max 200).
    Optional query params: ?status=pending|processing|completed|failed|retry_pending
    """
    filter_status = request.args.get("status")
    with _jobs_lock:
        jobs_dict = _load_jobs()

    all_jobs = sorted(
        jobs_dict.values(),
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )[:200]

    if filter_status:
        all_jobs = [j for j in all_jobs if j.get("status") == filter_status]

    return jsonify({
        "total": len(all_jobs),
        "jobs":  all_jobs,
    })


# ── Legacy endpoints ──────────────────────────────────────────────────────────

@app.route("/account/status", methods=["GET"])
@_require_api_key
def account_status():
    state = _load_state()
    used  = state.get("images_used", 0)
    return jsonify({
        "images_used":      used,
        "images_remaining": MAX_IMAGES_PER_ACCOUNT - used,
        "has_session":      bool(state.get("session")),
    })


@app.route("/account/reset", methods=["POST"])
@_require_api_key
def account_reset():
    """Force-register a fresh account immediately."""
    with _lock:
        token = _register_new_account()
        _save_state({"session": token, "images_used": 0})
    return jsonify({"status": "ok", "message": "New account registered and ready"})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = CONFIG.get("port", 5000)
    print(f"[api] Artlist Image API  →  http://0.0.0.0:{port}")
    print(f"[api] CapSolver key : {'SET' if CONFIG.get('capsolver_key') else 'NOT SET'}")
    print(f"[api] API key guard : {'ON' if CONFIG.get('api_key') else 'OFF (open)'}")
    print(f"[api] Job retry delays: 5min → 10min → 15min → fail")
    print(f"[api] Worker polls every {WORKER_POLL_S}s for pending jobs")
    app.run(host="0.0.0.0", port=port, threaded=True)
