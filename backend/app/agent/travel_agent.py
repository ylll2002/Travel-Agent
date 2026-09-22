from app.schemas import ChatRequest


class TravelAgent:
    """Travel assistant.

    当前是占位实现，接入真实 LLM 时只需替换 ``chat`` 方法的逻辑。
    """

    def chat(self, request: ChatRequest) -> str:
        user_messages = [m.content for m in request.messages if m.role == "user"]
        last_message = user_messages[-1] if user_messages else ""
        return (
            f"这是一个占位回复。你刚才说：「{last_message}」。"
            "接入大模型后，我可以在这里帮你规划行程、推荐目的地和整理攻略。"
        )

