import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TelegramError, Forbidden, BadRequest
import io
import asyncio
import csv
import random
import string
import json
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import logging
from pathlib import Path
import time
import re
from functools import wraps
from collections import defaultdict
import hashlib

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== 配置 - 使用环境变量 ==========
TOKEN = os.getenv("BOT_TOKEN", "8415726738:AAEoIicoiRZSSRSpYNIeTVVshrIiKOepmhg")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@APl57")
ADMIN_USER_IDS = [int(id) for id in os.getenv("ADMIN_USER_IDS", "7002638062").split(",")]
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cnzongheng2026")
DB_PATH = os.getenv("DB_PATH", "submissions.db")
BACKUP_PATH = os.getenv("BACKUP_PATH", "backups")
MEDIA_PATH = os.getenv("MEDIA_PATH", "media")

# 创建必要目录
Path(BACKUP_PATH).mkdir(exist_ok=True)
Path(MEDIA_PATH).mkdir(exist_ok=True)

# 验证必需配置
if not TOKEN:
    raise ValueError("请在环境变量中设置 BOT_TOKEN")

# 速率限制配置
RATE_LIMIT_CALLS = 15
RATE_LIMIT_WINDOW = 60

# ========== 数据模型 ==========
@dataclass
class Submission:
    id: str
    user_id: int
    username: str
    first_name: str
    draft_name: str
    text_content: str
    media_type: str
    media_file_id: str
    media_local_path: str
    is_anonymous: int
    status: str
    submit_time: str
    review_time: Optional[str] = None
    reviewer_id: Optional[int] = None
    reject_reason: Optional[str] = None
    publish_message_id: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_row(cls, row):
        data = dict(row)
        return cls(**data)

@dataclass
class User:
    user_id: int
    username: str
    first_name: str
    last_active: str
    total_submissions: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    is_admin: bool = False
    join_date: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)

@dataclass
class DraftState:
    sub_id: str
    action: str
    page: int = 0
    temp_data: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.temp_data is None:
            self.temp_data = {}

# ========== 速率限制器 ==========
class RateLimiter:
    def __init__(self, max_calls: int = 10, time_window: int = 60):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = defaultdict(list)
    
    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        user_calls = self.calls[user_id]
        user_calls = [t for t in user_calls if now - t < self.time_window]
        if len(user_calls) >= self.max_calls:
            return False
        user_calls.append(now)
        self.calls[user_id] = user_calls
        return True
    
    def get_remaining(self, user_id: int) -> int:
        now = time.time()
        user_calls = self.calls[user_id]
        user_calls = [t for t in user_calls if now - t < self.time_window]
        return max(0, self.max_calls - len(user_calls))

rate_limiter = RateLimiter(max_calls=RATE_LIMIT_CALLS, time_window=RATE_LIMIT_WINDOW)

