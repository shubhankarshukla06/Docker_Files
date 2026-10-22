"""Report Generation (Generate Report / Extract and Generate) AML par.

Dashboard title, description aur images (PNG) bhejta hai. Yahan ek job me ek hi login, ek hi tab:
har report ke baad Report URL nikaal ke popup band, phir usi tab ko refresh karke agli report.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout
from pydantic import BaseModel

from core import require_key, browser_page, aml_login, log_mem

router = APIRouter(dependencies=[Depends(require_key)])

AML_REPORT_URL = "https://aml-gui-v1.mfilterit.net/reports/generate"
X = {
    "landing":     "xpath=/html/body/div[1]/div/form/div[2]/input",                  # login ke baad Case Desk ka filter form
    "title":       "xpath=/html/body/div[1]/div/form/fieldset[1]/div/div[1]/input",
    "description": "xpath=/html/body/div[1]/div/form/fieldset[1]/div/div[2]/textarea",
    "left_btn":    "xpath=/html/body/div[1]/div/form/fieldset[2]/div/div[1]/div/button",
    "file_inputs": "xpath=/html/body/div[1]/div/form/fieldset[2]//input[@type='file']",
    "add_btn":     "xpath=/html/body/div[1]/div/form/fieldset[2]/button",
    "generate":    "xpath=/html/body/div[1]/div/form/div/button[2]",
    "result_link": "xpath=/html/body/div[1]/div/div/div/div[2]/div[2]/a[2]",
}
MAX_IMAGES_PER_REPORT = 6
MAX_ROWS = 200
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
JOB_KEEP_SECONDS = 2 * 3600
RESULT_WAIT_MS = 90000

JOBS = {}
JOBS_LOCK = threading.Lock()


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


def fresh_form(page):
    """Usi tab ko refresh karke naya khaali form (pichli report ka result/modal hat jaata hai)."""
    page.goto(AML_REPORT_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector(X["title"], state="visible", timeout=45000)


def close_extra_tabs(page):
    for other in list(page.context.pages):
        if other is not page:
            try:
                other.close()
            except PWError:
                pass


def upload_images(page, paths):
    """Images AML ke screenshot fields me page order me. 2 se zyada ho to 'add' button se fields badhao."""
    page.wait_for_selector(X["left_btn"], state="attached", timeout=30000)
    count = page.locator(X["file_inputs"]).count()
    while count < len(paths):
        page.locator(X["add_btn"]).evaluate("el => el.click()")       # JS click (purane code jaisa)
        expected = count + 2
        deadline = time.time() + 20
        while page.locator(X["file_inputs"]).count() < expected:
            if time.time() > deadline:
                raise RuntimeError("AML ne naye screenshot fields nahi dikhaye")
            page.wait_for_timeout(200)
        count = page.locator(X["file_inputs"]).count()
    for index, path in enumerate(paths):
        page.locator(X["file_inputs"]).nth(index).set_input_files(path)


def open_report_and_get_url(page):
    """Result link dabake report ka final URL lo, popup turant band. Main tab wahi rehta hai."""
    link = page.locator(X["result_link"])
    link.wait_for(state="visible", timeout=RESULT_WAIT_MS)
    before_url = page.url
    popup = None
    try:
        with page.context.expect_page(timeout=8000) as pop_info:
            _safe_click(page, X["result_link"])
        popup = pop_info.value
        popup.wait_for_url(lambda u: bool(u) and u != "about:blank", wait_until="commit", timeout=30000)
        return popup.url
    except PWTimeout:
        if popup is None and page.url != before_url:      # link usi tab me khula
            return page.url
        raise RuntimeError("Report link did not open a report page")
    finally:
        if popup is not None:
            try:
                popup.close()
            except PWError:
                pass


def generate_one(page, row, image_paths):
    """Ek report. Report URL return karta hai."""
    close_extra_tabs(page)
    fresh_form(page)
    page.locator(X["title"]).fill(row["title"])
    page.locator(X["description"]).fill(row["description"])
    upload_images(page, image_paths)
    _safe_click(page, X["generate"])
    return open_report_and_get_url(page)


def _run_job(job_id, rows, files_dir, aml_user, aml_pass):
    tries = 3 if aml_user else 8
    try:
        _update(job_id, status="running", stage="Logging in to AML")
        with browser_page() as page:
            aml_login(page, X["landing"], aml_user, aml_pass, max_tries=tries)
            for position, row in enumerate(rows, start=1):
                with JOBS_LOCK:
                    cancelled = JOBS[job_id]["cancel"]
                if cancelled:
                    result = {"index": row["index"], "status": "Skipped", "report_url": "", "message": "Stopped by user"}
                else:
                    _update(job_id, stage=f"Generating report {position} of {len(rows)}")
                    try:
                        paths = [os.path.join(files_dir, name) for name in row["images"]]
                        url = generate_one(page, row, paths)
                        result = {"index": row["index"], "status": "Success", "report_url": url, "message": ""}
                    except Exception as exc:
                        result = {"index": row["index"], "status": "Failed", "report_url": "",
                                  "message": _short(_scrub(exc, aml_pass)) or type(exc).__name__}
                with JOBS_LOCK:
                    job = JOBS[job_id]
                    job["results"].append(result)
                    job["completed"] += 1
                    job["successful"] += result["status"] == "Success"
                    job["failed"] += result["status"] != "Success"
                    job["updated_at"] = time.time()
            log_mem(f"report job {job_id[:8]} done")
        _update(job_id, status="done", stage="Completed")
    except Exception as exc:
        _update(job_id, status="error", stage="Failed",
                message=_short(_scrub(exc, aml_pass), 400) or type(exc).__name__)
        print(f"[REPORTS] job={job_id[:8]} failed: {type(exc).__name__}", flush=True)
    finally:
        shutil.rmtree(files_dir, ignore_errors=True)


class _Row(BaseModel):
    index: int
    title: str
    description: str
    images: list[str]


@router.post("/report-jobs")
def start_report_job(
    manifest: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    aml_user: str | None = Form(None),
    aml_pass: str | None = Form(None),
):
    if bool(aml_user) != bool(aml_pass):
        raise HTTPException(400, "aml_user aur aml_pass dono saath bhejo")
    try:
        data = json.loads(manifest)
        rows = [_Row(**item).model_dump() for item in data["rows"]]
    except Exception:
        raise HTTPException(400, "manifest galat hai (JSON: {\"rows\": [{index,title,description,images}]})")
    if not rows:
        raise HTTPException(400, "Kam se kam ek report row chahiye")
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"Ek job me maximum {MAX_ROWS} rows")

    files_dir = tempfile.mkdtemp(prefix="report_job_")
    try:
        saved, total_bytes = set(), 0
        for upload in files:
            name = os.path.basename(upload.filename or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name) or not name.lower().endswith(".png"):
                raise HTTPException(400, "Sirf simple naam wali .png files allowed hain")
            blob = upload.file.read(MAX_UPLOAD_BYTES + 1)
            total_bytes += len(blob)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Upload {MAX_UPLOAD_BYTES // (1024 * 1024)} MB se zyada hai")
            if blob[:8] != b"\x89PNG\r\n\x1a\n":
                raise HTTPException(400, f"{name} asli PNG nahi hai")
            with open(os.path.join(files_dir, name), "wb") as fh:
                fh.write(blob)
            saved.add(name)
        for row in rows:
            if not row["title"].strip():
                raise HTTPException(400, f"Row {row['index']}: title khaali hai")
            if not 1 <= len(row["images"]) <= MAX_IMAGES_PER_REPORT:
                raise HTTPException(400, f"Row {row['index']}: 1 se {MAX_IMAGES_PER_REPORT} images chahiye")
            missing = [n for n in row["images"] if n not in saved]
            if missing:
                raise HTTPException(400, f"Row {row['index']}: image upload nahi hui: {missing[0]}")

        _prune_jobs()
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            if any(j["status"] in ("queued", "running") for j in JOBS.values()):
                raise HTTPException(409, "Ek report job pehle se chal raha hai, uske khatam hone ka wait karo")
            JOBS[job_id] = {
                "status": "queued", "total": len(rows), "completed": 0, "successful": 0, "failed": 0,
                "stage": "Queued", "results": [], "message": None, "cancel": False, "updated_at": time.time(),
            }
    except Exception:
        shutil.rmtree(files_dir, ignore_errors=True)
        raise
    threading.Thread(target=_run_job, args=(job_id, rows, files_dir, aml_user, aml_pass), daemon=True).start()
    return {"job_id": job_id, "total": len(rows)}


@router.get("/report-jobs/{job_id}")
def report_job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {k: (list(v) if isinstance(v, list) else v) for k, v in job.items() if k != "cancel"}


@router.post("/report-jobs/{job_id}/cancel")
def report_job_cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job["cancel"] = True
    return {"ok": True}