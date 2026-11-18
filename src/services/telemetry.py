# SPDX-License-Identifier: MIT
"""Optional Azure Monitor custom metrics for local translation runs."""

import atexit
import os
from functools import lru_cache

from services.config import credential


@lru_cache(maxsize=1)
def _instruments():
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return None

    from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    exporter = AzureMonitorMetricExporter(
        connection_string=connection_string,
        credential=credential(),
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10_000)
    provider = MeterProvider(metric_readers=[reader])
    atexit.register(provider.shutdown)
    meter = provider.get_meter("foundry-translation-poc")
    return {
        "tokens": meter.create_counter("translation.tokens", unit="token"),
        "characters": meter.create_counter("translation.characters", unit="character"),
        "cost": meter.create_counter("translation.estimated_cost", unit="USD"),
        "duration": meter.create_histogram("translation.step_duration", unit="s"),
    }


def record_translation_metrics(job_id, engine, usage, *, duration_by_step, estimated_cost):
    instruments = _instruments()
    if instruments is None:
        return False
    attributes = {
        "jobId": job_id,
        "engine": engine.engine,
        "model": engine.model_name,
        "deploymentType": engine.deployment_type,
        "modelVersion": engine.model_version or "",
        "apiVersion": engine.api_version,
        "maturity": engine.maturity,
        "reasoningEffort": engine.reasoning_effort or "",
    }
    for token_type in ("input", "output", "reasoning"):
        value = usage.get(f"{token_type}Tokens")
        if value is not None:
            instruments["tokens"].add(value, {**attributes, "tokenType": token_type})
    for character_type in ("input", "output", "charged"):
        characters = usage.get({
            "input": "inputCharacters", "output": "outputCharacters", "charged": "charactersCharged",
        }[character_type])
        if characters is not None:
            instruments["characters"].add(characters, {**attributes, "characterType": character_type})
    if estimated_cost is not None:
        instruments["cost"].add(estimated_cost, attributes)
    for step, duration in duration_by_step.items():
        instruments["duration"].record(duration, {**attributes, "step": step})
    return True


def record_role_metrics(job_id, role, usage, *, deployment_type, estimated_cost):
    instruments = _instruments()
    if instruments is None:
        return False
    attributes = {
        "jobId": job_id,
        "engine": role,
        "model": usage.get("model", ""),
        "responseModel": usage.get("responseModel", ""),
        "agentVersion": usage.get("version", ""),
        "deploymentType": deployment_type,
        "modelVersion": "",
        "apiVersion": os.getenv("RESPONSES_API_VERSION", "v1"),
        "maturity": "GA" if usage.get("model") else "unknown",
        "reasoningEffort": usage.get("reasoningEffort", ""),
    }
    for token_type in ("input", "output", "reasoning"):
        value = usage.get(f"{token_type}Tokens")
        if value is not None:
            instruments["tokens"].add(value, {**attributes, "tokenType": token_type})
    if estimated_cost is not None:
        instruments["cost"].add(estimated_cost, attributes)
    if usage.get("durationSeconds") is not None:
        instruments["duration"].record(usage["durationSeconds"], {**attributes, "step": "review"})
    return True
