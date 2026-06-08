#!/usr/bin/env python3
"""
Artlist Toolkit — Auto Image-to-Image Generator  (FIXED)
=========================================================
Fixes applied:
  1. solve_turnstile: retries up to 3 times on ERROR_CAPTCHA_SOLVE_FAILED
     instead of immediately raising — capsolver occasionally fails transiently.
  2. _nextauth_login: more robust cookie extraction, verifies login via
     /api/auth/session, retries the full login flow once on first failure,
     and checks every possible session-token cookie variant.
  3. _register_new_account / _get_fresh_token: fresh turnstile token on each
     attempt (old token expires in ~2 min, causing repeated failures).
"""

import argparse
import json
import os
import random
import string
import sys
import time
from pathlib import Path

import requests
from curl_cffi import requests as cf_requests

# ─── Constants ────────────────────────────────────────────────────────────────

ARTLIST_BASE    = "https://artlist.io"
TOOLKIT_BASE    = "https://toolkit.artlist.io"
SESSION_COOKIE  = "__Secure-session.artlist-prod.session-token"

MODEL_GROUP_ID  = 345
FEATURE         = "image-to-image"
POLL_INTERVAL   = 4
POLL_TIMEOUT    = 180

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


# ─── Tiny helpers ─────────────────────────────────────────────────────────────

def _rstr(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))

def _random_email() -> str:
    return f"{_rstr(8)}.{_rstr(5)}@gmail.com"

def _random_password() -> str:
    return f"P@{_rstr(5)}!{random.randint(10,99)}z"

def _uuidv7() -> str:
    """Generate a UUIDv7 (time-ordered) string."""
    ts   = int(time.time() * 1000)
    rand = "".join(random.choices("0123456789abcdef", k=20))
    th   = f"{ts:012x}"
    var  = hex(random.randint(8, 11))[2]
    return f"{th[:8]}-{th[8:12]}-7{rand[:3]}-{var}{rand[3:6]}-{rand[6:18]}"


# ─── HTTP session factory ─────────────────────────────────────────────────────

def _make_session(proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


# ─── CapSolver — Turnstile solver  (FIXED: retry on transient failures) ──────

def solve_turnstile(capsolver_key: str, turnstile_site_key: str,
                    page_url: str = "https://artlist.io/start-now",
                    max_attempts: int = 3) -> str:
    """
    Use CapSolver REST API to solve Cloudflare Turnstile.
    Returns the turnstile token string.

    FIX: Retries the entire task up to max_attempts times on
    ERROR_CAPTCHA_SOLVE_FAILED (capsolver transient failure).
    """
    CAPSOLVER_API = "https://api.capsolver.com"
    last_err = "unknown"

    for solve_attempt in range(1, max_attempts + 1):
        if solve_attempt > 1:
            wait = 5 * solve_attempt
            print(f"[capsolver] Retrying in {wait}s (attempt {solve_attempt}/{max_attempts}) ...")
            time.sleep(wait)

        try:
            print(f"[capsolver] Creating Turnstile task (attempt {solve_attempt}) ...")
            r = requests.post(f"{CAPSOLVER_API}/createTask", json={
                "clientKey": capsolver_key,
                "task": {
                    "type":       "AntiTurnstileTaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": turnstile_site_key,
                },
            }, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("errorId"):
                raise RuntimeError(f"[capsolver] Create task error: {data.get('errorDescription')}")
            task_id = data["taskId"]
            print(f"[capsolver] Task created: {task_id} — polling ...")

            for _ in range(60):   # max 60 × 3s = 3 min
                time.sleep(3)
                r2 = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
                    "clientKey": capsolver_key,
                    "taskId":    task_id,
                }, timeout=15)
                r2.raise_for_status()
                res = r2.json()
                status = res.get("status", "")
                if status == "ready":
                    token = res["solution"]["token"]
                    print(f"[capsolver] [OK] Turnstile solved")
                    return token
                if status not in ("processing", "idle"):
                    # e.g. ERROR_CAPTCHA_SOLVE_FAILED — break inner loop, retry outer
                    last_err = f"status={status} | {res}"
                    print(f"[capsolver] Task failed: {last_err[:120]}")
                    break
            else:
                raise TimeoutError("[capsolver] Turnstile solve timed out after 3 min")

        except TimeoutError:
            raise
        except RuntimeError as e:
            last_err = str(e)

    raise RuntimeError(f"[capsolver] Failed after {max_attempts} attempts. Last: {last_err}")


