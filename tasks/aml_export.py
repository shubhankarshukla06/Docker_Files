import os, tempfile, uuid
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


class Req(BaseModel):
    from_date: str   # YYYY-MM-DD
    to_date: str


@router.post("/export")
def export(req: Req):
    try:
        datetime.strptime(req.from_date, "%Y-%m-%d")
        datetime.strptime(req.to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date format YYYY-MM-DD hona chahiye")

    try:
        with browser_page(accept_downloads=True) as page:
            aml_login(page, success_selector=X["from"])

            page.fill(X["from"], req.from_date)
            page.fill(X["to"], req.to_date)
            page.select_option(X["status"], index=1)    # option[2]
            page.select_option(X["feature"], index=2)   # option[3]
            page.click(X["filter"])
            page.wait_for_load_state("networkidle")

            with page.expect_download(timeout=60000) as dl:
                page.click(X["export"])
            download = dl.value

            name = os.path.basename(download.suggested_filename or "export.csv")
            path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:8]}_{name}")
            download.save_as(path)
    except Exception as e:
        raise HTTPException(500, f"Automation error: {e}")

    # bhejne ke baad temp file delete ho jaati hai
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))
