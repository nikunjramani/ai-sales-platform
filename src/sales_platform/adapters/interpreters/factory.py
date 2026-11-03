"""Choose the enquiry interpreter and the manager reply reader. Generic: no provider is named.

- No model configured: the free, offline rules interpreter, and no reply reader (written
  manager replies are then not read; buttons and the signed link still work, P5-5).
- A model "<provider>:<model>" (e.g. "google_genai:gemini-3.5-flash-lite"): LangChain's
  init_chat_model builds that provider's chat model (build_chat_model), wrapped in
  LangChainInterpreter and LangChainReplyClassifier. Both use the same SALES_MODEL.

Adding a provider (OpenAI, Claude, Groq, Ollama...): add its LangChain package to the "ai" extra
and set SALES_MODEL / SALES_MODEL_API_KEY in .env, then run
scripts/evaluate_replies.py (and try a few enquiries in the demo). Nothing else changes.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.ports.interpretation import EnquiryInterpreter
from sales_platform.ports.reply_classification import ReplyClassifier

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


def build_chat_model(
    model: str | None,
    api_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> "BaseChatModel | None":
    """The configured chat model, or None without one."""
    if not model:
        return None
    provider, separator, name = model.partition(":")
    if not separator or not provider or not name:
        raise ValueError("The model must look like '<provider>:<model>'")
    # Imported here so the optional "ai" packages are needed only when a model is configured.
    from langchain.chat_models import init_chat_model

    settings: dict[str, Any] = {"temperature": 0, **(options or {})}
    if api_key:
        settings["api_key"] = api_key
    try:
        chat: BaseChatModel = init_chat_model(name, model_provider=provider, **settings)
    except ImportError as error:
        raise ValueError(
            f"The LangChain package for provider {provider!r} is not installed: {error}"
        ) from None
    # Outages, rate limits and a bad key raise ordinary errors at call time: the durable job
    # retries them with backoff (user decision: wait and retry, no fallback to rules).
    return chat


def build_interpreter(
    model: str | None = None,
    api_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> EnquiryInterpreter:
    chat = build_chat_model(model, api_key, options)
    if chat is None:
        return RulesInterpreter()
    from sales_platform.adapters.interpreters.langchain import LangChainInterpreter

    return LangChainInterpreter(chat)


def build_reply_classifier(
    model: str | None = None,
    api_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> ReplyClassifier | None:
    chat = build_chat_model(model, api_key, options)
    if chat is None or model is None:
        return None
    from sales_platform.adapters.interpreters.reply_classifier import LangChainReplyClassifier

    return LangChainReplyClassifier(chat, model)
