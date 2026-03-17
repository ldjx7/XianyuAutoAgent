from typing import Any, Callable, Dict, Iterable, Optional

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback for minimal test env
    import logging

    logger = logging.getLogger(__name__)

from core.models import Action


class ActionExecutor:
    def __init__(
        self,
        send_msg_func: Optional[Callable[..., Any]],
        set_manual_mode_func: Optional[Callable[[str, bool], None]],
        send_image_func: Optional[Callable[..., Any]] = None,
        render_message_func: Optional[Callable[..., Any]] = None,
        track_async_task_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ):
        self.send_msg_func = send_msg_func
        self.send_image_func = send_image_func
        self.render_message_func = render_message_func
        self.set_manual_mode_func = set_manual_mode_func
        self.track_async_task_func = track_async_task_func

    def _looks_like_login_link(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            token in lowered
            for token in (
                "passport-tv-login",
                "auth_code=",
                "/qr/status",
                "/qr/image",
                "bilibili.com",
            )
        )

    def _build_login_link_text(self, text: str) -> str:
        stripped = text.strip()
        if any(keyword in stripped for keyword in ("登录", "扫码", "二维码", "备用")):
            return stripped
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return f"如果二维码不方便扫码，请打开下面这个备用登录链接完成登录，登录成功后系统会继续处理：\n{stripped}"
        if "http://" in stripped or "https://" in stripped:
            return f"如果二维码不方便扫码，请使用下面这个备用登录链接完成登录，登录成功后系统会继续处理：\n{stripped}"
        return stripped

    def _build_send_image_text(self, payload: Dict[str, Any]) -> Optional[str]:
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        image_url = payload.get("image_url")
        if isinstance(image_url, str) and self._looks_like_login_link(image_url):
            return "请扫码完成登录，登录成功后系统会自动继续处理。"
        return None

    def _build_send_image_fallback_text(self, payload: Dict[str, Any]) -> str:
        fallback_text = payload.get("fallback_text")
        if isinstance(fallback_text, str) and fallback_text.strip():
            return fallback_text.strip()

        text = self._build_send_image_text(payload)
        if isinstance(text, str) and text:
            return text

        image_url = payload.get("image_url")
        if isinstance(image_url, str) and self._looks_like_login_link(image_url):
            return "二维码未能正常发送，请使用备用登录链接完成登录。"
        return "请打开二维码图片完成扫码登录"

    def _build_send_text_text(self, payload: Dict[str, Any]) -> Optional[str]:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        stripped = text.strip()
        if self._looks_like_login_link(stripped):
            return self._build_login_link_text(stripped)
        return stripped

    async def execute(self, actions: Iterable[Action], context: Optional[Dict[str, Any]] = None) -> None:
        runtime = context or {}
        for action in actions:
            await self._execute_one(action, runtime)

    async def _execute_one(self, action: Action, runtime: Dict[str, Any]) -> None:
        if action.action_type == "send_text":
            await self._handle_send_text(action.payload, runtime)
            return
        if action.action_type == "send_image":
            await self._handle_send_image(action.payload, runtime)
            return
        if action.action_type == "render_message":
            await self._handle_render_message(action.payload, runtime)
            return
        if action.action_type == "set_manual_mode":
            self._handle_set_manual_mode(action.payload)
            return
        if action.action_type == "track_async_task":
            self._handle_track_async_task(action.payload)
            return
        logger.warning(f"unknown action_type={action.action_type}, ignored")

    async def _handle_send_text(self, payload: Dict[str, Any], runtime: Dict[str, Any]) -> None:
        if self.send_msg_func is None:
            logger.warning("send_msg_func is not configured, skip send_text")
            return

        websocket = runtime.get("websocket")
        chat_id = payload.get("chat_id")
        to_user_id = payload.get("to_user_id")
        text = self._build_send_text_text(payload)
        if websocket is None:
            logger.warning("websocket is missing in runtime context, skip send_text")
            return
        if not all(isinstance(v, str) and v for v in [chat_id, to_user_id, text]):
            logger.warning(f"invalid send_text payload={payload}")
            return
        await self.send_msg_func(websocket, chat_id, to_user_id, text)

    async def _handle_send_image(self, payload: Dict[str, Any], runtime: Dict[str, Any]) -> None:
        websocket = runtime.get("websocket")
        chat_id = payload.get("chat_id")
        to_user_id = payload.get("to_user_id")
        image_url = payload.get("image_url")
        text = self._build_send_image_text(payload)

        if websocket is None:
            logger.warning("websocket is missing in runtime context, skip send_image")
            return
        if not all(isinstance(v, str) and v for v in [chat_id, to_user_id, image_url]):
            logger.warning(f"invalid send_image payload={payload}")
            return

        if self.send_image_func is not None:
            try:
                await self.send_image_func(websocket, chat_id, to_user_id, image_url, text)
            except Exception as exc:
                logger.warning(f"send_image failed, continue with fallback actions: {exc}")
                fallback_text = self._build_send_image_fallback_text(payload)
                if self.send_msg_func is not None and isinstance(fallback_text, str) and fallback_text.strip():
                    try:
                        await self.send_msg_func(websocket, chat_id, to_user_id, fallback_text.strip())
                    except Exception as fallback_exc:
                        logger.warning(f"send_image fallback text failed: {fallback_exc}")
            return

        if self.send_msg_func is None:
            logger.warning("send_msg_func is not configured, skip send_image fallback")
            return

        fallback_text = self._build_send_image_fallback_text(payload)
        await self.send_msg_func(websocket, chat_id, to_user_id, f"{fallback_text}\n{image_url}")

    async def _handle_render_message(self, payload: Dict[str, Any], runtime: Dict[str, Any]) -> None:
        if self.render_message_func is None:
            logger.warning("render_message_func is not configured, skip render_message")
            return

        websocket = runtime.get("websocket")
        if websocket is None:
            logger.warning("websocket is missing in runtime context, skip render_message")
            return

        chat_id = payload.get("chat_id")
        to_user_id = payload.get("to_user_id")
        if not all(isinstance(v, str) and v for v in [chat_id, to_user_id]):
            logger.warning(f"invalid render_message payload={payload}")
            return

        await self.render_message_func(websocket, payload)

    def _handle_set_manual_mode(self, payload: Dict[str, Any]) -> None:
        if self.set_manual_mode_func is None:
            logger.warning("set_manual_mode_func is not configured, skip set_manual_mode")
            return
        chat_id = payload.get("chat_id")
        enabled = payload.get("enabled")
        if not isinstance(chat_id, str) or not chat_id:
            logger.warning(f"invalid set_manual_mode payload={payload}")
            return
        self.set_manual_mode_func(chat_id, bool(enabled))

    def _handle_track_async_task(self, payload: Dict[str, Any]) -> None:
        if self.track_async_task_func is None:
            logger.warning("track_async_task_func is not configured, skip track_async_task")
            return
        if not isinstance(payload, dict):
            logger.warning(f"invalid track_async_task payload={payload}")
            return
        task_id = payload.get("task_id")
        chat_id = payload.get("chat_id")
        to_user_id = payload.get("to_user_id")
        status_url = payload.get("status_url")
        if not all(isinstance(v, str) and v for v in [task_id, chat_id, to_user_id, status_url]):
            logger.warning(f"invalid track_async_task payload={payload}")
            return
        self.track_async_task_func(payload)
