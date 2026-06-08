#!/usr/bin/env python3
"""
Artlist API — Simple Test Script
=================================
Kaise chalao:
  python3 test_api.py

Koi install nahi karna — sirf Python3 chahiye.
"""

import json
import time
import urllib.request
import urllib.error

# ─── CONFIG — apna IP aur port yahan likho ────────────────────────
SERVER = "http://217.77.8.115:9222"
IMAGE  = "https://httpbin.org/image/jpeg"   # test image (real JPEG)
# ─────────────────────────────────────────────────────────────────

OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m→\033[0m"

passed = 0
failed = 0

def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  {OK} {label}")
    else:
        failed += 1
        print(f"  {FAIL} {label}")
        if detail:
            print(f"     {detail}")

def get(path):
    url = SERVER + path
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}

def post(path, data):
    url  = SERVER + path
    body = json.dumps(data).encode()
    req  = urllib.request.Request(url, data=body,
           headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}

# ═══════════════════════════════════════════════════════════
print()
print("═" * 55)
print(f"  Artlist API Test  →  {SERVER}")
print("═" * 55)

# ── 1. Health check ─────────────────────────────────────────
print("\n[1] HEALTH")
code, h = get("/health")
check("Server reachable",        code == 200,  f"HTTP {code}")
check("status = ok",             h.get("status") == "ok")
check("job_counts present",      "job_counts" in h)
check("retry_schedule present",  "retry_schedule" in h)
print(f"  {INFO} retry_schedule : {h.get('retry_schedule')}")
print(f"  {INFO} job_counts     : {h.get('job_counts')}")
print(f"  {INFO} account        : used={h.get('account_images_used')}  remaining={h.get('account_images_remaining')}")

# ── 2. Submit job ────────────────────────────────────────────
print("\n[2] JOB SUBMIT")
code2, j = post("/job", {
    "image_url":    IMAGE,
    "prompt":       "neon city vibes",
    "aspect_ratio": "16:9"
})
check("POST /job → 202",      code2 == 202, f"HTTP {code2}")
check("job_id in response",   "job_id" in j)
check("status = pending",     j.get("status") == "pending")
check("message has retry info","5min" in j.get("message",""))

JOB_ID = j.get("job_id", "")
print(f"  {INFO} job_id : {JOB_ID}")

# ── 3. Job status ────────────────────────────────────────────
print("\n[3] JOB STATUS  (GET /job/<id>)")
if JOB_ID:
    time.sleep(1)
    code3, s = get(f"/job/{JOB_ID}")
    check("GET /job/<id> → 200",    code3 == 200, f"HTTP {code3}")
    check("job_id matches",         s.get("job_id") == JOB_ID)
    check("max_attempts = 5",       s.get("max_attempts") == 5)
    check("attempts >= 0",          isinstance(s.get("attempts"), int))
    check("status field present",   "status" in s)
    check("error_type field present","error_type" in s)
    check("next_retry_at field",    "next_retry_at" in s)
    check("created_at field",       "created_at" in s)

    st = s.get("status","?")
    print(f"  {INFO} status        : {st}")
    print(f"  {INFO} attempts      : {s.get('attempts')}/{s.get('max_attempts')}")
    if st == "completed":
        print(f"  {INFO} result url   : {s.get('url')}")
    elif st == "retry_pending":
        print(f"  {INFO} next retry   : {s.get('next_retry_at')}  ({s.get('retry_in_seconds','-')}s)")
    elif st == "failed":
        print(f"  {INFO} error        : {s.get('error','')[:80]}")
        print(f"  {INFO} error_type   : {s.get('error_type')}")
else:
    check("Job ID obtained", False)

# ── 4. Job list ──────────────────────────────────────────────
print("\n[4] JOB LIST  (GET /jobs)")
code4, lst = get("/jobs")
check("GET /jobs → 200",     code4 == 200, f"HTTP {code4}")
check("total field present", "total" in lst)
check("jobs array present",  "jobs" in lst)
total = lst.get("total", 0)
print(f"  {INFO} total jobs in system: {total}")

# Filter test
code5, pend = get("/jobs?status=pending")
code6, done = get("/jobs?status=completed")
code7, fail = get("/jobs?status=failed")
code8, retr = get("/jobs?status=retry_pending")
check("?status=pending works",       code5 == 200)
check("?status=completed works",     code6 == 200)
check("?status=failed works",        code7 == 200)
check("?status=retry_pending works", code8 == 200)
print(f"  {INFO} pending={pend.get('total',0)}  completed={done.get('total',0)}  failed={fail.get('total',0)}  retry_pending={retr.get('total',0)}")

# ── 5. Error cases ───────────────────────────────────────────
print("\n[5] ERROR CASES")
c1, r1 = post("/job", {"prompt": "no url"})
check("Missing image_url → 400",     c1 == 400, f"HTTP {c1}")

c2, r2 = post("/job", {"image_url": IMAGE, "aspect_ratio": "bad:ratio"})
check("Bad aspect_ratio → 400",      c2 == 400, f"HTTP {c2}")

c3, r3 = get("/job/nonexistent-id-00000")
check("GET /job/bad-id → 404",       c3 == 404, f"HTTP {c3}")

# ── 6. Account status ────────────────────────────────────────
print("\n[6] ACCOUNT")
code_a, acc = get("/account/status")
check("GET /account/status → 200",   code_a == 200, f"HTTP {code_a}")
check("images_used present",         "images_used" in acc)
check("images_remaining present",    "images_remaining" in acc)
print(f"  {INFO} used={acc.get('images_used')}  remaining={acc.get('images_remaining')}  session={acc.get('has_session')}")

# ═══════════════════════════════════════════════════════════
print()
print("═" * 55)
total_t = passed + failed
if failed == 0:
    print(f"\033[92m  ALL {total_t} TESTS PASSED ✓\033[0m")
else:
    print(f"\033[91m  {passed}/{total_t} PASSED — {failed} FAILED\033[0m")
print("═" * 55)
print()
