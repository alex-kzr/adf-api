"""Diagnostic endpoints only; no factory execution API is implemented."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="ADF API foundation", version="0.0.1")

@app.get("/healthz")
def health() -> dict[str, str]:
    return {"service": "adf-api", "status": "alive", "implementation": "scaffold"}

@app.get("/readyz", status_code=503)
def ready() -> JSONResponse:
    return JSONResponse(status_code=503, content={"code": "SCAFFOLD_NOT_IMPLEMENTED", "message": "Factory API and persistence are not implemented.", "details": {}, "request_id": "diagnostic"})
