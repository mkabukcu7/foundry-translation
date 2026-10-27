# SPDX-License-Identifier: MIT
"""Loopback-only showcase for the local translation workflow."""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from activities.precheck import validate_document
from scripts.local_demo import run_file
from services.local_translation import native_pptx_only

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "showcase"
RESULTS = ROOT / "samples" / "results" / "local"
SAMPLES = {
    "scan": ("Scanned claim", "Image-only PDF / OCR", "local_scanned_it.pdf"),
    "pdf": ("Digital claim", "Native PDF / text extraction", "local_claim_it.pdf"),
    "slides": ("Claim presentation", "Native-text PPTX / glossary", "local_claim_it.pptx"),
}
JOB_ID = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger("showcase")


def create_app(results=RESULTS, *, translator_only=False):
    results = Path(results)
    port = 8766 if translator_only else 8765
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    origins = {f"http://{host}" for host in hosts}
    executor = ThreadPoolExecutor(max_workers=1)
    lock = threading.Lock()
    jobs = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        executor.shutdown(wait=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "Localhost access only"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            if (request.headers.get("origin") not in origins
                    or request.headers.get("x-showcase-request") != "1"):
                return JSONResponse({"detail": "Same-origin local requests only"}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'",
            "Referrer-Policy": "no-referrer",
        })
        return response

    def folder(job_id):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "Unknown job")
        return results / job_id

    def execute(job_id, data, filename):
        def progress(stage):
            with lock:
                jobs[job_id]["stage"] = stage
        try:
            with TemporaryDirectory(prefix="translation-showcase-") as temp:
                source = Path(temp) / filename
                source.write_bytes(data)
                options = {"translator_only": True} if translator_only else {}
                report = run_file(source, results, job_id=job_id, progress=progress, **options)
            with lock:
                jobs[job_id].update(state="finished", stage="save", status=report["status"])
        except Exception as error:
            # Service/schema exceptions may contain document text; never log their messages.
            with lock:
                failed_stage = jobs[job_id]["stage"]
                jobs[job_id].update(state="failed", error=(
                    f"Job failed during {failed_stage} ({type(error).__name__}). "
                    "Check Azure login, service access and configuration before retrying."
                ))
            logger.error("jobId=%s stage=%s errorType=%s", job_id, failed_stage, type(error).__name__)

    @app.get("/")
    def index():
        return FileResponse(WEB / ("baseline.html" if translator_only else "index.html"), media_type="text/html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(WEB / ("baseline.js" if translator_only else "app.js"), media_type="text/javascript")

    @app.get("/style.css")
    def stylesheet():
        return FileResponse(WEB / "style.css", media_type="text/css")

    @app.get("/api/samples")
    def samples():
        return [{"id": key, "title": title,
                 "description": description.replace(" / glossary", " / no glossary") if translator_only else description,
                 "available": (ROOT / "samples" / "out" / filename).is_file()}
                for key, (title, description, filename) in SAMPLES.items()]

    @app.get("/api/jobs")
    def history():
        saved = []
        if results.exists():
            for path in results.iterdir():
                if JOB_ID.fullmatch(path.name) and (path / "report.json").is_file():
                    report = json.loads((path / "report.json").read_text(encoding="utf-8"))
                    saved.append({"jobId": path.name, "status": report["status"],
                                  "score": report["score"], "savedAt": (path / "report.json").stat().st_mtime})
        return sorted(saved, key=lambda item: item["savedAt"], reverse=True)[:20]

    @app.post("/api/jobs", status_code=202)
    async def submit(request: Request):
        sample = request.query_params.get("sample")
        if sample:
            if sample not in SAMPLES:
                raise HTTPException(400, "Unknown sample")
            filename = SAMPLES[sample][2]
            path = ROOT / "samples" / "out" / filename
            if not path.is_file():
                raise HTTPException(400, "Generate the synthetic samples first with the validation task")
            data = path.read_bytes()
        else:
            filename = request.query_params.get("filename", "")
            if (not filename or len(filename) > 160
                    or any(c in filename for c in '/\\<>:"|?*')
                    or any(ord(c) < 32 for c in filename)):
                raise HTTPException(400, "Use a PDF or PPTX filename without directory components")
            if Path(filename).suffix.lower() not in (".pdf", ".pptx"):
                raise HTTPException(400, "Only PDF and PPTX documents are supported")
            limit = (10 if filename.lower().endswith(".pptx") else 40) * 1024 * 1024
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "PDF limit is 40 MiB; PPTX limit is 10 MiB")
                body.extend(chunk)
            data = bytes(body)
        try:
            validate_document(data, filename)
            if filename.lower().endswith(".pptx"):
                native_pptx_only(data)
        except ValueError as error:
            # Validation messages are fixed by our validators; avoid arbitrary parser content.
            logger.warning("jobId=pending validationError=%s", type(error).__name__)
            raise HTTPException(400, "Invalid or unsupported document. Use an unsecured PDF or native-text "
                                "PPTX without images, within the displayed size limits.") from None
        job_id = uuid4().hex
        with lock:
            if any(job["state"] == "running" for job in jobs.values()):
                raise HTTPException(409, "A live demo is already running. Wait for it to finish.")
            jobs[job_id] = {"jobId": job_id, "state": "running", "stage": "validate"}
        executor.submit(execute, job_id, data, filename)
        return {"jobId": job_id}

    @app.get("/api/jobs/{job_id}")
    def status(job_id: str):
        destination = folder(job_id)
        with lock:
            job = dict(jobs[job_id]) if job_id in jobs else None
        if job and job["state"] != "finished":
            return job
        if not (destination / "report.json").is_file():
            raise HTTPException(404, "Unknown job")
        report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        preview = destination / "showcase.json"
        detail = json.loads(preview.read_text(encoding="utf-8")) if preview.is_file() else None
        output = "translated.pptx" if (destination / "translated.pptx").is_file() else "translated.txt"
        return {"jobId": job_id, "state": "finished", "report": report, "preview": detail, "output": output}

    @app.get("/api/jobs/{job_id}/download/{name}")
    def download(job_id: str, name: str):
        if name not in ("report.json", "translated.txt", "translated.pptx"):
            raise HTTPException(404, "Unknown output")
        path = folder(job_id) / name
        if not path.is_file():
            raise HTTPException(404, "Output not available")
        return FileResponse(path, filename=name)

    return app


app = create_app()
baseline_app = create_app(ROOT / "samples" / "results" / "translator-only", translator_only=True)
