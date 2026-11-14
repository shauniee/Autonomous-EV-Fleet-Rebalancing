import asyncio
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID
import httpx
from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from ev_fleet.common.logging import base_app
from ev_fleet.contracts.common import Position
from .routing import road_match_spawn, routing_configuration
from ev_fleet.contracts.crds import CrdsRouteRequest, CrdsRouteEstimate
from ev_fleet.contracts.dispatch import DispatchApproval, CommandAcknowledgement, DispatchEvent, Dispatch, DispatchList, CommandList, BatchApproval, BatchApprovalLookup, BatchDispatch
from ev_fleet.contracts.dispatch import BatchApprovalGroup, BatchApprovalGroupPage, BatchApprovalGroupRequest
from .service import DispatchService
from .approval_evidence import ApprovalCheckError
from .recovery import assess_recovery
from .resolution import assess_resolution, reconcile_relocation, request_end, stop_intent
from ev_fleet.contracts.recovery import (RelocationEndRequest, RelocationResolutionAssessment,
    RelocationResolutionOperation, SimulatorStopRequest)
from ev_fleet.contracts.recovery import RecoveryAssessment, RecoveryRequest, RecoveryHandoffRequest, SimulatorSession
from ev_fleet.contracts.dispatch import DispatchApprovalResult, RecoveryOperation


