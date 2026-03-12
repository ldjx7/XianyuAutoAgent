import base64
import json
import asyncio
import inspect
import time
import os
import math
from urllib.parse import unquote, urlparse

import requests
import websockets
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import RiskControlError, XianyuApis
import sys


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager
from core.action_executor import ActionExecutor
from core.async_task_poller import AsyncTaskPoller
from core.async_task_store import AsyncTaskStore
from core.event_dedup import EventDedupStore
from core.event_parser import parse_events
from core.handlers.base import EventHandler
from core.handlers.order_route_handler import OrderRouteHandler
from core.handlers.registry import load_handlers_from_env
from core.item_whitelist import ItemWhitelistStore
from core.item_whitelist_api import ItemWhitelistApiServer
from core.models import Action, Event


class ChatAutoReplyHandler(EventHandler):
    name = "chat_auto_reply"

    def __init__(self, live):
        self.live = live
        self.enabled = os.getenv("CHAT_AUTO_REPLY_ENABLED", "true").lower() == "true"

    async def handle(self, event: Event):
        if not self.enabled or event.event_type != "chat.message.received":
            return []
        return await self.live.handle_chat_event(event)


class XianyuLive:
    def __init__(self, cookies_str):
        self.xianyu = XianyuApis()
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.context_manager = ChatContextManager()
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.proactive_token_refresh_enabled = os.getenv("PROACTIVE_TOKEN_REFRESH_ENABLED", "false").lower() == "true"
        self.risk_control_retry_interval = int(os.getenv("RISK_CONTROL_RETRY_INTERVAL", "1800"))
        self.last_reconnect_reason = None
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timeout = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))  # 人工接管超时时间，默认1小时
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        
        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")

        # 模拟人工输入配置
        self.simulate_human_typing = os.getenv("SIMULATE_HUMAN_TYPING", "False").lower() == "true"
        self.llm_request_timeout_seconds = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "45"))
        self.async_task_poll_enabled = os.getenv("ASYNC_TASK_POLL_ENABLED", "true").lower() == "true"
        self.async_task_poll_interval = int(os.getenv("ASYNC_TASK_POLL_INTERVAL_SECONDS", "5"))
        self.ws_request_timeout_seconds = float(os.getenv("WS_REQUEST_TIMEOUT_SECONDS", "10"))
        self.auto_reply_item_whitelist_enabled = os.getenv("AUTO_REPLY_ITEM_WHITELIST_ENABLED", "false").lower() == "true"
        self.item_whitelist_store = ItemWhitelistStore(
            file_path=os.getenv("AUTO_REPLY_ITEM_WHITELIST_FILE", "data/item_whitelist.json"),
            env_item_ids=os.getenv("AUTO_REPLY_ITEM_WHITELIST", ""),
        )
        self.item_whitelist_api_server = None
        self._init_item_whitelist_api_server()
        self.async_task_poll_task = None
        self.background_tasks = set()
        self.pending_ws_requests = {}
        self.async_task_store = AsyncTaskStore(db_path=self.context_manager.get_db_path())
        self.action_executor = ActionExecutor(
            send_msg_func=self.send_msg,
            send_image_func=self.send_image,
            render_message_func=self.render_message_action,
            set_manual_mode_func=self.set_manual_mode,
            track_async_task_func=self.async_task_store.upsert_task,
        )
        self.async_task_poller = AsyncTaskPoller(self.async_task_store)
        self.reply_bot = None
        self.event_dedup_store = EventDedupStore(
            db_path=self.context_manager.get_db_path(),
            ttl_seconds=int(os.getenv("EVENT_DEDUP_TTL_SECONDS", "86400")),
        )
        self.event_handlers = self._build_event_handlers()

    def _init_item_whitelist_api_server(self):
        api_enabled = os.getenv("AUTO_REPLY_ITEM_WHITELIST_API_ENABLED", "false").lower() == "true"
        if not api_enabled:
            return

        bearer_token = os.getenv("AUTO_REPLY_ITEM_WHITELIST_API_TOKEN", "").strip()
        if not bearer_token:
            logger.warning("商品白名单管理接口已禁用：缺少 AUTO_REPLY_ITEM_WHITELIST_API_TOKEN")
            return

        host = os.getenv("AUTO_REPLY_ITEM_WHITELIST_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.getenv("AUTO_REPLY_ITEM_WHITELIST_API_PORT", "8765"))
        self.item_whitelist_api_server = ItemWhitelistApiServer(
            store=self.item_whitelist_store,
            host=host,
            port=port,
            bearer_token=bearer_token,
        )
        self.item_whitelist_api_server.start()

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = self.xianyu.get_token(self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
        except RiskControlError as exc:
            logger.error(f"Token刷新触发风控: {exc}")
            raise
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)

            except RiskControlError as exc:
                logger.error(f"Token刷新触发风控，进入退避: {exc}")
                await asyncio.sleep(max(self.risk_control_retry_interval, 60))
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def send_msg(self, ws, cid, toid, text):
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    def _normalize_request_mid(self, mid):
        if not isinstance(mid, str):
            return ""
        return mid.split(" ", 1)[0].strip()

    def _track_background_task(self, task):
        self.background_tasks.add(task)

        def _done_callback(done_task):
            self.background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(f"后台任务执行失败: {exc}")

        task.add_done_callback(_done_callback)
        return task

    async def _send_ws_request(self, ws, lwp, body=None, headers=None, timeout=None):
        request_headers = dict(headers or {})
        request_headers["mid"] = request_headers.get("mid") or generate_mid()
        request_mid = self._normalize_request_mid(request_headers["mid"])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_ws_requests[request_mid] = future
        payload = {
            "lwp": lwp,
            "headers": request_headers,
        }
        if body is not None:
            payload["body"] = body
        await ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(
                future,
                timeout=timeout if timeout is not None else self.ws_request_timeout_seconds,
            )
        finally:
            self.pending_ws_requests.pop(request_mid, None)

    def _resolve_pending_ws_request(self, message_data):
        try:
            mid = message_data.get("headers", {}).get("mid")
        except Exception:
            return False
        request_mid = self._normalize_request_mid(mid)
        if not request_mid:
            return False
        future = self.pending_ws_requests.get(request_mid)
        if future is None or future.done():
            return False
        future.set_result(message_data)
        return True

    def _extract_message_id(self, raw_message):
        if not isinstance(raw_message, dict):
            return None
        message_node = raw_message.get("1")
        if isinstance(message_node, dict):
            message_id = message_node.get("3")
            if isinstance(message_id, str) and message_id:
                return message_id
        return None

    async def mark_message_read(self, ws, message_id):
        if not isinstance(message_id, str) or not message_id:
            return
        response = await self._send_ws_request(
            ws,
            "/r/MessageStatus/read",
            body=[[message_id]],
        )
        if response.get("code") != 200:
            raise RuntimeError(f"mark_message_read failed: {response}")

    async def clear_conversation_red_point(self, ws, chat_id, message_id):
        if not all(isinstance(v, str) and v for v in [chat_id, message_id]):
            return
        response = await self._send_ws_request(
            ws,
            "/r/Conversation/clearRedPoint",
            body=[[{"cid": chat_id, "messageId": message_id}]],
        )
        if response.get("code") != 200:
            raise RuntimeError(f"clear_conversation_red_point failed: {response}")

    async def mark_message_read_and_view(self, ws, chat_id, message_id):
        if not all(isinstance(v, str) and v for v in [chat_id, message_id]):
            return
        await self.mark_message_read(ws, message_id)
        await self.clear_conversation_red_point(ws, chat_id, message_id)

    def _guess_image_extension(self, image_url, content_type):
        parsed = urlparse(image_url)
        filename = os.path.basename(parsed.path or "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
            return "jpg" if ext == "jpeg" else ext
        content_main = content_type.split(";", 1)[0].strip().lower()
        return {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/bmp": "bmp",
            "image/webp": "webp",
        }.get(content_main, "png")

    def _guess_image_filename(self, image_url, content_type):
        parsed = urlparse(image_url)
        filename = os.path.basename(parsed.path or "")
        if filename:
            return unquote(filename)
        return f"qr-image.{self._guess_image_extension(image_url, content_type)}"

    def _extract_png_dimensions(self, image_bytes):
        if len(image_bytes) < 24 or image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width = int.from_bytes(image_bytes[16:20], "big")
        height = int.from_bytes(image_bytes[20:24], "big")
        return width, height

    def _extract_gif_dimensions(self, image_bytes):
        if len(image_bytes) < 10 or image_bytes[:6] not in {b"GIF87a", b"GIF89a"}:
            return None
        width = int.from_bytes(image_bytes[6:8], "little")
        height = int.from_bytes(image_bytes[8:10], "little")
        return width, height

    def _extract_jpeg_dimensions(self, image_bytes):
        if len(image_bytes) < 4 or image_bytes[:2] != b"\xff\xd8":
            return None
        offset = 2
        while offset + 9 < len(image_bytes):
            if image_bytes[offset] != 0xFF:
                offset += 1
                continue
            marker = image_bytes[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(image_bytes):
                return None
            segment_length = int.from_bytes(image_bytes[offset:offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(image_bytes):
                return None
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(image_bytes[offset + 3:offset + 5], "big")
                width = int.from_bytes(image_bytes[offset + 5:offset + 7], "big")
                return width, height
            offset += segment_length
        return None

    def _extract_webp_dimensions(self, image_bytes):
        if len(image_bytes) < 30 or image_bytes[:4] != b"RIFF" or image_bytes[8:12] != b"WEBP":
            return None
        chunk_type = image_bytes[12:16]
        if chunk_type == b"VP8X" and len(image_bytes) >= 30:
            width = int.from_bytes(image_bytes[24:27] + b"\x00", "little") + 1
            height = int.from_bytes(image_bytes[27:30] + b"\x00", "little") + 1
            return width, height
        return None

    def _extract_image_dimensions(self, image_bytes, image_type):
        parsers = {
            "png": self._extract_png_dimensions,
            "gif": self._extract_gif_dimensions,
            "jpg": self._extract_jpeg_dimensions,
            "webp": self._extract_webp_dimensions,
        }
        parser = parsers.get(image_type)
        if parser is None:
            return 0, 0
        dimensions = parser(image_bytes)
        if not dimensions:
            return 0, 0
        return dimensions

    def _build_image_file_info(self, image_url, content_type, image_bytes):
        image_type = self._guess_image_extension(image_url, content_type)
        width, height = self._extract_image_dimensions(image_bytes, image_type)
        filename = self._guess_image_filename(image_url, content_type)
        return {
            "name": filename,
            "size": len(image_bytes),
            "type": image_type,
            "typeOrigin": image_type,
            "width": width,
            "height": height,
            "typeId": {"jpg": 0, "gif": 1, "png": 2, "bmp": 3, "webp": 29}.get(image_type, 8),
            "fileType": {"webp": 1, "png": 2, "jpg": 3, "gif": 4}.get(image_type, 2),
        }

    async def _fetch_image_bytes(self, image_url):
        response = await asyncio.to_thread(requests.get, image_url, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("image/"):
            raise ValueError(f"image_url did not return image content: {content_type}")
        return response.content, content_type

    async def send_image(self, ws, cid, toid, image_url, text=None):
        if not all(isinstance(v, str) and v for v in [cid, toid, image_url]):
            raise ValueError("invalid send_image arguments")

        image_bytes, content_type = await self._fetch_image_bytes(image_url)
        file_info = self._build_image_file_info(image_url, content_type, image_bytes)
        upload_uuid = generate_uuid()
        pre_payload = {
            "conversationId": cid,
            "type": file_info["type"],
            "fileLen": file_info["size"],
            "isInternal": False,
            "mediaIdVer": 2,
            "authType": 6,
            "expireTime": 120,
            "onlyAuth": True,
            "bizType": "impaas",
            "bizEntity": {},
        }
        if file_info["width"] and file_info["height"]:
            pre_payload["width"] = file_info["width"]
            pre_payload["height"] = file_info["height"]

        pre_response = await self._send_ws_request(ws, "/r/FileUpload/pre", body=[pre_payload])
        pre_body = pre_response.get("body") or {}
        upload_info = pre_body.get("uploadInfo")
        media_id = pre_body.get("mediaId")
        if pre_response.get("code") != 200 or not upload_info or not media_id:
            raise RuntimeError(f"image upload pre failed: {pre_response}")

        frag_len = int(pre_body.get("fragLen") or 102400)
        total_parts = max(1, math.ceil(file_info["size"] / frag_len))
        last_part_number = total_parts - 1

        for part_number in range(total_parts - 1):
            chunk = image_bytes[part_number * frag_len:(part_number + 1) * frag_len]
            frag_response = await self._send_ws_request(
                ws,
                "/r/FileUpload/frag",
                body=[{
                    "uploadInfo": upload_info,
                    "mediaId": media_id,
                    "partNumber": part_number,
                    "body": base64.b64encode(chunk).decode("utf-8"),
                }],
            )
            frag_body = frag_response.get("body") or {}
            if frag_response.get("code") != 200 or frag_body.get("uploadInfo") != upload_info:
                raise RuntimeError(f"image upload frag failed: {frag_response}")

        final_chunk = image_bytes[last_part_number * frag_len:(last_part_number + 1) * frag_len]
        ci_response = await self._send_ws_request(
            ws,
            "/r/FileUpload/ci",
            body=[{
                "conversationId": cid,
                "uploadId": upload_info,
                "mediaId": media_id,
                "partNumber": last_part_number,
                "body": base64.b64encode(final_chunk).decode("utf-8"),
                "totalPartNumber": total_parts,
                "bizType": "impaas",
                "bizEntity": {},
            }],
        )
        ci_body = ci_response.get("body") or {}
        auth_media_id = ci_body.get("authMediaId")
        if ci_response.get("code") != 200 or not auth_media_id:
            raise RuntimeError(f"image upload ci failed: {ci_response}")

        message_body = {
            "uuid": upload_uuid,
            "cid": f"{cid}@goofish",
            "conversationType": 1,
            "content": {
                "photo": {
                    "mediaId": auth_media_id,
                    "picSize": file_info["size"],
                    "type": file_info["typeId"],
                    "fileType": file_info["fileType"],
                    "orientation": 0,
                    "extension": {
                        "width": str(file_info["width"] or 0),
                        "height": str(file_info["height"] or 0),
                        "type": file_info["type"],
                        "typeOrigin": file_info["typeOrigin"],
                    },
                    "filename": file_info["name"],
                },
                "contentType": 2,
            },
            "redPointPolicy": 0,
            "extension": {
                "extJson": "{}"
            },
            "ctx": {
                "appVersion": "1.0",
                "platform": "web"
            },
            "mtags": {},
            "msgReadStatusSetting": 1,
        }
        receiver_scope = {
            "actualReceivers": [
                f"{toid}@goofish",
                f"{self.myid}@goofish",
            ]
        }
        send_response = await self._send_ws_request(
            ws,
            "/r/MessageSend/sendByReceiverScope",
            body=[message_body, receiver_scope],
        )
        if send_response.get("code") != 200:
            raise RuntimeError(f"send photo message failed: {send_response}")

        if isinstance(text, str) and text.strip():
            await self.send_msg(ws, cid, toid, text.strip())

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.manual_mode_timeout:
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"

    def set_manual_mode(self, chat_id, enabled):
        """设置人工接管模式"""
        if enabled:
            self.enter_manual_mode(chat_id)
            return "manual"
        self.exit_manual_mode(chat_id)
        return "auto"

    async def async_task_poll_loop(self):
        while True:
            try:
                loop = asyncio.get_running_loop()
                notifications = await loop.run_in_executor(None, self.async_task_poller.poll_due_tasks)
                for notification in notifications:
                    try:
                        await self.action_executor.execute(notification.actions, context={"websocket": self.ws})
                    except Exception as exc:
                        logger.error(f"异步任务动作执行失败 task_id={notification.task_id}: {exc}")
                        continue
                    self.async_task_poller.acknowledge_delivered(notification, delivered_at=int(time.time()))
            except Exception as exc:
                logger.error(f"异步任务轮询失败: {exc}")
            await asyncio.sleep(max(self.async_task_poll_interval, 1))

    def get_reconnect_delay_seconds(self):
        if self.connection_restart_flag:
            return 0
        if self.last_reconnect_reason == "risk_control" and self.risk_control_retry_interval > 0:
            return self.risk_control_retry_interval
        return 5

    def _build_event_handlers(self):
        handlers = [
            ChatAutoReplyHandler(self),
            OrderRouteHandler(item_id_resolver=self.context_manager.get_item_id_by_chat),
        ]
        external = load_handlers_from_env(os.getenv("EVENT_HANDLERS", ""))
        handlers.extend(external)
        return handlers

    async def handle_pipeline_message(self, message, websocket):
        events = parse_events(message)
        if not events:
            logger.debug("未识别到可处理事件")
            return

        for event in events:
            if isinstance(event.payload, dict):
                event.payload["websocket"] = websocket
            if self.event_dedup_store.is_duplicate(event.event_id):
                logger.info(f"重复事件已跳过: {event.event_id}")
                continue
            actions = []
            for handler in self.event_handlers:
                try:
                    produced = handler.handle(event)
                    if inspect.isawaitable(produced):
                        produced = await produced
                    produced = produced or []
                    actions.extend(produced)
                except Exception as exc:
                    logger.error(f"handler={handler.name} event={event.event_type} error={exc}")
            if actions:
                await self.action_executor.execute(actions, context={"websocket": websocket})

    async def generate_bot_reply_async(self, bot_instance, send_message, item_description, context):
        started_at = time.time()
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    bot_instance.generate_reply,
                    send_message,
                    item_description,
                    context,
                ),
                timeout=self.llm_request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error(f"LLM请求超时，已跳过本次自动回复: timeout={self.llm_request_timeout_seconds}s")
            return None

        elapsed_seconds = time.time() - started_at
        if elapsed_seconds >= 10:
            logger.warning(f"LLM回复耗时较长: {elapsed_seconds:.2f}s")
        return reply

    async def render_workflow_message_async(self, bot_instance, scene, facts, instructions, item_description, context):
        started_at = time.time()
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    bot_instance.render_workflow_message,
                    scene=scene,
                    facts=facts,
                    instructions=instructions,
                    item_desc=item_description,
                    context=context,
                ),
                timeout=self.llm_request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error(f"工作流消息渲染超时，已回退模板: timeout={self.llm_request_timeout_seconds}s")
            return None
        except Exception as exc:
            logger.error(f"工作流消息渲染失败，已回退模板: {exc}")
            return None

        elapsed_seconds = time.time() - started_at
        if elapsed_seconds >= 10:
            logger.warning(f"工作流消息渲染耗时较长: {elapsed_seconds:.2f}s")
        return reply

    def _extract_render_message_fallback(self, payload):
        fallback_text = payload.get("fallback_text")
        if isinstance(fallback_text, str) and fallback_text.strip():
            return fallback_text.strip()

        facts = payload.get("facts")
        if isinstance(facts, dict):
            for key in ("message", "notify_text", "result_message", "text", "summary"):
                value = facts.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

            result = facts.get("result")
            if isinstance(result, dict):
                for key in ("message", "notify_text", "result_message", "text", "summary"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                score = result.get("score")
                if score is not None:
                    return f"处理已完成，结果分数 {score} 分。"

            score = facts.get("score")
            if score is not None:
                return f"处理已完成，结果分数 {score} 分。"

            status = facts.get("status")
            if isinstance(status, str) and status:
                return f"当前处理状态已更新：{status}"

        return "当前订单处理状态已更新，请留意后续消息。"

    async def render_message_action(self, ws, payload):
        chat_id = payload.get("chat_id")
        to_user_id = payload.get("to_user_id")
        if not all(isinstance(v, str) and v for v in [chat_id, to_user_id]):
            logger.warning(f"invalid render_message payload={payload}")
            return

        fallback_text = self._extract_render_message_fallback(payload)
        scene = payload.get("scene") or "workflow_result"
        facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
        instructions = payload.get("instructions")

        context = self.context_manager.get_context_by_chat(chat_id)
        item_id = self.context_manager.get_item_id_by_chat(chat_id)
        item_info = self.context_manager.get_item_info(item_id) if item_id else None
        if item_info:
            item_description = f"当前商品的信息如下：{self.build_item_description(item_info)}"
        else:
            item_description = "当前商品信息未知"

        bot_instance = self.reply_bot or globals().get("bot")
        rendered_text = None
        if bot_instance is not None and hasattr(bot_instance, "render_workflow_message"):
            rendered_text = await self.render_workflow_message_async(
                bot_instance,
                scene,
                facts,
                instructions,
                item_description,
                context,
            )

        final_text = rendered_text.strip() if isinstance(rendered_text, str) and rendered_text.strip() else fallback_text
        if not isinstance(final_text, str) or not final_text.strip():
            logger.warning(f"render_message produced empty output payload={payload}")
            return

        await self.send_msg(ws, chat_id, to_user_id, final_text.strip())

    def is_auto_reply_item_allowed(self, item_id):
        if not self.auto_reply_item_whitelist_enabled:
            return True
        return self.item_whitelist_store.is_allowed(item_id)

    async def handle_chat_event(self, event):
        payload = event.payload if isinstance(event.payload, dict) else {}
        raw_message = payload.get("raw")
        raw_message = raw_message if isinstance(raw_message, dict) else {}
        websocket = payload.get("websocket")

        chat_id = payload.get("chat_id")
        send_user_id = payload.get("user_id")
        send_user_name = payload.get("sender_name", "")
        send_message = payload.get("message")
        item_id = payload.get("item_id")
        create_time = int(payload.get("created_at", event.occurred_at))

        if not all(isinstance(v, str) and v for v in [chat_id, send_user_id, send_message]):
            return []

        # 时效性验证（过滤过期消息）
        if (time.time() * 1000 - create_time) > self.message_expire_time:
            logger.debug("过期消息丢弃")
            return []

        if not item_id:
            logger.warning("无法获取商品ID")
            return []
        self.context_manager.bind_chat_item(chat_id, item_id)

        # 卖家消息：支持人工接管切换和上下文记录
        if send_user_id == self.myid:
            if self.check_toggle_keywords(send_message):
                next_manual_mode = not self.is_manual_mode(chat_id)
                mode = self.set_manual_mode(chat_id, next_manual_mode)
                if mode == "manual":
                    logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                else:
                    logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                return []
            self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", send_message)
            logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
            return []

        if not self.is_auto_reply_item_allowed(item_id):
            logger.info(f"商品 {item_id} 不在自动回复白名单中，跳过自动回复")
            return []

        logger.info(
            f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, 会话: {chat_id}, 消息: {send_message}"
        )

        message_id = self._extract_message_id(raw_message)
        if websocket is not None and message_id:
            self._track_background_task(
                asyncio.create_task(self.mark_message_read_and_view(websocket, chat_id, message_id))
            )

        if self.is_manual_mode(chat_id):
            logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，跳过自动回复")
            self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
            return []

        if self.is_bracket_system_message(send_message):
            logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
            return []
        if self.is_system_message(raw_message):
            logger.debug("系统消息，跳过处理")
            return []

        item_info = self.context_manager.get_item_info(item_id)
        if not item_info:
            logger.info(f"从API获取商品信息: {item_id}")
            api_result = await asyncio.to_thread(self.xianyu.get_item_info, item_id)
            if "data" in api_result and "itemDO" in api_result["data"]:
                item_info = api_result["data"]["itemDO"]
                self.context_manager.save_item_info(item_id, item_info)
            else:
                logger.warning(f"获取商品信息失败: {api_result}")
                return []
        else:
            logger.info(f"从数据库获取商品信息: {item_id}")

        item_description = f"当前商品的信息如下：{self.build_item_description(item_info)}"
        context = self.context_manager.get_context_by_chat(chat_id)

        bot_instance = self.reply_bot or globals().get("bot")
        if bot_instance is None:
            logger.warning("未配置回复机器人，跳过自动回复")
            return []

        bot_reply = await self.generate_bot_reply_async(
            bot_instance,
            send_message,
            item_description,
            context,
        )
        if bot_reply is None:
            logger.warning(f"会话 {chat_id} 的AI回复超时或失败，跳过自动回复")
            return []
        if bot_reply == "-":
            logger.info(f"[无需回复] 用户 {send_user_name} 的消息被识别为无需回复类型")
            return []

        self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
        if getattr(bot_instance, "last_intent", None) == "price":
            self.context_manager.increment_bargain_count_by_chat(chat_id)
            bargain_count = self.context_manager.get_bargain_count_by_chat(chat_id)
            logger.info(f"用户 {send_user_name} 对商品 {item_id} 的议价次数: {bargain_count}")
        self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", bot_reply)

        logger.info(f"机器人回复: {bot_reply}")
        return [
            Action(
                action_type="send_text",
                payload={"chat_id": chat_id, "to_user_id": send_user_id, "text": bot_reply},
            )
        ]
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:
            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            await self.handle_pipeline_message(message, websocket)
            
        except Exception as e:
            logger.error(f"处理消息时发生错误: {str(e)}")
            logger.debug(f"原始消息: {message_data}")

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def main(self):
        while True:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                self.last_reconnect_reason = None
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    if self.proactive_token_refresh_enabled:
                        self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())

                    if self.async_task_poll_enabled:
                        self.async_task_poll_task = asyncio.create_task(self.async_task_poll_loop())
                    
                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))

                            if self._resolve_pending_ws_request(message_data):
                                continue
                            
                            # 处理其他消息
                            self._track_background_task(
                                asyncio.create_task(self.handle_message(message_data, websocket))
                            )
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except RiskControlError as exc:
                self.last_reconnect_reason = "risk_control"
                logger.error(f"连接过程触发风控，等待 {self.risk_control_retry_interval} 秒后重试: {exc}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                
            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                
            finally:
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass

                if self.async_task_poll_task:
                    self.async_task_poll_task.cancel()
                    try:
                        await self.async_task_poll_task
                    except asyncio.CancelledError:
                        pass
                    self.async_task_poll_task = None

                if self.background_tasks:
                    for task in list(self.background_tasks):
                        task.cancel()
                    await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
                    self.background_tasks.clear()

                # 如果是主动重启，立即重连；否则等待5秒
                delay = self.get_reconnect_delay_seconds()
                if delay == 0:
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info(f"等待{delay}秒后重连...")
                    await asyncio.sleep(delay)



def check_and_complete_env():
    """检查并补全关键环境变量"""
    # 定义关键变量及其默认无效值（占位符）
    critical_vars = {
        "API_KEY": "默认使用通义千问,apikey通过百炼模型平台获取",
        "COOKIES_STR": "your_cookies_here"
    }
    
    env_path = ".env"
    updated = False
    
    for key, placeholder in critical_vars.items():
        curr_val = os.getenv(key)
        
        # 如果变量未设置，或者值等于占位符
        if not curr_val or curr_val == placeholder:
            logger.warning(f"配置项 [{key}] 未设置或为默认值，请输入")
            while True:
                val = input(f"请输入 {key}: ").strip()
                if val:
                    # 更新当前环境
                    os.environ[key] = val
                    
                    # 尝试持久化到 .env
                    try:
                        # 如果没有.env文件，先创建
                        if not os.path.exists(env_path):
                            with open(env_path, 'w', encoding='utf-8') as f:
                                pass # Create empty file
                        
                        set_key(env_path, key, val)
                        updated = True
                    except Exception as e:
                        logger.warning(f"无法自动写入.env文件，请手动保存: {e}")
                    break
                else:
                    print(f"{key} 不能为空，请重新输入")
    
    if updated:
        logger.info("新的配置已保存/更新至 .env 文件中")


if __name__ == '__main__':
    # 加载环境变量
    if os.path.exists(".env"):
        load_dotenv()
        logger.info("已加载 .env 配置")
    
    if os.path.exists(".env.example"):
        load_dotenv(".env.example")  # 不会覆盖已存在的变量
        logger.info("已加载 .env.example 默认配置")
    
    # 配置日志级别
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    logger.info(f"日志级别设置为: {log_level}")
    
    # 交互式检查并补全配置
    check_and_complete_env()
    
    cookies_str = os.getenv("COOKIES_STR")
    bot = XianyuReplyBot()
    xianyuLive = XianyuLive(cookies_str)
    xianyuLive.reply_bot = bot
    # 常驻进程
    asyncio.run(xianyuLive.main())
