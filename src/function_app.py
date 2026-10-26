# SPDX-License-Identifier: MIT
import asyncio
import json
import logging
import time
import uuid
from functools import wraps

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.durable_functions import DFApp

from activities import check, notify, precheck, prepare, route, translate
from orchestrator import bp
from services import auth, intake, jobs_table, retention, storage
from services.config import ConfigurationError, ExternalServiceError

app = DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Entra Easy Auth, not function keys.
app.register_functions(bp)
log = logging.getLogger("translation")


def response(body, status=200):
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


def http_errors(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except intake.IntakeError as error:
            return response({"error": str(error)}, error.status)
        except ResourceNotFoundError:
            return response({"error": "Job or result not found"}, 404)
        except ConfigurationError as error:
            correlation = uuid.uuid4().hex
            log.error("jobId=%s configuration_error=%s", correlation, error)
            return response({"error": str(error), "correlationId": correlation}, 503)
        except Exception as error:
            # Never return or log upstream exceptions that may include document content/tokens.
            correlation = uuid.uuid4().hex
            log.error("jobId=%s http_failure exception=%s", correlation, type(error).__name__)
            return response({"error": "Request failed; check service configuration",
                             "correlationId": correlation}, 503)
    return wrapped


async def submit(req, durable_client, owner_id, metadata, content):
    job_id = uuid.uuid4().hex
    job = {"jobId": job_id, "ownerId": owner_id, **metadata,
           "sourcePath": f"{job_id}/{metadata['fileName']}",
           "direction": f"{metadata['sourceLanguage'] or 'auto'}->{metadata['targetLanguage']}"}
    await asyncio.to_thread(jobs_table.create, job)
    try:
        await asyncio.to_thread(storage.put, "source", job["sourcePath"], content)
        await durable_client.start_new("translation_orchestrator", job_id, job)
    except Exception:
        await asyncio.to_thread(jobs_table.update, job_id, status="failed",
                                error="Submission failed; retry with a new job")
        raise
    base = req.url.split("/jobs")[0]
    return response({"jobId": job_id, "statusUrl": f"{base}/jobs/{job_id}"}, 202)


@app.route(route="jobs", methods=["POST"])
@app.durable_client_input(client_name="client")
@http_errors
async def post_jobs(req: func.HttpRequest, client):
    owner_id = auth.owner(req.headers)
    metadata, content = intake.decode(req.get_body(), req.headers.get("content-type", ""))
    return await submit(req, client, owner_id, metadata, content)


@app.route(route="jobs/from-url", methods=["POST"])
@app.durable_client_input(client_name="client")
@http_errors
async def post_from_url(req: func.HttpRequest, client):
    owner_id = auth.owner(req.headers)
    if len(req.get_body()) > 16384:
        raise intake.IntakeError("URL request is too large", 413)
    try:
        body = req.get_json()
        if not isinstance(body, dict):
            raise ValueError()
    except ValueError as error:
        raise intake.IntakeError("Supply a JSON object with url and targetLanguage") from error
    metadata, content = await asyncio.to_thread(intake.from_url, body, req.headers.get("x-graph-token"))
    return await submit(req, client, owner_id, metadata, content)


def owned_job(req):
    caller = auth.owner(req.headers)
    job_id = req.route_params["id"]
    if not storage.valid_job_id(job_id):
        raise intake.IntakeError("Invalid job ID")
    job = jobs_table.get(job_id)
    if job["ownerId"] != caller:
        raise intake.IntakeError("Job not found", 404)
    return job


@app.route(route="jobs/{id}", methods=["GET"])
@http_errors
async def get_job(req: func.HttpRequest):
    job = await asyncio.to_thread(owned_job, req)
    exposed = ("jobId", "userId", "fileName", "direction", "status", "step", "createdAt", "updatedAt",
               "score", "decision", "error", "chars", "images", "tokens")
    result = {key: job.get(key) for key in exposed}
    base = req.url.rstrip("/")
    if job.get("reportPath"):
        result["reportUrl"], result["resultUrl"] = base + "/report", base + "/result"
        report = await asyncio.to_thread(storage.get_json, job["jobId"], "report")
        result["topReasons"] = report.get("topReasons", [])
    return response(result)


@app.route(route="jobs/{id}/report", methods=["GET"])
@http_errors
async def get_report(req: func.HttpRequest):
    job = await asyncio.to_thread(owned_job, req)
    if not job.get("reportPath"):
        return response({"error": "Report is not ready"}, 409)
    report = await asyncio.to_thread(storage.get, "reports", job["reportPath"])
    return func.HttpResponse(report, mimetype="application/json")


@app.route(route="jobs/{id}/result", methods=["GET"])
@http_errors
async def get_result(req: func.HttpRequest):
    job = await asyncio.to_thread(owned_job, req)
    if not job.get("resultPath") or job["status"] not in {"complete", "needs_review"}:
        return response({"error": "Result is not ready"}, 409)
    content = await asyncio.to_thread(
        storage.get, job.get("resultContainer", f"target-{job['jobId']}"), job["resultPath"],
    )
    mime = "application/pdf" if job["fileName"].endswith(".pdf") else (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    return func.HttpResponse(content, mimetype=mime, headers={
        "Content-Disposition": f"attachment; filename=translation-{job['jobId']}.{job['fileName'].split('.')[-1]}"
    })


def register_activity(name, operation):
    @app.function_name(name=name)
    @app.activity_trigger(input_name="job")
    def activity(job: dict):
        job_id = job["jobId"]
        started = time.monotonic()
        jobs_table.update(job_id, step=name, status="running", error="")
        log.info("jobId=%s step=%s started", job_id, name)
        try:
            result = operation(job)
        except Exception as error:
            safe = isinstance(error, (ConfigurationError, ExternalServiceError))
            detail = str(error) if safe or (name == "precheck" and isinstance(error, ValueError)) else (
                f"{name} failed ({type(error).__name__}); check Azure connections and protected reports")
            log.error("jobId=%s step=%s exception=%s reason=%s", job_id, name, type(error).__name__, detail)
            jobs_table.update(job_id, status="failed", error=detail)
            raise RuntimeError(detail) from None
        duration = time.monotonic() - started
        current = jobs_table.get(job_id)
        jobs_table.update(job_id, **{f"duration_{name}": current.get(f"duration_{name}", 0) + duration})
        log.info("jobId=%s step=%s duration=%.3f", job_id, name, duration)
        return result


for _name, _operation in {
    "precheck": precheck.run, "prepare": prepare.run,
    "translate_start": translate.start, "translate_poll": translate.poll,
    "check": check.run, "route": route.run, "notify": notify.run,
}.items():
    register_activity(_name, _operation)


@app.activity_trigger(input_name="job")
def record_failure(job: dict):
    current = jobs_table.get(job["jobId"])
    jobs_table.update(job["jobId"], status="failed",
                      error=current.get("error") or "Pipeline failed or timed out; inspect jobId-correlated logs")


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def cleanup_retention(timer: func.TimerRequest):
    count = retention.cleanup()
    log.info("jobId=retention deletedContainers=%s", count)
