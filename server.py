"""CROWN GP private GitHub -> Render -> trusted PC update gateway.

Render clones the private repository using its own GitHub integration.
This service serves ONLY the root main.py + version.json to authorized clients.
Never commit the UPDATE_ACCESS_TOKEN or place GitHub credentials in clients.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
MAX_MAIN_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024
VERSION_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}\Z")
SHA_RE = re.compile(r"[a-fA-F0-9]{64}\Z")


def response(start_response, status, data, content_type="text/plain; charset=utf-8"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    start_response(status, [
        ("Content-Type", content_type),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "no-store, max-age=0"),
        ("X-Content-Type-Options", "nosniff"),
    ])
    return [data]


def authorized(environ):
    expected = os.environ.get("UPDATE_ACCESS_TOKEN", "")
    authorization = environ.get("HTTP_AUTHORIZATION", "")
    # An unset / short token is a deployment error, not an open endpoint.
    if len(expected) < 32 or not authorization.startswith("Bearer "):
        return False
    received = authorization[len("Bearer "):]
    return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))


def release_files():
    manifest_bytes = (ROOT / "version.json").read_bytes()
    code = (ROOT / "main.py").read_bytes()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES or len(code) > MAX_MAIN_BYTES:
        raise ValueError("Release exceeds configured size limit")
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid release manifest")
    ver, digest = manifest.get("version"), manifest.get("sha256")
    if not isinstance(ver, str) or not VERSION_RE.fullmatch(ver):
        raise ValueError("Invalid release version")
    if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
        raise ValueError("Invalid SHA-256")
    if not hmac.compare_digest(hashlib.sha256(code).hexdigest(), digest.lower()):
        raise ValueError("main.py and version.json disagree: publish the two files together")
    return manifest_bytes, code


def application(environ, start_response):
    if environ.get("REQUEST_METHOD") != "GET":
        return response(start_response, "405 Method Not Allowed", "GET only\n")
    path = environ.get("PATH_INFO", "")
    if path == "/healthz":
        return response(start_response, "200 OK", "ok\n")
    if path not in ("/v1/version.json", "/v1/main.py"):
        return response(start_response, "404 Not Found", "not found\n")
    if len(os.environ.get("UPDATE_ACCESS_TOKEN", "")) < 32:
        return response(start_response, "503 Service Unavailable", "Update service is not configured\n")
    if not authorized(environ):
        return response(start_response, "401 Unauthorized", "Unauthorized\n")
    try:
        manifest_bytes, code = release_files()
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return response(start_response, "503 Service Unavailable", "Release is unavailable\n")
    if path.endswith("version.json"):
        return response(start_response, "200 OK", manifest_bytes, "application/json; charset=utf-8")
    return response(start_response, "200 OK", code, "text/x-python; charset=utf-8")


# Gunicorn entry point (start command: gunicorn server:application --bind 0.0.0.0:$PORT)
