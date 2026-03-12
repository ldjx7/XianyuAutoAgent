import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote, urlparse

try:
    from loguru import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ItemWhitelistApiServer:
    def __init__(self, store, host: str, port: int, bearer_token: str):
        self.store = store
        self.host = host
        self.port = int(port)
        self.bearer_token = bearer_token
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._httpd is not None:
            return

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server._handle_request(self, "GET")

            def do_POST(self):
                server._handle_request(self, "POST")

            def do_PUT(self):
                server._handle_request(self, "PUT")

            def do_DELETE(self):
                server._handle_request(self, "DELETE")

            def log_message(self, format, *args):  # pragma: no cover
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"商品白名单管理接口已启动: http://{self.host}:{self.port}")

    def stop(self):
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        finally:
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _handle_request(self, handler: BaseHTTPRequestHandler, method: str):
        try:
            if not self._is_authorized(handler):
                self._write_json(handler, 401, {"error": "unauthorized"})
                return

            path = urlparse(handler.path).path
            if method == "GET" and path == "/api/item-whitelist":
                self._write_json(handler, 200, self.store.describe())
                return
            if method == "PUT" and path == "/api/item-whitelist":
                payload = self._read_json(handler)
                item_ids = payload.get("item_ids")
                if not isinstance(item_ids, list):
                    self._write_json(handler, 400, {"error": "item_ids must be a list"})
                    return
                self.store.replace_items(item_ids)
                self._write_json(handler, 200, self.store.describe())
                return
            if method == "POST" and path == "/api/item-whitelist/items":
                payload = self._read_json(handler)
                item_id = payload.get("item_id")
                if not self._is_valid_item_id(item_id):
                    self._write_json(handler, 400, {"error": "invalid item_id"})
                    return
                self.store.add_item(item_id)
                self._write_json(handler, 200, self.store.describe())
                return
            if method == "DELETE" and path.startswith("/api/item-whitelist/items/"):
                item_id = unquote(path.rsplit("/", 1)[-1])
                if not self._is_valid_item_id(item_id):
                    self._write_json(handler, 400, {"error": "invalid item_id"})
                    return
                self.store.remove_item(item_id)
                self._write_json(handler, 200, self.store.describe())
                return

            self._write_json(handler, 404, {"error": "not_found"})
        except ValueError as exc:
            self._write_json(handler, 400, {"error": str(exc)})
        except Exception as exc:
            logger.error(f"商品白名单管理接口处理失败: {exc}")
            self._write_json(handler, 500, {"error": "internal_error"})

    def _is_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        auth = handler.headers.get("Authorization", "")
        return bool(self.bearer_token) and auth == f"Bearer {self.bearer_token}"

    def _read_json(self, handler: BaseHTTPRequestHandler):
        content_length = int(handler.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            return {}
        raw = handler.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, handler: BaseHTTPRequestHandler, status_code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _is_valid_item_id(self, item_id) -> bool:
        if not isinstance(item_id, str):
            return False
        return ITEM_ID_PATTERN.fullmatch(item_id.strip()) is not None
