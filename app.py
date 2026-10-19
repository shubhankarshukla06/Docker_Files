import os, re, tempfile, threading
from datetime import datetime

import ddddocr
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://aml-gui-v1.mfilterit.net"
USERNAME = os.environ["AML_USER"]
PASSWORD = os.environ["AML_PASS"]
API_KEY  = os.environ["API_KEY"]

X = {
    "user":    "xpath=/html/body/div[1]/div/div[2]/form/label[1]/input",
    "pass":    "xpath=/html/body/div[1]/div/div[2]/form/label[2]/input",
    "cap_img": "xpath=/html/body/div[1]/div/div[2]/form/label[6]/div/img",
    "cap_in":  "xpath=/html/body/div[1]/div/div[2]/form/label[6]/input",
    "login":   "xpath=/html/body/div[1]/div/div[2]/form/button",
    "from":    "xpath=/html/body/div[1]/div/form/div[2]/input",
    "to":      "xpath=/html/body/div[1]/div/form/div[3]/input",
    "status":  "xpath=/html/body/div[1]/div/form/div[5]/select",
    "feature": "xpath=/html/body/div[1]/div/form/div[6]/select",
    "filter":  "xpath=/html/body/div[1]/div/form/div[7]/button[1]",
    "export":  "xpath=/html/body/div[1]/div/form/div[7]/button[4]",
}

app = FastAPI()
lock = threading.Lock()
ocr = ddddocr.DdddOcr(show_ad=False)   # startup pe ek baar load


class Req(BaseModel):
    from_date: str   # YYYY-MM-DD
    to_date: str     # YYYY-MM-DD


def solve_captcha(img_bytes: bytes) -> str:
    return re.sub(r"\W", "", ocr.classification(img_bytes))


def login(page, max_tries=8):
    for attempt in range(1, max_tries + 1):
        page.goto(URL, wait_until="networkidle")
        page.fill(X["user"], USERNAME)
        page.fill(X["pass"], PASSWORD)

        text = solve_captcha(page.locator(X["cap_img"]).screenshot())
        if not text:
            continue
        page.fill(X["cap_in"], text)
        page.click(X["login"])

        try:
            page.wait_for_selector(X["from"], timeout=6000)
            return
        except PWTimeout:
            print(f"Captcha attempt {attempt} fail (read: {text!r})")
    raise RuntimeError("Login fail: captcha multiple tries me solve nahi hua")


@app.post("/export")
def export(req: Req, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")
    try:
        datetime.strptime(req.from_date, "%Y-%m-%d")
        datetime.strptime(req.to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date format YYYY-MM-DD hona chahiye")

    with lock, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(20000)
        try:
            login(page)

            page.fill(X["from"], req.from_date)
            page.fill(X["to"], req.to_date)
            page.select_option(X["status"], index=1)    # option[2]
            page.select_option(X["feature"], index=2)   # option[3]
            page.click(X["filter"])
            page.wait_for_load_state("networkidle")

            with page.expect_download(timeout=60000) as dl:
                page.click(X["export"])
            download = dl.value

            path = os.path.join(tempfile.gettempdir(), download.suggested_filename)
            download.save_as(path)
            return FileResponse(path, filename=download.suggested_filename)
        except Exception as e:
            raise HTTPException(500, f"Automation error: {e}")
        finally:
            browser.close()


@app.get("/health")
def health():
    return {"ok": True}