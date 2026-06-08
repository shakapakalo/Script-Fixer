#!/usr/bin/env python3
"""
Test script for fixed_api.py and artlist_auto_fixed.py
Tests: job queue, retry schedule, error classification, cookie detection,
       capsolver retry logic, worker loop — all WITHOUT real network calls.
"""

import json
import os
import sys
import time
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── Setup temp dir so file operations don't pollute project ──────────────────
TMP_DIR = tempfile.mkdtemp(prefix="artlist_test_")
os.chdir(TMP_DIR)

# ── Stub curl_cffi before importing artlist_auto_fixed ───────────────────────
class _FakeSession:
    def __init__(self):
        self.headers  = {}
        self.cookies  = {}
        self.proxies  = {}
    def update(self, d):    self.headers.update(d)
    def get(self, *a, **k): return MagicMock(status_code=200, json=lambda: {"csrfToken": "test-csrf"})
    def post(self, *a, **k):return MagicMock(status_code=200, json=lambda: {})
    def put(self, *a, **k): return MagicMock(status_code=200)
    def set(self, *a, **k): pass

sys.modules["curl_cffi"] = MagicMock()
sys.modules["curl_cffi.requests"] = MagicMock()
import curl_cffi.requests as cf_mock
cf_mock.Session = _FakeSession

# ── Stub flask for fixed_api import ──────────────────────────────────────────
from flask import Flask   # real flask installed

# ── Patch accounts/state files to tmp dir ────────────────────────────────────
import importlib, types

# Patch Path references inside modules
_orig_path = Path

# ── Now import our fixed modules ─────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# Patch artlist_auto import inside fixed_api to use our fixed version
artlist_stub = types.ModuleType("artlist_auto")
artlist_stub.ARTLIST_BASE    = "https://artlist.io"
artlist_stub.TOOLKIT_BASE    = "https://toolkit.artlist.io"
artlist_stub.SESSION_COOKIE  = "__Secure-session.artlist-prod.session-token"
artlist_stub.ToolkitClient   = MagicMock()
artlist_stub.pool_add_account= MagicMock()
artlist_stub.solve_turnstile = MagicMock(return_value="fake-turnstile-token")
artlist_stub._nextauth_login = MagicMock(return_value="fake-session-token")
artlist_stub._pool_load      = MagicMock(return_value=[])
artlist_stub._pool_save      = MagicMock()
artlist_stub._random_email   = MagicMock(return_value="test@gmail.com")
artlist_stub._random_password= MagicMock(return_value="P@test!12z")
sys.modules["artlist_auto"]  = artlist_stub

import fixed_api as api

# Override file paths to tmp dir
api.CONFIG_FILE   = Path(TMP_DIR) / "config.json"
api.STATE_FILE    = Path(TMP_DIR) / "api_state.json"
api.ACCOUNTS_FILE = Path(TMP_DIR) / "accounts.json"
api.JOBS_FILE     = Path(TMP_DIR) / "jobs.json"

# ─────────────────────────────────────────────────────────────────────────────
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
results = []

