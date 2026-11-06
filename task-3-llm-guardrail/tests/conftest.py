from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

# "ci" is the default gate: deterministic and reasonably deep. "deep" is for local soak runs:
#   HYPOTHESIS_PROFILE=deep uv run pytest -q
settings.register_profile(
    "ci", derandomize=True, max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "deep", max_examples=5000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
