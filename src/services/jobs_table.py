# SPDX-License-Identifier: MIT
from datetime import datetime, timezone
from functools import lru_cache

from azure.data.tables import TableClient, UpdateMode

from services.config import credential, required


@lru_cache
def table():
    return TableClient(required("STORAGE_TABLE_ENDPOINT"), "jobs", credential=credential())


def now():
    return datetime.now(timezone.utc).isoformat()


def create(job: dict):
    table().create_entity({
        "PartitionKey": "jobs", "RowKey": job["jobId"], **job,
        "createdAt": now(), "updatedAt": now(), "status": "queued", "step": "submit",
        "score": -1, "decision": "", "reportPath": "", "error": "", "chars": 0, "images": 0, "tokens": 0,
    })


def update(job_id: str, **fields):
    table().update_entity(
        {"PartitionKey": "jobs", "RowKey": job_id, "updatedAt": now(), **fields},
        mode=UpdateMode.MERGE,
    )


def get(job_id: str) -> dict:
    return dict(table().get_entity("jobs", job_id))
