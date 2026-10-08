"""模型 key 未配置时的统一提示，不改变已配置服务的调用方式。"""

import os


MISSING_MODEL_API_KEY = "未配置模型 API Key，请先在服务端配置 API Key 后重试。"


def model_api_key_configured() -> bool:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return bool(key) and key not in {
        "sk-your-key-here", "sk-your-qwen-key-here", "sk-your-bailian-key-here",
    }
