"""Synchronous worker-scoped HTTP transport for execution engines."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import requests

from api_tester.catalog import Endpoint
from api_tester.client import ApiResult, execute_endpoint
from api_tester.headers import merge_headers
from api_tester.suite import apply_variables

from .errors import ClassifiedError, classify_exception, classify_status
from .models import ExecutionEnvironmentSnapshot, RequestSample, RequestTemplate


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _timing(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class WorkerTransport:
    """Owns one pooled requests session for a single execution worker."""

    def __init__(
        self,
        environment: ExecutionEnvironmentSnapshot,
        worker_id: int,
        run_id: str,
        probe_connection: bool = False,
    ) -> None:
        self._environment = environment
        self._worker_id = worker_id
        self._run_id = run_id
        self._probe_connection = probe_connection
        self._session = requests.Session()

    def __enter__(self) -> "WorkerTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def execute(
        self,
        endpoint: Endpoint,
        template: RequestTemplate,
        *,
        extra_values: dict[str, str] | None = None,
        payload_override: Any = None,
    ) -> tuple[ApiResult | None, RequestSample, ClassifiedError | None]:
        started_at = _now_iso()
        wall_started = perf_counter()
        values = dict(template.values)
        if extra_values:
            values.update(extra_values)
        values = {
            key: str(apply_variables(value, self._environment.variables))
            if key.startswith(("header:", "file:")) else value
            for key, value in values.items()
        }
        payload = template.payload if payload_override is None else payload_override
        try:
            result = execute_endpoint(
                endpoint,
                self._environment.base_url(template.service),
                self._environment.access_token,
                self._environment.api_key,
                values,
                payload,
                template.expected_status,
                timeout=self._environment.timeout,
                verify_ssl=self._environment.verify_ssl,
                custom_headers={
                    name: str(value) for name, value in apply_variables(
                        merge_headers(self._environment.custom_headers, template.custom_headers),
                        self._environment.variables,
                    ).items()
                },
                auth_context=self._environment.auth_context,
                auth_mode=template.auth_mode,
                session=self._session,
                probe_connection=self._probe_connection,
            )
        except Exception as exc:
            classified = classify_exception(exc)
            sample = RequestSample(
                run_id=self._run_id,
                endpoint_id=endpoint.id,
                service=template.service,
                method=template.method,
                outcome="error",
                started_at=started_at,
                offset_ms=0.0,
                queue_delay_ms=0.0,
                http_ms=round((perf_counter() - wall_started) * 1000, 2),
                status_code=None,
                auth_ms=None,
                dns_ms=None,
                tcp_ms=None,
                tls_ms=None,
                worker_id=self._worker_id,
                error_category=classified.category,
                error_message=classified.message,
            )
            return None, sample, classified

        timings = result.timings if isinstance(result.timings, dict) else {}
        http_ms = _timing(timings.get("http_ms"))
        sample = RequestSample(
            run_id=self._run_id,
            endpoint_id=endpoint.id,
            service=template.service,
            method=template.method,
            outcome="passed" if result.passed else "failed",
            started_at=started_at,
            offset_ms=0.0,
            queue_delay_ms=0.0,
            http_ms=http_ms if http_ms is not None else round((perf_counter() - wall_started) * 1000, 2),
            status_code=result.status_code,
            auth_ms=_timing(timings.get("auth_ms")),
            dns_ms=_timing(timings.get("dns_ms")),
            tcp_ms=_timing(timings.get("tcp_ms")),
            tls_ms=_timing(timings.get("tls_ms")),
            worker_id=self._worker_id,
            request_bytes=len(result.request_body.encode("utf-8")) if result.request_body else 0,
            response_bytes=result.size,
        )
        if result.passed:
            return result, sample, None

        classified = classify_status(result.status_code)
        if classified is None:
            classified = ClassifiedError(
                "validation",
                f"Expected {template.expected_status}, got {result.status_code}",
            )
        sample = RequestSample(
            **{
                **sample.to_dict(),
                "error_category": classified.category,
                "error_message": classified.message,
            }
        )
        return result, sample, classified
