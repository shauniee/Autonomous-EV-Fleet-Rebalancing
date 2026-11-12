"""Persist and execute explicitly approved groups through normal batch admission."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from fastapi import HTTPException

from ev_fleet.contracts.dispatch import (
    BatchApproval,
    BatchApprovalGroup,
    BatchApprovalGroupConflict,
    BatchApprovalGroupPage,
    BatchApprovalGroupPlan,
    BatchApprovalGroupRequest,
    BatchDispatch,
    queued_batch_approval_id,
)
from ev_fleet.contracts.recommendations import BatchRecommendation

if TYPE_CHECKING:
    from .service import DispatchService

ACTIVE = ("QUEUED", "RUNNING")
DEFINITIVE_ERRORS = (404, 409, 422)
QUEUE_CAPACITY = 20
HISTORY_LIMIT = 500


class GroupRecord(TypedDict):
    group: BatchApprovalGroup
    preflight_done: bool


class BatchApprovalGroups:
    def __init__(self, service: DispatchService):
        self.service = service
        self.records: dict[str, GroupRecord] = {}
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()

    def approval(self, plan: BatchApprovalGroupPlan) -> BatchDispatch | None:
        data = self.service.batches.get(str(plan.request_id))
        if data is None or data["batch_recommendation_id"] != str(plan.batch_recommendation_id):
            return None
        return BatchDispatch.model_validate(self.service.batch_view(data))

    def view(self, group: BatchApprovalGroup) -> BatchApprovalGroup:
        plans = []
        for plan in group.plans:
            admitted = plan.status in ("RUNNING", "UNKNOWN", "APPROVED", "PARTIAL") or (
                plan.status == "REJECTED" and plan.reason_code is None)
            approval = self.approval(plan) if admitted else None
            if approval:
                status = plan.status if plan.status == "RUNNING" and approval.status == "UNKNOWN" else approval.status
                plan = plan.model_copy(update={
                    "approval": approval,
                    "status": status,
                    "reason_code": plan.reason_code if approval.status == "UNKNOWN" else None,
                })
            plans.append(plan)
        status = group.status
        if status == "COMPLETED" and any(plan.status == "UNKNOWN" for plan in plans):
            status = "UNKNOWN"
        return group.model_copy(update={"plans": plans, "status": status})

    def committed(self) -> dict[str, GroupRecord]:
        snapshot = self.service.persisted_snapshot or {}
        return snapshot.get("batch_approval_groups", {})

    def detail(self, group_id: UUID) -> BatchApprovalGroup:
        record = self.committed().get(str(group_id))
        if record is None:
            raise HTTPException(404, "BATCH_APPROVAL_GROUP_NOT_FOUND")
        return self.view(record["group"])

    def page(self, offset: int, limit: int, *, unresolved: bool) -> BatchApprovalGroupPage:
        groups = [record["group"] for record in reversed(self.committed().values())]
        if unresolved:
            groups = [group for group in groups if self.unresolved(group)]
        end = offset + limit
        return BatchApprovalGroupPage(
            enabled=self.service.repository.enabled,
            groups=[self.view(group) for group in groups[offset:end]],
            next_offset=end if end < len(groups) else None,
        )

    def unresolved(self, group: BatchApprovalGroup) -> bool:
        if group.status in (*ACTIVE, "UNKNOWN"):
            return True
        return group.status == "COMPLETED" and any(
            plan.status == "UNKNOWN" and ((approval := self.approval(plan)) is None
                                          or approval.status == "UNKNOWN")
            for plan in group.plans
        )

    def prune_history(self):
        cutoff = self.service.clock.now() - timedelta(days=7)
        terminal = [key for key, record in self.records.items()
                    if not self.unresolved(record["group"])]
        removable = set(terminal[:max(0, len(self.records) - HISTORY_LIMIT + 1)])
        removable.update(key for key in terminal if self.records[key]["group"].updated_at <= cutoff)
        for key in removable:
            del self.records[key]

    async def submit(self, body: BatchApprovalGroupRequest) -> BatchApprovalGroup:
        if not self.service.repository.enabled:
            raise HTTPException(503, "BATCH_APPROVAL_QUEUE_DISABLED")
        key = str(body.group_id)
        async with self.lock:
            record = self.records.get(key)
            if record:
                group = record["group"]
                if [plan.batch_recommendation_id for plan in group.plans] != body.batch_recommendation_ids:
                    raise HTTPException(409, "BATCH_APPROVAL_GROUP_ID_CONFLICT")
                view = self.view(group)
                if view.status != "UNKNOWN":
                    return view
            if sum(record["group"].status in ACTIVE for record in self.records.values()) >= QUEUE_CAPACITY:
                raise HTTPException(429, "BATCH_APPROVAL_QUEUE_FULL")
            now = self.service.clock.now()
            if record:
                # Keep completed plans and only retry outcomes still uncertain.
                plans = [plan.model_copy(update={"status": "QUEUED", "approval": None, "reason_code": None})
                         if plan.status == "UNKNOWN" else plan.model_copy(update={"approval": None})
                         for plan in view.plans]
                group = group.model_copy(update={
                    "status": "QUEUED", "reason_code": None, "plans": plans, "updated_at": now,
                })
                preflight_done = record["preflight_done"]
            else:
                self.prune_history()
                if len(self.records) >= HISTORY_LIMIT:
                    raise HTTPException(429, "BATCH_APPROVAL_HISTORY_FULL")
                group = BatchApprovalGroup(group_id=body.group_id, status="QUEUED", created_at=now,
                    updated_at=now, plans=[BatchApprovalGroupPlan(batch_recommendation_id=batch_id,
                        request_id=queued_batch_approval_id(batch_id), status="QUEUED")
                        for batch_id in body.batch_recommendation_ids])
                preflight_done = False
            self.records[key] = {"group": group, "preflight_done": preflight_done}
            await self.service.persist()
            self.wake.set()
            return self.view(group)

    async def checkpoint(self, group: BatchApprovalGroup, preflight_done: bool, **updates) -> BatchApprovalGroup:
        async with self.lock:
            group = group.model_copy(update={**updates, "updated_at": self.service.clock.now()})
            self.records[str(group.group_id)] = {"group": group, "preflight_done": preflight_done}
            await self.service.persist()
            return group

    async def preflight(self, group: BatchApprovalGroup) -> BatchApprovalGroup:
        previews: dict[UUID, BatchRecommendation] = {}
        owners: dict[str, list[UUID]] = {}
        plans = []
        for plan in group.plans:
            known_refusal = self.service.batch_approval_refusals.get(str(plan.request_id))
            if known_refusal:
                plans.append(plan.model_copy(update={"status": "REJECTED", "reason_code": known_refusal["reason_code"]}))
                continue
            existing = self.approval(plan)
            if existing:
                if existing.status == "UNKNOWN":
                    for vehicle_id in {item.vehicle_id for item in existing.items if item.approval_status != "REJECTED"}:
                        owners.setdefault(vehicle_id, []).append(plan.batch_recommendation_id)
                plans.append(plan.model_copy(update={"status": existing.status if existing.status != "UNKNOWN" else "QUEUED"}))
                continue
            if str(plan.batch_recommendation_id) in self.service.batch_recommendations:
                plans.append(plan.model_copy(update={"status": "REJECTED", "reason_code": "BATCH_ALREADY_APPROVED"}))
                continue
            try:
                previews[plan.batch_recommendation_id] = await self.service.load_batch_preview(
                    plan.batch_recommendation_id, queued=True)
            except HTTPException as exc:
                if exc.status_code not in DEFINITIVE_ERRORS:
                    # A missing preview could hide an overlap. Check the entire selection again on retry.
                    return await self.checkpoint(group, False, status="UNKNOWN", reason_code=str(exc.detail))
                plan = plan.model_copy(update={"status": "REJECTED", "reason_code": str(exc.detail)})
            plans.append(plan)
        now = self.service.clock.now()
        for index, plan in enumerate(plans):
            preview = previews.get(plan.batch_recommendation_id)
            if preview is None:
                continue
            if preview.expires_at <= now:
                plans[index] = plan.model_copy(update={"status": "REJECTED", "reason_code": "BATCH_EXPIRED"})
                continue
            for vehicle_id in {item.vehicle_id for item in preview.assignments}:
                owners.setdefault(vehicle_id, []).append(plan.batch_recommendation_id)
        conflicts = [BatchApprovalGroupConflict(vehicle_id=vehicle_id, batch_recommendation_ids=ids)
                     for vehicle_id, ids in sorted(owners.items()) if len(ids) > 1]
        if conflicts:
            plans = [plan.model_copy(update={"status": "SKIPPED", "reason_code": "BATCH_APPROVAL_GROUP_CONFLICT"})
                     if plan.status == "QUEUED" else plan for plan in plans]
            return await self.checkpoint(group, False, status="CONFLICT", plans=plans, conflicts=conflicts,
                                         reason_code="BATCH_APPROVAL_GROUP_CONFLICT")
        return await self.checkpoint(group, True, plans=plans)

    async def process(self, record: GroupRecord):
        group = await self.checkpoint(record["group"], record["preflight_done"], status="RUNNING")
        if not record["preflight_done"]:
            group = await self.preflight(group)
            if group.status != "RUNNING":
                return
        for index, plan in enumerate(group.plans):
            if plan.status not in ACTIVE:
                continue
            plans = list(group.plans)
            plans[index] = plan.model_copy(update={"status": "RUNNING"})
            group = await self.checkpoint(group, True, plans=plans)
            try:
                result = await self.service.approve_batch(BatchApproval(
                    request_id=plan.request_id, batch_recommendation_id=plan.batch_recommendation_id))
                updated = plan.model_copy(update={"status": result["status"], "reason_code": None})
            except HTTPException as exc:
                updated = plan.model_copy(update={
                    "status": "REJECTED" if exc.status_code in DEFINITIVE_ERRORS else "UNKNOWN",
                    "reason_code": str(exc.detail),
                })
            group = await self.checkpoint(group, True, plans=[*plans[:index], updated, *plans[index + 1:]])
        await self.checkpoint(group, True, status="COMPLETED")

    async def run(self):
        while True:
            async with self.lock:
                record = next((item for item in self.records.values() if item["group"].status in ACTIVE), None)
                if record is None:
                    self.wake.clear()
            if record is None:
                await self.wake.wait()
                continue
            try:
                await self.process(record)
            except Exception:
                logging.exception("Batch approval group interrupted: %s", record["group"].group_id)
                if not self.service.repository.ready:
                    raise
                # Leave the durable checkpoint in place. Retry through the same child identities.
                await asyncio.sleep(1)