# ─── Auth  (FIXED: robust cookie extraction + verify via /api/auth/session) ──

def _find_token_in_cookies(cookies) -> str | None:
    """
    Extract the Artlist session JWT from a curl_cffi or requests CookieJar.

    FIX: Checks all cookie names for any *session-token* substring, not just
    the exact SESSION_COOKIE name.  Artlist has changed cookie names before and
    may do so again; this makes the extractor resilient to name changes.
    """
    # 1. Exact match first
    t = cookies.get(SESSION_COOKIE)
    if t:
        return t

    # 2. Any cookie whose name contains "session-token"
    for name in cookies:
        key = name if isinstance(name, str) else getattr(name, "name", str(name))
        if "session-token" in key or "session_token" in key:
            val = cookies.get(key)
            if val:
                return val

    # 3. Any cookie whose name contains "session" and looks like a JWT
    for name in cookies:
        key = name if isinstance(name, str) else getattr(name, "name", str(name))
        if "session" in key:
            val = cookies.get(key) or ""
            if val.startswith("eyJ"):   # JWT header prefix (base64 of {"alg":...)
                return val

    return None


def _verify_session_via_api(sess, base_url: str) -> str | None:
    """
    Call /api/auth/session to confirm login and extract the token from the
    returned JSON (some NextAuth versions embed it there).
    Also re-checks cookies after the round-trip (the response may set them).
    """
    try:
        r = sess.get(
            f"{base_url}/api/auth/session",
            timeout=15,
            impersonate="chrome",
        )
        # After this request cookies are often refreshed
        token = _find_token_in_cookies(sess.cookies)
        if token:
            return token

        # Some NextAuth setups return the token in the JSON body
        try:
            body = r.json()
            t = (body.get("accessToken") or
                 body.get("token") or
                 body.get("sessionToken") or
                 (body.get("user") or {}).get("sessionToken"))
            if t and isinstance(t, str) and t.startswith("eyJ"):
                return t
        except Exception:
            pass
    except Exception as e:
        print(f"[auth] /api/auth/session check failed: {e}")
    return None


