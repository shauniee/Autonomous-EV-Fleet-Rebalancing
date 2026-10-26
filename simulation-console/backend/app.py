from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


app = FastAPI(title="EV Simulation Probe", version="0.1.0")
frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"

if (frontend / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index():
    if not (frontend / "index.html").is_file():
        raise HTTPException(503, "Build the console frontend before opening the page.")
    return FileResponse(frontend / "index.html")
