"""Offline health probes: no actual model/video API requests or paid calls."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from src.gui import health_routes as health
from src.gui.server import AgentState, create_app, create_user_app


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, handler, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await health._probe(client, 'http://test.invalid/health', **kwargs)

    async def test_empty_model_and_video_json_success(self):
        for status in (200, 204):
            result = await self.probe(lambda request: httpx.Response(status))
            self.assertEqual(result['status'], 'healthy')
            self.assertEqual(result['http_status'], status)
            self.assertGreaterEqual(result['response_time_ms'], 0)
        result = await self.probe(lambda request: httpx.Response(200, json={'status': 'healthy'}), require_status=True)
        self.assertEqual(result['status'], 'healthy')

    async def test_http_and_body_errors_are_redacted(self):
        cases = [
            (httpx.Response(401, text='secret: api-key'), False, 'http_error'),
            (httpx.Response(429), False, 'http_error'),
            (httpx.Response(503), True, 'http_error'),
            (httpx.Response(302, headers={'Location': 'http://elsewhere.invalid'}), False, 'http_error'),
            (httpx.Response(200, json={'status': 'unhealthy', 'detail': 'secret'}), True, 'backend_not_healthy'),
            (httpx.Response(200, json={'status': 'degraded'}), False, 'backend_not_healthy'),
            (httpx.Response(200, text='<html>login</html>'), True, 'invalid_health_response'),
            (httpx.Response(200, json={}), True, 'backend_not_healthy'),
            (httpx.Response(200, content=b'x' * 65537), False, 'response_too_large'),
        ]
        for response, strict, reason in cases:
            with self.subTest(reason=reason):
                result = await self.probe(lambda request: response, require_status=strict)
                self.assertEqual(result['status'], 'unhealthy')
                self.assertEqual(result['reason'], reason)
                self.assertNotIn('secret', json.dumps(result))

    async def test_missing_invalid_network_and_deadline(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('must not request'))) as client:
            self.assertEqual(await health._probe(client, ''), {'status': 'not_configured'})
            for url in ('file:///tmp/private', 'http://name:secret@host/health', 'http://host:999999/health'):
                self.assertEqual((await health._probe(client, url))['reason'], 'invalid_health_url')
        for error, reason in ((httpx.ConnectError('secret URL'), 'connection_error'),
                              (httpx.ReadTimeout('secret URL'), 'timeout')):
            def fail(request):
                raise error
            self.assertEqual((await self.probe(fail))['reason'], reason)

        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200)
        with patch.object(health, 'PROBE_TIMEOUT_SECONDS', 0.02):
            self.assertEqual((await self.probe(slow))['reason'], 'timeout')

    async def test_parallel_probes_do_not_send_credentials(self):
        requests = []
        arrived = asyncio.Event()

        async def handler(request):
            requests.append(request)
            if len(requests) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=1)
            return httpx.Response(200, json={'status': 'healthy'})

        factory = httpx.AsyncClient
        environment = {'MODEL_API_HEALTH_URL': 'https://model.invalid/custom-ready',
                       'OPENAI_API_KEY': 'chat-only-secret',
                       'VIDEO_ANALYSIS_API': 'video.invalid:8000/',
                       'VIDEO_PROCESSING_API': 'http://must-not-call.invalid',
                       'MODEL_TRAINING_API': 'http://must-not-call.invalid'}
        with patch.dict(os.environ, environment, clear=True), patch.object(health.httpx, 'AsyncClient',
                side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), trust_env=False, **kw)):
            result = await health.collect_health()
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['summary'], {'total': 4, 'checked': 2, 'healthy': 2, 'unhealthy': 0, 'not_configured': 2})
        by_host = {r.url.host: r for r in requests}
        self.assertNotIn('authorization', by_host['model.invalid'].headers)
        self.assertEqual(by_host['model.invalid'].url.path, '/custom-ready')
        self.assertNotIn('authorization', by_host['video.invalid'].headers)
        self.assertEqual(by_host['video.invalid'].url.path, '/health')
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('.invalid', json.dumps(result))

    async def test_one_timeout_does_not_hide_other_service(self):
        async def handler(request):
            if request.url.host == 'model.invalid':
                raise httpx.ReadTimeout('sensitive network detail')
            return httpx.Response(200, json={'status': 'healthy'})
        factory = httpx.AsyncClient
        environment = {'MODEL_API_HEALTH_URL': 'http://model.invalid/health',
                       'VIDEO_ANALYSIS_API': 'http://video.invalid'}
        with patch.dict(os.environ, environment, clear=True), patch.object(health.httpx, 'AsyncClient',
                side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), trust_env=False, **kw)):
            result = await health.collect_health()
        self.assertEqual(result['status'], 'unhealthy')
        self.assertEqual(result['summary'], {'total': 4, 'checked': 2, 'healthy': 1, 'unhealthy': 1, 'not_configured': 2})
        self.assertEqual(result['services']['model_api']['reason'], 'timeout')
        self.assertEqual(result['services']['video_analysis']['status'], 'healthy')
        self.assertNotIn('sensitive', json.dumps(result))


class HealthApiTests(unittest.TestCase):
    def test_only_public_app_has_health_no_login_needed_and_no_user_created(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'status': 'healthy'})

        factory = httpx.AsyncClient
        environment = {'MODEL_API_HEALTH_URL': 'http://model.invalid/health', 'VIDEO_ANALYSIS_API': 'http://video.invalid'}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, environment, clear=True):
            root = Path(tmp) / 'workspace'
            root.mkdir()
            state = AgentState(cwd=root, model='test', base_url='http://model.invalid/v1', api_key='chat-secret',
                               allow_shell=False, allow_write=False, session_directory=root / 'sessions')
            public_app = create_app(state)
            # The inner app is instantiated per user and for static files; it must
            # not register a duplicate health handler.
            with TestClient(create_user_app(state)) as inner:
                self.assertEqual(inner.get('/health').status_code, 404)
            for app in (public_app,):
                with TestClient(app) as client, patch.object(health.httpx, 'AsyncClient',
                        side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), trust_env=False, **kw)):
                    response = client.get('/health')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['summary']['healthy'], 2)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertNotIn('chat-secret', response.text)
                    client.get('/health')
            self.assertEqual(len(calls), 4)  # Fresh GET probes on each health call.
            self.assertTrue(all(r.method == 'GET' and 'authorization' not in r.headers for r in calls))
            self.assertEqual(public_app.state.user_apps, {})
            with TestClient(public_app) as client:
                self.assertEqual(client.get('/api/state').status_code, 401)

    def test_failure_http_503_and_unconfigured_http_200(self):
        factory = httpx.AsyncClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(health.create_health_router())
        with TestClient(app) as client, patch.dict(os.environ, {}, clear=True):
            result = client.get('/health')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['summary']['not_configured'], 4)
            self.assertEqual(result.json()['summary']['checked'], 0)
            with patch.dict(os.environ, {'VIDEO_ANALYSIS_API': 'http://video.invalid'}), patch.object(health.httpx, 'AsyncClient',
                    side_effect=lambda **kw: factory(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw)):
                result = client.get('/health')
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()['status'], 'unhealthy')
                self.assertEqual(result.json()['summary']['unhealthy'], 1)
