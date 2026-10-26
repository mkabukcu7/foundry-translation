# SPDX-License-Identifier: MIT
from datetime import timedelta

from azure.durable_functions import Blueprint, DurableOrchestrationContext, RetryOptions

from services.translator_client import TERMINAL

bp = Blueprint()


def run(context: DurableOrchestrationContext):
    job = context.get_input()
    retry = RetryOptions(5000, 3)
    try:
        for name in ("precheck", "prepare"):
            yield context.call_activity_with_retry(name, retry, job)
        # Avoid automatic retries around a non-idempotent POST. A persisted operation is reused.
        operation = yield context.call_activity("translate_start", job)
        job = {**job, **operation}
        deadline = context.current_utc_datetime + timedelta(minutes=30)
        delay = 2
        while True:
            if context.current_utc_datetime >= deadline:
                raise TimeoutError("Translation exceeded 30 minutes")
            status = yield context.call_activity_with_retry("translate_poll", retry, job)
            if context.current_utc_datetime > deadline:
                raise TimeoutError("Translation exceeded 30 minutes")
            if status["status"] in TERMINAL:
                if status["status"] != "Succeeded":
                    raise RuntimeError("Translation did not succeed")
                break
            now = context.current_utc_datetime
            if now >= deadline:
                raise TimeoutError("Translation exceeded 30 minutes")
            yield context.create_timer(min(now + timedelta(seconds=delay), deadline))
            delay = min(delay * 2, 30)
        yield context.call_activity_with_retry("check", retry, job)
        routed = yield context.call_activity_with_retry("route", retry, job)
        yield context.call_activity_with_retry("notify", retry, {**job, "finalStatus": routed["status"]})
        return {"jobId": job["jobId"], "status": routed["status"]}
    except Exception:
        # Only metadata goes into orchestration failure history; diagnostics are correlated by jobId.
        yield context.call_activity("record_failure", {"jobId": job["jobId"]})
        raise


@bp.orchestration_trigger(context_name="context")
def translation_orchestrator(context: DurableOrchestrationContext):
    return (yield from run(context))
