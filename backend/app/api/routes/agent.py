from fastapi import APIRouter

from app.agent.travel_agent import TravelAgent
from app.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/agent", tags=["agent"])

agent = TravelAgent()


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return ChatResponse(reply=agent.chat(request))

