"""TravelAgent 会话状态存储抽象。"""

import json
from typing import cast

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.agent.state import InitialTravelAgentState, TravelAgentState
from app.models.agent_session import AgentSession

StateInput = InitialTravelAgentState | TravelAgentState


class InMemorySessionStore:
    """开发期使用的内存会话仓库，后续可替换为持久化实现。"""

    def __init__(self) -> None:
        self._sessions: dict[str, StateInput] = {}

    def get(self, session_id: str) -> TravelAgentState | None:
        return cast(TravelAgentState | None, self._sessions.get(session_id))

    def save(self, session_id: str, state: StateInput) -> None:
        self._sessions[session_id] = state

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def clear(self) -> None:
        self._sessions.clear()


class PersistentSessionStore:
    """使用 SQLAlchemy 保存会话状态，跨进程重启保留。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, session_id: str) -> TravelAgentState | None:
        with self._session_factory() as db:
            record = db.get(AgentSession, session_id)
            if record is None:
                return None
            return json.loads(record.state_json)

    def save(self, session_id: str, state: StateInput) -> None:
        with self._session_factory() as db:
            record = db.get(AgentSession, session_id)
            if record is None:
                record = AgentSession(session_id=session_id, state_json="{}")
                db.add(record)
            record.state_json = json.dumps(state, ensure_ascii=False)
            db.commit()

    def delete(self, session_id: str) -> None:
        with self._session_factory() as db:
            db.execute(delete(AgentSession).where(AgentSession.session_id == session_id))
            db.commit()

    def clear(self) -> None:
        with self._session_factory() as db:
            db.execute(delete(AgentSession))
            db.commit()
