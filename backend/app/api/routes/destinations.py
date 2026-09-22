from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Destination
from app.schemas import DestinationCreate, DestinationRead

router = APIRouter(prefix="/destinations", tags=["destinations"])


@router.get("", response_model=list[DestinationRead])
def list_destinations(db: Session = Depends(get_db)) -> list[Destination]:
    stmt = select(Destination).order_by(Destination.name)
    return list(db.scalars(stmt))


@router.post("", response_model=DestinationRead, status_code=status.HTTP_201_CREATED)
def create_destination(
    payload: DestinationCreate, db: Session = Depends(get_db)
) -> Destination:
    destination = Destination(**payload.model_dump())
    db.add(destination)
    db.commit()
    db.refresh(destination)
    return destination

