from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .demand import router as demand_router
from .observations import lifespan, router
from .rentals import router as rental_router
from .traffic import router as traffic_router

app = FastAPI(title="EV Simulation Probe", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(demand_router)
app.include_router(rental_router)
app.include_router(traffic_router)
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
