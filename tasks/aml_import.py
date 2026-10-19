import csv, io, os, re, tempfile, time, uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from playwright.sync_api import Error as PWError

from core import require_key, browser_page, aml_login

router = APIRouter(dependencies=[Depends(require_key)])

X = {
    "import":  "xpath=/html/body/div[1]/div/form/div[7]/button[3]",
    "file":    "xpath=/html/body/div[1]/div/div[4]/div/div[2]/input",
    "confirm": "xpath=/html/body/div[1]/div/div[4]/div/div[3]/button[2]",
}

MAX_BYTES = 10 * 1024 * 1024   # 10 MB

SUCCESS_RE = re.compile(
    r"(success(?:fully)?|imported\s+successfully|uploaded\s+successfully|inserted\s+successfully"
    r"|record[s]?\s+(?:added|inserted)|data\s+import(?:ed)?\s+successfully)", re.I)
ERROR_RE = re.compile(
    r"(oops|no\s+column\s+present|fatal\s+error|import\s+failed|upload\s+failed|invalid\s+file"
    r"|not\s+imported|not\s+uploaded|duplicate\s+entr(?:y|ies)|wrong\s+format|column\s+mismatch"
    r"|sqlstate|specified\s+twice)", re.I)


def validate_csv(filename: str, data: bytes) -> int:
    """CSV check karke data rows ki count return karta hai."""
    if not (filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Sirf .csv file allowed hai")
    if not data:
        raise HTTPException(400, "File khaali hai")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File 10 MB se badi hai")
    if data[:2] == b"PK" or data.lstrip()[:5].lower() in (b"<!doc", b"<html"):
        raise HTTPException(400, "Ye asli CSV nahi hai (xlsx ya HTML lag raha hai)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV UTF-8 encoding me honi chahiye")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise HTTPException(400, "CSV me header ke baad kam se kam 1 row honi chahiye")
    return len(rows) - 1


def safe_click(page, selector):
    loc = page.locator(selector)
    try:
        loc.click(timeout=15000)
    except PWError:
        loc.dispatch_event("click")      # purane code ka JS-click wala fallback


def _n(rx, text):
    return len(rx.findall(text))


def wait_for_result(page, baseline_text, baseline_alerts, timeout_s=90):
    """success / error / unconfirmed / timeout + page ka chhota preview."""
    deadline = time.time() + timeout_s
    closed_at, preview = None, ""
    while time.time() < deadline:
        try:
            text = page.inner_text("body")
            modal_open = page.locator(X["file"]).is_visible()
            alerts = page.locator(".alert-danger").count()
        except PWError:                  # page reload ke beech
            page.wait_for_timeout(1000)
            continue
        preview = re.sub(r"\s+", " ", text)[:500]

        # sirf NAYA aaya hua message gino (menu/footer ke purane words se dhoka na ho)
        if _n(SUCCESS_RE, text) > _n(SUCCESS_RE, baseline_text):
            return "success", preview
        if _n(ERROR_RE, text) > _n(ERROR_RE, baseline_text) or alerts > baseline_alerts:
            return "error", preview

        if not modal_open:
            closed_at = closed_at or time.time()
            if time.time() - closed_at > 3:
                return "unconfirmed", preview    # modal band hua par koi message nahi mila
        page.wait_for_timeout(1000)
    return "timeout", preview


@router.post("/import")
def import_sheet(
    file: UploadFile = File(...),
    aml_user: str | None = Form(None),
    aml_pass: str | None = Form(None),
):
    data = file.file.read(MAX_BYTES + 1)
    rows = validate_csv(file.filename, data)
    if bool(aml_user) != bool(aml_pass):
        raise HTTPException(400, "aml_user aur aml_pass dono saath bhejo")

    tries = 3 if aml_user else 8      # user ke apne account pe kam retries (lock ka risk)
    path = os.path.join(tempfile.gettempdir(), f"aml_gui_import_{uuid.uuid4().hex}.csv")
    with open(path, "wb") as f:
        f.write(data)

    try:
        with browser_page() as page:
            aml_login(page, X["import"], aml_user, aml_pass, max_tries=tries)

            safe_click(page, X["import"])
            page.wait_for_selector(X["file"], state="attached", timeout=30000)
            page.set_input_files(X["file"], path)
            page.wait_for_selector(X["confirm"], state="visible", timeout=30000)

            baseline_text = page.inner_text("body")
            baseline_alerts = page.locator(".alert-danger").count()
            safe_click(page, X["confirm"])
            outcome, preview = wait_for_result(page, baseline_text, baseline_alerts)
    except Exception as e:
        raise HTTPException(500, f"Automation error: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if outcome == "error":
        raise HTTPException(422, f"AML_IMPORT_REJECTED: {preview}")
    if outcome == "timeout":
        raise HTTPException(504, f"IMPORT_STATUS_UNKNOWN: AML me check karo, dobara import mat chalao. {preview}")
    return {"status": outcome, "rows_submitted": rows, "message": preview}
