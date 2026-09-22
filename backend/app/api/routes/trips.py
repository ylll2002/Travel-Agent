from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import ItineraryItem, Trip
from app.schemas import ItineraryItemCreate, TripCreate, TripRead, TripUpdate

router = APIRouter(prefix="/trips", tags=["trips"])


def _get_trip_or_404(db: Session, trip_id: int) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found"
        )
    return trip


@router.get("", response_model=list[TripRead])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    stmt = (
        select(Trip)
        .options(selectinload(Trip.items))
        .order_by(Trip.created_at.desc())
    )
    return list(db.scalars(stmt))


@router.post("", response_model=TripRead, status_code=status.HTTP_201_CREATED)
def create_trip(payload: TripCreate, db: Session = Depends(get_db)) -> Trip:
    trip = Trip(
        title=payload.title,
        destination=payload.destination,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=payload.status,
        budget=payload.budget,
        notes=payload.notes,
    )
    for item in payload.items:
        trip.items.append(
            ItineraryItem(
                day=item.day,
                title=item.title,
                description=item.description,
                location=item.location,
                start_time=item.start_time,
                end_time=item.end_time,
            )
        )
    db.add(trip)
    db.commit()
    db.refresh(trip)
    return trip


@router.get("/{trip_id}", response_model=TripRead)
def get_trip(trip_id: int, db: Session = Depends(get_db)) -> Trip:
    trip = _get_trip_or_404(db, trip_id)
    return trip


@router.patch("/{trip_id}", response_model=TripRead)
def update_trip(
    trip_id: int, payload: TripUpdate, db: Session = Depends(get_db)
) -> Trip:
    trip = _get_trip_or_404(db, trip_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(trip, field, value)
    db.commit()
    db.refresh(trip)
    return trip


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip_id: int, db: Session = Depends(get_db)) -> None:
    trip = _get_trip_or_404(db, trip_id)
    db.delete(trip)
    db.commit()

