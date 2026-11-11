"""HTTP client for the hosted SWE-DT evaluation API.

Standard library only. Every call sends the API key in ``x-api-key``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = 'https://nz0fly8erk.execute-api.us-east-1.amazonaws.com/prod'
FINISHED = frozenset({'DONE', 'FAILED', 'TIMEOUT'})
USER_AGENT = 'swe-dt-client'


class ApiError(Exception):
    """The API answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f'{status}: {message}')
        self.status = status
        self.message = message

    @property
    def busy(self) -> bool:
        """All workers are busy (worth retrying, unlike the daily limit)."""
        return self.status == 429 and 'busy' in self.message


class Client:
    """A client for one API key.

    ``api_key`` defaults to the ``SWE_DT_API_KEY`` environment variable and
    ``url`` to ``SWE_DT_API_URL``, then to the public deployment.
    """

    def __init__(
        self,
        api_key: str | None = None,
        url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        key = api_key or os.environ.get('SWE_DT_API_KEY')
        if not key:
            raise ValueError('no API key: pass api_key= or set SWE_DT_API_KEY')
        self.api_key = key
        self.url = url or os.environ.get('SWE_DT_API_URL') or DEFAULT_URL
        self.url = self.url.rstrip('/')
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                'x-api-key': self.api_key,
                'Content-Type': 'application/json',
                'User-Agent': USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                return json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
                message = payload.get('error') or payload.get('message')
            except ValueError:
                message = raw.decode(errors='replace')
            raise ApiError(error.code, str(message or error.reason)) from None

    def instances(self) -> list[str]:
        """Instance ids that can be evaluated."""
        return self._request('GET', '/v1/instances')['instances']

    def submit(
        self,
        instance_id: str,
        patch: str,
        agent_name: str | None = None,
        retry_busy: float = 0.0,
    ) -> dict[str, Any]:
        """Submit a patch; returns the new evaluation (``id``, ``status``).

        With ``retry_busy`` > 0, keep retrying for up to that many seconds
        while all workers are busy.
        """
        body: dict[str, Any] = {'instance_id': instance_id, 'patch': patch}
        if agent_name:
            body['agent_name'] = agent_name
        deadline = time.monotonic() + retry_busy
        delay = 15.0
        while True:
            try:
                return self._request('POST', '/v1/evaluations', body)
            except ApiError as error:
                if not error.busy or time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 120.0)

    def status(self, evaluation_id: str) -> dict[str, Any]:
        """The evaluation, with its ``result`` once it is ``DONE``."""
        return self._request('GET', f'/v1/evaluations/{evaluation_id}')

    def wait(
        self,
        evaluation_id: str,
        poll: float = 20.0,
        timeout: float = 90 * 60,
    ) -> dict[str, Any]:
        """Poll until the evaluation is DONE, FAILED or TIMEOUT."""
        deadline = time.monotonic() + timeout
        while True:
            evaluation = self.status(evaluation_id)
            if evaluation.get('status') in FINISHED:
                return evaluation
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'{evaluation_id} still {evaluation.get("status")}'
                )
            time.sleep(poll)

    def evaluate(
        self, instance_id: str, patch: str, agent_name: str | None = None
    ) -> dict[str, Any]:
        """Submit a patch and wait for its verdict."""
        job = self.submit(instance_id, patch, agent_name, retry_busy=3600)
        return self.wait(job['id'])
