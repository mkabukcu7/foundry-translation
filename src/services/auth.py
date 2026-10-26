# SPDX-License-Identifier: MIT
import base64
import json
import os

from services.intake import IntakeError


def owner(headers):
    if (os.getenv("ALLOW_LOCAL_AUTH_BYPASS") == "1"
            and os.getenv("AZURE_FUNCTIONS_ENVIRONMENT") == "Development"
            and not os.getenv("WEBSITE_INSTANCE_ID")):
        return "local-developer"
    try:
        principal = json.loads(base64.b64decode(headers["x-ms-client-principal"], validate=True))
        claims = principal["claims"]
        object_id = next(c["val"] for c in claims
                         if c["typ"] in {"oid", "http://schemas.microsoft.com/identity/claims/objectidentifier"})
        scopes = [c["val"] for c in claims if c["typ"] in {"scp", "http://schemas.microsoft.com/identity/claims/scope"}]
        roles = [c["val"] for c in claims if c["typ"] in {"roles", "role",
                                                       "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}]
        if "Translation.Invoke" not in " ".join(scopes).split() and "Translation.Invoke.All" not in roles:
            raise IntakeError("Token requires Translation.Invoke scope or Translation.Invoke.All app role", 403)
        return object_id
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        if isinstance(error, IntakeError):
            raise
        raise IntakeError("Entra authentication is required", 401) from error
