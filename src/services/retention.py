# SPDX-License-Identifier: MIT
"""Daily identity-only retention for dynamic target containers, which ARM cannot enumerate."""

from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError

from services import storage


def cleanup(now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=14)
    deleted = 0
    for container in storage.client().list_containers(name_starts_with="target-"):
        job_id = container.name.removeprefix("target-")
        if not storage.valid_job_id(job_id) or container.last_modified >= cutoff:
            continue
        # Never delete arbitrary containers: exact application job IDs and an age bound are mandatory.
        for name in ("source", "reports"):
            client = storage.client().get_container_client(name)
            for blob in client.list_blobs(name_starts_with=f"{job_id}/"):
                client.delete_blob(blob.name)
        try:
            storage.client().get_blob_client("glossaries", f"{job_id}.tsv").delete_blob()
        except ResourceNotFoundError:
            pass
        storage.client().delete_container(container.name)
        deleted += 1
    return deleted
