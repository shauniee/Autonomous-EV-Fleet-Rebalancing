"""Uvicorn entrypoint for an isolated HTTP test, never deployed.

Dispatch is the production FastAPI app; only the external OneMap HTTP adapter
is replaced with a deterministic fixture for the offline relocation test.
"""
from ev_fleet.dispatch import service
from ev_fleet.dispatch.app import create_app
from tests.routing_fixture import fixture_driving_route

service.driving_route = fixture_driving_route
app = create_app()
