import base64

USERNAME = "test1"
PASSWORD = "clouderocks!"


def handler(event, context):
    auth_header = event.get("headers", {}).get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == USERNAME and password == PASSWORD:
                return {"isAuthorized": True}
        except Exception:
            pass
    return {"isAuthorized": False}
