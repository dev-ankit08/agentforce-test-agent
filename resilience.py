"""
resilience.py -- retry/backoff helper and the typed error raised when a model
call can't be completed. Pure stdlib; no Anthropic/Salesforce imports here so it
stays trivially unit-testable.

Used to wrap the fallible network calls in model_client.py (and, indirectly, the
Agent API client) so a transient hiccup is retried rather than aborting a whole
bulk run. The policy is deliberately simple: a fixed number of attempts with
exponential backoff, and a single typed failure (ModelCallError) that callers
can catch to isolate the failure (e.g. mark one execution EXCLUDED and keep
going) instead of crashing the batch.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

log = logging.getLogger("agentforce_test.resilience")

T = TypeVar("T")


class ModelCallError(Exception):
    """A model call failed after exhausting retries, or returned output that
    could not be parsed. Callers isolate this to avoid crashing a bulk run."""


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn` up to `attempts` times, retrying on any exception in `retry_on`
    with exponential backoff (base_delay * 2**n, capped at max_delay). Re-raises
    the last error wrapped in ModelCallError once attempts are exhausted. Any
    exception NOT in retry_on propagates immediately (it isn't transient).

    `sleep` is injectable so tests don't actually wait."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            log.warning(
                "transient failure (attempt %d/%d): %s: %s -- retrying in %.1fs",
                attempt, attempts, type(exc).__name__, exc, delay,
            )
            sleep(delay)
    raise ModelCallError(f"call failed after {attempts} attempt(s): {last}") from last
