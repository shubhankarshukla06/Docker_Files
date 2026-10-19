from fastapi import FastAPI
from tasks import aml_export, aml_import

app = FastAPI()
app.include_router(aml_export.router)
app.include_router(aml_import.router)


@app.get("/health")
def health():
    return {"ok": True}