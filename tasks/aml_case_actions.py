"""GUI QC: AML cases ko bulk me Approve / Reject (remark ke saath).

Flow (har job me ek hi login): AML login -> har case ke liye /cases/<id> kholo ->
(reject ho to action + remark select karo) -> submit dabao -> AML ka HTTP response dekho.
"""
import re
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout
from pydantic import BaseModel

from core import require_key, browser_page, aml_login, AML_URL, log_mem

router = APIRouter(dependencies=[Depends(require_key)])

X = {
    "landing":       "xpath=/html/body/div[1]/div/form/div[2]/input",    # login ke baad Case Desk ka filter form
    "action_select": "xpath=/html/body/div[1]/div/form/div/div[1]/div/div[7]/div[1]/select",
    "remark_select": "xpath=/html/body/div[1]/div/form/div/div[1]/div/div[7]/div[2]/select",
    "submit":        "xpath=/html/body/div/div/form/div/div[2]/div[2]/button",
}
REJECT_OPTION_INDEX = 2          # action select ka option[3] (1-based) = Reject
MAX_IDS = 500
MAX_REMARK_INDEX = 60
MAX_CONSECUTIVE_FAILS = 5        # itne cases lagataar fail hon to job rok do (session expire/UI badla ho sakta hai)
SUBMIT_RESPONSE_WAIT_MS = 15000
JOB_KEEP_SECONDS = 2 * 3600

JOBS = {}
JOBS_LOCK = threading.Lock()


class StartReq(BaseModel):
    case_ids: list[str]
    action: str                      # "approved" | "rejected"
    remark_index: int | None = None  # remark option ki position (1-based, XPath option[N] jaisi); sirf rejected ke liye
    aml_user: str | None = None      # user ka apna AML login (na do to service ka default login)
    aml_pass: str | None = None


def _short(text, limit=200):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _scrub(text, secret):
    text = str(text or "")
    return text.replace(secret, "[redacted]") if secret else text


def _update(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields, updated_at=time.time())


def _snapshot(job):
    return {k: (list(v) if isinstance(v, list) else v) for k, v in job.items() if k != "cancel"}


def _prune_jobs():
    cutoff = time.time() - JOB_KEEP_SECONDS
    with JOBS_LOCK:
        for jid in [j for j, job in JOBS.items() if job["status"] in ("done", "error") and job["updated_at"] < cutoff]:
            JOBS.pop(jid, None)


def _safe_click(page, selector):
    loc = page.locator(selector)
    try:
        loc.click(timeout=15000)
    except PWError:
        loc.dispatch_event("click")       # purane code ka JS-click wala fallback


def process_case(page, case_id, action, remark_index):
    """Ek case par action. (status, message) return karta hai: Success / Failed / Unconfirmed."""
    page.goto(f"{AML_URL.rstrip('/')}/cases/{case_id}", wait_until="domcontentloaded", timeout=40000)
    if action == "rejected":
        page.wait_for_selector(X["action_select"], state="visible", timeout=40000)
        page.locator(X["action_select"]).select_option(index=REJECT_OPTION_INDEX)       # input+change events fire hote hain
        page.wait_for_selector(X["remark_select"], state="visible", timeout=15000)
        page.locator(X["remark_select"]).select_option(index=remark_index - 1)
    page.wait_for_selector(X["submit"], state="visible", timeout=40000)

    clicked, http_status = False, None
    try:
        with page.expect_response(
            lambda r: r.request.method in ("POST", "PUT", "PATCH"), timeout=SUBMIT_RESPONSE_WAIT_MS
        ) as resp_info:
            _safe_click(page, X["submit"])
            clicked = True
        http_status = resp_info.value.status
    except PWTimeout:
        if not clicked:
            raise                          # click hi nahi hua: asli error
        http_status = None                 # click hua par AML ko koi request gayi nahi
    page.wait_for_timeout(750)

    if http_status is None:
        return "Unconfirmed", "Submit clicked, but AML received no request. Check this case in AML."
    if http_status >= 400:
        return "Failed", f"AML returned HTTP {http_status}"
    return "Success", f"Submitted (HTTP {http_status})"


