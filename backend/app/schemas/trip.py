from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field


class ItineraryItemBase(BaseModel):
    day: int = Field(ge=1, default=1)
    title: str
    description: str = ""
    location: str = ""
    start_time: time | None = None
    end_time: time | None = None


class ItineraryItemCreate(ItineraryItemBase):
    pass


class ItineraryItemRead(ItineraryItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_id: int


class TripBase(BaseModel):
    title: str
    destination: str
    start_date: date
    end_date: date
    status: str = "draft"
    budget: float | None = None
    notes: str = ""


class TripCreate(TripBase):
    items: list[ItineraryItemCreate] = []


class TripUpdate(BaseModel):
    title: str | None = None
    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None
    budget: float | None = None
    notes: str | None = None


class TripRead(TripBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    items: list[ItineraryItemRead] = []

