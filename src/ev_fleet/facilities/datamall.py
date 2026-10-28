"""Read-only LTA DataMall adapter. No real parking or charging reservations.

Reference: API User Guide v6.10, sections 1, 2.12, 2.28 and 2.29.
Carpark totals and carpark-to-charger relationships are not supplied by these APIs.
"""
import asyncio
from datetime import timedelta
import os
import re
import logging

import httpx
from pydantic import BaseModel, Field, SecretStr

from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.common import Position
from ev_fleet.contracts.facilities import Facility
from ev_fleet.demand.store import SINGAPORE_ZONES

BASE_URL = "https://datamall2.mytransport.sg/ltaodataservice"


class DataMallError(Exception):
    """Only fixed, non-sensitive error codes may reach logs or API responses."""


class FacilitiesSettings(BaseModel):
    source: str = "MOCK"
    account_key: SecretStr = SecretStr("")
    parking_refresh_seconds: int = Field(default=60, ge=60)
    charging_refresh_seconds: int = Field(default=300, ge=300)
    parking_max_age_seconds: int = Field(default=180, ge=60)
    charging_max_age_seconds: int = Field(default=900, ge=300)

    @classmethod
    def from_env(cls):
        config = cls(
            source=os.getenv("FACILITIES_SOURCE", "MOCK").upper(),
            account_key=os.getenv("LTA_DATAMALL_ACCOUNT_KEY", ""),
            **{name: os.getenv("LTA_" + name.upper(), str(field.default))
               for name, field in cls.model_fields.items()
               if name not in ("source", "account_key")})
        if config.source not in ("MOCK", "LTA_DATAMALL"):
            raise ValueError("FACILITIES_SOURCE must be MOCK or LTA_DATAMALL")
        return config


def rows(payload):
    records = payload.get("value") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise DataMallError("LTA_INVALID_RESPONSE")
    return records


def count(value):
    # Reject fractional, negative and boolean counts rather than invent capacity.
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError("invalid count")
    return int(value)


def zone_for(position):
    return min(SINGAPORE_ZONES, key=lambda zone: (
        distance_km(position.model_dump(), {"latitude": zone[2], "longitude": zone[3]}),
        zone[0]))[0]


def parking_sites(records, observed_at, max_age):
    result = {}
    for row in records:
        if row.get("LotType") != "C":
            continue
        try:
            lat, lng = row["Location"].split()
            position = Position(latitude=float(lat), longitude=float(lng))
            agency = str(row["Agency"]).strip()
            identifier = str(row["CarParkID"]).strip()
            if not agency or not identifier:
                raise ValueError("missing identity")
            available = count(row["AvailableLots"])
            site = Facility(
                facility_id=f"lta-parking-{agency}-{identifier}",
                name=str(row.get("Development") or identifier),
                zone_id=zone_for(position), position=position,
                parking_available=available, charging_available=0,
                status="OPEN", source="LTA_DATAMALL", agency=agency,
                availability_observed_at=observed_at,
                availability_expires_at=observed_at + timedelta(seconds=max_age),
                reported_parking_available=available)
            if site.facility_id in result:
                raise DataMallError("LTA_DUPLICATE_IDENTITY")
            result[site.facility_id] = site
        except (KeyError, TypeError, ValueError, AttributeError):
            # Missing/invalid coordinates are documented possibilities. Never guess a position.
            continue
    if records and not result:
        raise DataMallError("LTA_NO_USABLE_PARKING")
    return list(result.values())


