#!/usr/bin/env python3
"""
Live image-job integration test.
Starts the Flask app in-process, submits real jobs, checks retry flow.
No real Artlist/CapSolver calls — generation is mocked.
Uses a real public image URL to test the download + validation path.
"""

import json, os, sys, time, tempfile, threading, types
from pathlib import Path
from unittest.mock import patch, MagicMock

TMP = tempfile.mkdtemp(prefix="artlist_imgtest_")
os.chdir(TMP)

# ── stub curl_cffi ────────────────────────────────────────────────
class _FakeSess:
    headers  = {}
    cookies  = {}
    proxies  = {}
    def update(self, d): self.headers.update(d)
    def get(self, *a, **k):  return MagicMock(status_code=200, json=lambda: {"csrfToken":"x"})
    def post(self, *a, **k): return MagicMock(status_code=200, json=lambda: {})
    def put(self, *a, **k):  return MagicMock(status_code=200)
    def set(self, *a, **k):  pass

sys.modules["curl_cffi"]          = MagicMock()
sys.modules["curl_cffi.requests"] = MagicMock()
import curl_cffi.requests as cfm
cfm.Session = _FakeSess

# ── stub artlist_auto ─────────────────────────────────────────────
stub = types.ModuleType("artlist_auto")
stub.ARTLIST_BASE     = "https://artlist.io"
stub.TOOLKIT_BASE     = "https://toolkit.artlist.io"
stub.SESSION_COOKIE   = "__Secure-session.artlist-prod.session-token"
stub.ToolkitClient    = MagicMock()
stub.pool_add_account = MagicMock()
stub.solve_turnstile  = MagicMock(return_value="tok")
stub._nextauth_login  = MagicMock(return_value="sess")
stub._pool_load       = MagicMock(return_value=[])
stub._pool_save       = MagicMock()
stub._random_email    = MagicMock(return_value="t@t.com")
stub._random_password = MagicMock(return_value="P@ss1!")
sys.modules["artlist_auto"] = stub

sys.path.insert(0, str(Path(__file__).parent))
import fixed_api as api

api.CONFIG_FILE   = Path(TMP) / "config.json"
api.STATE_FILE    = Path(TMP) / "api_state.json"
api.ACCOUNTS_FILE = Path(TMP) / "accounts.json"
api.JOBS_FILE     = Path(TMP) / "jobs.json"

