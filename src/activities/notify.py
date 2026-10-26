# SPDX-License-Identifier: MIT
from services import jobs_table


def run(job):
    # TODO(AZURE-CONNECT-6): add Teams/email notification; never send raw document text to logs.
    jobs_table.update(job["jobId"], status=job["finalStatus"], step="notify", notification="stub")
    return {"status": job["finalStatus"]}
