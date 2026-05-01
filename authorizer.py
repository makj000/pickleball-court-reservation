import base64
import os


def handler(event, context):
    expected_user = os.environ.get("API_USERNAME", "")
    expected_pass = os.environ.get("API_PASSWORD", "")
    if not expected_user or not expected_pass:
        return {"isAuthorized": False}
    auth_header = event.get("headers", {}).get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == expected_user and password == expected_pass:
                return {"isAuthorized": True}
        except Exception:
            pass
    return {"isAuthorized": False}
