from pydantic import BaseModel, ConfigDict


class DestinationBase(BaseModel):
    name: str
    country: str
    description: str = ""
    tags: str = ""


class DestinationCreate(DestinationBase):
    pass


class DestinationRead(DestinationBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

