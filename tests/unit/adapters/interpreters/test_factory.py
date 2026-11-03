import pytest

from sales_platform.adapters.interpreters.factory import build_interpreter
from sales_platform.adapters.interpreters.langchain import LangChainInterpreter
from sales_platform.adapters.interpreters.rules import RulesInterpreter


def test_no_model_means_the_free_rules_interpreter() -> None:
    assert isinstance(build_interpreter(), RulesInterpreter)
    assert isinstance(build_interpreter(None, "unused-key"), RulesInterpreter)


def test_any_installed_provider_builds_without_a_network_call() -> None:
    interpreter = build_interpreter(
        "google_genai:gemini-3.5-flash-lite", "synthetic-test-key", {"timeout": 30}
    )
    assert isinstance(interpreter, LangChainInterpreter)


def test_a_provider_without_its_package_is_explained() -> None:
    with pytest.raises(ValueError, match="not installed"):
        build_interpreter("mistralai:any-model", "synthetic-test-key")


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", ":model", "provider:"])
def test_model_must_name_provider_and_model(model: str) -> None:
    with pytest.raises(ValueError, match="<provider>:<model>"):
        build_interpreter(model)