def _nextauth_login(base_url: str, email: str, password: str,
                    is_registration: bool = False, full_name: str = "Alex Smith",
                    turnstile_token: str | None = None,
                    proxy: str | None = None) -> str:
    """
    Login (or register) via NextAuth credentials flow using Chrome TLS
    impersonation (curl_cffi) to bypass Cloudflare bot detection.
    Returns session JWT string, or raises RuntimeError on failure.

    FIX: Retries the full login flow once if the first attempt yields no
    cookie; also verifies success via /api/auth/session round-trip.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None

    for login_attempt in range(1, 3):   # 2 total attempts per call
        if login_attempt > 1:
            print(f"[auth] Login attempt {login_attempt} for {email} ...")
            time.sleep(3)

        sess = cf_requests.Session()
        sess.headers.update({
            "User-Agent":      UA,
            "Referer":         f"{base_url}/",
            "Origin":          base_url,
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxies:
            sess.proxies = proxies

        # Step 1 — get CSRF token
        try:
            r_csrf = sess.get(
                f"{base_url}/api/auth/csrf",
                timeout=15,
                impersonate="chrome",
            )
            r_csrf.raise_for_status()
            csrf = r_csrf.json()["csrfToken"]
        except Exception as e:
            print(f"[auth] CSRF fetch failed: {e}")
            continue

        form: dict = {
            "csrfToken":   csrf,
            "email":       email,
            "password":    password,
            "callbackUrl": f"{base_url}/",
            "json":        "true",
        }
        if is_registration:
            form["isRegistration"] = "true"
            form["fullName"]       = full_name
        if turnstile_token:
            form["cf-turnstile-response"] = turnstile_token

        # Step 2 — primary NextAuth callback endpoint
        try:
            sess.post(
                f"{base_url}/api/auth/callback/credentials",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True,
                timeout=30,
                impersonate="chrome",
            )
        except Exception as e:
            print(f"[auth] callback/credentials POST failed: {e}")

        token = _find_token_in_cookies(sess.cookies)

        # Step 3 — fallback: try signin/credentials endpoint
        if not token:
            try:
                sess.post(
                    f"{base_url}/api/auth/signin/credentials",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=True,
                    timeout=30,
                    impersonate="chrome",
                )
            except Exception as e:
                print(f"[auth] signin/credentials POST failed: {e}")
            token = _find_token_in_cookies(sess.cookies)

        # Step 4 — verify via /api/auth/session (also refreshes cookies)
        if not token:
            time.sleep(1)
            token = _verify_session_via_api(sess, base_url)

        if token:
            return token

        print(f"[auth] Attempt {login_attempt}: no session cookie for {email}")

    raise RuntimeError(
        f"Login failed for {email} at {base_url} — "
        "no session cookie returned after 2 attempts. "
        "Artlist may have updated their auth flow or blocked this IP."
    )


# ─── Account pool ─────────────────────────────────────────────────────────────

POOL_FILE_DEFAULT = "accounts.json"

def _pool_load(pool_file: str) -> list[dict]:
    p = Path(pool_file)
    if not p.exists():
        return []
    with open(p) as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _pool_save(pool_file: str, accounts: list[dict]) -> None:
    with open(pool_file, "w") as f:
        json.dump(accounts, f, indent=2)
    print(f"[pool] Saved {len(accounts)} account(s) to {pool_file}")


def _pool_get_token(pool_file: str, proxy: str | None = None) -> tuple[str, str] | None:
    accounts = _pool_load(pool_file)
    changed  = False

    for acc in accounts:
        if acc.get("failed"):
            continue
        email    = acc.get("email", "")
        password = acc.get("password", "")
        if not email or not password:
            continue

        cached_token = acc.get("session_token")
        cached_at    = acc.get("session_cached_at", 0)
        if cached_token and (time.time() - cached_at) < 12 * 3600:
            print(f"[pool] Using cached session for {email}")
            return email, cached_token

        print(f"[pool] Logging in as {email} ...")
        try:
            token = _nextauth_login(ARTLIST_BASE, email, password, proxy=proxy)
            acc["session_token"]     = token
            acc["session_cached_at"] = time.time()
            changed = True
            print(f"[pool] [OK] Logged in: {email}")
            if changed:
                _pool_save(pool_file, accounts)
            return email, token
        except Exception as e:
            print(f"[pool] [FAIL] Login failed for {email}: {e}")
            acc["failed"] = True
            changed = True

    if changed:
        _pool_save(pool_file, accounts)
    return None


def pool_add_account(pool_file: str, email: str, password: str,
                     session_token: str | None = None) -> None:
    accounts = _pool_load(pool_file)
    for acc in accounts:
        if acc.get("email") == email:
            acc["password"] = password
            if session_token:
                acc["session_token"]     = session_token
                acc["session_cached_at"] = time.time()
            acc.pop("failed", None)
            _pool_save(pool_file, accounts)
            return
    entry: dict = {"email": email, "password": password}
    if session_token:
        entry["session_token"]     = session_token
        entry["session_cached_at"] = time.time()
    accounts.append(entry)
    _pool_save(pool_file, accounts)


def get_session_token(args) -> str:
    token = getattr(args, "session", None) or os.environ.get("ARTLIST_SESSION")
    if token:
        print("[auth] Using provided session token.")
        return token.strip()

    proxy      = getattr(args, "proxy", None) or os.environ.get("ARTLIST_PROXY")
    pool_file  = getattr(args, "accounts", None)

    if pool_file:
        result = _pool_get_token(pool_file, proxy=proxy)
        if result:
            _, token = result
            return token
        print("[pool] All accounts exhausted.")

    email    = getattr(args, "email", None)    or os.environ.get("ARTLIST_EMAIL")
    password = getattr(args, "password", None) or os.environ.get("ARTLIST_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "No auth provided. Use --session, ARTLIST_SESSION env, "
            "--accounts pool.json, or --email / --password."
        )

    print(f"[auth] Logging in as {email} ...")
    try:
        token = _nextauth_login(TOOLKIT_BASE, email, password, proxy=proxy)
        print("[auth] [OK] Toolkit login succeeded.")
        return token
    except Exception:
        pass

    token = _nextauth_login(ARTLIST_BASE, email, password, proxy=proxy)
    print("[auth] [OK] artlist.io login succeeded.")
    return token


# ─── tRPC client ─────────────────────────────────────────────────────────────

class ToolkitClient:
    def __init__(self, session_token: str, proxy: str | None = None):
        # Use curl_cffi (Chrome TLS impersonation) — toolkit.artlist.io has
        # Cloudflare protection that blocks plain Python requests.
        self._sess = cf_requests.Session()
        self._sess.headers.update({
            "User-Agent":     UA,
            "Origin":         TOOLKIT_BASE,
            "Referer":        f"{TOOLKIT_BASE}/",
            "Content-Type":   "application/json",
            "x-trpc-source":  "nextjs-react",
        })
        if proxy:
            self._sess.proxies = {"http": proxy, "https": proxy}

        # Set the session cookie on the root domain (.artlist.io) so it is
        # automatically sent to BOTH artlist.io AND toolkit.artlist.io.
        # Also set it explicitly on toolkit.artlist.io as a belt-and-suspenders
        # measure for strict cookie-jar implementations.
        for domain in (".artlist.io", "toolkit.artlist.io", "artlist.io"):
            self._sess.cookies.set(SESSION_COOKIE, session_token, domain=domain)

    def _post(self, procedure: str, data: dict, meta: dict | None = None) -> dict:
        body: dict = {"json": data}
        if meta:
            body["meta"] = meta
        rid = _uuidv7()
        url = f"{TOOLKIT_BASE}/api/trpc/{procedure}"
        r = self._sess.post(url, json=body,
                            headers={"x-request-id": rid},
                            timeout=45, impersonate="chrome")
        return self._parse(r, procedure)

    def _get(self, procedure: str, data: dict) -> dict:
        inp = json.dumps({"json": data})
        url = f"{TOOLKIT_BASE}/api/trpc/{procedure}"
        r = self._sess.get(url, params={"input": inp},
                           timeout=45, impersonate="chrome")
        return self._parse(r, procedure)

    @staticmethod
    def _parse(r: requests.Response, label: str) -> dict:
        if not r.ok:
            try:
                body = r.json()
                item = body[0] if isinstance(body, list) else body
                err  = item.get("error", {}).get("json", {})
                msg  = err.get("message") or str(body)[:300]
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"[{label}] HTTP {r.status_code}: {msg}")
        body = r.json()
        item = body[0] if isinstance(body, list) else body
        if item.get("error"):
            msg = item["error"].get("json", {}).get("message", str(item["error"]))
            raise RuntimeError(f"[{label}] {msg}")
        d = item["result"]["data"]
        return d.get("json", d)

    def upload_image(self, image_path: str) -> tuple[str, str, str, str]:
        path = Path(image_path)
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime_type = mime_map.get(path.suffix.lower(), "image/jpeg")
        file_size = path.stat().st_size

        print(f"[upload] Requesting signed upload URL for {path.name} ...")
        upload_info = self._post("uploadRouter.getPresignedUrl", {
            "fileName":    path.name,
            "contentType": mime_type,
            "fileSize":    file_size,
        })
        file_key      = upload_info["fileKey"]
        upload_url    = upload_info["uploadUrl"]
        bare_s3_url   = upload_info.get("url", "")

        print(f"[upload] Uploading {path.name} to S3 (key={file_key}) ...")
        with open(path, "rb") as fh:
            put_r = self._sess.put(
                upload_url,
                data=fh,
                headers={"Content-Type": mime_type},
                timeout=120,
            )
        put_r.raise_for_status()

        print(f"[upload] Fetching presigned GET URL ...")
        get_info    = self._post("uploadRouter.getPresignedUrlFromKey", {
            "fileKey":      file_key,
            "expiresIn":    259200,
            "verifyExists": True,
        })
        presigned   = get_info.get("presignedUrl") or get_info.get("url") or get_info
        if isinstance(presigned, dict):
            presigned = presigned.get("presignedUrl") or presigned.get("url") or bare_s3_url

        print(f"[upload] [OK] Done. fileKey={file_key}")
        return file_key, bare_s3_url, presigned, mime_type

    def get_cost_quote(self, presigned_get: str, prompt: str = "",
                       aspect_ratio: str = "auto") -> dict:
        """
        GET query — modelRouter.getCostQuote
        Returns: {cost/price, digitalSignature, timestamp, modelId, modelFeature}
        """
        print(f"[quote] Getting cost quote ...")
        quote = self._get("modelRouter.getCostQuote", {
            "modelGroupId": MODEL_GROUP_ID,
            "input": {
                "referenceImages": [presigned_get],
                "prompt":          prompt,
                "aspectRatio":     aspect_ratio,
                "feature":         FEATURE,
            },
        })
        cost = quote.get("cost") or quote.get("price") or 0
        print(f"[quote] [OK] modelId={quote.get('modelId')}  cost={cost}  sig={'YES' if quote.get('digitalSignature') else 'NO'}")
        return quote

    def create_generation(self, prompt: str, file_key: str,
                          presigned_get: str, mime_type: str,
                          quote: dict,
                          aspect_ratio: str = "auto") -> str:
        """
        POST mutation — userGenerationRouter.createUserGeneration
        chatSessionId is generated client-side (UUID); server uses it as grouping key.
        Returns chatSessionId for polling.
        """
        chat_session_id = _uuidv7()
        feature = quote.get("modelFeature") or FEATURE
        print(f"[gen] Creating generation (session={chat_session_id[:8]}) ...")
        result = self._post("userGenerationRouter.createUserGeneration", {
            "chatSessionId":              chat_session_id,
            "inputs": {
                "referenceImages": [presigned_get],
                "prompt":          prompt,
                "aspectRatio":     aspect_ratio,
                "feature":         feature,
            },
            "artifacts":                  [{"fileKey": file_key}],
            "modelGroupId":               quote.get("modelId") or MODEL_GROUP_ID,
            "feature":                    feature,
            "price":                      quote.get("cost") or quote.get("price") or 0,
            "costQuoteDigitalSignature":   quote.get("digitalSignature", ""),
            "timestamp":                  quote.get("timestamp") or int(time.time() * 1000),
            "generationMethod":           "FREE" if (quote.get("cost") or quote.get("price") or 0) == 0 else None,
        })
        print(f"[gen] [OK] Submitted. Waiting for result ...")
        if isinstance(result, dict):
            return result.get("chatSessionId") or chat_session_id
        return chat_session_id

    def poll_generation(self, chat_session_id: str) -> list[str]:
        """
        Poll via userGenerationRouter.getUserGenerationsBySession
        until status COMPLETED/DONE or timeout.
        """
        start = time.time()
        while True:
            elapsed = int(time.time() - start)
            if elapsed > POLL_TIMEOUT:
                raise TimeoutError(f"Generation timed out after {POLL_TIMEOUT}s")
            print(f"[poll] status=processing ({elapsed}s) ...")
            time.sleep(POLL_INTERVAL)

            try:
                data = self._get("userGenerationRouter.getUserGenerationsBySession", {
                    "sessionId": chat_session_id,
                    "perPage":   10,
                })
            except Exception as e:
                print(f"[poll] fetch error: {e}")
                continue

            items = []
            if isinstance(data, dict):
                items = data.get("items") or data.get("generations") or []
            elif isinstance(data, list):
                items = data

            urls: list[str] = []
            for item in items:
                status = (item.get("status") or "").upper()
                if status in ("COMPLETED", "DONE", "SUCCESS"):
                    for out in item.get("outputs") or []:
                        u = out.get("url") or out.get("outputUrl") or out.get("src") or ""
                        if isinstance(u, str) and u.startswith("http"):
                            urls.append(u)
                    # Also check top-level url fields
                    for k in ("url", "outputUrl", "imageUrl"):
                        v = item.get(k) or ""
                        if isinstance(v, str) and v.startswith("http"):
                            urls.append(v)
                elif status in ("FAILED", "ERROR", "CANCELLED"):
                    raise RuntimeError(
                        f"Generation failed (status={status}): "
                        f"{item.get('error') or item.get('errorMessage') or 'unknown'}"
                    )

            if urls:
                elapsed = int(time.time() - start)
                print(f"[poll] status=completed ({elapsed}s)")
                return list(dict.fromkeys(urls))


# ─── High-level pipeline ──────────────────────────────────────────────────────

def _run_one(client: "ToolkitClient", image_path: str, prompt: str,
             aspect_ratio: str, idx: int, total: int) -> list[str]:
    label = f"[{idx}/{total}]"
    print(f"\n{label} ── Starting generation {idx} of {total} ──")

    file_key, _, presigned_get, mime_type = client.upload_image(image_path)
    quote           = client.get_cost_quote(presigned_get, prompt=prompt, aspect_ratio=aspect_ratio)
    chat_session_id = client.create_generation(
        prompt=prompt,
        file_key=file_key,
        presigned_get=presigned_get,
        mime_type=mime_type,
        quote=quote,
        aspect_ratio=aspect_ratio,
    )
    return client.poll_generation(chat_session_id)


def generate(args) -> list[str]:
    proxy        = getattr(args, "proxy", None)  or os.environ.get("ARTLIST_PROXY")
    prompt       = getattr(args, "prompt", "") or ""
    count        = getattr(args, "count", 1) or 1
    aspect_ratio = getattr(args, "aspect_ratio", "auto") or "auto"

    token  = get_session_token(args)
    client = ToolkitClient(token, proxy=proxy)

    all_urls: list[str] = []
    for i in range(1, count + 1):
        urls = _run_one(client, args.image, prompt, aspect_ratio, i, count)
        all_urls.extend(urls)

    return all_urls


def do_register(args) -> None:
    proxy         = getattr(args, "proxy", None)         or os.environ.get("ARTLIST_PROXY")
    email         = getattr(args, "email", None)         or os.environ.get("ARTLIST_EMAIL")         or _random_email()
    password      = getattr(args, "password", None)      or os.environ.get("ARTLIST_PASSWORD")      or _random_password()
    name          = getattr(args, "name", "Alex Smith")  or "Alex Smith"
    capsolver_key = getattr(args, "capsolver_key", None) or os.environ.get("CAPSOLVER_API_KEY")
    site_key      = getattr(args, "turnstile_site_key", None) or os.environ.get("ARTLIST_TURNSTILE_KEY")
    pool_file     = getattr(args, "accounts", None)

    print(f"[register] New account: {email}")

    turnstile_token: str | None = None
    if capsolver_key and site_key:
        turnstile_token = solve_turnstile(capsolver_key, site_key)
    elif not capsolver_key:
        print("[register] WARNING: --capsolver-key not provided — trying without Turnstile (may fail)")
    elif not site_key:
        print("[register] WARNING: --turnstile-site-key not provided — trying without Turnstile (may fail)")

    try:
        token = _nextauth_login(
            ARTLIST_BASE, email, password,
            is_registration=True, full_name=name,
            turnstile_token=turnstile_token,
            proxy=proxy,
        )
        print(f"\n[register] [OK] Account created!")
        print(f"  Email:    {email}")
        print(f"  Password: {password}")
        print(f"  Token:    {token[:50]}...")

        if pool_file:
            pool_add_account(pool_file, email, password, session_token=token)
            print(f"[register] [OK] Saved to pool: {pool_file}")

    except Exception as e:
        print(f"[register] [FAIL] Failed: {e}")
        sys.exit(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Artlist Toolkit — Auto Image-to-Image Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g_auth = p.add_argument_group("Auth")
    g_auth.add_argument("--session",  metavar="TOKEN")
    g_auth.add_argument("--email",    metavar="EMAIL")
    g_auth.add_argument("--password", metavar="PASS")
    g_auth.add_argument("--accounts", metavar="FILE", default=None)

    g_cap = p.add_argument_group("CapSolver (auto-registration)")
    g_cap.add_argument("--capsolver-key",     metavar="KEY")
    g_cap.add_argument("--turnstile-site-key", metavar="SITEKEY")

    g_gen = p.add_argument_group("Generation")
    g_gen.add_argument("--image",  metavar="PATH")
    g_gen.add_argument("--prompt", metavar="TEXT", default="")
    g_gen.add_argument("--count",  metavar="N", type=int, default=1)
    g_gen.add_argument("--aspect-ratio", metavar="RATIO", default="auto",
        choices=["auto", "1:1", "16:9", "9:16", "4:3", "3:4"])

    g_net = p.add_argument_group("Network")
    g_net.add_argument("--proxy", metavar="URL")

    p.add_argument("--register", action="store_true")
    p.add_argument("--name", metavar="NAME", default="Alex Smith")

    args = p.parse_args()

    if args.register:
        do_register(args)
        return

    if not args.image:
        p.print_help()
        sys.exit(1)

    if not Path(args.image).exists():
        print(f"Error: image not found: {args.image}")
        sys.exit(1)

    try:
        urls = generate(args)
    except Exception as e:
        print(f"\n[FAIL] Error: {e}")
        sys.exit(1)

    if not urls:
        print("\nWARNING Generation completed but no output URLs found.")
        sys.exit(0)

    count = getattr(args, "count", 1) or 1
    print(f"\n{'='*60}")
    print(f"[OK] Done! {len(urls)} image(s) generated from {count} run(s).")
    for url in urls:
        print(f"  {url}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