def test(name, expr, expected=True):
    ok = bool(expr) == bool(expected)
    results.append((name, ok))
    icon = PASS if ok else FAIL
    print(f"  {icon} {name}")
    if not ok:
        print(f"      Expected {expected!r}, got {expr!r}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "═"*60)
print("  ARTLIST API — TEST SUITE")
print("═"*60)

# ══════════════════════════════════════════════════════════════════
print("\n[1] RETRY SCHEDULE CONFIG")
# ══════════════════════════════════════════════════════════════════
test("RETRY_DELAYS has 4 entries",  len(api.RETRY_DELAYS) == 4)
test("Delay 1 =  5 min (300s)",    api.RETRY_DELAYS[0] == 300)
test("Delay 2 = 10 min (600s)",    api.RETRY_DELAYS[1] == 600)
test("Delay 3 = 15 min (900s)",    api.RETRY_DELAYS[2] == 900)
test("Delay 4 = 60 min (3600s)",   api.RETRY_DELAYS[3] == 3600)
test("MAX_ATTEMPTS = 5",           api.MAX_ATTEMPTS == 5)

# ══════════════════════════════════════════════════════════════════
print("\n[2] ERROR CLASSIFICATION")
# ══════════════════════════════════════════════════════════════════
cases = [
    ("no session cookie returned after 2 attempts", "login_failed"),
    ("Login failed for x@gmail.com at https://artlist.io", "login_failed"),
    ("OUT_OF_CREDITS: Account has no free generations left", "credits_exhausted"),
    ("[capsolver] Unexpected status: failed | errorCode: ERROR_CAPTCHA_SOLVE_FAILED", "capsolver_failed"),
    ("Generation timed out after 180s", "timeout"),
    ("Failed to download image: ConnectionError", "download_failed"),
    ("Generation completed but returned no URLs", "generation_failed"),
    ("Some unexpected internal error", "unknown"),
]
for msg, expected_type in cases:
    got = api._classify_error(msg)
    test(f"classify '{msg[:45]}...' → {expected_type}", got == expected_type)

# ══════════════════════════════════════════════════════════════════
print("\n[3] JOB QUEUE — CREATE / READ / UPDATE")
# ══════════════════════════════════════════════════════════════════
j1 = api._create_job("https://example.com/photo.jpg", "neon city", "16:9")
test("Job created with status=pending",   j1["status"] == "pending")
test("Job has unique job_id",             len(j1["job_id"]) == 36)
test("Job has 0 attempts",               j1["attempts"] == 0)
test("Job has no error",                 j1["error"] is None)
test("jobs.json written to disk",        (Path(TMP_DIR) / "jobs.json").exists())

j2 = api._create_job("https://example.com/pic2.jpg", "", "auto")
test("Second job created with different id", j1["job_id"] != j2["job_id"])

loaded = api._get_job(j1["job_id"])
test("_get_job returns correct job",     loaded["job_id"] == j1["job_id"])
test("Loaded job image_url correct",     loaded["image_url"] == "https://example.com/photo.jpg")

api._update_job(j1["job_id"], status="processing", attempts=1)
updated = api._get_job(j1["job_id"])
test("Update status → processing",      updated["status"] == "processing")
test("Update attempts → 1",             updated["attempts"] == 1)

# ══════════════════════════════════════════════════════════════════
print("\n[4] JOB WORKER — RETRY SCHEDULE LOGIC")
# ══════════════════════════════════════════════════════════════════

def make_failing_job():
    j = api._create_job("https://example.com/img.jpg", "test", "auto")
    return j

# Simulate 4 consecutive failures and check retry schedule
j = make_failing_job()
job_id = j["job_id"]
delays_seen = []

with patch.object(api, "_attempt_generation", side_effect=RuntimeError("Login failed for x@test.com — no session cookie")):
    for attempt_num in range(1, 6):
        jj = api._get_job(job_id)
        api._process_job(jj)
        jj = api._get_job(job_id)
        if jj["status"] == "retry_pending":
            retry_ts  = datetime.fromisoformat(jj["next_retry_at"]).timestamp()
            delay_sec = retry_ts - time.time()
            delays_seen.append(round(delay_sec / 60))   # minutes, rounded
        elif jj["status"] == "failed":
            break

test("4 retries scheduled before final fail", len(delays_seen) == 4)
if len(delays_seen) >= 4:
    test("Retry 1 ≈  5 min",  4 <= delays_seen[0] <= 6)
    test("Retry 2 ≈ 10 min", 9 <= delays_seen[1] <= 11)
    test("Retry 3 ≈ 15 min", 14 <= delays_seen[2] <= 16)
    test("Retry 4 ≈ 60 min", 59 <= delays_seen[3] <= 61)

final = api._get_job(job_id)
test("After 5 attempts → status=failed", final["status"] == "failed")
test("error_type = login_failed",        final.get("error_type") == "login_failed")
test("attempts = 5",                     final["attempts"] == 5)

# ══════════════════════════════════════════════════════════════════
print("\n[5] JOB WORKER — SUCCESS PATH")
# ══════════════════════════════════════════════════════════════════
js = api._create_job("https://example.com/success.jpg", "prompt", "auto")
with patch.object(api, "_attempt_generation", return_value=["https://cdn.artlist.io/result.jpg"]):
    api._process_job(api._get_job(js["job_id"]))

done = api._get_job(js["job_id"])
test("Successful job → status=completed", done["status"] == "completed")
test("url is set",                        done["url"] == "https://cdn.artlist.io/result.jpg")
test("all_urls has 1 entry",              len(done["all_urls"]) == 1)
test("error is None on success",          done["error"] is None)
test("attempts = 1",                      done["attempts"] == 1)

# ══════════════════════════════════════════════════════════════════
print("\n[6] COOKIE DETECTION (artlist_auto_fixed)")
# ══════════════════════════════════════════════════════════════════
sys.path.insert(0, str(Path(__file__).parent))
# Import cookie finder directly from artlist_auto_fixed
import artlist_auto_fixed as aa

class FakeCookieJar(dict):
    pass

# Exact match
c1 = FakeCookieJar()
c1["__Secure-session.artlist-prod.session-token"] = "exact-token"
test("Exact cookie name match",          aa._find_token_in_cookies(c1) == "exact-token")

# Partial name match
c2 = FakeCookieJar()
c2["some-other-session-token-value"] = "partial-token"
test("Partial 'session-token' name match", aa._find_token_in_cookies(c2) == "partial-token")

# JWT detection by value prefix
c3 = FakeCookieJar()
c3["artlist-session-xyz"] = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
test("JWT prefix (eyJ) detected",        aa._find_token_in_cookies(c3) is not None)

# Empty jar
c4 = FakeCookieJar()
test("Empty cookies → None",             aa._find_token_in_cookies(c4) is None)

# ══════════════════════════════════════════════════════════════════
print("\n[7] CAPSOLVER RETRY LOGIC (artlist_auto_fixed)")
# ══════════════════════════════════════════════════════════════════
call_count = 0

def _mock_post_capsolver(url, json=None, timeout=None):
    global call_count
    r = MagicMock()
    r.raise_for_status = MagicMock()
    if "createTask" in url:
        call_count += 1
        r.json = lambda: {"taskId": f"task-{call_count}", "errorId": 0}
    elif "getTaskResult" in url:
        if call_count < 3:
            # First 2 attempts: capsolver fails
            r.json = lambda: {"status": "failed", "errorId": 1,
                              "errorCode": "ERROR_CAPTCHA_SOLVE_FAILED"}
        else:
            # 3rd attempt: success
            r.json = lambda: {"status": "ready", "solution": {"token": "solved-token-123"}}
    return r

call_count = 0
with patch("artlist_auto_fixed.requests.post", side_effect=_mock_post_capsolver), \
     patch("artlist_auto_fixed.time.sleep"):   # don't actually sleep
    token = aa.solve_turnstile("fake-key", "0x4AAAAAAA1gJJb7OkkH_gL6", max_attempts=3)

test("Capsolver retries on failure",         call_count == 3)
test("Returns token after successful retry", token == "solved-token-123")

# Test that it raises after max_attempts exceeded
call_count = 0
def _always_fail(url, json=None, timeout=None):
    global call_count
    r = MagicMock()
    r.raise_for_status = MagicMock()
    if "createTask" in url:
        call_count += 1
        r.json = lambda: {"taskId": f"task-{call_count}", "errorId": 0}
    else:
        r.json = lambda: {"status": "failed", "errorCode": "ERROR_CAPTCHA_SOLVE_FAILED"}
    return r

call_count = 0
raised = False
with patch("artlist_auto_fixed.requests.post", side_effect=_always_fail), \
     patch("artlist_auto_fixed.time.sleep"):
    try:
        aa.solve_turnstile("fake-key", "0x4AAAAAAA", max_attempts=2)
    except RuntimeError:
        raised = True

test("RuntimeError raised after max_attempts exhausted", raised)
test("Tried exactly max_attempts=2 times",               call_count == 2)

# ══════════════════════════════════════════════════════════════════
print("\n[8] FLASK API ROUTES (with test client)")
# ══════════════════════════════════════════════════════════════════
# Reset jobs file
(Path(TMP_DIR) / "jobs.json").write_text("{}")

app_client = api.app.test_client()

# POST /job — submit
with patch.object(api, "_create_job", wraps=api._create_job):
    rv = app_client.post("/job",
         json={"image_url": "https://example.com/img.jpg", "prompt": "test", "aspect_ratio": "auto"},
         content_type="application/json")
test("POST /job → 202",              rv.status_code == 202)
body = rv.get_json()
test("Response has job_id",          "job_id" in body)
test("Response status=pending",      body.get("status") == "pending")
submitted_job_id = body.get("job_id")

# GET /job/<id>
rv2 = app_client.get(f"/job/{submitted_job_id}")
test("GET /job/<id> → 200",          rv2.status_code == 200)
jbody = rv2.get_json()
test("status field present",         "status" in jbody)
test("max_attempts = 5 in response", jbody.get("max_attempts") == 5)

# GET /jobs
rv3 = app_client.get("/jobs")
test("GET /jobs → 200",              rv3.status_code == 200)
lbody = rv3.get_json()
test("jobs list is non-empty",       lbody.get("total", 0) > 0)

# GET /job/<bad-id>
rv4 = app_client.get("/job/nonexistent-id-12345")
test("GET /job/<bad-id> → 404",     rv4.status_code == 404)

# POST /job — missing image_url
rv5 = app_client.post("/job", json={"prompt": "no url"}, content_type="application/json")
test("POST /job without image_url → 400", rv5.status_code == 400)

# POST /job — bad aspect_ratio
rv6 = app_client.post("/job",
      json={"image_url": "https://x.com/y.jpg", "aspect_ratio": "bad"},
      content_type="application/json")
test("POST /job bad aspect_ratio → 400", rv6.status_code == 400)

# GET /health
rv7 = app_client.get("/health")
test("GET /health → 200",            rv7.status_code == 200)
hbody = rv7.get_json()
test("/health has retry_schedule",   "retry_schedule" in hbody)
test("/health has job_counts",       "job_counts" in hbody)

# ══════════════════════════════════════════════════════════════════
print("\n[9] STATE / ACCOUNT FILES")
# ══════════════════════════════════════════════════════════════════
api._save_state({"session": "tok123", "images_used": 1})
s = api._load_state()
test("State save/load roundtrip",    s["session"] == "tok123")
test("images_used saved correctly",  s["images_used"] == 1)

# Mark pool exhausted (empty pool — should not crash)
api._mark_pool_exhausted("some-session-token")
test("_mark_pool_exhausted with empty pool doesn't crash", True)

# ══════════════════════════════════════════════════════════════════
# RESULTS SUMMARY
# ══════════════════════════════════════════════════════════════════
total  = len(results)
passed = sum(1 for _, ok in results if ok)
failed = total - passed

print()
print("═"*60)
if failed == 0:
    print(f"\033[92m  ALL {total} TESTS PASSED ✓\033[0m")
else:
    print(f"\033[91m  {passed}/{total} PASSED — {failed} FAILED\033[0m")
    print("\n  Failed tests:")
    for name, ok in results:
        if not ok:
            print(f"    \033[91m✗ {name}\033[0m")
print("═"*60)

# Cleanup
import shutil
shutil.rmtree(TMP_DIR, ignore_errors=True)

sys.exit(0 if failed == 0 else 1)