# ─────────────────────────────────────────────────────────────────
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m→\033[0m"
ok_count = [0]; fail_count = [0]

def check(label, expr, expected=True):
    ok = bool(expr) == bool(expected)
    ok_count[0]  += ok
    fail_count[0] += not ok
    print(f"  {PASS if ok else FAIL} {label}")
    if not ok:
        print(f"      got={expr!r}  want={expected!r}")

client = api.app.test_client()

# ═══════════════════════════════════════════════════════════════
print("\n" + "═"*62)
print("  IMAGE JOB — INTEGRATION TEST")
print("═"*62)

# ── 1. Real image download ───────────────────────────────────────
print("\n[1] IMAGE DOWNLOAD & VALIDATION")

# 100×100 red PNG (public httpbin)
REAL_IMG = "https://httpbin.org/image/jpeg"
SMALL_IMG = "https://httpbin.org/bytes/100"   # too small

print(f"  {INFO} Downloading real image: {REAL_IMG}")
try:
    tmp = api._download_image(REAL_IMG)
    size = os.path.getsize(tmp)
    check(f"Downloaded successfully ({size/1024:.1f} KB)", size > 5000)
    check("Temp file is a .jpg", tmp.endswith(".jpg") or tmp.endswith(".jpeg"))
    os.unlink(tmp)
except Exception as e:
    check(f"Download OK: {e}", False)

print(f"  {INFO} Checking small image rejection (<5KB)")
try:
    api._download_image(SMALL_IMG)
    check("Small image rejected", False)
except ValueError as e:
    check(f"Small image correctly rejected: {str(e)[:50]}", True)

# ── 2. Submit job via POST /job ───────────────────────────────────
print("\n[2] JOB SUBMISSION")

rv = client.post("/job",
    json={"image_url": REAL_IMG, "prompt": "neon city vibes", "aspect_ratio": "16:9"},
    content_type="application/json")
check("POST /job → 202 Accepted", rv.status_code == 202)
body = rv.get_json()
check("job_id in response",   "job_id" in body)
check("status = pending",     body.get("status") == "pending")
check("message mentions retry schedule", "5min" in body.get("message",""))
JOB_ID = body["job_id"]
print(f"  {INFO} Job ID: {JOB_ID}")

# ── 3. Poll status ────────────────────────────────────────────────
print("\n[3] JOB STATUS POLL")

rv2 = client.get(f"/job/{JOB_ID}")
check("GET /job/<id> → 200",      rv2.status_code == 200)
j = rv2.get_json()
check("status field present",      "status" in j)
check("max_attempts = 5",          j.get("max_attempts") == 5)
check("attempts = 0",              j.get("attempts") == 0)
check("url = None (not run yet)",  j.get("url") is None)
print(f"  {INFO} status={j['status']}  attempts={j['attempts']}/{j['max_attempts']}")

# ── 4. Simulate job SUCCESS ───────────────────────────────────────
print("\n[4] JOB SUCCESS SIMULATION")

FAKE_URL = "https://cdn.artlist.io/output/abc123.jpg"
with patch.object(api, "_attempt_generation", return_value=[FAKE_URL]):
    api._process_job(api._get_job(JOB_ID))

rv3 = client.get(f"/job/{JOB_ID}")
j2  = rv3.get_json()
check("status = completed",        j2.get("status") == "completed")
check("url is set",                j2.get("url") == FAKE_URL)
check("all_urls has 1 entry",      len(j2.get("all_urls",[])) == 1)
check("error = null",              j2.get("error") is None)
check("attempts = 1",              j2.get("attempts") == 1)
print(f"  {INFO} result URL: {j2.get('url')}")

# ── 5. Simulate retry flow ────────────────────────────────────────
print("\n[5] RETRY FLOW SIMULATION (login_failed × 5)")

rv4 = client.post("/job",
    json={"image_url": REAL_IMG, "prompt": "", "aspect_ratio": "auto"},
    content_type="application/json")
RETRY_JOB = rv4.get_json()["job_id"]

delays_min = []
with patch.object(api, "_attempt_generation",
                  side_effect=RuntimeError("Login failed for x@test.com — no session cookie")):
    for i in range(1, 6):
        jj = api._get_job(RETRY_JOB)
        api._process_job(jj)
        jj = api._get_job(RETRY_JOB)
        if jj["status"] == "retry_pending":
            from datetime import datetime, timezone
            ts  = datetime.fromisoformat(jj["next_retry_at"]).timestamp()
            mins = round((ts - time.time()) / 60)
            delays_min.append(mins)
            print(f"  {INFO} Attempt {i} failed → retry in ~{mins} min  (error_type={jj['error_type']})")
        elif jj["status"] == "failed":
            print(f"  {INFO} Attempt {i} failed → PERMANENTLY FAILED")

check("4 retries scheduled",       len(delays_min) == 4)
check("Retry 1 ≈  5 min",         4  <= delays_min[0] <= 6)
check("Retry 2 ≈ 10 min",         9  <= delays_min[1] <= 11)
check("Retry 3 ≈ 15 min",         14 <= delays_min[2] <= 16)
check("Retry 4 ≈ 60 min",         59 <= delays_min[3] <= 61)

final = api._get_job(RETRY_JOB)
check("Final status = failed",     final["status"] == "failed")
check("error_type = login_failed", final["error_type"] == "login_failed")
check("attempts = 5",              final["attempts"] == 5)

rv_status = client.get(f"/job/{RETRY_JOB}").get_json()
check("GET /job shows failed",     rv_status["status"] == "failed")
check("error_type in API response",rv_status.get("error_type") == "login_failed")

# ── 6. GET /jobs filtering ────────────────────────────────────────
print("\n[6] JOB LIST & FILTER")

rv_all   = client.get("/jobs").get_json()
rv_done  = client.get("/jobs?status=completed").get_json()
rv_fail  = client.get("/jobs?status=failed").get_json()
rv_pend  = client.get("/jobs?status=pending").get_json()

check("GET /jobs total ≥ 2",        rv_all["total"] >= 2)
check("?status=completed has 1",    rv_done["total"] == 1)
check("?status=failed has 1",       rv_fail["total"] == 1)
check("?status=pending has 0",      rv_pend["total"] == 0)
print(f"  {INFO} Total={rv_all['total']}  completed={rv_done['total']}  failed={rv_fail['total']}")

# ── 7. Health endpoint ───────────────────────────────────────────
print("\n[7] HEALTH ENDPOINT")

rv_h = client.get("/health").get_json()
check("status = ok",                rv_h.get("status") == "ok")
check("job_counts present",         "job_counts" in rv_h)
check("retry_schedule present",     "retry_schedule" in rv_h)
print(f"  {INFO} retry_schedule: {rv_h.get('retry_schedule')}")
print(f"  {INFO} job_counts: {rv_h.get('job_counts')}")

# ── 8. Edge cases ────────────────────────────────────────────────
print("\n[8] EDGE CASES")

# Bad aspect ratio
rv_bad = client.post("/job",
    json={"image_url": "https://x.com/y.jpg", "aspect_ratio": "3:1"},
    content_type="application/json")
check("Bad aspect_ratio → 400",     rv_bad.status_code == 400)

# Missing image_url
rv_mis = client.post("/job",
    json={"prompt": "test"},
    content_type="application/json")
check("Missing image_url → 400",    rv_mis.status_code == 400)

# Non-existent job
rv_nf = client.get("/job/does-not-exist-xyz")
check("GET /job/bad-id → 404",      rv_nf.status_code == 404)

# Duplicate jobs (same url) are allowed
rv_d1 = client.post("/job", json={"image_url": REAL_IMG}, content_type="application/json")
rv_d2 = client.post("/job", json={"image_url": REAL_IMG}, content_type="application/json")
check("Duplicate jobs get different IDs",
      rv_d1.get_json()["job_id"] != rv_d2.get_json()["job_id"])

# ═══════════════════════════════════════════════════════════════
print()
print("═"*62)
total = ok_count[0] + fail_count[0]
if fail_count[0] == 0:
    print(f"\033[92m  ALL {total} TESTS PASSED ✓\033[0m")
else:
    print(f"\033[91m  {ok_count[0]}/{total} PASSED — {fail_count[0]} FAILED\033[0m")
print("═"*62)

import shutil; shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if fail_count[0] == 0 else 1)
