import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from core import require_key, browser_page, aml_login

router = APIRouter(dependencies=[Depends(require_key)])

X = {
    "from":    "xpath=/html/body/div[1]/div/form/div[2]/input",
    "to":      "xpath=/html/body/div[1]/div/form/div[3]/input",
    "status":  "xpath=/html/body/div[1]/div/form/div[5]/select",
    "feature": "xpath=/html/body/div[1]/div/form/div[6]/select",
    "filter":  "xpath=/html/body/div[1]/div/form/div[7]/button[1]",
    "export":  "xpath=/html/body/div[1]/div/form/div[7]/button[4]",
}
JOB_KEEP_SECONDS = 2 * 3600
JOBS = {}
JOBS_LOCK = threading.Lock()


class Req(BaseModel):
    from_date: str   # YYYY-MM-DD
    to_date: str
    aml_user: str | None = None
    aml_pass: str | None = None


def _validate_request(req):
    try:
        datetime.strptime(req.from_date, "%Y-%m-%d")
        datetime.strptime(req.to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date format YYYY-MM-DD hona chahiye")
    if bool(req.aml_user) != bool(req.aml_pass):
        raise HTTPException(400, "aml_user aur aml_pass dono saath bhejo")


def _prune_jobs():
    cutoff = time.time() - JOB_KEEP_SECONDS
    with JOBS_LOCK:
        expired = [
            job_id for job_id, job in JOBS.items()
            if job["status"] in ("done", "error") and job["updated_at"] < cutoff
        ]
        stale_paths = [JOBS.pop(job_id).get("path") for job_id in expired]
    for path in stale_paths:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields, updated_at=time.time())


def _run_export_job(job_id, req):
    tries = 3 if req.aml_user else 8
    try:
        _set_job(job_id, status="queued", stage="Waiting for the AML browser")

        def acquired():
            _set_job(job_id, status="running", stage="Logging in to AML")

        with browser_page(accept_downloads=True, on_acquired=acquired) as page:
            aml_login(page, success_selector=X["from"], username=req.aml_user,
                      password=req.aml_pass, max_tries=tries)
            _set_job(job_id, stage="Preparing report filters")
            page.fill(X["from"], req.from_date)
            page.fill(X["to"], req.to_date)
            page.select_option(X["status"], index=1)
            page.select_option(X["feature"], index=2)
            page.click(X["filter"])
            page.wait_for_load_state("networkidle")

            _set_job(job_id, stage="Downloading AML export")
            with page.expect_download(timeout=60000) as dl:
                page.click(X["export"])
            download = dl.value
            name = os.path.basename(download.suggested_filename or "export.csv")
            path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:8]}_{name}")
            download.save_as(path)
        _set_job(job_id, status="done", stage="Completed", path=path, filename=name, message=None)
    except Exception as exc:
        message = re.sub(r"\s+", " ", str(exc)).strip()[:500] or type(exc).__name__
        if req.aml_pass:
            message = message.replace(req.aml_pass, "[redacted]")
        _set_job(job_id, status="error", stage="Failed", message=message)


@router.post("/export-jobs")
def start_export_job(req: Req):
    _validate_request(req)
    _prune_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued", "stage": "Waiting for the AML browser", "message": None,
            "path": None, "filename": None, "created_at": time.time(), "updated_at": time.time(),
        }
    threading.Thread(target=_run_export_job, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}


@router.get("/export-jobs/{job_id}")
def export_job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Export job not found")
        return {key: job.get(key) for key in ("status", "stage", "message", "created_at", "updated_at")}


@router.get("/export-jobs/{job_id}/download")
def download_export_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Export job not found")
        if job.get("status") != "done" or not job.get("path") or not os.path.isfile(job["path"]):
            raise HTTPException(409, "Export file is not ready")
        path, name = job["path"], job["filename"] or "export.csv"
        job["path"] = None
        job["status"] = "downloaded"
        job["updated_at"] = time.time()
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))


@router.post("/export")
def export(req: Req):
    """Compatibility endpoint for existing integrations; new clients should use export-jobs."""
    _validate_request(req)
    tries = 3 if req.aml_user else 8
    try:
        with browser_page(accept_downloads=True) as page:
            aml_login(page, success_selector=X["from"], username=req.aml_user,
                      password=req.aml_pass, max_tries=tries)
            page.fill(X["from"], req.from_date)
            page.fill(X["to"], req.to_date)
            page.select_option(X["status"], index=1)
            page.select_option(X["feature"], index=2)
            page.click(X["filter"])
            page.wait_for_load_state("networkidle")
            with page.expect_download(timeout=60000) as dl:
                page.click(X["export"])
            download = dl.value
            name = os.path.basename(download.suggested_filename or "export.csv")
            path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:8]}_{name}")
            download.save_as(path)
    except Exception as exc:
        raise HTTPException(500, f"Automation error: {exc}")
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))
