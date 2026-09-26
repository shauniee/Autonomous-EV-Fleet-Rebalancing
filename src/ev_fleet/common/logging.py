import json
import logging
from time import perf_counter
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError


def base_app(name: str) -> FastAPI:
    app = FastAPI(title=name, version="0.1.0")
    instance_id = str(uuid4())
    logger = logging.getLogger("uvicorn.error")

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request.state.request_id = str(uuid4())
        start = perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(json.dumps({"service": name, "request_id": request.state.request_id,
                                "method": request.method, "path": request.url.path,
                                "status": response.status_code,
                                "duration_ms": (perf_counter() - start) * 1000}))
        return response

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {"code": exc.detail, "request_id": request.state.request_id}})

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "request_id": request.state.request_id,
            "details": [{"location": list(item["loc"]), "message": item["msg"]}
                        for item in exc.errors()]}})

    @app.get("/health/live")
    async def live():
        return {"status": "ok", "instance_id": instance_id}

    @app.get("/health/ready")
    async def ready():
        return {"status": "ready", "instance_id": instance_id, "storage": "ephemeral"}

    return app