def create_app(settings=None, client=None, clock=None, background=True):
    app = base_app("Dispatch Service")

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=2) as owned:
            service = DispatchService(client or owned, settings, clock)
            app.state.service = service
            app.state.persistence = service.repository
            await service.start()
            worker = asyncio.create_task(service.reconcile_loop()) if background else None
            queue_worker = asyncio.create_task(service.queue_loop()) if background else None
            group_worker = (asyncio.create_task(service.batch_groups.run())
                            if background and service.repository.enabled else None)
            app.state.batch_group_worker = group_worker
            try:
                yield
            finally:
                workers = [task for task in (worker, queue_worker, group_worker) if task is not None]
                for task in workers:
                    task.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                await service.close()

    app.router.lifespan_context = lifespan

    @app.get("/api/v1/routing/status")
    async def routing_status():
        # Read-only setup signal for operators. A configured token is not proof
        # of provider reachability; actual road routes are checked per request.
        return routing_configuration(app.state.service.routing)

    @app.post("/api/v1/road-matches")
    async def match_idle_spawn(position: Position):
        # The simulator calls this before its first telemetry/session, never
        # while an EV is executing a dispatch. Does not change Fleet state.
        service = app.state.service
        return await road_match_spawn(service.client, service.routing, position.model_dump())

    @app.post("/api/v1/dispatches", status_code=202, response_model=DispatchApprovalResult)
    async def approve(body: DispatchApproval, request: Request):
        service = app.state.service
        try:
            await service.approve(body)
        except ApprovalCheckError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": {
                "code": exc.detail, "request_id": request.state.request_id,
                "approval_decision": exc.decision.model_dump(mode="json", exclude_none=True),
            }})
        return service.approval_view(body.request_id)

    @app.get("/api/v1/approvals/{request_id}", response_model=DispatchApprovalResult)
    async def approval(request_id: UUID):
        return app.state.service.approval_view(request_id)

    @app.post("/api/v1/approvals/{request_id}/close", response_model=DispatchApprovalResult)
    async def close_approval(request_id: UUID):
        return await app.state.service.close_unstarted(request_id)

    @app.post("/api/v1/batches/{request_id}/items/{recommendation_id}/close", response_model=BatchDispatch)
    async def close_batch_item(request_id: UUID, recommendation_id: UUID):
        return await app.state.service.close_batch_item(request_id, recommendation_id)

    @app.post("/api/v1/batches", status_code=202, response_model=BatchDispatch)
    async def approve_batch(body: BatchApproval):
        return await app.state.service.enqueue_batch(body)

    @app.get("/api/v1/batches/{request_id}/items/{recommendation_id}/intent")
    async def approved_batch_intent(request_id: UUID, recommendation_id: UUID):
        return await app.state.service.batch_intent(request_id, recommendation_id)

    @app.get("/api/v1/batches", response_model=BatchApprovalLookup)
    async def lookup_batches(request_id: Annotated[list[UUID], Query(min_length=1, max_length=31)]):
        return app.state.service.lookup_batch_approvals(request_id)

    @app.get("/api/v1/batches/{request_id}", response_model=BatchDispatch)
    async def batch(request_id: UUID):
        return await app.state.service.get_batch(request_id)

    @app.post("/api/v1/batch-approval-groups", status_code=202, response_model=BatchApprovalGroup)
    async def submit_batch_group(body: BatchApprovalGroupRequest):
        worker = app.state.batch_group_worker
        if not app.state.service.repository.enabled:
            raise HTTPException(503, "BATCH_APPROVAL_QUEUE_DISABLED")
        if worker is None or worker.done():
            raise HTTPException(503, "BATCH_APPROVAL_QUEUE_UNAVAILABLE")
        return await app.state.service.batch_groups.submit(body)

    @app.get("/api/v1/batch-approval-groups", response_model=BatchApprovalGroupPage)
    async def list_batch_groups(offset: int = Query(default=0, ge=0),
                                limit: int = Query(default=30, ge=1, le=100), unresolved: bool = False):
        return app.state.service.batch_groups.page(offset, limit, unresolved=unresolved)

    @app.get("/api/v1/batch-approval-groups/{group_id}", response_model=BatchApprovalGroup)
    async def batch_group(group_id: UUID):
        return app.state.service.batch_groups.detail(group_id)

    @app.get("/api/v1/dispatches", response_model=DispatchList)
    async def dispatches(vehicle_id: str | None = None):
        service = app.state.service
        return {"dispatches": [record.view() for record in service.store.records.values()
                               if vehicle_id is None or record.data["vehicle_id"] == vehicle_id]}

    @app.get("/api/v1/dispatch-planning-state")
    async def planning_state():
        return await app.state.service.planning_snapshot()

    @app.post("/api/v1/route-estimates", response_model=CrdsRouteEstimate)
    async def route_estimates(body: CrdsRouteRequest):
        return await app.state.service.estimate_route(body)

    @app.get("/api/v1/dispatches/{dispatch_id}/traffic-check")
    async def traffic_check(dispatch_id: UUID):
        return await app.state.service.traffic_check(dispatch_id)

    @app.get("/api/v1/dispatches/{dispatch_id}/resolution", response_model=RelocationResolutionAssessment)
    async def resolution_assessment(dispatch_id: UUID):
        return await assess_resolution(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/resolution", status_code=202,
              response_model=RelocationResolutionOperation)
    async def end_relocation(dispatch_id: UUID, body: RelocationEndRequest):
        return await request_end(app.state.service, dispatch_id, body)

    @app.get("/api/v1/dispatches/{dispatch_id}/resolution/intent", response_model=SimulatorStopRequest)
    async def resolution_intent(dispatch_id: UUID):
        return await stop_intent(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/reconcile", response_model=Dispatch)
    async def reconcile_dispatch(dispatch_id: UUID):
        service = app.state.service
        record = service.store.get(dispatch_id)
        async with record.lock:
            service.recovery.ensure_ready()
            try:
                await reconcile_relocation(service, record)
            finally:
                await service.persist()
            return record.view()

    @app.get("/api/v1/dispatches/{dispatch_id}/recovery", response_model=RecoveryAssessment)
    async def recovery_assessment(dispatch_id: UUID):
        return await assess_recovery(app.state.service, dispatch_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/recovery", status_code=202, response_model=RecoveryOperation)
    async def recover(dispatch_id: UUID, body: RecoveryRequest):
        return await app.state.service.recovery.request(dispatch_id, body)

    @app.post("/api/v1/dispatches/{dispatch_id}/recovery/handoff", response_model=SimulatorSession)
    async def recovery_handoff(dispatch_id: UUID, body: RecoveryHandoffRequest):
        return await app.state.service.recovery.handoff(dispatch_id, body)

    @app.get("/api/v1/dispatches/{dispatch_id}", response_model=Dispatch)
    async def dispatch(dispatch_id: UUID):
        return await app.state.service.get(dispatch_id)

    @app.get("/api/v1/vehicles/{vehicle_id}/commands", response_model=CommandList)
    async def commands(vehicle_id: str):
        return await app.state.service.commands(vehicle_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/acknowledgements", response_model=Dispatch)
    async def acknowledge(dispatch_id: UUID, body: CommandAcknowledgement,
                          simulator_generation: int | None = Header(default=None, alias="X-Simulator-Generation"),
                          simulator_session_id: UUID | None = Header(default=None, alias="X-Simulator-Session")):
        return await app.state.service.acknowledge(dispatch_id, body,
            simulator_generation=simulator_generation, simulator_session_id=simulator_session_id)

    @app.post("/api/v1/dispatches/{dispatch_id}/events", response_model=Dispatch)
    async def event(dispatch_id: UUID, body: DispatchEvent,
                    simulator_generation: int | None = Header(default=None, alias="X-Simulator-Generation"),
                    simulator_session_id: UUID | None = Header(default=None, alias="X-Simulator-Session")):
        return await app.state.service.event(dispatch_id, body,
            simulator_generation=simulator_generation, simulator_session_id=simulator_session_id)

    return app


app = create_app()
