# SPDX-License-Identifier: MIT
"""Loopback-only showcase for the local translation workflow."""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from requests.exceptions import ConnectionError

from activities.precheck import validate_document
from scripts.local_demo import run_file
from services.config import (
    ConfigurationError,
    ReconstructionError,
    ReviewConnectionError,
    ReviewOutputError,
    ReviewRateLimitError,
    ReviewTimeoutError,
)
from services.local_translation import validate_pptx
from services.translation_engines import ENGINES
from services.translation_engines import config as engine_config

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "showcase"
RESULTS = ROOT / "samples" / "results" / "local"
SAMPLES = {
    "scan": ("Scanned claim", "Image-only PDF / OCR", "local_scanned_it.pdf"),
    "pdf": ("Digital claim", "Native PDF / text extraction", "local_claim_it.pdf"),
    "slides": ("Claim presentation", "Native-text PPTX / glossary", "local_claim_it.pptx"),
}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
JOB_ID = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger("showcase")


def create_app(results=RESULTS, *, translator_only=False):
    skip_review = os.getenv("SHOWCASE_SKIP_REVIEW", "").lower() == "true"
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

    def execute(job_id, data, filename, strategy, source_language, target_language, engine=None,
                reasoning_effort=None, glossary_mode="static", hybrid=False):
        def progress(stage):
            with lock:
                jobs[job_id]["stage"] = stage
        try:
            with TemporaryDirectory(prefix="translation-showcase-") as temp:
                source = Path(temp) / filename
                source.write_bytes(data)
                options = {"translator_only": True} if translator_only else {"strategy": strategy}
                if engine and not translator_only:
                    options["engine"] = engine
                if not translator_only:
                    if reasoning_effort is not None:
                        options["reasoning_effort"] = reasoning_effort
                    if glossary_mode != "static":
                        options["glossary_mode"] = glossary_mode
                    if hybrid:
                        options["hybrid"] = True
                if skip_review and not translator_only:
                    options["skip_review"] = True
                    options["hybrid"] = False
                report = run_file(source, results, source_language=source_language, target_language=target_language,
                                  job_id=job_id, progress=progress, **options)
            with lock:
                jobs[job_id].update(state="finished", stage="save", status=report["status"])
        except Exception as error:
            # Service/schema exceptions may contain document text; never log their messages.
            with lock:
                failed_stage = jobs[job_id]["stage"]
                jobs[job_id].update(state="failed", error=(
                    str(error) if isinstance(error, (ReviewConnectionError, ReviewOutputError, ReviewRateLimitError,
                                                     ReviewTimeoutError, ReconstructionError)) else
                    "The app could not complete its connection to the translation service. "
                    "No translated file was saved. Check connectivity before retrying."
                    if failed_stage == "translate" and isinstance(error, ConnectionError) else
                    "Translation completed, but document reconstruction failed. "
                    "Download the translated TXT and inspect document layout." if failed_stage == "reconstruct" else
                    f"Job failed during {failed_stage} ({type(error).__name__}). "
                    "Check Azure login, service access and configuration before retrying."
                ))
                if (results / job_id / "translated.txt").is_file():
                    jobs[job_id]["partialOutput"] = "translated.txt"
                if (results / job_id / "translated-unreviewed.pptx").is_file():
                    jobs[job_id]["partialDocument"] = "translated-unreviewed.pptx"
            logger.error("jobId=%s stage=%s errorType=%s", job_id, failed_stage, type(error).__name__)

    @app.get("/")
    def index():
        return FileResponse(WEB / ("baseline.html" if translator_only else "index.html"), media_type="text/html")

    @app.get("/api/config")
    def configuration():
        return {"reviewEnabled": not (skip_review or translator_only)}

    @app.get("/app.js")
    def javascript():
        return FileResponse(WEB / ("baseline.js" if translator_only else "app.js"), media_type="text/javascript")

    @app.get("/job-status.js")
    def job_status_javascript():
        return FileResponse(WEB / "job-status.js", media_type="text/javascript")

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
        source_language = request.query_params.get("sourceLanguage", "it")
        target_language = request.query_params.get("targetLanguage", "en")
        if source_language not in ("it", "es", "en") or target_language not in ("it", "es", "en"):
            raise HTTPException(400, "Source and target languages must be Italian (it), Spanish (es) or English (en)")
        if source_language == target_language:
            raise HTTPException(400, "Source and target languages must differ")
        engine = request.query_params.get("engine")
        reasoning_effort = request.query_params.get("reasoningEffort")
        glossary_mode = request.query_params.get("glossaryMode", "static")
        hybrid_value = request.query_params.get("hybrid", "false").lower()
        if hybrid_value not in {"true", "false"}:
            raise HTTPException(400, "Hybrid must be true or false")
        hybrid = hybrid_value == "true"
        if hybrid and (skip_review or translator_only):
            raise HTTPException(400, "Hybrid routing requires automated quality review")
        if glossary_mode not in {"none", "static", "retrieval"}:
            raise HTTPException(400, "Glossary mode must be none, static or retrieval")
        strategy = request.query_params.get("strategy", "translator")
        if engine is not None:
            if engine not in ENGINES:
                raise HTTPException(400, "Unknown translation engine")
            try:
                engine_config(engine, reasoning_effort=reasoning_effort)
            except ConfigurationError as error:
                raise HTTPException(400, str(error)) from None
            strategy = "translator" if engine in {"translator", "doc_llm"} else "direct_gpt"
            if hybrid and engine not in {"translator", "gpt6_luna"}:
                raise HTTPException(400, "Hybrid routing is available with Translator NMT or GPT-6 Luna")
        elif strategy not in ("translator", "direct_gpt", "translator_refine"):
            raise HTTPException(400, "Unknown translation strategy")
        if translator_only and strategy != "translator":
            raise HTTPException(400, "Translator-only mode supports only Azure Translator")
        if translator_only and engine not in (None, "translator"):
            raise HTTPException(400, "Translator-only mode supports only Azure Translator")
        sample = request.query_params.get("sample")
        if sample:
            if source_language != "it":
                raise HTTPException(400, "Built-in samples are Italian; select Italian or upload a Spanish file")
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
                raise HTTPException(400, "Use a document filename without directory components")
            suffix = Path(filename).suffix.lower()
            if suffix not in (".pdf", ".pptx", ".docx", ".xlsx", *IMAGE_EXTENSIONS):
                raise HTTPException(400, "Use a PDF, PPTX, DOCX, XLSX, JPEG, PNG, BMP or TIFF image")
            limit = (10 if suffix in (".pptx", ".docx", ".xlsx") else 40) * 1024 * 1024
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "PDF/image limit is 40 MiB; PPTX/DOCX/XLSX limit is 10 MiB")
                body.extend(chunk)
            data = bytes(body)
        try:
            validate_document(data, filename)
            if filename.lower().endswith(".pptx"):
                validate_pptx(data, allow_images=True)
        except Exception as error:
            # Validation messages are fixed by our validators; avoid arbitrary parser content.
            logger.warning("jobId=pending validationError=%s", type(error).__name__)
            raise HTTPException(400, "Invalid or unsupported file. Use an unsecured PDF, supported image, "
                                "DOCX, XLSX or PPTX within the displayed size limits. Macros are unsupported. "
                                "DOCX tracked changes and "
                                "dynamic fields must be converted to plain text; PPTX charts are unsupported.") from None
        job_id = uuid4().hex
        with lock:
            if any(job["state"] == "running" for job in jobs.values()):
                raise HTTPException(409, "A live demo is already running. Wait for it to finish.")
            jobs[job_id] = {"jobId": job_id, "state": "running", "stage": "validate"}
        executor.submit(
            execute, job_id, data, filename, strategy, source_language, target_language, engine,
            reasoning_effort, glossary_mode, hybrid,
        )
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
        output = report.get("outputFile") or (
            "translated.pptx" if (destination / "translated.pptx").is_file() else "translated.txt")
        return {"jobId": job_id, "state": "finished", "report": report, "preview": detail, "output": output}

    @app.get("/api/jobs/{job_id}/download/{name}")
    def download(job_id: str, name: str):
        if name not in ("report.json", "translated.txt", "translated.pptx", "translated-unreviewed.pptx",
                        "translated.docx", "translated.xlsx", "translated.pdf",
                        "translated.jpg", "translated.jpeg", "translated.png", "translated.bmp",
                        "translated.tif", "translated.tiff"):
            raise HTTPException(404, "Unknown output")
        path = folder(job_id) / name
        if not path.is_file():
            raise HTTPException(404, "Output not available")
        return FileResponse(path, filename=name)

    return app


app = create_app()
baseline_app = create_app(ROOT / "samples" / "results" / "translator-only", translator_only=True)
