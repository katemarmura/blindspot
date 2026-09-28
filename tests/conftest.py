from collections.abc import Iterator

import anthropic
import pytest


@pytest.fixture(autouse=True)
def _no_real_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly if any test would construct a real Anthropic client.

    Tests that exercise the SDK path install their own fake with
    `monkeypatch.setattr(anthropic, "AsyncAnthropic", ...)`, which overrides this.
    """

    def refuse(**_: object) -> None:
        raise AssertionError("a test tried to create a real anthropic.AsyncAnthropic client")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", refuse)
    yield
