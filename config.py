import os
import re
from dotenv import load_dotenv
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

load_dotenv()

# مسیرهای فایل‌ها
BASE_DIR = Path(__file__).resolve().parent
USER_DATA_FILE = BASE_DIR / "user_data.json"


def _split_list(value: str) -> list[str]:
    """Split comma, semicolon, whitespace, or Persian-comma separated values."""
    items = [item for item in re.split(r'[,;،\s]+', value.strip()) if item]
    return list(dict.fromkeys(items))

# تنظیمات امنیتی
class SecurityConfig:
    MAX_RETRIES = 3
    RATE_LIMIT = 5
    SESSION_TIMEOUT = 3600

class Config:
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    BASE_URI_PROXY = os.environ.get('BASE_URI_PROXY')
    BARGHE_MAN_PROXY = os.environ.get('BARGHE_MAN_PROXY')
    DOMESTIC_SERVER_AVAILABLE = os.getenv('DOMESTIC_SERVER_AVAILABLE', 'true')
    ADMIN_CHAT_IDS = _split_list(os.getenv('TELEGRAM_ADMIN_CHAT_IDS', ''))
    CHECK_TIMES = _split_list(os.getenv('CHECK_TIMES', '08:00'))
    BLACKOUT_REMINDER_MINUTES = os.getenv('BLACKOUT_REMINDER_MINUTES', '30')
    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Tehran')
    USER_DATA_FILE = str(USER_DATA_FILE)

    # تنظیمات امنیتی
    SECURITY = SecurityConfig()

    @classmethod
    def validate(cls):
        """اعتبارسنجی تنظیمات"""
        if not cls.TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN not set in environment variables")
        availability = str(cls.DOMESTIC_SERVER_AVAILABLE).strip().lower()
        if availability not in ('true', 'false', '1', '0', 'yes', 'no'):
            raise ValueError(
                "DOMESTIC_SERVER_AVAILABLE must be true or false"
            )
        cls.DOMESTIC_SERVER_AVAILABLE = availability in ('true', '1', 'yes')
        if cls.DOMESTIC_SERVER_AVAILABLE:
            if not cls.BASE_URI_PROXY:
                raise ValueError("BASE_URI_PROXY not set in environment variables")
            if not cls.BARGHE_MAN_PROXY:
                raise ValueError(
                    "BARGHE_MAN_PROXY is required when DOMESTIC_SERVER_AVAILABLE=true"
                )
            if not re.fullmatch(r'socks5h?://[^\s]+', cls.BARGHE_MAN_PROXY):
                raise ValueError(
                    "BARGHE_MAN_PROXY must be a socks5:// or socks5h:// URL"
                )
        if not cls.ADMIN_CHAT_IDS:
            raise ValueError(
                "TELEGRAM_ADMIN_CHAT_IDS must contain at least one authorized private chat ID"
            )
        for chat_id in cls.ADMIN_CHAT_IDS:
            try:
                int(chat_id)
            except ValueError as exc:
                raise ValueError(
                    f"TELEGRAM_ADMIN_CHAT_IDS contains a non-numeric value: {chat_id}"
                ) from exc
        if not cls.CHECK_TIMES:
            raise ValueError("CHECK_TIMES must contain at least one HH:MM value")
        normalized_times = []
        for check_time in cls.CHECK_TIMES:
            match = re.fullmatch(r'(\d{1,2}):(\d{2})', check_time)
            if not match:
                raise ValueError(
                    f"Invalid CHECK_TIMES value '{check_time}'; expected HH:MM"
                )
            hour, minute = (int(value) for value in match.groups())
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(
                    f"Invalid CHECK_TIMES value '{check_time}'; expected HH:MM"
                )
            normalized_times.append(f"{hour:02d}:{minute:02d}")
        cls.CHECK_TIMES = sorted(set(normalized_times))
        try:
            reminder_minutes = int(cls.BLACKOUT_REMINDER_MINUTES)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "BLACKOUT_REMINDER_MINUTES must be a whole number from 0 to 1440"
            ) from exc
        if not 0 <= reminder_minutes <= 1440:
            raise ValueError(
                "BLACKOUT_REMINDER_MINUTES must be between 0 and 1440"
            )
        cls.BLACKOUT_REMINDER_MINUTES = reminder_minutes
        try:
            ZoneInfo(cls.TIMEZONE)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown TIMEZONE value '{cls.TIMEZONE}'") from exc

        # اطمینان از وجود فایل‌های مورد نیاز
        if not USER_DATA_FILE.exists():
            USER_DATA_FILE.touch()
        USER_DATA_FILE.chmod(0o600)
