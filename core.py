import hmac
import os, re, threading
from contextlib import contextmanager
import ddddocr
from fastapi import Header, HTTPException
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

API_KEYS = tuple(dict.fromkeys(
    value.strip()
    for name in ("API_KEY", "DOCKER_FILES_API_KEY", "SUMMARY_CSV_API_KEY")
    if (value := os.environ.get(name, "").strip())
))
if not API_KEYS:
    raise RuntimeError("Configure API_KEY or DOCKER_FILES_API_KEY for service authentication")
AML_URL  = "https://aml-gui-v1.mfilterit.net"
AML_USER = os.environ.get("AML_USER")      # shared login (optional, baad me hata sakte ho)
AML_PASS = os.environ.get("AML_PASS")

LOGIN_X = {
    "user":    "xpath=/html/body/div[1]/div/div[2]/form/label[1]/input",
    "pass":    "xpath=/html/body/div[1]/div/div[2]/form/label[2]/input",
    "cap_img": "xpath=/html/body/div[1]/div/div[2]/form/label[6]/div/img",
    "cap_in":  "xpath=/html/body/div[1]/div/div[2]/form/label[6]/input",
    "login":   "xpath=/html/body/div[1]/div/div[2]/form/button",
}

# 512MB instance ke liye memory-saving flags
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",          # Docker me /dev/shm chhota hota hai, iske bina Chromium crash karta hai
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
    "--no-first-run",
    "--renderer-process-limit=1",
]

browser_lock = threading.Lock()          # ek time pe ek hi Chromium
ocr = ddddocr.DdddOcr(show_ad=False)     # startup pe ek baar load


def _read_int(path):
    try:
        v = open(path).read().strip()
        return None if v == "max" else int(v)
    except Exception:
        return None


def mem_mb():
    """Container ki memory use / limit (cgroup v2 ya v1). Logs me OOM pakadne ke liye."""
    cur = _read_int("/sys/fs/cgroup/memory.current") or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    lim = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    f = lambda b: round(b / 1048576) if b else "?"
    return f"{f(cur)}MB / {f(lim)}MB"


def log_mem(tag):
    print(f"[MEM] {tag}: {mem_mb()}", flush=True)


log_mem("startup (ocr loaded)")

def require_key(x_api_key: str = Header(None)):
    got = (x_api_key or "").strip()
    if not any(hmac.compare_digest(got.encode(), key.encode()) for key in API_KEYS):
        print(f"[AUTH] 401: header_len={len(got)} configured_key_count={len(API_KEYS)}", flush=True)
        raise HTTPException(401, "Invalid API key")


def solve_captcha(img_bytes: bytes) -> str:
    return re.sub(r"\W", "", ocr.classification(img_bytes))


@contextmanager
def browser_page(accept_downloads=False):
    with browser_lock, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=BROWSER_ARGS)
        log_mem("browser launched")
        ctx = browser.new_context(accept_downloads=accept_downloads)
        page = ctx.new_page()
        page.set_default_timeout(20000)
        try:
            yield page
        finally:
            log_mem("before close")
            browser.close()


def aml_login(page, success_selector, username=None, password=None, max_tries=8):
    """Login ke baad success_selector dikhna chahiye (us flow ka pehla element)."""
    username = username or AML_USER
    password = password or AML_PASS
    if not username or not password:
        raise RuntimeError("AML credentials missing (na request me, na server env me)")
    last_text = ""
    for attempt in range(1, max_tries + 1):
        page.goto(AML_URL, wait_until="networkidle")
        page.fill(LOGIN_X["user"], username)
        page.fill(LOGIN_X["pass"], password)

        text = solve_captcha(page.locator(LOGIN_X["cap_img"]).screenshot())
        if not text:
            continue
        page.fill(LOGIN_X["cap_in"], text)
        page.click(LOGIN_X["login"])

        try:
            page.wait_for_selector(success_selector, timeout=6000)
            log_mem("login ok")
            return
        except PWTimeout:
            try:
                last_text = re.sub(r"\s+", " ", page.inner_text("body"))[:200]
            except Exception:
                last_text = ""
            print(f"Login attempt {attempt} fail (captcha read: {text!r}) page: {last_text!r}", flush=True)
    raise RuntimeError(
        f"Login fail after {max_tries} tries (captcha or AML username/password wrong). Last page text: {last_text}"
    )
