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
        configured = getattr(app.state, "persistence", None)
        repositories = (configured if isinstance(configured, tuple) else
                        (() if configured is None else (configured,)))
        if (request.url.path not in ("/health/live", "/health/ready") and
                any(getattr(repository, "faulted", False) for repository in repositories)):
            # A PostgreSQL commit response may be lost after the write. All
            # normal reads/mutations are denied until a durable reload instead
            # of exposing RAM that might not match the persisted snapshot.
            response = JSONResponse(status_code=503, content={"error": {
                "code": "PERSISTENCE_STATE_UNCERTAIN",
                "request_id": request.state.request_id}})
            response.headers["X-Request-ID"] = request.state.request_id
            return response
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
        configured = getattr(app.state, "persistence", None)
        repositories = configured if isinstance(configured, tuple) else (() if configured is None else (configured,))
        if any(getattr(repository, "faulted", False) for repository in repositories):
            raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN")
        if any(not repository.ready for repository in repositories):
            raise HTTPException(503, "PERSISTENCE_NOT_READY")
        storage = "postgresql" if any(repository.enabled for repository in repositories) else "ephemeral"
        return {"status": "ready", "instance_id": instance_id, "storage": storage}

    return app
