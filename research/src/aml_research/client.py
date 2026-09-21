"""Small synchronous client for the asynchronous research command API."""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


class OperationError(RuntimeError):
    def __init__(self, operation):
        self.operation = operation
        super().__init__(f"Operation {operation.get('id')} ended as {operation.get('status')}")


class ResearchClient:
    def __init__(self, base_url="http://localhost:8000", *, timeout=30, operation_timeout=360,
                 poll_interval=1, transport=None):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if min(timeout, operation_timeout, poll_interval) <= 0:
            raise ValueError("timeouts and polling interval must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.operation_timeout = operation_timeout
        self.poll_interval = poll_interval
        self.transport = transport or self._http

    def _http(self, method, path, body, headers):
        encoded = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(self.base_url + "/v1/" + path, data=encoded,
                          headers=headers, method=method)
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def request(self, method, path, body=None, *, key=None):
        headers = {"Content-Type": "application/json", "X-AML-API-Version": "aml.research.v1"}
        if method == "POST":
            headers["Idempotency-Key"] = key or uuid4().hex
        # Retrying a transport failure must reuse the same request key and body.
        for attempt in range(3):
            try:
                return self.transport(method, path, body, headers)
            except HTTPError:
                raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def wait(self, operation):
        deadline = time.monotonic() + self.operation_timeout
        while operation["status"] in {"pending", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Operation {operation['id']} has unknown completion status")
            time.sleep(self.poll_interval)
            operation = self.request("GET", f"research-operations/{operation['id']}")
        if operation["status"] != "completed":
            raise OperationError(operation)
        return operation

    def command(self, session_id, kind, body):
        operation = self.request("POST", f"research-sessions/{session_id}/{kind}", body)
        return self.wait(operation)
