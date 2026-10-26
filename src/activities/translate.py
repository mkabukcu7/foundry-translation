# SPDX-License-Identifier: MIT
from services import jobs_table, storage
from services.translator_client import TERMINAL, Translator


def start(job):
    current = jobs_table.get(job["jobId"])
    if current.get("operationUrl"):
        return {"operationUrl": current["operationUrl"]}
    storage.ensure_container(f"target-{job['jobId']}")
    operation = Translator().submit(
        job, storage.url("source", job["sourcePath"]),
        storage.url(f"target-{job['jobId']}", job["fileName"]),
        storage.url("glossaries", f"{job['jobId']}.tsv") if current.get("glossaryAvailable") else None,
    )
    jobs_table.update(job["jobId"], operationUrl=operation)
    return {"operationUrl": operation}


def poll(job):
    translator = Translator()
    status = translator.poll(job["operationUrl"], job["jobId"])
    state = status["status"]
    if state not in TERMINAL:
        return {"status": state}
    documents = translator.documents(job["operationUrl"], job["jobId"])
    storage.put_json(job["jobId"], "translation", {"batch": status, "documents": documents})
    jobs_table.update(job["jobId"],
                      chars=sum(d.get("characterCharged", 0) for d in documents),
                      images=sum(d.get("imageCharged", 0) for d in documents))
    if state != "Succeeded" or not documents or any(d["status"] != "Succeeded" for d in documents):
        raise ValueError("Translator failed one or more documents; see protected translation status report")
    return {"status": state}
