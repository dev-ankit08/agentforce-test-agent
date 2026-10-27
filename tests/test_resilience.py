"""Unit tests for the retry/backoff helper (offline, no sleeping)."""
from __future__ import annotations

import pytest

from resilience import ModelCallError, with_retries


def test_succeeds_without_retry():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    assert with_retries(fn, sleep=lambda _: None) == "ok"
    assert calls["n"] == 1


def test_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "recovered"

    out = with_retries(fn, attempts=3, retry_on=(ValueError,), sleep=lambda _: None)
    assert out == "recovered"
    assert calls["n"] == 3


def test_exhaustion_raises_model_call_error():
    def fn():
        raise ValueError("always fails")

    with pytest.raises(ModelCallError):
        with_retries(fn, attempts=2, retry_on=(ValueError,), sleep=lambda _: None)


def test_non_retryable_propagates_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise KeyError("not retryable")

    with pytest.raises(KeyError):
        with_retries(fn, attempts=5, retry_on=(ValueError,), sleep=lambda _: None)
    assert calls["n"] == 1  # not retried
