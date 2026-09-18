"""Tests for OpenAI-compatible vision endpoint selection."""

from types import SimpleNamespace

from src.libs.llm.openai_vision_llm import OpenAIVisionLLM


def test_vision_llm_uses_its_own_compatible_base_url() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="text-key",
            temperature=0.0,
            max_tokens=256,
            azure_endpoint=None,
            api_version=None,
            model="deepseek-chat",
        ),
        vision_llm=SimpleNamespace(
            api_key="vision-key",
            model="deepseek-v4-flash-vision-exp",
            deployment_name=None,
            max_image_size=2048,
            base_url="https://api.deepseek.com/v1",
            azure_endpoint=None,
            api_version=None,
            thinking={"type": "disabled"},
        ),
    )

    llm = OpenAIVisionLLM(settings)

    assert llm.api_key == "vision-key"
    assert llm.base_url == "https://api.deepseek.com/v1"
    assert llm.model == "deepseek-v4-flash-vision-exp"
    assert llm.thinking == {"type": "disabled"}
