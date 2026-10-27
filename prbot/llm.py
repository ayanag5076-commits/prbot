"""Chat model factory (OpenAI by default, any LangChain chat model with tool calling works)."""
from __future__ import annotations


def make_chat_model(model: str = "gpt-4.1-mini", temperature: float = 0.0):
    from langchain_openai import ChatOpenAI  # reads OPENAI_API_KEY from the environment

    return ChatOpenAI(model=model, temperature=temperature, timeout=120, max_retries=2)
