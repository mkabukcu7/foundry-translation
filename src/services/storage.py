# SPDX-License-Identifier: MIT
import json
import re
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings

from services.config import credential, required


@lru_cache
def client():
    return BlobServiceClient(required("STORAGE_BLOB_ENDPOINT"), credential=credential())


def ensure_container(container: str):
    try:
        client().create_container(container)
    except ResourceExistsError:
        pass


def put(container: str, path: str, data: bytes, content_type: str = "application/octet-stream"):
    client().get_blob_client(container, path).upload_blob(
        data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
    )


def get(container: str, path: str) -> bytes:
    return client().get_blob_client(container, path).download_blob().readall()


def put_json(job_id: str, name: str, value):
    put("reports", f"{job_id}/{name}.json", json.dumps(value, ensure_ascii=False).encode(), "application/json")


def get_json(job_id: str, name: str):
    return json.loads(get("reports", f"{job_id}/{name}.json"))


def url(container: str, path: str = "") -> str:
    from urllib.parse import quote

    return f"{required('STORAGE_BLOB_ENDPOINT').rstrip('/')}/{container}/{quote(path, safe='/')}".rstrip("/")


def source(job: dict) -> bytes:
    return get("source", job["sourcePath"])


def target(job: dict) -> tuple[str, bytes]:
    prefix = f"{job['jobId']}/"
    names = [b.name for b in client().get_container_client(f"target-{job['jobId']}").list_blobs()
             if b.name.endswith((".pdf", ".pptx"))]
    if len(names) != 1:
        raise ValueError(f"Expected one translated document; found {len(names)}")
    name = names[0]
    if not (name.startswith(prefix) or name == job["fileName"]):
        raise ValueError("Unexpected translated blob path")
    return name, get(f"target-{job['jobId']}", name)


def valid_job_id(job_id: str) -> bool:
    return re.fullmatch(r"[a-f0-9]{32}", job_id) is not None
