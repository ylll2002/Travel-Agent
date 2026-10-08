from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.api.routes import (
    agent,
    auth,
    behavior_signal,
    destinations,
    health,
    memory_cache,
    plan,
    preferences,
    profile,
    question,
    search,
    trip_memory,
    trips,
)
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.services.destination_service import seed_destinations

settings = get_settings()


def ensure_sqlite_legacy_schema() -> None:
    """Add columns that create_all cannot backfill on an existing SQLite DB."""
    if not settings.database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    if "trip_memories" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("trip_memories")}
    if "conversation" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE trip_memories "
                    "ADD COLUMN conversation JSON NOT NULL DEFAULT '[]'"
                )
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_legacy_schema()
    with SessionLocal() as db:
        seed_destinations(db)
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_prefix = settings.api_prefix
app.include_router(health.router, prefix=api_prefix)
app.include_router(auth.router, prefix=api_prefix)
app.include_router(trips.router, prefix=api_prefix)
app.include_router(destinations.router, prefix=api_prefix)
app.include_router(agent.router, prefix=api_prefix)
app.include_router(profile.router, prefix=api_prefix)
app.include_router(preferences.router, prefix=api_prefix)
app.include_router(question.router, prefix=api_prefix)
app.include_router(trip_memory.router, prefix=api_prefix)
app.include_router(behavior_signal.router, prefix=api_prefix)
app.include_router(memory_cache.router, prefix=api_prefix)
app.include_router(plan.router, prefix=api_prefix)
app.include_router(search.router, prefix=api_prefix)
