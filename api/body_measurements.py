from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from schemas import BodyMeasurementCreate, BodyMeasurementUpdate
from security import current_user
from services.body_measurements import BodyMeasurementService

router = APIRouter(prefix="/api/body-measurements")


def service(request: Request, user: Annotated[dict, Depends(current_user)]):
    return BodyMeasurementService(request.app.state.database, user["id"])


Service = Annotated[BodyMeasurementService, Depends(service)]


@router.get("")
def review(day: date, service: Service, days: int = Query(30, ge=30, le=90)):
    return service.review(day, days)


@router.post("", status_code=201)
def create(body: BodyMeasurementCreate, service: Service):
    return service.create(body)


@router.put("/{record_id}")
def update(record_id: int, body: BodyMeasurementUpdate, service: Service):
    return service.update(record_id, body)


@router.delete("/{record_id}", status_code=204)
def delete(record_id: int, service: Service, version: int = Query(ge=1)):
    service.delete(record_id, version)
