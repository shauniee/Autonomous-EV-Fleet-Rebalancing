"""Uvicorn entrypoint for an isolated HTTP test, never deployed.

Dispatch is the production FastAPI app; only the external OneMap HTTP adapter
is replaced with a deterministic fixture for the offline relocation test.
"""
from ev_fleet.dispatch import routing
from ev_fleet.dispatch.app import create_app
from tests.routing_fixture import fixture_route


async def fixture_onemap_route(client, settings, origin, destination):
    return fixture_route(origin, destination)


routing._onemap_route = fixture_onemap_route
app = create_app()