def charging_sites(records, observed_at, max_age):
    result = {}
    for row in records:
        try:
            # "longtitude" is the spelling in LTA's documented schema.
            position = Position(latitude=float(row["latitude"]),
                                longitude=float(row["longtitude"]))
            identifier = str(row["locationId"]).strip()
            if not identifier:
                raise ValueError("missing identity")
            chargers = row["chargingPoints"]
            if not isinstance(chargers, list):
                raise ValueError("invalid chargers")
            units = {}
            connectors = set()
            for charger in chargers:
                charger_id = str(charger["id"]).strip()
                if not charger_id or charger_id in units:
                    raise ValueError("invalid charger identity")
                states = {}
                # Some responses group evIds under plugTypes; retain a single
                # connector identity when it appears in more than one group.
                groups = [charger, *charger.get("plugTypes", [])]
                for group in groups:
                    if group.get("plugType"):
                        connectors.add(str(group["plugType"]))
                    for connector in group.get("evIds", []):
                        connector_id = str(connector["evCpId"]).strip()
                        if not connector_id:
                            raise ValueError("missing connector identity")
                        state = str(connector.get("status", ""))
                        previous = states.get(connector_id)
                        states[connector_id] = state if previous is None or previous == state else ""
                # Conservative simulation: one vehicle per charger, even when
                # several connectors exist. Aggregate station status is not used.
                units[charger_id] = any(state == "1" for state in states.values())
            available = sum(units.values())
            site = Facility(
                facility_id=f"lta-charging-{identifier}",
                name=str(row.get("name") or identifier),
                address=str(row.get("address") or ""),
                zone_id=zone_for(position), position=position,
                facility_type="CHARGING", availability_basis="CHARGERS",
                parking_capacity=None, parking_available=available,
                charging_bays=len(units), charging_available=available,
                connector_types=sorted(connectors),
                status="OPEN", source="LTA_DATAMALL",
                availability_observed_at=observed_at,
                availability_expires_at=observed_at + timedelta(seconds=max_age),
                reported_charging_available=available)
            if site.facility_id in result:
                raise DataMallError("LTA_DUPLICATE_IDENTITY")
            result[site.facility_id] = site
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if records and not result:
        raise DataMallError("LTA_NO_USABLE_CHARGING")
    return list(result.values())


class DataMallClient:
    def __init__(self, client, account_key):
        self.client = client
        self.account_key = account_key

    async def get(self, url, *, authenticated=False, params=None):
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["AccountKey"] = self.account_key.get_secret_value()
        try:
            response = await self.client.get(url, headers=headers, params=params,
                                             timeout=10, follow_redirects=False)
            if response.status_code in (401, 403):
                raise DataMallError("LTA_AUTH_FAILED")
            if response.status_code == 429:
                raise DataMallError("LTA_RATE_LIMITED")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Do not log HTTP exceptions: signed download URLs contain credentials.
            raise DataMallError("LTA_REQUEST_FAILED") from None

    async def parking(self):
        records = []
        previous = None
        for offset in range(0, 50000, 500):
            page = rows(await self.get(f"{BASE_URL}/CarParkAvailabilityv2",
                                      authenticated=True, params={"$skip": offset}))
            if len(page) > 500 or (page and page == previous):
                raise DataMallError("LTA_INVALID_PAGINATION")
            records.extend(page)
            if len(page) < 500:
                return records
            previous = page
        raise DataMallError("LTA_PAGINATION_LIMIT")

    async def charging(self):
        links = rows(await self.get(f"{BASE_URL}/EVCBatch", authenticated=True))
        if len(links) != 1 or not isinstance(links[0].get("Link"), str):
            raise DataMallError("LTA_INVALID_DOWNLOAD_LINK")
        try:
            url = httpx.URL(links[0]["Link"])
        except httpx.InvalidURL:
            raise DataMallError("LTA_INVALID_DOWNLOAD_LINK") from None
        # Only download HTTPS S3 objects; never send AccountKey to this host.
        if (url.scheme != "https" or url.userinfo or url.port not in (None, 443)
                or not re.fullmatch(r"[a-z0-9.-]+\.s3(?:\.ap-southeast-1)?\.amazonaws\.com", url.host)):
            raise DataMallError("LTA_INVALID_DOWNLOAD_LINK")
        return rows(await self.get(url))


async def refresh_feed(store, provider, name):
    try:
        # Pagination and download together have a bounded refresh deadline.
        records = await asyncio.wait_for(getattr(provider, name)(), timeout=45)
        now = store.clock.now()
        max_age = getattr(store.settings, f"{name}_max_age_seconds")
        convert = parking_sites if name == "parking" else charging_sites
        sites = convert(records, now, max_age)
        await store.replace_feed(name, sites, now)
    except (DataMallError, asyncio.TimeoutError) as exc:
        code = str(exc) if isinstance(exc, DataMallError) else "LTA_TIMEOUT"
        await store.feed_failed(name, code)


async def poll_feed(store, provider, name):
    while True:
        try:
            await refresh_feed(store, provider, name)
        except Exception:
            # Keep the independent refresher alive even after storage failures.
            # Exception text may contain connection credentials; never log it.
            logging.getLogger(__name__).warning("Facilities %s refresh failed", name)
        await asyncio.sleep(getattr(store.settings, f"{name}_refresh_seconds"))
