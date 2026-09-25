# utils.py
import jdatetime
from datetime import datetime
import pytz
from config import Config

def get_jalali_today():
    """Return today's Jalali date in the configured application timezone."""
    local_now = datetime.now(pytz.timezone(Config.TIMEZONE))
    jalali_date = jdatetime.date.fromgregorian(date=local_now.date())
    return jalali_date.strftime("%Y/%m/%d")

def sanitize_input(text: str) -> str:
    """پاکسازی ورودی کاربر"""
    return text.strip()

def validate_session(session_data: dict) -> bool:
    """اعتبارسنجی نشست کاربر"""
    if not session_data or 'timestamp' not in session_data:
        return False
    
    session_time = datetime.fromisoformat(session_data['timestamp'])
    current_time = datetime.now(pytz.UTC)
    time_diff = (current_time - session_time).total_seconds()
    
    return time_diff < Config.SECURITY.SESSION_TIMEOUT
