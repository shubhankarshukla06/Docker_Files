from fastapi import FastAPI
from tasks import aml_export, aml_import, aml_case_actions, aml_reports

app = FastAPI()
app.include_router(aml_export.router)
app.include_router(aml_import.router)
app.include_router(aml_case_actions.router)
app.include_router(aml_reports.router)


@app.get("/health")
def health():
    return {"ok": True}