def _run_job(job_id, case_ids, action, remark_index, aml_user, aml_pass):
    tries = 3 if aml_user else 8           # user ke apne account pe kam retries (lock ka risk)
    try:
        _update(job_id, status="running")
        with browser_page() as page:
            aml_login(page, X["landing"], aml_user, aml_pass, max_tries=tries)
            consecutive = 0
            stopped_reason = None
            for case_id in case_ids:
                with JOBS_LOCK:
                    cancelled = JOBS[job_id]["cancel"]
                if cancelled:
                    stopped_reason = "Stopped by user"
                elif consecutive >= MAX_CONSECUTIVE_FAILS:
                    stopped_reason = f"Stopped after {MAX_CONSECUTIVE_FAILS} consecutive failures"
                if stopped_reason:
                    status, message = "Skipped", stopped_reason
                else:
                    try:
                        status, message = process_case(page, case_id, action, remark_index)
                    except Exception as exc:
                        status, message = "Failed", _short(_scrub(exc, aml_pass)) or type(exc).__name__
                    consecutive = 0 if status == "Success" else consecutive + 1
                with JOBS_LOCK:
                    job = JOBS[job_id]
                    job["results"].append({"case_id": case_id, "status": status, "message": message})
                    job["completed"] += 1
                    job["successful"] += status == "Success"
                    job["failed"] += status != "Success"
                    job["updated_at"] = time.time()
            log_mem(f"case-actions job {job_id[:8]} done")
        _update(job_id, status="done", message=stopped_reason)
    except Exception as exc:
        _update(job_id, status="error", message=_short(_scrub(exc, aml_pass), 400) or type(exc).__name__)
        print(f"[CASE ACTIONS] job={job_id[:8]} failed: {type(exc).__name__}", flush=True)


@router.post("/case-actions")
def start_case_actions(req: StartReq):
    action = (req.action or "").strip().lower()
    if action not in ("approved", "rejected"):
        raise HTTPException(400, "action approved ya rejected hona chahiye")
    ids, seen = [], set()
    for raw in req.case_ids:
        case_id = str(raw).strip()
        if not re.fullmatch(r"\d+", case_id):
            raise HTTPException(400, f"Invalid Case ID: {case_id[:30]}")
        if case_id not in seen:
            seen.add(case_id)
            ids.append(case_id)
    if not ids:
        raise HTTPException(400, "Kam se kam ek Case ID chahiye")
    if len(ids) > MAX_IDS:
        raise HTTPException(400, f"Ek run me maximum {MAX_IDS} Case IDs")
    if action == "rejected" and not (req.remark_index and 1 <= req.remark_index <= MAX_REMARK_INDEX):
        raise HTTPException(400, "Rejected ke liye valid remark_index chahiye")
    if bool(req.aml_user) != bool(req.aml_pass):
        raise HTTPException(400, "aml_user aur aml_pass dono saath bhejo")

    _prune_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        if any(j["status"] in ("queued", "running") for j in JOBS.values()):
            raise HTTPException(409, "Ek case-action job pehle se chal raha hai, uske khatam hone ka wait karo")
        JOBS[job_id] = {
            "status": "queued", "total": len(ids), "completed": 0, "successful": 0, "failed": 0,
            "action": action, "results": [], "message": None, "cancel": False, "updated_at": time.time(),
        }
    threading.Thread(
        target=_run_job,
        args=(job_id, ids, action, req.remark_index if action == "rejected" else None, req.aml_user, req.aml_pass),
        daemon=True,
    ).start()
    return {"job_id": job_id, "total": len(ids)}


@router.get("/case-actions/{job_id}")
def case_actions_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return _snapshot(job)


@router.post("/case-actions/{job_id}/cancel")
def case_actions_cancel(job_id: str):
    """Bacha hua queue skip kar deta hai (jo case ho chuke wo wapas nahi hote)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job["cancel"] = True
    return {"ok": True}