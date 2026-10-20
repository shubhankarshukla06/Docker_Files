import csv, io, os, re, tempfile, time, uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout

from core import require_key, browser_page, aml_login

router = APIRouter(dependencies=[Depends(require_key)])

X = {
    "import":  "xpath=/html/body/div[1]/div/form/div[7]/button[3]",
    "file":    "xpath=/html/body/div[1]/div/div[4]/div/div[2]/input",
    "confirm": "xpath=/html/body/div[1]/div/div[4]/div/div[3]/button[2]",
}

MAX_BYTES = 10 * 1024 * 1024   # 10 MB
POST_LOGIN_PAUSE_MS = 2000     # login ke baad 2 sec ka break, phir turant import
RESULT_TIMEOUT_S = 90          # confirm ke baad UI result ka max wait (sirf worst case)
CLOSED_GRACE_S = 3             # modal band hone ke baad itna ruko, phir "unconfirmed"
TOTAL_RE = re.compile(r"([\d,]+)\s+Total\b", re.I)

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


def _total(text):
    """Dashboard ke '801 Total' counter ki value (agar page par dikhe)."""
    m = TOTAL_RE.search(text or "")
    return int(m.group(1).replace(",", "")) if m else None


def safe_click(page, selector):
    loc = page.locator(selector)
    try:
        loc.click(timeout=15000)
    except PWError:
        loc.dispatch_event("click")      # purane code ka JS-click wala fallback


def _n(rx, text):
    return len(rx.findall(text))


def wait_for_result(page, baseline_text, baseline_alerts, timeout_s=None):
    """success / error / unconfirmed / timeout + page ka chhota preview."""
    deadline = time.time() + (timeout_s or RESULT_TIMEOUT_S)
    closed_at, preview = None, ""
    while time.time() < deadline:
        try:
            text = page.inner_text("body")
            modal_open = page.locator(X["confirm"]).is_visible()   # file input hidden ho sakta hai, button nahi
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
            if time.time() - closed_at > CLOSED_GRACE_S:
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
            page.wait_for_timeout(POST_LOGIN_PAUSE_MS)   # 2 sec break, uske baad bina ruke import

            safe_click(page, X["import"])
            page.wait_for_selector(X["file"], state="attached", timeout=30000)
            page.set_input_files(X["file"], path)
            page.wait_for_selector(X["confirm"], state="visible", timeout=30000)

            # portal alert()/confirm() dikhaye to Playwright use chupchaap dismiss kar deta hai, isliye pakdo
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

            baseline_text = page.inner_text("body")
            baseline_alerts = page.locator(".alert-danger").count()
            total_before = _total(baseline_text)

            # confirm click ke baad portal ka apna HTTP response pakdo (sabse bharosemand signal)
            http_status, http_body = None, ""
            try:
                with page.expect_response(
                    lambda r: r.request.method in ("POST", "PUT", "PATCH"), timeout=30000
                ) as resp_info:
                    safe_click(page, X["confirm"])
                resp = resp_info.value
                http_status = resp.status
                try:
                    http_body = re.sub(r"\s+", " ", resp.text())[:400]
                except PWError:
                    http_body = ""
            except PWTimeout:
                http_status = None       # click ke baad koi POST request gayi hi nahi

            outcome, preview = wait_for_result(page, baseline_text, baseline_alerts)
            try:
                total_after = _total(page.inner_text("body"))
            except PWError:
                total_after = None
    except Exception as e:
        raise HTTPException(500, f"Automation error: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    # UI se result na mila ho to HTTP response / dialog se faisla
    signals = " ".join(dialogs + [http_body])
    if outcome in ("unconfirmed", "timeout"):
        if ERROR_RE.search(signals) or (http_status is not None and http_status >= 400):
            outcome = "error"
        elif SUCCESS_RE.search(signals):
            outcome = "success"
        # NOTE: 'Total' counter sirf diagnostics ke liye hai. Doosre users bhi ek saath cases file karte hain,
        # to counter badhna hamare import ka saboot nahi hai.

    diag = (f"http={http_status} body={http_body[:150]!r} dialogs={dialogs} "
            f"total {total_before}->{total_after}")
    print(f"[IMPORT] outcome={outcome} rows={rows} {diag}", flush=True)
    message = f"{preview[:250]} || {diag}"

    if outcome == "error":
        raise HTTPException(422, f"AML_IMPORT_REJECTED: {message}")
    if outcome == "timeout":
        raise HTTPException(504, f"IMPORT_STATUS_UNKNOWN: AML me check karo, dobara import mat chalao. {message}")
    return {
        "status": outcome, "rows_submitted": rows, "message": message,
        "diagnostics": {"http_status": http_status, "http_body": http_body, "dialogs": dialogs,
                        "total_before": total_before, "total_after": total_after},
    }