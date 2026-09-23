"""Read-only dependency probes; never submit inference, training or model chat."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from time import perf_counter
from urllib.parse import urlsplit

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import httpx


PROBE_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 65536


async def _probe(client: httpx.AsyncClient, url: str, *, require_status: bool = False) -> dict:
    if not url:
        return {'status': 'not_configured'}
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('invalid URL')
        parsed.port  # Validate malformed/out-of-range ports without revealing URL.
    except ValueError:
        return {'status': 'unhealthy', 'reason': 'invalid_health_url'}

    started = perf_counter()
    result = {'status': 'unhealthy'}

    async def request_health():
        async with client.stream('GET', url) as response:
            result['http_status'] = response.status_code
            if not 200 <= response.status_code < 300:
                result['reason'] = 'http_error'
                return
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    result['reason'] = 'response_too_large'
                    return
                body.extend(chunk)
            # Video Analysis has an explicit JSON health contract. Model health
            # endpoints may instead return an empty 200/204 or plain text.
            if require_status or 'json' in response.headers.get('content-type', '').lower():
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    result['reason'] = 'invalid_health_response'
                    return
                status = payload.get('status') if isinstance(payload, dict) else None
                if status is not None or require_status:
                    if not isinstance(status, str) or status.lower() not in {'healthy', 'ok', 'ready', 'up'}:
                        result['reason'] = 'backend_not_healthy'
                        return
            result['status'] = 'healthy'

    try:
        # Wall-clock cap also covers slow trickling response bodies, unlike a
        # socket read timeout alone. Do not follow redirects or retry requests.
        await asyncio.wait_for(request_health(), timeout=PROBE_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result['reason'] = 'timeout'
    except httpx.HTTPError:
        result['reason'] = 'connection_error'
    except (ValueError, httpx.InvalidURL):
        result['reason'] = 'invalid_health_response'
    result['response_time_ms'] = round((perf_counter() - started) * 1000, 1)
    return result


async def collect_health() -> dict:
    model_url = os.environ.get('MODEL_API_HEALTH_URL', '').strip()
    analysis_base = os.environ.get('VIDEO_ANALYSIS_API', '').strip().rstrip('/')
    if analysis_base and '://' not in analysis_base:
        analysis_base = 'http://' + analysis_base
    analysis_url = analysis_base + '/health' if analysis_base else ''
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False) as client:
        model, analysis = await asyncio.gather(
            _probe(client, model_url),
            _probe(client, analysis_url, require_status=True),
        )
    services = {
        'model_api': model,
        'video_analysis': analysis,
        'video_processing': {'status': 'not_configured', 'reason': 'health_endpoint_not_available'},
        'model_training': {'status': 'not_configured', 'reason': 'health_endpoint_not_available'},
    }
    counts = {status: sum(item['status'] == status for item in services.values())
              for status in ('healthy', 'unhealthy', 'not_configured')}
    status = 'unhealthy' if counts['unhealthy'] else ('degraded' if counts['not_configured'] else 'healthy')
    return {
        'status': status,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'summary': {'total': len(services), 'checked': counts['healthy'] + counts['unhealthy'], **counts},
        'services': services,
    }


def create_health_router() -> APIRouter:
    router = APIRouter()

    @router.get('/health')
    async def health():
        report = await collect_health()
        return JSONResponse(report, status_code=503 if report['status'] == 'unhealthy' else 200,
                            headers={'Cache-Control': 'no-store'})

    return router
