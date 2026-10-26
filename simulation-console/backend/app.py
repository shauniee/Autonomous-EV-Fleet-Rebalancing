from fastapi import FastAPI


app = FastAPI(title="EV Simulation Probe", version="0.1.0")


@app.get("/health/live")
async def live():
    return {"status": "ok"}
