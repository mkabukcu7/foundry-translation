# SPDX-License-Identifier: MIT
"""Entra-protected LibreOffice renderer for deployments without a local soffice binary."""

import asyncio
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from jwt import PyJWKClient

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache
def signing_keys(tenant):
    return PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys")


def authorize(authorization):
    tenant = os.environ["AZURE_TENANT_ID"]
    audience = os.environ["RENDERER_CLIENT_ID"]
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Entra bearer token required")
    token = authorization.removeprefix("Bearer ")
    try:
        keys = signing_keys(tenant)
        claims = jwt.decode(
            token, keys.get_signing_key_from_jwt(token).key, algorithms=["RS256"], audience=audience,
            issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
        if "Renderer.Invoke" not in claims.get("roles", []):
            raise HTTPException(403, "Renderer.Invoke application role required")
    except jwt.PyJWTError as error:
        raise HTTPException(401, "Invalid renderer token") from error


@app.get("/healthz")
async def health():
    return {"status": "ready"}


@app.post("/render")
async def render(request: Request):
    await asyncio.to_thread(authorize, request.headers.get("authorization"))
    maximum = int(os.environ.get("MAX_DOCUMENT_MB", "40")) * 1024 * 1024
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > maximum:
            raise HTTPException(413, "Presentation exceeds size limit")
    # Validate expanded archive size before invoking LibreOffice.
    import io
    from zipfile import BadZipFile, ZipFile
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            if "ppt/presentation.xml" not in archive.namelist():
                raise HTTPException(415, "A PPTX presentation is required")
            if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                raise HTTPException(413, "Expanded presentation exceeds safety limit")
    except BadZipFile as error:
        raise HTTPException(415, "A PPTX presentation is required") from error
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "slides.pptx"
        source.write_bytes(data)
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["libreoffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless",
                 "--convert-to", "pdf", "--outdir", tmp, str(source)],
                check=True, capture_output=True, timeout=120,
            )
            result = (Path(tmp) / "slides.pdf").read_bytes()
        except (subprocess.SubprocessError, FileNotFoundError) as error:
            raise HTTPException(422, "Slide conversion failed") from error
    return Response(result, media_type="application/pdf")
