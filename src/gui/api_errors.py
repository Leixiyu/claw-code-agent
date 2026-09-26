"""One HTTP/NDJSON error contract; legacy error/detail strings stay readable."""
from __future__ import annotations

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..usage_ledger import UsageLedgerError

STATUS_CODES = {400: 'invalid_request', 401: 'authentication_required', 403: 'forbidden',
                404: 'not_found', 405: 'method_not_allowed', 409: 'conflict',
                413: 'payload_too_large', 422: 'validation_error', 429: 'rate_limited',
                500: 'internal_error', 502: 'model_backend_error', 503: 'service_unavailable',
                504: 'timeout'}


def error_payload(status: int, message: str, *, code: str | None = None,
                  error_type: str = 'HTTPException', fields=None) -> dict:
    payload = {'code': code or STATUS_CODES.get(status, 'request_failed'),
               'message': message, 'status': status,
               'error': message, 'detail': message, 'error_type': error_type}
    if fields is not None:
        payload['fields'] = fields
    return payload


def exception_payload(exc: Exception) -> dict:
    if isinstance(exc, UsageLedgerError):
        return error_payload(503, '用量记录服务暂不可用，请稍后重试。', code='usage_unavailable', error_type='UsageLedgerError')
    if isinstance(exc, RequestValidationError):
        # Never echo request bodies/passwords/API keys in validation errors.
        fields = [{'path': '.'.join(map(str, item['loc'])), 'type': item['type']}
                  for item in exc.errors()]
        return error_payload(422, '请求参数不正确，请检查必填项和参数格式。', fields=fields)
    if isinstance(exc, HTTPException):
        return error_payload(exc.status_code, str(exc.detail))
    # Unexpected exceptions may contain private paths, provider responses or credentials.
    return error_payload(500, '服务暂时无法完成请求，请稍后重试。', error_type=type(exc).__name__)


def error_response(status: int, message: str, *, code=None):
    return JSONResponse(error_payload(status, message, code=code), status_code=status)


class ErrorBoundaryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        started = False
        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)
        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            if started or scope['type'] != 'http':
                raise
            payload = exception_payload(exc)
            await JSONResponse(payload, status_code=payload['status'])(scope, receive, send)


def install_error_handlers(app):
    async def handle(request, exc):
        payload = exception_payload(exc)
        return JSONResponse(payload, status_code=payload['status'], headers=getattr(exc, 'headers', None))
    for cls in (HTTPException, RequestValidationError, UsageLedgerError):
        app.add_exception_handler(cls, handle)
    app.add_middleware(ErrorBoundaryMiddleware)
