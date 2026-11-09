"""Stateless evaluator: no Fleet, database, provider credentials or commands."""
from fastapi import HTTPException

from ev_fleet.common.logging import base_app
from ev_fleet.contracts.crds import CrdsEvaluationTask, CrdsTaskResult
from .crds import evaluate_moves
from .crds_execution import task_fingerprint


def create_app():
    app = base_app("CRDS Evaluator")

    @app.post("/api/v1/crds/evaluate", response_model=CrdsTaskResult)
    def evaluate(task: CrdsEvaluationTask):
        if task.input_fingerprint != task_fingerprint(task):
            raise HTTPException(422, "CRDS_INPUT_FINGERPRINT_MISMATCH")
        try:
            result = evaluate_moves(task.futures, task.moves, task.parameters)
        except ValueError:
            raise HTTPException(422, "CRDS_INVALID_INPUTS") from None
        return CrdsTaskResult(epoch_id=task.epoch_id, model_version=task.model_version,
            input_fingerprint=task.input_fingerprint, future_ids=[f.future_id for f in task.futures],
            evaluation=result)

    return app


app = create_app()