def rate_limit(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user:
            user_id = update.effective_user.id
            if not rate_limiter.is_allowed(user_id):
                remaining = rate_limiter.get_remaining(user_id)
                if update.effective_message:
                    await update.effective_message.reply_text(
                        f"❌ 操作过于频繁，请稍后再试\n剩余次数：{remaining}/分钟"
                    )
                return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========== 数据库管理 ==========
class DatabaseManager:
    def __init__(self, db_path: str = "submissions.db"):
        self.db_path = db_path
        self.init_db()
        self.migrate_db()
    
    @contextmanager
    def get_connection(self):
        conn = None
        for attempt in range(3):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as e:
                if attempt == 2:
                    raise
                logger.warning(f"数据库连接失败，重试 {attempt + 1}/3: {e}")
                time.sleep(0.1)
        
        try:
            yield conn
            conn.commit()
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_active TEXT,
                total_submissions INTEGER DEFAULT 0,
                approved_count INTEGER DEFAULT 0,
                rejected_count INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                join_date TEXT
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                draft_name TEXT DEFAULT '未命名',
                text_content TEXT DEFAULT '',
                media_type TEXT DEFAULT '',
                media_file_id TEXT DEFAULT '',
                media_local_path TEXT DEFAULT '',
                is_anonymous INTEGER DEFAULT 0,
                status TEXT DEFAULT 'editing',
                submit_time TEXT,
                review_time TEXT,
                reviewer_id INTEGER,
                reject_reason TEXT,
                publish_message_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id TEXT,
                tag TEXT,
                FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS review_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id TEXT,
                action TEXT,
                reviewer_id INTEGER,
                reason TEXT,
                timestamp TEXT,
                FOREIGN KEY (submission_id) REFERENCES submissions(id)
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS statistics (
                date TEXT PRIMARY KEY,
                total_submissions INTEGER DEFAULT 0,
                approved_submissions INTEGER DEFAULT 0,
                rejected_submissions INTEGER DEFAULT 0,
                new_users INTEGER DEFAULT 0,
                active_users INTEGER DEFAULT 0
            )''')
            
            c.execute('CREATE INDEX IF NOT EXISTS idx_submissions_user_id ON submissions(user_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_submissions_submit_time ON submissions(submit_time)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_tags_submission_id ON tags(submission_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_review_logs_submission_id ON review_logs(submission_id)')
    
    def migrate_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            
            c.execute("PRAGMA table_info(submissions)")
            existing_sub_columns = [col[1] for col in c.fetchall()]
            
            if "media_local_path" not in existing_sub_columns:
                try:
                    c.execute("ALTER TABLE submissions ADD COLUMN media_local_path TEXT DEFAULT ''")
                    logger.info("添加 media_local_path 列到 submissions 表")
                except Exception as e:
                    logger.warning(f"添加 media_local_path 列失败: {e}")
            
            sub_required_columns = {
                "draft_name": "TEXT DEFAULT '未命名'",
                "text_content": "TEXT DEFAULT ''",
                "media_type": "TEXT DEFAULT ''",
                "media_file_id": "TEXT DEFAULT ''",
                "is_anonymous": "INTEGER DEFAULT 0",
                "review_time": "TEXT",
                "reviewer_id": "INTEGER",
                "reject_reason": "TEXT",
                "publish_message_id": "TEXT",
                "submit_time": "TEXT"
            }
            
            for column, col_type in sub_required_columns.items():
                if column not in existing_sub_columns:
                    try:
                        c.execute(f"ALTER TABLE submissions ADD COLUMN {column} {col_type}")
                        logger.info(f"添加列 {column} 到 submissions 表")
                    except Exception as e:
                        logger.warning(f"添加列 {column} 失败: {e}")
            
            c.execute("PRAGMA table_info(users)")
            existing_user_columns = [col[1] for col in c.fetchall()]
            
            user_required_columns = {
                "total_submissions": "INTEGER DEFAULT 0",
                "approved_count": "INTEGER DEFAULT 0",
                "rejected_count": "INTEGER DEFAULT 0",
                "is_admin": "INTEGER DEFAULT 0",
                "join_date": "TEXT"
            }
            
            for column, col_type in user_required_columns.items():
                if column not in existing_user_columns:
                    try:
                        c.execute(f"ALTER TABLE users ADD COLUMN {column} {col_type}")
                        logger.info(f"添加列 {column} 到 users 表")
                    except Exception as e:
                        logger.warning(f"添加列 {column} 失败: {e}")
    
    def add_or_update_user(self, user_id: int, username: str, first_name: str):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            existing = c.fetchone()
            
            if existing:
                c.execute(
                    """UPDATE users 
                    SET username = ?, first_name = ?, last_active = ?
                    WHERE user_id = ?""",
                    (username or "无", first_name or "无", datetime.now().isoformat(), user_id)
                )
            else:
                c.execute(
                    """INSERT INTO users (user_id, username, first_name, last_active, join_date, is_admin)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, username or "无", first_name or "无", 
                     datetime.now().isoformat(), datetime.now().isoformat(),
                     1 if user_id in ADMIN_USER_IDS else 0)
                )
    
    def get_user(self, user_id: int) -> Optional[User]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            if row:
                return User(**dict(row))
            return None
    
    def get_all_users(self, limit: int = 50, offset: int = 0) -> List[User]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users ORDER BY last_active DESC LIMIT ? OFFSET ?", 
                     (limit, offset))
            return [User(**dict(row)) for row in c.fetchall()]
    
    def update_user_stats(self, user_id: int):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT status, COUNT(*) FROM submissions WHERE user_id = ? GROUP BY status", (user_id,))
            rows = c.fetchall()
            
            stats = {}
            for row in rows:
                stats[row[0]] = row[1]
            
            total = sum(stats.values())
            approved = stats.get('approved', 0)
            rejected = stats.get('rejected', 0)
            
            c.execute(
                """UPDATE users 
                SET total_submissions = ?, approved_count = ?, rejected_count = ?
                WHERE user_id = ?""",
                (total, approved, rejected, user_id)
            )
    
    def create_submission(self, user_id: int, username: str, first_name: str) -> Submission:
        sub_id = self.generate_sub_id()
        
        submission = Submission(
            id=sub_id,
            user_id=user_id,
            username=username or "无",
            first_name=first_name or "无",
            draft_name="未命名",
            text_content="",
            media_type="",
            media_file_id="",
            media_local_path="",
            is_anonymous=0,
            status="editing",
            submit_time=datetime.now().isoformat()
        )
        
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO submissions 
                (id, user_id, username, first_name, draft_name, text_content, 
                media_type, media_file_id, media_local_path, is_anonymous, status, submit_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (submission.id, submission.user_id, submission.username, 
                 submission.first_name, submission.draft_name, submission.text_content,
                 submission.media_type, submission.media_file_id, submission.media_local_path,
                 submission.is_anonymous, submission.status, submission.submit_time)
            )
        
        return submission
    
    def get_submission(self, sub_id: str) -> Optional[Submission]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,))
            row = c.fetchone()
            if row:
                return Submission.from_row(row)
            return None
    
    def update_submission(self, sub_id: str, **kwargs):
        allowed_fields = [
            'draft_name', 'text_content', 'media_type', 'media_file_id', 'media_local_path',
            'is_anonymous', 'status', 'review_time', 'reviewer_id',
            'reject_reason', 'publish_message_id', 'submit_time'
        ]
        
        update_data = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not update_data:
            return
        
        set_clause = ", ".join(f"{k} = ?" for k in update_data.keys())
        values = list(update_data.values()) + [sub_id]
        
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE submissions SET {set_clause} WHERE id = ?", values)
    
    def delete_submission(self, sub_id: str):
        sub = self.get_submission(sub_id)
        if sub and sub.media_local_path:
            try:
                Path(sub.media_local_path).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"删除媒体文件失败: {e}")
        
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM tags WHERE submission_id = ?", (sub_id,))
            c.execute("DELETE FROM review_logs WHERE submission_id = ?", (sub_id,))
            c.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
    
    def get_user_submissions(self, user_id: int, limit: int = 50, offset: int = 0) -> List[Tuple]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                """SELECT id, draft_name, status, submit_time, 
                CASE WHEN media_file_id != '' AND media_file_id IS NOT NULL THEN 1 ELSE 0 END as has_media
                FROM submissions 
                WHERE user_id = ? 
                ORDER BY submit_time DESC 
                LIMIT ? OFFSET ?""",
                (user_id, limit, offset)
            )
            return c.fetchall()
    
    def get_user_submission_count(self, user_id: int) -> int:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM submissions WHERE user_id = ?", (user_id,))
            return c.fetchone()[0]
    
    def get_pending_submissions(self) -> List[Submission]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM submissions WHERE status = 'pending' ORDER BY submit_time ASC")
            return [Submission.from_row(row) for row in c.fetchall()]
    
    def get_submissions_by_status(self, status: str) -> List[Submission]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM submissions WHERE status = ? ORDER BY submit_time DESC", (status,))
            return [Submission.from_row(row) for row in c.fetchall()]
    
    def get_all_submissions(self, limit: int = 100, offset: int = 0) -> List[Submission]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM submissions ORDER BY submit_time DESC LIMIT ? OFFSET ?", 
                     (limit, offset))
            return [Submission.from_row(row) for row in c.fetchall()]
    
    def get_submission_count(self, status: str = None) -> int:
        with self.get_connection() as conn:
            c = conn.cursor()
            if status:
                c.execute("SELECT COUNT(*) FROM submissions WHERE status = ?", (status,))
            else:
                c.execute("SELECT COUNT(*) FROM submissions")
            return c.fetchone()[0]
    
    def add_tag(self, submission_id: str, tag: str):
        with self.get_connection() as conn:
            c = conn.cursor()
            try:
                c.execute("INSERT INTO tags (submission_id, tag) VALUES (?, ?)", 
                         (submission_id, tag))
            except sqlite3.IntegrityError:
                pass
    
    def get_tags(self, submission_id: str) -> List[str]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT tag FROM tags WHERE submission_id = ?", (submission_id,))
            return [row[0] for row in c.fetchall()]
    
    def remove_tag(self, submission_id: str, tag: str):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM tags WHERE submission_id = ? AND tag = ?", 
                     (submission_id, tag))
    
    def add_review_log(self, submission_id: str, action: str, reviewer_id: int, reason: str = ""):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO review_logs (submission_id, action, reviewer_id, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)""",
                (submission_id, action, reviewer_id, reason, datetime.now().isoformat())
            )
    
    def get_review_logs(self, submission_id: str) -> List[Dict]:
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM review_logs WHERE submission_id = ? ORDER BY timestamp DESC",
                (submission_id,)
            )
            return [dict(row) for row in c.fetchall()]
    
    def get_statistics(self, days: int = 7) -> Dict:
        with self.get_connection() as conn:
            c = conn.cursor()
            
            today = datetime.now().date()
            start_date = today - timedelta(days=days-1)
            
            stats = {
                "total_users": 0,
                "active_users": 0,
                "total_submissions": 0,
                "pending_submissions": 0,
                "approved_submissions": 0,
                "rejected_submissions": 0,
                "daily_stats": []
            }
            
            c.execute("SELECT COUNT(*) FROM users")
            stats["total_users"] = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", 
                     (start_date.isoformat(),))
            stats["active_users"] = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions")
            stats["total_submissions"] = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE status = 'pending'")
            stats["pending_submissions"] = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE status = 'approved'")
            stats["approved_submissions"] = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE status = 'rejected'")
            stats["rejected_submissions"] = c.fetchone()[0]
            
            for i in range(days):
                date = start_date + timedelta(days=i)
                date_str = date.isoformat()
                
                c.execute("SELECT COUNT(*) FROM submissions WHERE date(submit_time) = ?", (date_str,))
                daily_subs = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM submissions WHERE date(submit_time) = ? AND status = 'approved'", 
                         (date_str,))
                daily_approved = c.fetchone()[0]
                
                stats["daily_stats"].append({
                    "date": date_str,
                    "submissions": daily_subs,
                    "approved": daily_approved
                })
            
            return stats
    
    def update_daily_statistics(self):
        today = datetime.now().date().isoformat()
        
        with self.get_connection() as conn:
            c = conn.cursor()
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE date(submit_time) = ?", (today,))
            total = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE date(submit_time) = ? AND status = 'approved'", 
                     (today,))
            approved = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM submissions WHERE date(submit_time) = ? AND status = 'rejected'", 
                     (today,))
            rejected = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM users WHERE date(join_date) = ?", (today,))
            new_users = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM users WHERE date(last_active) = ?", (today,))
            active_users = c.fetchone()[0]
            
            c.execute(
                """INSERT OR REPLACE INTO statistics 
                (date, total_submissions, approved_submissions, rejected_submissions, new_users, active_users)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (today, total, approved, rejected, new_users, active_users)
            )
    
    def backup_database(self) -> str:
        backup_file = Path(BACKUP_PATH) / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        
        with self.get_connection() as conn:
            backup = sqlite3.connect(str(backup_file))
            conn.backup(backup)
            backup.close()
        
        return str(backup_file)
    
    def export_to_csv(self, filename: str = None) -> str:
        if not filename:
            filename = Path(BACKUP_PATH) / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM submissions ORDER BY submit_time DESC")
            rows = c.fetchall()
            
            with open(filename, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([desc[0] for desc in c.description])
                writer.writerows(rows)
        
        return str(filename)
    
    @staticmethod
    def generate_sub_id() -> str:
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        return f"I-kD{int(datetime.now().timestamp())}{rand_str}"

db = DatabaseManager(DB_PATH)

# ========== 媒体文件管理 ==========
async def download_media_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, media_type: str, sub_id: str) -> str:
    try:
        file = await context.bot.get_file(file_id)
        
        ext_map = {
            "photo": ".jpg",
            "video": ".mp4",
            "document": "",
            "audio": ".mp3"
        }
        ext = ext_map.get(media_type, "")
        
        if media_type == "document" and not ext:
            ext = ".bin"
        
        filename = f"{sub_id}_{int(time.time())}{ext}"
        file_path = Path(MEDIA_PATH) / filename
        
        await file.download_to_drive(file_path)
        
        logger.info(f"媒体文件已保存: {file_path}")
        return str(file_path)
    except Exception as e:
        logger.error(f"下载媒体文件失败: {e}")
        return ""

# ========== 状态管理 ==========
class StateManager:
    def __init__(self):
        self._states: Dict[int, Dict] = {}
        self._lock = asyncio.Lock()
        self._timeout = 1800
    
    async def set_state(self, user_id: int, state: DraftState):
        async with self._lock:
            self._states[user_id] = {
                'state': state,
                'timestamp': datetime.now()
            }
    
    async def get_state(self, user_id: int) -> Optional[DraftState]:
        async with self._lock:
            data = self._states.get(user_id)
            if data:
                if (datetime.now() - data['timestamp']).seconds > self._timeout:
                    del self._states[user_id]
                    return None
                return data['state']
            return None
    
    async def clear_state(self, user_id: int):
        async with self._lock:
            self._states.pop(user_id, None)
    
    async def cleanup_expired(self):
        async with self._lock:
            current_time = datetime.now()
            expired = [
                uid for uid, data in self._states.items()
                if (current_time - data['timestamp']).seconds > self._timeout
            ]
            for uid in expired:
                del self._states[uid]

state_manager = StateManager()

# ========== 工具函数 ==========
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

def validate_draft_name(name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "稿件名称不能为空"
    if len(name) < 2 or len(name) > 50:
        return False, "稿件名称长度应在2-50个字符之间"
    if re.search(r'[<>"/\\|?*]', name):
        return False, "稿件名称不能包含特殊字符: < > \" / \\ | ? *"
    return True, ""

def validate_text_content(text: str) -> tuple[bool, str]:
    if len(text) > 5000:
        return False, "文本内容不能超过5000字"
    sensitive_words = ["赌博", "毒品", "色情", "暴力"]
    for word in sensitive_words:
        if word in text:
            return False, f"文本内容包含敏感词: {word}"
    return True, ""

def format_submission_preview(sub: Submission) -> str:
    status_emoji = {
        "editing": "✏️",
        "pending": "⏳",
        "approved": "✅",
        "rejected": "❌"
    }
    
    status_text = {
        "editing": "编辑中",
        "pending": "待审核",
        "approved": "已通过",
        "rejected": "已拒绝"
    }
    
    emoji = status_emoji.get(sub.status, "📝")
    s_text = status_text.get(sub.status, sub.status)
    
    text = f"{emoji} 稿件编辑 ({sub.id})\n\n"
    text += f"📝 稿件名字: {sub.draft_name}\n"
    text += f"🖼️ 媒体文件: {'✅ 已上传' if sub.media_file_id else '❌ 未上传'}"
    if sub.media_type:
        text += f" ({sub.media_type})"
    text += "\n"
    text += f"📄 文本内容: {'✅ 已填写' if sub.text_content else '❌ 未填写'}"
    if sub.text_content:
        text += f" ({len(sub.text_content)}字)"
    text += "\n"
    text += f"👤 匿名投稿: {'✅ 是' if sub.is_anonymous else '❌ 否'}\n"
    text += f"📊 当前状态: {s_text}\n"
    
    if sub.status == "rejected" and sub.reject_reason:
        text += f"❌ 拒绝原因: {sub.reject_reason[:100]}\n"
    
    return text

def get_submission_keyboard(sub: Submission) -> InlineKeyboardMarkup:
    sub_id = sub.id
    
    if sub.status == "editing":
        keyboard = [
            [InlineKeyboardButton("✏️ 编辑名字", callback_data=f"edit_name_{sub_id}")],
            [InlineKeyboardButton("📝 编辑文本", callback_data=f"edit_text_{sub_id}"),
             InlineKeyboardButton("🖼️ 上传媒体", callback_data=f"edit_media_{sub_id}")],
            [InlineKeyboardButton("👤 匿名设置", callback_data=f"toggle_anonymous_{sub_id}"),
             InlineKeyboardButton("🏷️ 添加标签", callback_data=f"add_tag_{sub_id}")],
            [InlineKeyboardButton("👁️ 预览稿件", callback_data=f"preview_{sub_id}")],
            [InlineKeyboardButton("📤 提交审核", callback_data=f"submit_review_{sub_id}")],
            [InlineKeyboardButton("🗑️ 删除稿件", callback_data=f"delete_draft_{sub_id}")],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]
        ]
    elif sub.status == "pending":
        keyboard = [
            [InlineKeyboardButton("👁️ 查看稿件", callback_data=f"preview_{sub_id}")],
            [InlineKeyboardButton("❌ 撤回投稿", callback_data=f"withdraw_{sub_id}")],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]
        ]
    elif sub.status in ["approved", "rejected"]:
        keyboard = [
            [InlineKeyboardButton("👁️ 查看稿件", callback_data=f"preview_{sub_id}")],
            [InlineKeyboardButton("📝 重新编辑", callback_data=f"reedit_{sub_id}")],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]
        ]
    else:
        keyboard = [[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]]
    
    return InlineKeyboardMarkup(keyboard)

def get_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📝 我要投稿", callback_data="new_draft")],
        [InlineKeyboardButton("📋 我的稿件", callback_data="my_submissions")],
        [InlineKeyboardButton("📊 个人统计", callback_data="my_stats")],
        [InlineKeyboardButton("📢 订阅频道", url=f"https://t.me/{TARGET_CHANNEL.replace('@', '')}")],
    ]
    
    if is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton("⚙️ 管理面板", callback_data="admin_panel"),
            InlineKeyboardButton("📊 数据统计", callback_data="admin_stats")
        ])
    
    return InlineKeyboardMarkup(keyboard)

# ========== 基础命令 ==========
@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != 'private':
        return
    
    user = update.effective_user
    db.add_or_update_user(user.id, user.username, user.first_name)
    await show_main_menu(update, context)

@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != 'private':
        return
    
    user = update.effective_user
    
    text = "📚 投稿机器人使用帮助\n\n"
    text += "📝 投稿流程：\n"
    text += "1. 点击「我要投稿」创建新稿件\n"
    text += "2. 编辑稿件名字和内容\n"
    text += "3. 可选上传图片/视频等媒体\n"
    text += "4. 设置是否匿名投稿\n"
    text += "5. 预览确认后提交审核\n"
    text += "6. 等待管理员审核\n\n"
    text += "💡 提高通过率的小技巧：\n"
    text += "• 详细描述使用方法\n"
    text += "• 说明效果和注意事项\n"
    text += "• 上传清晰的图片或视频\n"
    text += "• 给稿件起个好记的名字\n\n"
    
    await update.message.reply_text(text)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_or_update_user(user.id, user.username, user.first_name)
    
    user_data = db.get_user(user.id)
    if not user_data:
        user_data = User(user_id=user.id, username=user.username or "无", 
                        first_name=user.first_name or "无", last_active=datetime.now().isoformat())
        db.add_or_update_user(user.id, user.username, user.first_name)
        user_data = db.get_user(user.id)
    
    await state_manager.clear_state(user.id)
    
    identity = "管理员" if is_admin(user.id) else "普通用户"
    
    text = f"👋 欢迎回来，{user.first_name}！\n\n"
    text += f"📱 用户信息\n"
    text += f"├ ID: `{user.id}`\n"
    text += f"└ 身份: {identity}\n\n"
    
    if user_data:
        text += f"📊 投稿统计\n"
        text += f"├ 总投稿: {user_data.total_submissions} 件\n"
        text += f"├ 已通过: {user_data.approved_count} 件\n"
        text += f"├ 已拒绝: {user_data.rejected_count} 件\n"
        
        if is_admin(user.id):
            pending_count = db.get_submission_count('pending')
            text += f"└ 待审核: {pending_count} 件\n"
        
        editing_count = user_data.total_submissions - user_data.approved_count - user_data.rejected_count
        text += f"\n✏️ 编辑中: {max(0, editing_count)} 件\n"
    
    text += f"\n📢 频道: {TARGET_CHANNEL}"
    
    keyboard = get_main_menu_keyboard(user.id)
    
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard
            )
    else:
        await update.message.reply_text(text, reply_markup=keyboard)

# ========== 投稿功能 ==========
async def new_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    user_subs = db.get_user_submissions(user.id)
    if len(user_subs) >= 50:
        await query.edit_message_text(
            "❌ 您的稿件数量已达上限（50个）\n请删除一些旧稿件后再创建新的。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 返回", callback_data="back_main")
            ]])
        )
        return
    
    submission = db.create_submission(user.id, user.username, user.first_name)
    db.update_user_stats(user.id)
    
    logger.info(f"用户 {user.id} 创建了新稿件 {submission.id}")
    
    await show_draft_editor(query, submission.id)

async def show_draft_editor(query, sub_id: str):
    sub = db.get_submission(sub_id)
    if not sub:
        await query.edit_message_text("❌ 稿件不存在或已删除")
        return
    
    text = format_submission_preview(sub)
    keyboard = get_submission_keyboard(sub)
    
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def my_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    page = 0
    
    if query.data.startswith("subs_page_"):
        try:
            page = int(query.data.split("_")[2])
        except (IndexError, ValueError):
            page = 0
    
    subs = db.get_user_submissions(user.id, limit=10, offset=page * 10)
    total = db.get_user_submission_count(user.id)
    
    if not subs:
        text = "📭 您还没有任何稿件\n\n点击下方按钮开始投稿吧！"
        keyboard = [[InlineKeyboardButton("📝 创建第一个稿件", callback_data="new_draft")],
                    [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]]
    else:
        text = f"📋 我的稿件列表 ({total}件)\n\n"
        keyboard = []
        
        status_emoji = {"editing": "✏️", "pending": "⏳", "approved": "✅", "rejected": "❌"}
        status_text = {"editing": "编辑中", "pending": "待审核", "approved": "已通过", "rejected": "已拒绝"}
        
        for idx, row in enumerate(subs):
            if isinstance(row, (tuple, list)):
                sub_id = row[0]
                name = row[1]
                status = row[2]
                time = row[3]
                has_media = row[4] if len(row) > 4 else 0
            else:
                continue
            
            emoji = status_emoji.get(status, "📝")
            s_text = status_text.get(status, status)
            media_icon = "📎" if has_media else ""
            time_str = time[:10] if time else "未知"
            text += f"{idx+1}. {emoji} {media_icon} {name[:30]}\n"
            text += f"   └ {s_text} | {time_str}\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"{emoji} {name[:20]}", 
                    callback_data=f"view_draft_{sub_id}"
                )
            ])
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton("⬅️ 上一页", callback_data=f"subs_page_{page-1}")
            )
        if len(subs) == 10:
            nav_buttons.append(
                InlineKeyboardButton("➡️ 下一页", callback_data=f"subs_page_{page+1}")
            )
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        keyboard.append([InlineKeyboardButton("📝 新建稿件", callback_data="new_draft")])
        keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def view_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    sub = db.get_submission(sub_id)
    if not sub:
        await query.edit_message_text("❌ 稿件不存在")
        return
    
    user_id = update.effective_user.id
    if sub.user_id != user_id and not is_admin(user_id):
        await query.answer("❌ 无权访问此稿件", show_alert=True)
        return
    
    await show_draft_editor(query, sub_id)

# ========== 编辑功能 ==========
async def edit_draft_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    state = DraftState(sub_id=sub_id, action="set_name")
    await state_manager.set_state(update.effective_user.id, state)
    
    await query.edit_message_text(
        "✏️ 请输入新的稿件名字：\n\n"
        "要求：2-50个字符，不能包含特殊字符\n"
        "直接发送文本即可，发送 /cancel 取消"
    )

async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    sub = db.get_submission(sub_id)
    current_text = sub.text_content if sub else ""
    
    state = DraftState(sub_id=sub_id, action="set_text", 
                      temp_data={"current_text": current_text})
    await state_manager.set_state(update.effective_user.id, state)
    
    text = "📝 请输入文本内容：\n\n"
    if current_text:
        text += f"当前内容：\n{current_text[:200]}...\n\n"
    text += "支持Markdown格式，最大5000字\n发送 /cancel 取消"
    
    await query.edit_message_text(text)

async def edit_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    state = DraftState(sub_id=sub_id, action="set_media")
    await state_manager.set_state(update.effective_user.id, state)
    
    await query.edit_message_text(
        "🖼️ 请发送媒体文件：\n\n"
        "支持：图片、视频、文档\n"
        "文件大小限制：20MB\n"
        "发送 /cancel 取消"
    )

async def toggle_anonymous(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    sub = db.get_submission(sub_id)
    
    if not sub:
        await query.answer("❌ 稿件不存在", show_alert=True)
        return
    
    new_status = 0 if sub.is_anonymous else 1
    db.update_submission(sub_id, is_anonymous=new_status)
    
    await query.answer(
        f"✅ 已{'启用' if new_status else '关闭'}匿名投稿",
        show_alert=True
    )
    await show_draft_editor(query, sub_id)

async def add_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    tags = db.get_tags(sub_id)
    
    text = f"🏷️ 稿件标签管理\n\n"
    if tags:
        text += f"当前标签：{', '.join(tags)}\n\n"
    text += "请直接发送标签名称（多个用逗号分隔，最多5个）\n发送 /cancel 取消"
    
    state = DraftState(sub_id=sub_id, action="add_tag")
    await state_manager.set_state(update.effective_user.id, state)
    
    await query.edit_message_text(text)

async def preview_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    sub = db.get_submission(sub_id)
    
    if not sub:
        await query.edit_message_text("❌ 稿件不存在")
        return
    
    preview_text = f"📋 稿件预览\n\n"
    preview_text += f"📝 标题：{sub.draft_name}\n"
    preview_text += f"👤 作者：{'匿名' if sub.is_anonymous else f'@{sub.username}'}\n"
    preview_text += f"📊 状态：{sub.status}\n"
    
    tags = db.get_tags(sub_id)
    if tags:
        preview_text += f"🏷️ 标签：{', '.join(tags)}\n"
    
    preview_text += f"\n📄 内容：\n{sub.text_content[:500] if sub.text_content else '(无文本内容)'}\n"
    
    if not sub.media_file_id:
        preview_text += "\n🖼️ (无媒体文件)"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回编辑", callback_data=f"view_draft_{sub_id}")],
        [InlineKeyboardButton("📤 提交审核", callback_data=f"submit_review_{sub_id}")] 
        if sub.status == "editing" else 
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]
    ])
    
    media_to_send = None
    if sub.media_local_path and Path(sub.media_local_path).exists():
        media_to_send = sub.media_local_path
    elif sub.media_file_id:
        media_to_send = sub.media_file_id
    
    if media_to_send:
        try:
            if sub.media_type == "photo":
                await query.message.reply_photo(
                    media_to_send, 
                    caption=preview_text,
                    reply_markup=keyboard
                )
            elif sub.media_type == "video":
                await query.message.reply_video(
                    media_to_send,
                    caption=preview_text,
                    reply_markup=keyboard
                )
            elif sub.media_type == "document":
                await query.message.reply_document(
                    media_to_send,
                    caption=preview_text,
                    reply_markup=keyboard
                )
            else:
                await query.message.reply_text(
                    preview_text,
                    reply_markup=keyboard
                )
            
            await query.edit_message_text("✅ 预览已生成")
        except Exception as e:
            logger.error(f"预览失败: {e}")
            await query.edit_message_text(preview_text, reply_markup=keyboard)
    else:
        await query.edit_message_text(preview_text, reply_markup=keyboard)

async def delete_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    sub = db.get_submission(sub_id)
    
    if not sub or sub.user_id != update.effective_user.id:
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认删除", callback_data=f"confirm_delete_{sub_id}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"view_draft_{sub_id}")
        ]
    ])
    
    await query.edit_message_text(
        f"⚠️ 确认删除稿件？\n\n"
        f"📝 {sub.draft_name}\n"
        f"此操作不可恢复！",
        reply_markup=keyboard
    )

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    
    db.delete_submission(sub_id)
    db.update_user_stats(update.effective_user.id)
    
    logger.info(f"用户 {update.effective_user.id} 删除了稿件 {sub_id}")
    
    await query.edit_message_text(
        "✅ 稿件已删除",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")
        ]])
    )

async def submit_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[2]
    sub = db.get_submission(sub_id)
    
    if not sub or sub.user_id != update.effective_user.id:
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    errors = []
    
    valid, msg = validate_draft_name(sub.draft_name)
    if not valid:
        errors.append(msg)
    
    if not sub.text_content and not sub.media_file_id:
        errors.append("请至少填写文本或上传媒体")
    
    if sub.text_content:
        valid, msg = validate_text_content(sub.text_content)
        if not valid:
            errors.append(msg)
    
    if errors:
        await query.answer(
            "❌ " + "\n".join(errors),
            show_alert=True
        )
        return
    
    db.update_submission(
        sub_id, 
        status="pending",
        submit_time=datetime.now().isoformat()
    )
    db.add_review_log(sub_id, "submitted", update.effective_user.id)
    db.update_user_stats(update.effective_user.id)
    
    try:
        for admin_id in ADMIN_USER_IDS:
            await notify_admin_new_submission(context, admin_id, sub)
    except Exception as e:
        logger.error(f"通知管理员失败: {e}")
    
    await query.edit_message_text(
        f"✅ 稿件已提交审核！\n\n"
        f"📝 ID: `{sub_id}`\n"
        f"⏳ 状态: 等待管理员审核\n\n"
        f"审核结果会通过私信通知您\n"
        f"请耐心等待...",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")
        ]])
    )

async def withdraw_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    sub = db.get_submission(sub_id)
    
    if not sub or sub.user_id != update.effective_user.id:
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    db.update_submission(sub_id, status="editing")
    db.add_review_log(sub_id, "withdrawn", update.effective_user.id)
    
    await query.edit_message_text(
        "✅ 稿件已撤回，当前状态：编辑中\n"
        "您可以继续编辑或重新提交",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 继续编辑", callback_data=f"view_draft_{sub_id}"),
            InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")
        ]])
    )

async def reedit_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    sub = db.get_submission(sub_id)
    
    if not sub or sub.user_id != update.effective_user.id:
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    db.update_submission(
        sub_id, 
        status="editing",
        review_time=None,
        reviewer_id=None,
        reject_reason=None
    )
    
    await show_draft_editor(query, sub_id)

# ========== 管理员功能 ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权访问", show_alert=True)
        return
    
    pending_count = db.get_submission_count('pending')
    
    text = "⚙️ 管理员控制面板\n\n"
    text += f"📊 待审核: {pending_count} 件\n\n"
    text += "请选择操作："
    
    keyboard = [
        [InlineKeyboardButton(f"📋 待审核列表 ({pending_count})", callback_data="admin_pending")],
        [InlineKeyboardButton("👥 用户管理", callback_data="admin_users")],
        [InlineKeyboardButton("📊 统计数据", callback_data="admin_stats")],
        [InlineKeyboardButton("📤 导出数据", callback_data="admin_export")],
        [InlineKeyboardButton("💾 数据库备份", callback_data="admin_backup")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_pending_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权访问", show_alert=True)
        return
    
    pending = db.get_pending_submissions()
    
    if not pending:
        await query.edit_message_text(
            "📭 暂无待审核投稿",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 返回", callback_data="admin_panel")
            ]])
        )
        return
    
    context.user_data['pending_list'] = [s.id for s in pending]
    context.user_data['pending_index'] = 0
    
    sub = pending[0]
    await show_admin_review(query, context, sub)

async def show_admin_review(query, context: ContextTypes.DEFAULT_TYPE, sub: Submission):
    text = f"📋 审核稿件\n\n"
    text += f"📝 ID: `{sub.id}`\n"
    text += f"📌 标题: {sub.draft_name}\n"
    text += f"👤 作者: {sub.first_name} (@{sub.username})\n"
    text += f"👤 用户ID: {sub.user_id}\n"
    text += f"🕵️ 匿名: {'是' if sub.is_anonymous else '否'}\n"
    
    tags = db.get_tags(sub.id)
    if tags:
        text += f"🏷️ 标签: {', '.join(tags)}\n"
    
    text += f"📅 提交时间: {sub.submit_time[:19] if sub.submit_time else '未知'}\n\n"
    text += f"📄 内容:\n{sub.text_content[:500] if sub.text_content else '(无文本)'}\n"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ 通过", callback_data=f"approve_{sub.id}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"reject_{sub.id}")
        ],
        [
            InlineKeyboardButton("💬 拒绝并说明", callback_data=f"reject_reason_{sub.id}"),
            InlineKeyboardButton("👁️ 查看媒体", callback_data=f"viewmedia_{sub.id}")
        ]
    ]
    
    pending_list = context.user_data.get('pending_list', [])
    if pending_list:
        idx = context.user_data.get('pending_index', 0)
        total = len(pending_list)
        nav_buttons = []
        if idx > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ 上一个", callback_data="pending_prev"))
        if idx < total - 1:
            nav_buttons.append(InlineKeyboardButton("➡️ 下一个", callback_data="pending_next"))
        if nav_buttons:
            keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🔙 返回管理面板", callback_data="admin_panel")])
    
    try:
        if sub.media_file_id:
            if sub.media_type == "photo":
                await query.message.reply_photo(sub.media_file_id, caption=text[:1024], 
                                               reply_markup=InlineKeyboardMarkup(keyboard),
                                               parse_mode="Markdown")
            elif sub.media_type == "video":
                await query.message.reply_video(sub.media_file_id, caption=text[:1024],
                                               reply_markup=InlineKeyboardMarkup(keyboard),
                                               parse_mode="Markdown")
            else:
                await query.message.reply_document(sub.media_file_id, caption=text[:1024],
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
            
            await query.edit_message_text(f"✅ 稿件 {sub.id} 审核界面已打开")
        else:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                         parse_mode="Markdown")
    except Exception as e:
        logger.error(f"显示审核界面失败: {e}")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    sub = db.get_submission(sub_id)
    
    if not sub:
        await query.edit_message_text("❌ 稿件不存在")
        return
    
    db.update_submission(
        sub_id,
        status="approved",
        review_time=datetime.now().isoformat(),
        reviewer_id=update.effective_user.id
    )
    db.add_review_log(sub_id, "approved", update.effective_user.id)
    db.update_user_stats(sub.user_id)
    
    # 发布到频道
    publish_success = False
    try:
        author = "匿名投稿" if sub.is_anonymous else f"来自 @{sub.username}"
        caption = f"📮 {author}\n📝 {sub.draft_name}\n\n"
        if sub.text_content:
            caption += sub.text_content[:900]
        
        if sub.media_file_id:
            if sub.media_type == "photo":
                msg = await context.bot.send_photo(TARGET_CHANNEL, sub.media_file_id, caption=caption)
            elif sub.media_type == "video":
                msg = await context.bot.send_video(TARGET_CHANNEL, sub.media_file_id, caption=caption)
            elif sub.media_type == "document":
                msg = await context.bot.send_document(TARGET_CHANNEL, sub.media_file_id, caption=caption)
            else:
                msg = await context.bot.send_message(TARGET_CHANNEL, caption)
        else:
            msg = await context.bot.send_message(TARGET_CHANNEL, caption)
        
        if msg:
            db.update_submission(sub_id, publish_message_id=str(msg.message_id))
            publish_success = True
            
    except Forbidden as e:
        logger.error(f"频道权限错误: {e}")
        await query.edit_message_text(
            f"⚠️ 稿件已通过，但发布到频道失败\n\n"
            f"原因：机器人无权访问频道\n"
            f"请确保机器人是频道管理员\n\n"
            f"稿件ID: {sub_id}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("继续审核", callback_data="admin_pending")
            ]])
        )
        return
        
    except BadRequest as e:
        logger.error(f"发布到频道请求错误: {e}")
        await query.edit_message_text(
            f"⚠️ 稿件已通过，但发布到频道失败\n\n"
            f"原因：{str(e)[:100]}\n\n"
            f"稿件ID: {sub_id}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("继续审核", callback_data="admin_pending")
            ]])
        )
        return
        
    except Exception as e:
        logger.error(f"发布到频道失败: {e}")
        await query.edit_message_text(
            f"⚠️ 稿件已通过，但发布到频道失败\n\n"
            f"原因：{str(e)[:100]}\n"
            f"请检查日志\n\n"
            f"稿件ID: {sub_id}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("继续审核", callback_data="admin_pending")
            ]])
        )
        return
    
    try:
        if publish_success:
            notification = (
                f"✅ 恭喜！您的稿件已通过审核并发布到频道\n\n"
                f"📝 稿件ID: {sub_id}\n"
                f"📢 频道: {TARGET_CHANNEL}"
            )
        else:
            notification = (
                f"✅ 您的稿件已通过审核！\n\n"
                f"📝 稿件ID: {sub_id}\n"
                f"⚠️ 但发布到频道时出现问题，请联系管理员"
            )
        await context.bot.send_message(sub.user_id, notification)
    except Exception as e:
        logger.warning(f"通知用户 {sub.user_id} 失败: {e}")
    
    await query.edit_message_text(
        f"✅ 稿件已通过{'并发布' if publish_success else ''}\n"
        f"ID: {sub_id}\n"
        f"作者: {sub.first_name}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("继续审核", callback_data="admin_pending")
        ]])
    )

async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    parts = query.data.split("_", 1)
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    
    if parts[0] == "reject_reason":
        state = DraftState(sub_id=sub_id, action="reject_reason")
        await state_manager.set_state(update.effective_user.id, state)
        
        await query.edit_message_text(
            "📝 请输入拒绝原因：\n\n"
            "这会发送给投稿用户\n"
            "发送 /cancel 取消"
        )
        return
    
    sub = db.get_submission(sub_id)
    if not sub:
        await query.edit_message_text("❌ 稿件不存在")
        return
    
    db.update_submission(
        sub_id,
        status="rejected",
        review_time=datetime.now().isoformat(),
        reviewer_id=update.effective_user.id
    )
    db.add_review_log(sub_id, "rejected", update.effective_user.id)
    db.update_user_stats(sub.user_id)
    
    try:
        notification = f"❌ 很遗憾，您的稿件未被通过\n\n📝 稿件ID: {sub_id}"
        await context.bot.send_message(sub.user_id, notification)
    except Exception as e:
        logger.warning(f"通知用户 {sub.user_id} 失败: {e}")
    
    await query.edit_message_text(
        f"❌ 稿件已拒绝\n"
        f"ID: {sub_id}\n"
        f"作者: {sub.first_name}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("继续审核", callback_data="admin_pending")
        ]])
    )

async def admin_view_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权操作", show_alert=True)
        return
    
    parts = query.data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    sub_id = parts[1]
    sub = db.get_submission(sub_id)
    
    if not sub or not sub.media_file_id:
        await query.edit_message_text("❌ 无媒体文件")
        return
    
    try:
        if sub.media_type == "photo":
            await query.message.reply_photo(sub.media_file_id, caption=f"📸 媒体预览 - {sub_id}")
        elif sub.media_type == "video":
            await query.message.reply_video(sub.media_file_id, caption=f"🎥 媒体预览 - {sub_id}")
        elif sub.media_type == "document":
            await query.message.reply_document(sub.media_file_id, caption=f"📎 媒体预览 - {sub_id}")
        
        await query.edit_message_text(f"✅ 媒体文件已显示")
    except Exception as e:
        logger.error(f"获取媒体失败: {e}")
        await query.edit_message_text(f"❌ 获取媒体失败: {str(e)}")

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权访问", show_alert=True)
        return
    
    users = db.get_all_users()
    
    if not users:
        await query.edit_message_text("📭 暂无用户")
        return
    
    text = "👥 用户列表\n\n"
    for idx, user in enumerate(users[:20], 1):
        admin_badge = "👑" if user.is_admin else ""
        text += f"{idx}. {admin_badge} {user.first_name} (@{user.username})\n"
        text += f"   ID: {user.user_id}\n"
        text += f"   投稿: {user.total_submissions} | "
        text += f"通过: {user.approved_count} | "
        text += f"拒绝: {user.rejected_count}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]
    
    if len(text) > 4000:
        f = io.BytesIO(text.encode('utf-8'))
        f.name = "users.txt"
        await query.message.reply_document(InputFile(f))
        await query.edit_message_text("📄 用户列表已导出", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权访问", show_alert=True)
        return
    
    stats = db.get_statistics()
    
    text = "📊 数据统计\n\n"
    text += f"👥 总用户数: {stats['total_users']}\n"
    text += f"👤 活跃用户(7天): {stats['active_users']}\n"
    text += f"📝 总投稿数: {stats['total_submissions']}\n"
    text += f"⏳ 待审核: {stats['pending_submissions']}\n"
    text += f"✅ 已通过: {stats['approved_submissions']}\n"
    text += f"❌ 已拒绝: {stats['rejected_submissions']}\n\n"
    text += "📈 近7天趋势:\n"
    
    for daily in stats['daily_stats']:
        text += f"{daily['date'][5:]}: 投稿{daily['submissions']} | 通过{daily['approved']}\n"
    
    keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.answer("❌ 无权访问", show_alert=True)
        return
    
    keyboard = [
        [InlineKeyboardButton("📊 导出CSV", callback_data="export_csv")],
        [InlineKeyboardButton("💾 备份数据库", callback_data="export_backup")],
        [InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]
    ]
    
    await query.edit_message_text(
        "📤 数据导出\n\n请选择导出格式：",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        filename = db.export_to_csv()
        
        with open(filename, 'rb') as f:
            await query.message.reply_document(
                InputFile(f, filename=Path(filename).name),
                caption=f"📊 数据导出 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        
        await query.edit_message_text("✅ 数据导出成功")
    except Exception as e:
        logger.error(f"导出失败: {e}")
        await query.edit_message_text(f"❌ 导出失败: {str(e)}")

async def backup_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        filename = db.backup_database()
        
        with open(filename, 'rb') as f:
            await query.message.reply_document(
                InputFile(f, filename=Path(filename).name),
                caption=f"💾 数据库备份 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        
        await query.edit_message_text("✅ 数据库备份成功")
    except Exception as e:
        logger.error(f"备份失败: {e}")
        await query.edit_message_text(f"❌ 备份失败: {str(e)}")

async def admin_navigate_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        return
    
    direction = query.data.split("_")[1]
    pending_list = context.user_data.get('pending_list', [])
    current_idx = context.user_data.get('pending_index', 0)
    
    if direction == "prev":
        new_idx = max(0, current_idx - 1)
    else:
        new_idx = min(len(pending_list) - 1, current_idx + 1)
    
    if new_idx != current_idx:
        context.user_data['pending_index'] = new_idx
        sub_id = pending_list[new_idx]
        sub = db.get_submission(sub_id)
        if sub:
            await show_admin_review(query, context, sub)
    else:
        await query.answer("已经是第一个/最后一个了", show_alert=True)

# ========== 消息处理 ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 忽略来自频道的消息
    if update.effective_chat:
        chat_type = update.effective_chat.type
        if chat_type in ['channel', 'supergroup']:
            return
    
    if not update.message or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    message = update.message
    
    if message.from_user and message.from_user.is_bot:
        return
    
    db.add_or_update_user(user_id, update.effective_user.username, update.effective_user.first_name)
    
    if not rate_limiter.is_allowed(user_id):
        remaining = rate_limiter.get_remaining(user_id)
        await message.reply_text(f"❌ 操作过于频繁，请稍后再试\n剩余次数：{remaining}/分钟")
        return
    
    state = await state_manager.get_state(user_id)
    
    if state:
        if message.text and message.text == "/cancel":
            await state_manager.clear_state(user_id)
            await message.reply_text("❌ 操作已取消")
            await show_main_menu(update, context)
            return
        
        try:
            await handle_editing_state(update, context, state)
        except Exception as e:
            logger.error(f"处理编辑状态失败: {e}")
            await message.reply_text("❌ 操作失败，请重试")
            await state_manager.clear_state(user_id)
        return
    
    if context.user_data.get('awaiting_password'):
        await handle_password_check(update, context)
        return
    
    if message.text and message.text.startswith('/'):
        cmd = message.text[1:].strip().lower()
        if cmd in HIDDEN_COMMANDS:
            await handle_hidden_command(update, context)
            return
    
    await message.reply_text(
        "👋 您好！请使用菜单操作\n发送 /start 打开主菜单"
    )

async def handle_editing_state(update: Update, context: ContextTypes.DEFAULT_TYPE, state: DraftState):
    message = update.message
    user_id = update.effective_user.id
    
    if state.action == "set_name":
        text = message.text or message.caption or ""
        text = text.strip()
        
        valid, msg = validate_draft_name(text)
        if not valid:
            await message.reply_text(f"❌ {msg}\n请重新输入")
            return
        
        db.update_submission(state.sub_id, draft_name=text)
        await state_manager.clear_state(user_id)
        await message.reply_text(f"✅ 稿件名字已更新为：{text}")
        
        sub = db.get_submission(state.sub_id)
        if sub:
            preview_text = format_submission_preview(sub)
            keyboard = get_submission_keyboard(sub)
            await message.reply_text(preview_text, reply_markup=keyboard)
    
    elif state.action == "set_text":
        text = message.text or message.caption or ""
        
        valid, msg = validate_text_content(text)
        if not valid:
            await message.reply_text(f"❌ {msg}\n请重新输入")
            return
        
        db.update_submission(state.sub_id, text_content=text)
        await state_manager.clear_state(user_id)
        await message.reply_text(f"✅ 文本内容已更新（{len(text)}字）")
        
        sub = db.get_submission(state.sub_id)
        if sub:
            preview = format_submission_preview(sub)
            keyboard = get_submission_keyboard(sub)
            await message.reply_text(preview, reply_markup=keyboard)
    
    elif state.action == "set_media":
        file_id = None
        media_type = None
        file_size = 0
        
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
            file_size = message.photo[-1].file_size or 0
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
            file_size = message.video.file_size or 0
        elif message.document:
            file_id = message.document.file_id
            media_type = "document"
            file_size = message.document.file_size or 0
        elif message.audio:
            file_id = message.audio.file_id
            media_type = "audio"
            file_size = message.audio.file_size or 0
        else:
            await message.reply_text("❌ 请发送图片、视频、文档或音频")
            return
        
        if file_size > 20 * 1024 * 1024:
            await message.reply_text("❌ 文件大小不能超过20MB")
            return
        
        local_path = await download_media_file(context, file_id, media_type, state.sub_id)
        
        db.update_submission(state.sub_id, media_file_id=file_id, media_type=media_type, media_local_path=local_path)
        await state_manager.clear_state(user_id)
        await message.reply_text(f"✅ {media_type} 文件已上传{'并保存到本地' if local_path else ''}")
        
        sub = db.get_submission(state.sub_id)
        if sub:
            preview = format_submission_preview(sub)
            keyboard = get_submission_keyboard(sub)
            await message.reply_text(preview, reply_markup=keyboard)
    
    elif state.action == "add_tag":
        text = message.text or ""
        tags = [tag.strip() for tag in text.split(",") if tag.strip()]
        
        if not tags:
            await message.reply_text("❌ 请输入有效的标签")
            return
        
        if len(tags) > 5:
            await message.reply_text("❌ 标签数量不能超过5个")
            return
        
        existing_tags = db.get_tags(state.sub_id)
        for tag in existing_tags:
            db.remove_tag(state.sub_id, tag)
        
        for tag in tags[:5]:
            if len(tag) <= 20:
                db.add_tag(state.sub_id, tag)
        
        await state_manager.clear_state(user_id)
        
        current_tags = db.get_tags(state.sub_id)
        await message.reply_text(f"✅ 标签已更新：{', '.join(current_tags) if current_tags else '无'}")
        
        sub = db.get_submission(state.sub_id)
        if sub:
            preview = format_submission_preview(sub)
            keyboard = get_submission_keyboard(sub)
            await message.reply_text(preview, reply_markup=keyboard)
    
    elif state.action == "reject_reason":
        reason = message.text or ""
        if not reason.strip():
            await message.reply_text("❌ 请输入拒绝原因")
            return
        
        sub = db.get_submission(state.sub_id)
        if sub:
            db.update_submission(
                state.sub_id,
                status="rejected",
                review_time=datetime.now().isoformat(),
                reviewer_id=user_id,
                reject_reason=reason
            )
            db.add_review_log(state.sub_id, "rejected", user_id, reason)
            db.update_user_stats(sub.user_id)
            
            try:
                await context.bot.send_message(
                    sub.user_id,
                    f"❌ 很遗憾，您的稿件未被通过\n\n"
                    f"📝 稿件ID: {state.sub_id}\n"
                    f"📝 原因: {reason}\n\n"
                    f"您可以重新编辑后再次提交"
                )
            except Exception as e:
                logger.warning(f"通知用户失败: {e}")
            
            await message.reply_text(f"✅ 稿件 {state.sub_id} 已拒绝")
            await state_manager.clear_state(user_id)
            
            await admin_pending_list(update, context)

async def handle_password_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text.strip() if update.message.text else ""
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    expected_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
    
    if password_hash == expected_hash or password == ADMIN_PASSWORD:
        context.user_data.pop('awaiting_password')
        action = context.user_data.get('pending_action')
        context.user_data.pop('pending_action', None)
        
        await update.message.reply_text("✅ 验证成功")
        
        if action:
            await execute_admin_action(update, context, action)
    else:
        await update.message.reply_text("❌ 密码错误")
        context.user_data.pop('awaiting_password', None)
        context.user_data.pop('pending_action', None)

async def execute_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    actions = {
        "list_pending": admin_pending_list,
        "view_users": admin_users,
        "all_submissions": admin_view_all_submissions,
        "export_data": admin_export,
        "admin_help": admin_help,
        "admin_stats": admin_statistics
    }
    
    if action in actions:
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def answer(self):
                pass
            async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                if reply_markup:
                    await self.message.reply_text(text, reply_markup=reply_markup)
                else:
                    await self.message.reply_text(text)
        
        mock_query = MockQuery(update.message)
        if action == "admin_help":
            await admin_help(update, context)
        elif action == "admin_stats":
            class MockUpdate:
                def __init__(self, message, user):
                    self.callback_query = MockQuery(message)
                    self.effective_user = user
            mock_up = MockUpdate(update.message, update.effective_user)
            await admin_statistics(mock_up, context)
        else:
            if action == "view_users":
                class MockUpdate2:
                    def __init__(self, message, user):
                        self.callback_query = MockQuery(message)
                        self.effective_user = user
                mock_up2 = MockUpdate2(update.message, update.effective_user)
                await admin_users(mock_up2, context)

# ========== 隐藏命令 ==========
HIDDEN_COMMANDS = {
    "admin123": "list_pending",
    "users456": "view_users",
    "all789": "all_submissions",
    "export012": "export_data",
    "help345": "admin_help",
    "stats678": "admin_stats"
}

async def handle_hidden_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        logger.warning(f"非管理员 {user_id} 尝试使用隐藏命令")
        await update.message.reply_text("❌ 未知命令")
        return
    
    cmd = update.message.text[1:].strip().lower()
    action = HIDDEN_COMMANDS.get(cmd)
    
    if action:
        if action in ["view_users", "all_submissions", "export_data"]:
            context.user_data['pending_action'] = action
            context.user_data['awaiting_password'] = True
            await update.message.reply_text("🔐 请输入管理员密码：")
        else:
            await execute_admin_action(update, context, action)

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔐 管理员隐藏命令:\n\n"
    for cmd, desc in HIDDEN_COMMANDS.items():
        text += f"/{cmd} → {desc}\n"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)

async def admin_view_all_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    subs = db.get_all_submissions(limit=20)
    
    if not subs:
        text = "📭 暂无投稿"
    else:
        text = "📋 最近20条投稿\n\n"
        for s in subs:
            emoji = {"editing": "✏️", "pending": "⏳", "approved": "✅", "rejected": "❌"}.get(s.status, "📝")
            text += f"{emoji} {s.id} | {s.draft_name[:20]} | {s.first_name}\n"
    
    await update.message.reply_text(text)

# ========== 回调处理 ==========
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 忽略来自频道的回调
    if update.effective_chat:
        chat_type = update.effective_chat.type
        if chat_type in ['channel', 'supergroup']:
            return
    
    query = update.callback_query
    
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"回调查询确认失败: {e}")
    
    user_id = update.effective_user.id
    data = query.data
    
    try:
        if data == "new_draft":
            await new_draft(update, context)
        elif data == "my_submissions":
            await my_submissions(update, context)
        elif data == "my_stats":
            await show_user_stats(update, context)
        elif data == "back_main":
            await show_main_menu(update, context)
        elif data == "admin_panel":
            await admin_panel(update, context)
        elif data == "admin_pending":
            await admin_pending_list(update, context)
        elif data == "admin_users":
            await admin_users(update, context)
        elif data == "admin_stats":
            await admin_statistics(update, context)
        elif data == "admin_export":
            await admin_export(update, context)
        elif data == "admin_backup":
            await backup_database(update, context)
        elif data == "export_csv":
            await export_csv(update, context)
        elif data == "export_backup":
            await backup_database(update, context)
        elif data.startswith("view_draft_"):
            await view_draft(update, context)
        elif data.startswith("edit_name_"):
            await edit_draft_name(update, context)
        elif data.startswith("edit_text_"):
            await edit_text(update, context)
        elif data.startswith("edit_media_"):
            await edit_media(update, context)
        elif data.startswith("toggle_anonymous_"):
            await toggle_anonymous(update, context)
        elif data.startswith("add_tag_"):
            await add_tag(update, context)
        elif data.startswith("preview_"):
            await preview_draft(update, context)
        elif data.startswith("delete_draft_"):
            await delete_draft(update, context)
        elif data.startswith("confirm_delete_"):
            await confirm_delete(update, context)
        elif data.startswith("submit_review_"):
            await submit_review(update, context)
        elif data.startswith("withdraw_"):
            await withdraw_submission(update, context)
        elif data.startswith("reedit_"):
            await reedit_submission(update, context)
        elif data.startswith("approve_"):
            await admin_approve(update, context)
        elif data.startswith("reject_"):
            await admin_reject(update, context)
        elif data.startswith("reject_reason_"):
            await admin_reject(update, context)
        elif data.startswith("viewmedia_"):
            await admin_view_media(update, context)
        elif data.startswith("pending_"):
            await admin_navigate_pending(update, context)
        elif data.startswith("subs_page_"):
            await my_submissions(update, context)
        else:
            await query.answer(f"未知操作", show_alert=True)
            
    except Exception as e:
        logger.error(f"回调处理失败: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ 操作失败: {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")
                ]])
            )
        except:
            pass

async def show_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_data = db.get_user(user.id)
    
    text = f"📊 个人统计\n\n"
    text += f"👤 用户: {user.first_name}\n"
    text += f"📝 总投稿: {user_data.total_submissions if user_data else 0}\n"
    text += f"✅ 通过率: "
    
    if user_data and user_data.total_submissions > 0:
        rate = (user_data.approved_count / user_data.total_submissions) * 100
        text += f"{rate:.1f}%\n"
    else:
        text += "0%\n"
    
    text += f"📅 加入时间: {user_data.join_date[:10] if user_data and user_data.join_date else '未知'}\n"
    text += f"🕐 最后活跃: {user_data.last_active[:16] if user_data and user_data.last_active else '未知'}"
    
    keyboard = [[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== 通知功能 ==========
async def notify_admin_new_submission(context, admin_id: int, sub: Submission):
    try:
        text = (
            f"📬 新投稿通知\n\n"
            f"📝 ID: `{sub.id}`\n"
            f"📌 标题: {sub.draft_name}\n"
            f"👤 作者: {sub.first_name} (@{sub.username})\n"
            f"🕵️ 匿名: {'是' if sub.is_anonymous else '否'}"
        )
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 快速通过", callback_data=f"approve_{sub.id}"),
            InlineKeyboardButton("❌ 快速拒绝", callback_data=f"reject_{sub.id}")
        ]])
        
        await context.bot.send_message(
            admin_id,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"通知管理员 {admin_id} 失败: {e}")

# ========== 错误处理 ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    
    ignore_errors = [
        "Message is not modified",
        "Query is too old",
        "No message found",
        "Chat not found",
        "Can't parse entities",
        "Message can't be edited",
        "Message to edit not found",
        "Callback query expired",
    ]
    
    error_str = str(error)
    for ignore in ignore_errors:
        if ignore in error_str:
            logger.debug(f"忽略无害错误: {error_str[:100]}")
            return
    
    if update and update.effective_chat:
        chat_type = getattr(update.effective_chat, 'type', '')
        if chat_type in ['channel', 'supergroup']:
            logger.debug(f"忽略频道/群组错误: {error_str[:100]}")
            return
    
    logger.error(f"错误: {error_str}", exc_info=True)
    
    try:
        if update and update.effective_message and update.effective_chat:
            if update.effective_chat.type == 'private':
                error_msg = "❌ 发生错误，请重试"
                
                if isinstance(error, Forbidden):
                    error_msg = "❌ 权限不足，请检查机器人权限"
                elif isinstance(error, BadRequest):
                    if "not enough rights" in str(error):
                        error_msg = "❌ 机器人权限不足，请添加为频道管理员"
                    else:
                        error_msg = "❌ 请求无效，请稍后重试"
                elif isinstance(error, TelegramError):
                    error_msg = f"❌ 错误: {str(error)[:50]}"
                
                await update.effective_message.reply_text(error_msg)
    except Exception:
        pass

# ========== 定时任务 ==========
async def scheduled_tasks(context: ContextTypes.DEFAULT_TYPE):
    try:
        db.update_daily_statistics()
        await state_manager.cleanup_expired()
        
        now = datetime.now()
        if now.hour == 3 and now.minute == 0:
            backup_path = db.backup_database()
            logger.info(f"每日数据库备份完成: {backup_path}")
        
    except Exception as e:
        logger.error(f"定时任务失败: {e}")

# ========== 主函数 ==========
def main():
    if not TOKEN:
        logger.error("未设置 BOT_TOKEN 环境变量")
        return
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    for cmd in HIDDEN_COMMANDS:
        app.add_handler(CommandHandler(cmd, handle_hidden_command))
    
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO,
        handle_message
    ))
    
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)
    
    app.job_queue.run_repeating(scheduled_tasks, interval=1800, first=10)
    
    logger.info("=" * 50)
    logger.info("🤖 投稿机器人启动中...")
    logger.info(f"📱 管理员: {ADMIN_USER_IDS}")
    logger.info(f"📢 频道: {TARGET_CHANNEL}")
    logger.info(f"💾 数据库: {DB_PATH}")
    logger.info("=" * 50)
    
    print("=" * 50)
    print("🤖 投稿机器人启动成功！")
    print(f"📱 管理员ID: {ADMIN_USER_IDS}")
    print(f"📢 目标频道: {TARGET_CHANNEL}")
    print(f"💾 数据库路径: {DB_PATH}")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()