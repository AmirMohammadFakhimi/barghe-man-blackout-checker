import asyncio
import hashlib
import json
import logging
import requests
import re
import jdatetime
from pathlib import Path
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters,
    ConversationHandler, TypeHandler, ApplicationHandlerStop
)

from utils import get_jalali_today
from datetime import time, datetime, timedelta
import pytz
from config import Config
from telegram.ext import Application

# --- تنظیمات امنیتی ---
BASE_URI_PROXY = Config.BASE_URI_PROXY
BARGHE_MAN_PROXY = Config.BARGHE_MAN_PROXY
TIMEOUT = 55
MAX_RETRIES = 3
TELEGRAM_CONNECT_TIMEOUT = 30
TELEGRAM_READ_TIMEOUT = 30
TELEGRAM_WRITE_TIMEOUT = 30
TELEGRAM_POOL_TIMEOUT = 10
MOBILE_PATTERN = r'^09[0-9]{9}$'
OTP_PATTERN = r'^\d{6}$'
# The upstream project queries planned outages through five days after today.
ALL_AVAILABLE_PLAN_DAYS = 6
BLACKOUT_CHECK_URL = "https://bargheman.com/profile/blackout/my-blackouts"
LINK_CHECK_NOTICE = (
    "🔗 برنامه خاموشی را در این صفحه بررسی کنید:\n"
    f"{BLACKOUT_CHECK_URL}"
)
_PERSIAN_AND_ARABIC_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

# --- تنظیمات Rate Limiting ---
RATE_LIMIT = {
    'daily_limit': 5,
    'window_hours': 24
}
user_rate_limits = {}  # ذخیره وضعیت Rate Limit کاربران
notification_state = {}  # وضعیت ارسال فقط تا زمان اجرای فعلی در حافظه نگهداری می‌شود
scheduled_blackout_reminders = {}  # Jobهای یک‌باره یادآوری در اجرای فعلی
sent_blackout_reminders = set()  # جلوگیری از ارسال تکراری در اجرای فعلی
TOKEN = Config.TOKEN
USER_DATA_FILE = Config.USER_DATA_FILE

# --- تنظیمات لاگ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
# HTTPX logs Bot API URLs containing the secret token at INFO level. Keep
# transport internals out of normal logs; actionable failures are logged by our
# retry wrapper without exposing credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- مراحل گفتگو ---
GET_MOBILE, GET_OTP, EDIT_TARGETS, EDIT_WATCHED_HOUSES = range(4)

# --- اعتبارسنجی ورودی‌ها ---
def validate_mobile(mobile: str) -> bool:
    """اعتبارسنجی شماره موبایل"""
    return bool(re.match(MOBILE_PATTERN, mobile))

def validate_otp(otp: str) -> bool:
    """اعتبارسنجی کد OTP"""
    return bool(re.match(OTP_PATTERN, otp))


def split_user_values(value: str) -> list[str]:
    items = [item for item in re.split(r'[,;،\s]+', value.strip()) if item]
    return list(dict.fromkeys(items))


def parse_target_chat_ids(value: str, private_chat_id: str) -> list[str]:
    targets = []
    for item in split_user_values(value):
        if item.lower() in ('pv', 'private', 'خصوصی'):
            item = str(private_chat_id)
        if not (
            re.fullmatch(r'-?\d+', item)
            or re.fullmatch(r'@[A-Za-z0-9_]{5,}', item)
        ):
            raise ValueError(
                f"مقصد «{item}» معتبر نیست؛ Chat ID عددی یا @username وارد کنید."
            )
        targets.append(item)
    if not targets:
        raise ValueError("حداقل یک مقصد لازم است.")
    return list(dict.fromkeys(targets))


def parse_bill_ids(value: str) -> list[str]:
    bill_ids = split_user_values(value)
    if not bill_ids:
        raise ValueError("حداقل یک شناسه قبض لازم است.")
    invalid = [bill_id for bill_id in bill_ids if not bill_id.isdigit()]
    if invalid:
        raise ValueError("شناسه قبض باید فقط عدد باشد: " + "، ".join(invalid))
    return bill_ids


def normalize_plan_days(value) -> int:
    try:
        days = int(str(value).translate(_PERSIAN_AND_ARABIC_DIGITS))
    except (TypeError, ValueError):
        return 1
    return days if days in (1, ALL_AVAILABLE_PLAN_DAYS) else 1


def plan_days_description(plan_days: int) -> str:
    return {
        1: "فقط امروز",
        ALL_AVAILABLE_PLAN_DAYS: "همه روزهای موجود",
    }[normalize_plan_days(plan_days)]


def get_plan_dates(plan_days: int) -> list[str]:
    """Return today through the selected Jalali horizon as YYYY/MM/DD."""
    today_text = get_jalali_today().translate(_PERSIAN_AND_ARABIC_DIGITS)
    year, month, day = (int(value) for value in today_text.split('/'))
    gregorian_today = jdatetime.date(year, month, day).togregorian()
    return [
        jdatetime.date.fromgregorian(
            date=gregorian_today + timedelta(days=offset)
        ).strftime("%Y/%m/%d")
        for offset in range(normalize_plan_days(plan_days))
    ]


def bill_identifiers_from_bills(bills: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        str(bill.get('bill_identifier', '')).strip()
        for bill in bills
        if str(bill.get('bill_identifier', '')).strip().isdigit()
    ))


def account_houses_from_bills(bills: list[dict]) -> list[dict]:
    """Keep the minimal, stable account-house data needed by the bot."""
    houses = []
    seen = set()
    for index, bill in enumerate(bills, 1):
        if not isinstance(bill, dict):
            continue
        identifier = str(bill.get('bill_identifier', '')).strip()
        if not identifier.isdigit() or identifier in seen:
            continue
        seen.add(identifier)
        title = str(bill.get('bill_title') or f"خانه {index}").strip()
        houses.append({
            'bill_identifier': identifier,
            'bill_title': title,
        })
    return houses


def house_titles_by_bill_id(user: dict) -> dict[str, str]:
    """Return the saved برق من house title for each bill identifier."""
    houses = user.get('account_houses', [])
    if not isinstance(houses, list):
        return {}
    return {
        str(house.get('bill_identifier', '')).strip():
            str(house.get('bill_title', '')).strip()
        for house in houses
        if isinstance(house, dict)
        and str(house.get('bill_identifier', '')).strip()
        and str(house.get('bill_title', '')).strip()
    }


def ensure_user_settings(admin_id: str, user: dict) -> bool:
    """Normalize the current user schema without migrating deprecated data."""
    changed = False
    private_chat_id = str(user.get('telegram_chat_id') or admin_id)
    if not isinstance(user.get('target_chat_ids'), list) or not user['target_chat_ids']:
        user['target_chat_ids'] = [private_chat_id]
        changed = True

    watched_source = user.get('watched_bill_ids')
    if not isinstance(watched_source, list):
        watched_source = []
    normalized_watched = bill_identifiers_from_bills([
        {'bill_identifier': bill_id}
        for bill_id in watched_source
    ])
    if user.get('watched_bill_ids') != normalized_watched:
        user['watched_bill_ids'] = normalized_watched
        changed = True

    account_houses = user.get('account_houses')
    normalized_houses = account_houses_from_bills(
        account_houses if isinstance(account_houses, list) else []
    )
    if account_houses != normalized_houses:
        user['account_houses'] = normalized_houses
        changed = True

    for deprecated_key in (
        'bill_ids',
        'bill_ids_initialized',
        'watched_bill_ids_initialized',
        'account_houses_initialized',
        'check_times',
        'plan_days',
    ):
        if deprecated_key in user:
            user.pop(deprecated_key)
            changed = True
    return changed

# --- مدیریت Rate Limiting ---
def check_rate_limit(chat_id: str) -> tuple:
    """بررسی Rate Limit برای کاربر"""
    now = datetime.now()
    chat_id = str(chat_id)

    if chat_id not in user_rate_limits:
        # کاربر جدید، محدودیتی ندارد
        user_rate_limits[chat_id] = {
            'count': 1,
            'first_request': now,
            'last_request': now
        }
        return True, RATE_LIMIT['daily_limit'] - 1, None

    user_limit = user_rate_limits[chat_id]
    time_diff = (now - user_limit['first_request']).total_seconds() / 3600  # به ساعت

    if time_diff > RATE_LIMIT['window_hours']:
        # بازه زمانی جدید، ریست محدودیت
        user_rate_limits[chat_id] = {
            'count': 1,
            'first_request': now,
            'last_request': now
        }
        return True, RATE_LIMIT['daily_limit'] - 1, None

    if user_limit['count'] >= RATE_LIMIT['daily_limit']:
        # کاربر به محدودیت رسیده
        reset_time = user_limit['first_request'] + timedelta(hours=RATE_LIMIT['window_hours'])
        return False, 0, reset_time

    # افزایش تعداد درخواست‌ها
    user_rate_limits[chat_id]['count'] += 1
    remaining = RATE_LIMIT['daily_limit'] - user_rate_limits[chat_id]['count']
    return True, remaining, None

async def reset_daily_limits(context: ContextTypes.DEFAULT_TYPE):
    """ریست روزانه محدودیت‌های کاربران"""
    global user_rate_limits
    logger.info("♻️ ریست روزانه محدودیت‌های درخواست کاربران")
    user_rate_limits = {}

# --- دکمه‌ها ---
def get_admin_id(update: Update) -> str | None:
    user = update.effective_user
    return str(user.id) if user else None


def is_authorized_admin(update: Update) -> bool:
    admin_id = get_admin_id(update)
    return bool(admin_id and admin_id in Config.ADMIN_CHAT_IDS)


async def enforce_admin_allowlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Stop all handler processing without responding to unauthorized users."""
    if not is_authorized_admin(update):
        raise ApplicationHandlerStop


def get_menu_markup(admin_id: str = None) -> ReplyKeyboardMarkup:
    """Show authentication only until this authorized admin has a token."""
    authenticated = bool(
        admin_id
        and isinstance(user_data.get(admin_id), dict)
        and user_data[admin_id].get('token')
    )
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        buttons = [
            ["🔗 مشاهده برنامه در وب‌سایت"],
            ["🎯 مقصدهای ارسال"],
        ]
    elif authenticated:
        buttons = [
            ["🏠 نمایش خانه‌های موجود"],
            ["🗓 همه روزهای موجود", "📅 برنامه امروز"],
            ["🎯 مقصدهای ارسال", "👁 خانه‌های تحت نظر"],
            ["🔄 احراز هویت مجدد", "🚪 حذف نشست ورود"],
        ]
    else:
        buttons = [["🔐 احراز هویت"]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# --- مدیریت خطاهای عمومی ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Unauthorized Telegram users are intentionally ignored without a reply or
    # an application log entry. This also prevents the generic error handler
    # from accidentally revealing that the bot is active.
    if isinstance(update, Update) and not is_authorized_admin(update):
        return
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and isinstance(update, Update):
        try:
            await send_private_text_with_retry(
                update,
                "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید.",
            )
        except Exception as exc:
            logger.error(
                "Could not send the generic Telegram error message after retries: %s",
                exc.__class__.__name__,
            )

# --- لود و ذخیره داده‌های کاربران ---
def load_user_data():
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            stored_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.error(f"Error loading user data: {str(e)}")
        return {}

    if not isinstance(stored_data, dict):
        logger.error("User data file must contain a JSON object; ignoring its contents")
        return {}

    raw_users = {
        str(admin_id): record
        for admin_id, record in stored_data.items()
        if isinstance(record, dict)
    }
    ignored_count = len(stored_data) - len(raw_users)
    if ignored_count:
        logger.warning(
            "Ignored %s non-raw user record(s); users must authenticate again",
            ignored_count,
        )
    return raw_users

def save_user_data(data):
    data_path = Path(USER_DATA_FILE)
    temporary_path = data_path.with_name(f".{data_path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    temporary_path.chmod(0o600)
    temporary_path.replace(data_path)

user_data = load_user_data()

# --- توابع API ---
class BargheManAPIError(Exception):
    """An API failure with a safe, actionable message suitable for Telegram."""

    def __init__(
        self,
        user_message: str,
        *,
        retryable: bool = False,
        delivery_uncertain: bool = False,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable
        self.delivery_uncertain = delivery_uncertain


COMMON_API_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://ios.bargheman.com",
    "referer": "https://ios.bargheman.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}


def create_session():
    session = requests.Session()
    # Keep the Iran-only API proxy scoped to Barghe Man requests. Telegram and
    # other traffic continue to use the machine's normal network/VPN route.
    session.trust_env = False
    session.proxies.update({
        'http': BARGHE_MAN_PROXY,
        'https': BARGHE_MAN_PROXY,
    })

    return session


def _extract_api_message(payload) -> str:
    if not isinstance(payload, dict):
        return ""

    for key in ('message', 'Message', 'error', 'Error', 'title'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]

    data = payload.get('data')
    if isinstance(data, dict):
        for key in ('message', 'Message', 'error', 'Error'):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return ""


def _request_json_sync(
    method: str,
    path: str,
    *,
    operation: str,
    headers: dict | None = None,
    payload: dict | None = None,
):
    url = f"{BASE_URI_PROXY.rstrip('/')}/{path.lstrip('/')}"
    request_headers = {**COMMON_API_HEADERS, **(headers or {})}
    session = create_session()

    try:
        try:
            response = session.request(
                method,
                url,
                headers=request_headers,
                json=payload,
                timeout=TIMEOUT,
            )
        except requests.exceptions.ProxyError as exc:
            raise BargheManAPIError(
                "اتصال به پراکسی برقرار نشد. تونل SOCKS روی 127.0.0.1:1080 را بررسی کنید.\n"
                + LINK_CHECK_NOTICE,
                retryable=True,
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise BargheManAPIError(
                "اعتبار گواهی HTTPS سرویس برق من تأیید نشد.",
                retryable=False,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise BargheManAPIError(
                f"سرویس برق من هنگام {operation} در مهلت مقرر پاسخ نداد.\n"
                + LINK_CHECK_NOTICE,
                retryable=True,
                delivery_uncertain=operation == "ارسال کد پیامک",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise BargheManAPIError(
                "ارتباط با سرویس برق من برقرار نشد. DNS محلی، تونل SSH و دسترسی VPS ایرانی را بررسی کنید.\n"
                + LINK_CHECK_NOTICE,
                retryable=True,
                delivery_uncertain=operation == "ارسال کد پیامک",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise BargheManAPIError(
                f"خطای ارتباطی هنگام {operation}: {exc.__class__.__name__}",
                retryable=True,
                delivery_uncertain=operation == "ارسال کد پیامک",
            ) from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            if response.ok:
                raise BargheManAPIError(
                    f"پاسخ {operation} JSON معتبر نبود؛ احتمالاً سرویس یا پراکسی پاسخ غیرمنتظره داده است.",
                    retryable=True,
                    delivery_uncertain=operation == "ارسال کد پیامک",
                ) from exc
            response_payload = {}

        server_message = _extract_api_message(response_payload)
        if not response.ok:
            if operation == "تأیید کد پیامک" and response.status_code in (400, 401, 403):
                message = "کد پیامک رد شده یا منقضی شده است."
            elif operation == "ارسال کد پیامک" and response.status_code in (401, 403):
                message = (
                    "سرویس برق من درخواست ارسال کد را رد کرد. این مرحله به نشست "
                    "ورود وابسته نیست؛ محدودیت موقت ارسال پیامک برای شماره یا IP، "
                    "یا ایرانی نبودن IP خروجی پراکسی را بررسی کنید و کمی بعد دوباره "
                    "تلاش کنید."
                )
            elif response.status_code in (401, 403):
                message = "دسترسی برق من رد شد یا نشست ورود منقضی شده است."
            elif response.status_code == 429:
                message = "تعداد درخواست‌ها بیش از حد مجاز است؛ کمی بعد دوباره تلاش کنید."
            elif response.status_code >= 500:
                message = "سرویس برق من موقتاً دچار خطای داخلی است."
            else:
                message = f"سرویس برق من درخواست {operation} را نپذیرفت."

            if server_message:
                message += f" پیام سرور: {server_message}"
            message += f" (HTTP {response.status_code})"
            raise BargheManAPIError(
                message,
                retryable=response.status_code == 429 or response.status_code >= 500,
                delivery_uncertain=(
                    operation == "ارسال کد پیامک"
                    and response.status_code in (429, 500, 502, 503, 504)
                ),
            )

        if not isinstance(response_payload, dict):
            raise BargheManAPIError(
                f"ساختار پاسخ {operation} معتبر نبود.",
                retryable=True,
            )

        # sendCode has returned non-standard application-level status values
        # while still delivering the SMS. A successful HTTP response is the
        # reliable acknowledgement for this endpoint.
        if operation == "ارسال کد پیامک":
            return response_payload

        api_status = response_payload.get('status')
        if api_status is not None and api_status not in (200, "200", True):
            message = server_message or f"سرویس برق من عملیات {operation} را ناموفق اعلام کرد."
            raise BargheManAPIError(
                message,
                retryable=str(api_status).startswith('5'),
                delivery_uncertain=operation == "ارسال کد پیامک",
            )

        return response_payload
    finally:
        session.close()


async def request_json(*args, **kwargs):
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        raise BargheManAPIError(
            "اتصال به سرویس برق من در تنظیمات غیرفعال است.\n" + LINK_CHECK_NOTICE
        )
    return await asyncio.to_thread(_request_json_sync, *args, **kwargs)


async def safe_api_call(func, *args, attempts: int = MAX_RETRIES, **kwargs):
    """Run every Barghe Man request exactly three times before surfacing an error."""
    for attempt in range(1, attempts + 1):
        try:
            return await func(*args, **kwargs)
        except BargheManAPIError as exc:
            logger.warning(
                "API attempt %s/%s failed: %s",
                attempt,
                attempts,
                exc.user_message,
            )
            if attempt == attempts:
                raise
            await asyncio.sleep(attempt)


async def send_otp_with_retry(mobile: str) -> bool:
    """Return False when delivery may have succeeded despite the final error."""
    last_error = None
    delivery_uncertain = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await send_otp(mobile)
            return True
        except BargheManAPIError as exc:
            last_error = exc
            delivery_uncertain = delivery_uncertain or exc.delivery_uncertain
            logger.warning(
                "SMS attempt %s/%s failed: %s",
                attempt,
                MAX_RETRIES,
                exc.user_message,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(attempt)

    if delivery_uncertain:
        return False
    raise last_error


async def send_otp(mobile: str):
    return await request_json(
        "POST",
        "/api/otp/sendCode",
        operation="ارسال کد پیامک",
        payload={"mobile": mobile},
    )


async def verify_otp(mobile: str, code: str):
    return await request_json(
        "POST",
        "/api/otp/verifyCode",
        operation="تأیید کد پیامک",
        payload={
            "mobile": mobile,
            "code": code,
            "request_source": 5,
            "device_token": "",
        },
    )


def _auth_headers(auth_token: str) -> dict:
    return {"authorization": f"Bearer {auth_token}"}


async def get_user_bills(auth_token: str) -> list[dict]:
    response = await request_json(
        "GET",
        "/api/ebills/GetBills",
        operation="دریافت خانه‌های موجود",
        headers=_auth_headers(auth_token),
    )
    data = response.get('data')
    if not isinstance(data, dict):
        raise BargheManAPIError(
            "پاسخ خانه‌های موجود فاقد بخش data معتبر بود.",
            retryable=False,
        )
    bills = data.get('bill_data', [])
    if bills is None:
        bills = []
    if not isinstance(bills, list):
        raise BargheManAPIError(
            "فهرست خانه‌های موجود ساختار معتبری نداشت.",
            retryable=False,
        )
    return [bill for bill in bills if isinstance(bill, dict)]


async def refresh_account_houses(admin_id: str, user: dict) -> list[dict]:
    """Refresh and persist all houses belonging to the authenticated account."""
    ensure_user_settings(admin_id, user)
    bills = await safe_api_call(get_user_bills, user['token'])
    houses = account_houses_from_bills(bills)
    user['account_houses'] = houses
    if not user['watched_bill_ids']:
        user['watched_bill_ids'] = bill_identifiers_from_bills(houses)
    save_user_data(user_data)
    return houses


async def initialize_watched_bill_ids(admin_id: str, user: dict) -> str | None:
    """Default the watched list to all account houses once, without coupling them."""
    ensure_user_settings(admin_id, user)
    if user['watched_bill_ids']:
        return None
    try:
        await refresh_account_houses(admin_id, user)
    except BargheManAPIError as exc:
        return exc.user_message

    return None


async def get_plan(
    auth_token: str,
    bill_id: str,
    plan_days: int,
) -> list[dict]:
    requested_dates = get_plan_dates(plan_days)
    response = await request_json(
        "POST",
        "/api/ebills/PlannedBlackoutsReport",
        operation=f"دریافت برنامه {len(requested_dates)} روزه قبض {bill_id}",
        headers=_auth_headers(auth_token),
        payload={
            "bill_id": bill_id,
            "from_date": requested_dates[0],
            "to_date": requested_dates[-1],
        },
    )

    try:
        items = _extract_plan_items(response.get("data"))
    except ValueError as exc:
        raise BargheManAPIError(
            f"ساختار برنامه خاموشی قبض {bill_id} معتبر نبود.",
            retryable=True,
        ) from exc

    # Keep a defensive range filter because some upstream responses may include
    # records outside from_date/to_date. A missing date is treated as today,
    # matching the historical API behavior.
    allowed_dates = set(requested_dates)
    range_items = []
    for item in items:
        normalized_item = _normalize_plan_item(item)
        normalized_date = normalized_item["date"]
        if not normalized_date or normalized_date in allowed_dates:
            range_items.append(item)
            if not normalized_item["start"]:
                logger.warning(
                    "Could not identify blackout start time for bill %s; "
                    "API fields were: %s",
                    bill_id,
                    sorted(str(key) for key in item),
                )
    return range_items


async def collect_plans(
    auth_token: str,
    bill_ids: list[str],
    plan_days: int,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    plans = {}
    errors = {}

    for bill_id in bill_ids:
        try:
            plans[bill_id] = await safe_api_call(
                get_plan,
                auth_token,
                bill_id,
                plan_days,
            )
        except BargheManAPIError as exc:
            errors[bill_id] = exc.user_message
        except Exception:
            logger.exception("Unexpected error while checking bill %s", bill_id)
            errors[bill_id] = "خطای داخلی غیرمنتظره رخ داد؛ جزئیات در خروجی کنسول ثبت شد."

    return plans, errors

_DATE_PATTERN = re.compile(
    r"(?<!\d)((?:13|14|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)"
)
_TIME_PATTERN = re.compile(
    r"(?<!\d)([01]?\d|2[0-3])\s*[:٫.]\s*([0-5]?\d)"
    r"(?:\s*[:٫.]\s*[0-5]?\d)?(?!\d)"
)


def _normalized_api_key(key) -> str:
    return re.sub(r"[\W_]+", "", str(key).casefold())


def _scalar_text(value) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _item_field(item: dict, *aliases: str) -> str:
    normalized = {
        _normalized_api_key(key): value
        for key, value in item.items()
    }
    for alias in aliases:
        value = _scalar_text(normalized.get(_normalized_api_key(alias)))
        if value:
            return value
    return ""


def _item_field_containing(item: dict, *fragments: str) -> str:
    normalized_fragments = tuple(_normalized_api_key(value) for value in fragments)
    for key, value in item.items():
        normalized_key = _normalized_api_key(key)
        if any(fragment in normalized_key for fragment in normalized_fragments):
            text = _scalar_text(value)
            if text:
                return text
    return ""


def _date_and_times(value) -> tuple[str, list[str]]:
    text = _scalar_text(value).translate(_PERSIAN_AND_ARABIC_DIGITS)
    date = ""
    date_match = _DATE_PATTERN.search(text)
    if date_match:
        year, month, day = (int(part) for part in date_match.groups())
        try:
            if year >= 1700:
                jalali = jdatetime.date.fromgregorian(
                    date=datetime(year, month, day).date()
                )
                date = jalali.strftime("%Y/%m/%d")
            else:
                date = f"{year:04d}/{month:02d}/{day:02d}"
        except ValueError:
            date = ""

    times = [
        f"{int(hour):02d}:{minute}"
        for hour, minute in _TIME_PATTERN.findall(text)
    ]
    return date, list(dict.fromkeys(times))


def _looks_like_plan_item(item: dict) -> bool:
    fragments = (
        "outage", "blackout", "date", "time", "start", "stop", "end",
        "address", "location", "خاموش", "تاریخ", "ساعت", "آدرس", "نشانی",
    )
    if any(
        fragment in _normalized_api_key(key)
        for key in item
        for fragment in fragments
    ):
        return True
    return any(_date_and_times(value) != ("", []) for value in item.values())


def _extract_plan_items(data) -> list[dict]:
    """Accept both the historical list response and nested result objects."""
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        raise ValueError("plan data is neither a list nor an object")
    if not data:
        return []

    normalized = {
        _normalized_api_key(key): value
        for key, value in data.items()
    }
    for key in (
        "planned_blackouts", "planned_outages", "planned", "blackouts",
        "outages", "records", "rows", "items", "results", "result",
        "data", "list",
    ):
        normalized_key = _normalized_api_key(key)
        if normalized_key not in normalized:
            continue
        candidate = normalized[normalized_key]
        if candidate is None:
            return []
        if isinstance(candidate, (list, dict)):
            return _extract_plan_items(candidate)

    if _looks_like_plan_item(data):
        return [data]

    candidate_lists = [value for value in data.values() if isinstance(value, list)]
    if candidate_lists:
        return [
            item
            for candidate in candidate_lists
            for item in candidate
            if isinstance(item, dict)
        ]
    raise ValueError("plan object contains no recognizable records")


def _normalize_plan_item(item: dict) -> dict:
    date_value = _item_field(
        item,
        "outage_date", "date", "outage_start_date", "start_date",
        "from_date", "outage_datetime", "outage_start_datetime",
        "start_datetime", "start_date_time", "outage_from_datetime",
        "from_datetime", "outage_from_date", "تاریخ",
    )
    start_value = _item_field(
        item,
        "outage_start_time", "start_time", "from_time", "start",
        "outage_time", "blackout_time", "cut_time", "disconnect_time",
        "power_cut_time", "outage_start", "from", "outage_start_date",
        "start_date",
        "outage_datetime", "outage_start_datetime", "start_datetime",
        "start_date_time", "outage_from_datetime", "from_datetime",
        "outage_from_date", "ساعت شروع",
    )
    stop_value = _item_field(
        item,
        "outage_stop_time", "outage_end_time", "stop_time", "end_time",
        "to_time", "finish_time", "stop", "end", "outage_stop",
        "outage_end", "to", "outage_stop_date", "outage_end_date",
        "end_date", "to_date", "outage_stop_datetime",
        "outage_end_datetime", "stop_datetime", "end_datetime",
        "to_datetime", "ساعت پایان", "ساعت اتمام",
    )

    date_from_date, times_from_date = _date_and_times(date_value)
    date_from_start, times_from_start = _date_and_times(start_value)
    date_from_stop, times_from_stop = _date_and_times(stop_value)

    # The live API has used a combined date/start-time value under field names
    # that differ between clients. Scan scalar values as a final fallback, but
    # only accept a time from a value that also contains a date so an end-time
    # or an unrelated number cannot be mistaken for the start.
    all_parts = [_date_and_times(value) for value in item.values()]
    combined_parts = [
        (parsed_date, parsed_times)
        for parsed_date, parsed_times in all_parts
        if parsed_date and parsed_times
    ]
    fallback_date = next(
        (parsed_date for parsed_date, _ in all_parts if parsed_date),
        "",
    )
    combined_start = next(
        (parsed_times[0] for _, parsed_times in combined_parts),
        "",
    )
    fallback_stop = next(
        (
            parsed_times[1]
            for _, parsed_times in combined_parts
            if len(parsed_times) > 1
        ),
        "",
    )

    # Some API versions send the date and start time as separate properties,
    # even though the website renders them together in one table column. If
    # the start key is unfamiliar, select a time-like scalar while excluding
    # the already recognized end-time property and value.
    stop_times = set(times_from_stop)
    separate_start_candidates = []
    for position, (key, value) in enumerate(item.items()):
        normalized_key = _normalized_api_key(key)
        _, parsed_times = _date_and_times(value)
        if not parsed_times or any(
            fragment in normalized_key
            for fragment in (
                "stop", "end", "finish", "totime", "رفع", "پایان", "اتمام",
            )
        ):
            continue
        priority = 0 if any(
            fragment in normalized_key
            for fragment in (
                "start", "from", "outage", "blackout", "cut", "disconnect",
                "time", "قطع", "شروع",
            )
        ) else 1
        separate_start_candidates.extend(
            (priority, position, parsed_time)
            for parsed_time in parsed_times
            if parsed_time not in stop_times
        )
    separate_start = min(separate_start_candidates, default=(2, 0, ""))[2]

    start = (
        (times_from_start[0] if times_from_start else "")
        or (times_from_date[0] if times_from_date else "")
        or combined_start
        or separate_start
    )
    stop = (
        (times_from_stop[0] if times_from_stop else "")
        or (times_from_start[1] if len(times_from_start) > 1 else "")
        or (times_from_date[1] if len(times_from_date) > 1 else "")
        or fallback_stop
    )
    address = _item_field(
        item,
        "outage_address", "address", "outage_location", "location",
        "address_text", "bill_address", "آدرس", "نشانی",
    ) or _item_field_containing(item, "address", "location", "آدرس", "نشانی")

    return {
        "date": date_from_date or date_from_start or date_from_stop or fallback_date,
        "start": start,
        "stop": stop,
        "address": address,
    }


def normalized_snapshot(plans: dict[str, list[dict]]) -> dict[str, list[dict]]:
    snapshot = {}
    for bill_id in sorted(plans):
        items = [_normalize_plan_item(item) for item in plans[bill_id]]
        snapshot[bill_id] = sorted(
            items,
            key=lambda item: (
                item["date"],
                item["start"],
                item["stop"],
                item["address"],
            ),
        )
    return snapshot


def snapshot_digest(
    plans: dict[str, list[dict]],
    plan_days: int = 1,
) -> str:
    canonical = json.dumps(
        {
            "plan_days": normalize_plan_days(plan_days),
            "plans": normalized_snapshot(plans),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def format_plan(
    plans: dict[str, list[dict]],
    bill_ids: list[str],
    *,
    plan_days: int = 1,
    house_titles: dict[str, str] | None = None,
    changed: bool = False,
) -> str:
    plan_days = normalize_plan_days(plan_days)
    requested_dates = get_plan_dates(plan_days)
    if plan_days == 1:
        heading = f"برنامه خاموشی امروز — {requested_dates[0]}"
    else:
        heading = (
            "همه روزهای موجود برنامه خاموشی — "
            f"{requested_dates[0]} تا {requested_dates[-1]}"
        )
    if changed:
        lines = [f"🔄 {heading} تغییر کرده است."]
    else:
        lines = [f"📅 {heading}"]

    date_labels = {
        0: "امروز",
        1: "فردا",
        2: "پس‌فردا",
    }
    house_titles = house_titles or {}
    for bill_id in bill_ids:
        house_title = house_titles.get(bill_id) or "نام خانه ثبت نشده"
        lines.extend([
            "",
            f"🏠 {house_title}",
            f"🔢 شناسه قبض: {bill_id}",
        ])
        items = normalized_snapshot({bill_id: plans.get(bill_id, [])})[bill_id]
        items_by_date = {date_value: [] for date_value in requested_dates}
        for item in items:
            item_date = item["date"] or requested_dates[0]
            if item_date in items_by_date:
                items_by_date[item_date].append(item)

        if plan_days == 1:
            displayed_dates = requested_dates
        else:
            # "All available" means dates for which the API actually returned
            # at least one record, not every empty date in the query window.
            displayed_dates = [
                date_value
                for date_value in requested_dates
                if items_by_date[date_value]
            ]

        if not displayed_dates:
            lines.extend([
                "",
                f"ℹ️ داده‌ای در بازه {requested_dates[0]} تا "
                f"{requested_dates[-1]} موجود نیست؛ ممکن است خاموشی‌ای "
                "برنامه‌ریزی نشده باشد.",
            ])
            continue

        for date_value in displayed_dates:
            offset = requested_dates.index(date_value)
            date_label = date_labels.get(offset, f"{offset} روز آینده")
            lines.extend(["", f"📆 {date_label} — {date_value}"])
            day_items = items_by_date[date_value]
            if not day_items:
                lines.append(
                    "ℹ️ داده‌ای برای این تاریخ موجود نیست؛ "
                    "ممکن است خاموشی‌ای برنامه‌ریزی نشده باشد."
                )
                continue

            for index, item in enumerate(day_items, 1):
                start = item["start"] or "؟"
                stop = item["stop"] or "؟"
                address = item["address"] or "نشانی اعلام نشده"
                lines.append(f"{index}. ⏰ {start} تا {stop}")
                lines.append(f"📍 {address}")

    return "\n".join(lines)


def reminder_setting_text() -> str:
    minutes = int(Config.BLACKOUT_REMINDER_MINUTES)
    if minutes == 0:
        return "🔕 یادآوری پیش از خاموشی غیرفعال است."
    return f"🔔 یادآوری خاموشی: {minutes} دقیقه پیش از شروع"


def _plan_start_datetime(item: dict) -> datetime | None:
    """Convert a normalized Jalali plan date/start time to local datetime."""
    date_text = (item.get('date') or get_jalali_today()).translate(
        _PERSIAN_AND_ARABIC_DIGITS
    )
    start_text = str(item.get('start') or '').translate(
        _PERSIAN_AND_ARABIC_DIGITS
    )
    date_match = re.fullmatch(r'(\d{4})/(\d{2})/(\d{2})', date_text)
    time_match = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)', start_text)
    if not date_match or not time_match:
        return None

    try:
        jalali_date = jdatetime.date(
            *(int(value) for value in date_match.groups())
        )
        gregorian_date = jalali_date.togregorian()
        hour, minute = (int(value) for value in time_match.groups())
        return pytz.timezone(Config.TIMEZONE).localize(datetime(
            gregorian_date.year,
            gregorian_date.month,
            gregorian_date.day,
            hour,
            minute,
        ))
    except ValueError:
        return None


def format_blackout_reminder(
    house_title: str,
    bill_id: str,
    item: dict,
    remaining_minutes: int,
) -> str:
    """Build the Telegram reminder from one normalized plan item."""
    return "\n".join([
        "🔔 یادآوری خاموشی",
        "",
        f"🏠 {house_title}",
        f"🔢 شناسه قبض: {bill_id}",
        f"📅 تاریخ: {item.get('date') or get_jalali_today()}",
        f"⚡ شروع خاموشی: {item.get('start') or '؟'}",
        f"⏳ حدود {remaining_minutes} دقیقه تا شروع خاموشی باقی مانده است.",
        f"🕒 پایان اعلام‌شده: {item.get('stop') or '؟'}",
        f"📍 {item.get('address') or 'نشانی اعلام نشده'}",
    ])


def _reminder_occurrence_key(bill_id: str, item: dict) -> str:
    return "|".join((
        bill_id,
        item.get('date') or get_jalali_today(),
        item.get('start') or '',
    ))


def _reminder_signature(item: dict) -> str:
    canonical = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _cancel_reminder_handle(handle):
    if isinstance(handle, asyncio.Task):
        handle.cancel()
        return
    schedule_removal = getattr(handle, 'schedule_removal', None)
    if schedule_removal:
        schedule_removal()


def cancel_user_reminders(admin_id: str, *, clear_sent: bool = True):
    """Cancel all pending in-memory reminders belonging to one user."""
    reminders = scheduled_blackout_reminders.pop(str(admin_id), {})
    for reminder in reminders.values():
        _cancel_reminder_handle(reminder.get('handle'))
    if clear_sent:
        sent_blackout_reminders.difference_update({
            key for key in sent_blackout_reminders if key[0] == str(admin_id)
        })


def _forget_reminder(admin_id: str, occurrence: str, signature: str):
    reminders = scheduled_blackout_reminders.get(admin_id, {})
    current = reminders.get(occurrence)
    if current and current.get('signature') == signature:
        reminders.pop(occurrence, None)
    if not reminders:
        scheduled_blackout_reminders.pop(admin_id, None)


async def _deliver_blackout_reminder(bot, payload: dict):
    admin_id = payload['admin_id']
    occurrence = payload['occurrence']
    signature = payload['signature']
    current = scheduled_blackout_reminders.get(admin_id, {}).get(occurrence)
    if not current or current.get('signature') != signature:
        return

    try:
        auth_user = user_data.get(admin_id)
        if not isinstance(auth_user, dict) or not auth_user.get('token'):
            return
        ensure_user_settings(admin_id, auth_user)
        bill_id = payload['bill_id']
        if bill_id not in auth_user['watched_bill_ids']:
            return
        item_date = payload['item'].get('date') or get_jalali_today()
        if item_date != get_jalali_today():
            return

        start_at = datetime.fromisoformat(payload['start_at'])
        now = datetime.now(pytz.timezone(Config.TIMEZONE))
        remaining_seconds = (start_at - now).total_seconds()
        if remaining_seconds <= 0:
            return
        remaining_minutes = max(1, int((remaining_seconds + 59) // 60))
        house_title = (
            house_titles_by_bill_id(auth_user).get(bill_id)
            or "نام خانه ثبت نشده"
        )
        await send_to_targets(
            bot,
            auth_user['target_chat_ids'],
            format_blackout_reminder(
                house_title,
                bill_id,
                payload['item'],
                remaining_minutes,
            ),
        )
        sent_blackout_reminders.add((admin_id, occurrence))
        logger.info(
            "Sent blackout reminder for user %s and bill %s",
            admin_id,
            bill_id,
        )
    finally:
        _forget_reminder(admin_id, occurrence, signature)


async def dispatch_blackout_reminder(context: ContextTypes.DEFAULT_TYPE):
    await _deliver_blackout_reminder(context.bot, context.job.data)


async def _run_blackout_reminder_task(
    application: Application,
    run_at: datetime,
    payload: dict,
):
    delay = max(
        0,
        (run_at - datetime.now(pytz.timezone(Config.TIMEZONE))).total_seconds(),
    )
    await asyncio.sleep(delay)
    await _deliver_blackout_reminder(application.bot, payload)


def schedule_blackout_reminders(
    application: Application,
    admin_id: str,
    auth_user: dict,
    plans: dict[str, list[dict]],
):
    """Reconcile one-shot reminders with the latest successful plan result."""
    admin_id = str(admin_id)
    reminder_minutes = int(Config.BLACKOUT_REMINDER_MINUTES)
    if reminder_minutes == 0:
        cancel_user_reminders(admin_id, clear_sent=False)
        return

    local_tz = pytz.timezone(Config.TIMEZONE)
    now = datetime.now(local_tz)
    desired = {}
    for bill_id, items in normalized_snapshot(plans).items():
        for item in items:
            start_at = _plan_start_datetime(item)
            if start_at is None:
                logger.warning(
                    "Could not schedule reminder for bill %s: start date/time is missing",
                    bill_id,
                )
                continue
            if start_at <= now:
                continue
            occurrence = _reminder_occurrence_key(bill_id, item)
            signature = _reminder_signature(item)
            desired[occurrence] = {
                'admin_id': admin_id,
                'bill_id': bill_id,
                'item': item,
                'start_at': start_at.isoformat(),
                'occurrence': occurrence,
                'signature': signature,
                'run_at': max(
                    start_at - timedelta(minutes=reminder_minutes),
                    now + timedelta(seconds=1),
                ),
            }

    active = scheduled_blackout_reminders.setdefault(admin_id, {})
    for occurrence in set(active) - set(desired):
        _cancel_reminder_handle(active.pop(occurrence).get('handle'))

    job_queue = getattr(application, 'job_queue', None)
    for occurrence, payload in desired.items():
        previous = active.get(occurrence)
        if (admin_id, occurrence) in sent_blackout_reminders:
            if previous:
                _cancel_reminder_handle(previous.get('handle'))
                active.pop(occurrence, None)
            continue
        if previous and previous.get('signature') == payload['signature']:
            continue
        if previous:
            _cancel_reminder_handle(previous.get('handle'))

        try:
            if job_queue:
                job_hash = hashlib.sha256(
                    f"{admin_id}|{occurrence}".encode('utf-8')
                ).hexdigest()[:16]
                handle = job_queue.run_once(
                    callback=dispatch_blackout_reminder,
                    when=payload['run_at'],
                    data=payload,
                    name=f"blackout_reminder_{job_hash}",
                    job_kwargs={'misfire_grace_time': 3600},
                )
            else:
                handle = asyncio.create_task(_run_blackout_reminder_task(
                    application,
                    payload['run_at'],
                    payload,
                ))
            active[occurrence] = {
                'signature': payload['signature'],
                'handle': handle,
            }
        except Exception:
            logger.exception(
                "Could not schedule blackout reminder for user %s and bill %s",
                admin_id,
                payload['bill_id'],
            )

    if not active:
        scheduled_blackout_reminders.pop(admin_id, None)


def format_plan_errors(errors: dict[str, str]) -> str:
    lines = [
        "❌ بررسی برنامه خاموشی کامل نشد.",
        "تا رفع خطا وضعیت برنامه به‌روزرسانی نمی‌شود:",
    ]
    needs_manual_link = False
    for bill_id, message in errors.items():
        if message.endswith("\n" + LINK_CHECK_NOTICE):
            message = message[:-(len(LINK_CHECK_NOTICE) + 1)]
            needs_manual_link = True
        lines.append(f"• شناسه قبض {bill_id}: {message}")
    if needs_manual_link:
        lines.extend(["", LINK_CHECK_NOTICE])
    return "\n".join(lines)


def split_message(message: str, limit: int = 4000) -> list[str]:
    chunks = []
    current_lines = []
    current_length = 0

    for line in message.splitlines():
        added_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + added_length > limit:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

        if len(line) > limit:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            chunks.extend(line[index:index + limit] for index in range(0, len(line), limit))
            continue

        current_lines.append(line)
        current_length += len(line) + (1 if current_length else 0)

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or [""]


def telegram_chat_reference(value: str):
    return int(value) if re.fullmatch(r'-?\d+', value) else value


async def telegram_request(func, *args, operation: str, **kwargs):
    """Retry a Telegram API operation three times before reporting failure."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Telegram attempt %s/%s failed during %s: %s",
                attempt,
                MAX_RETRIES,
                operation,
                exc.__class__.__name__,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(attempt)
    raise last_error


async def reply_text_with_retry(update: Update, text: str, **kwargs):
    """Retry a direct reply in the update's current chat."""
    message = update.effective_message
    if message is None:
        raise RuntimeError("Telegram update has no effective message to reply to")
    return await telegram_request(
        message.reply_text,
        text,
        operation=f"ارسال پاسخ به گفت‌وگوی {update.effective_chat.id}",
        **kwargs,
    )


async def send_private_text_with_retry(update: Update, text: str, **kwargs):
    """Send an interactive response only to the authorized user's PV."""
    message = update.effective_message
    admin_id = get_admin_id(update)
    if message is None or admin_id is None:
        raise RuntimeError("Telegram update has no user/message for a private reply")
    return await telegram_request(
        message.get_bot().send_message,
        chat_id=telegram_chat_reference(admin_id),
        text=text,
        operation=f"ارسال پاسخ خصوصی به کاربر {admin_id}",
        **kwargs,
    )


async def send_long_message(bot, chat_id, message: str):
    for chunk in split_message(message):
        await telegram_request(
            bot.send_message,
            chat_id=chat_id,
            text=chunk,
            disable_notification=False,
            operation=f"ارسال پیام به {chat_id}",
        )


async def send_to_targets(bot, target_chat_ids: list[str], message: str):
    """Best-effort delivery; one unavailable target must not block the others."""
    for target_chat_id in target_chat_ids:
        try:
            await send_long_message(
                bot,
                telegram_chat_reference(target_chat_id),
                message,
            )
        except Exception:
            logger.exception(
                "Could not send Telegram message to target %s",
                target_chat_id,
            )


async def check_user_and_notify(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: str,
    auth_user: dict,
):
    global notification_state
    ensure_user_settings(admin_id, auth_user)
    plan_days = 1
    logger.info(
        "Starting scheduled today-only blackout check for user %s",
        admin_id,
    )
    target_chat_ids = auth_user['target_chat_ids']
    initialization_error = await initialize_watched_bill_ids(admin_id, auth_user)
    if initialization_error:
        await send_to_targets(
            context.bot,
            target_chat_ids,
            "❌ بررسی خودکار انجام نشد؛ فهرست خانه‌های تحت نظر هنوز تنظیم نشده است.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {initialization_error}",
        )
        return
    bill_ids = auth_user['watched_bill_ids']

    if not bill_ids:
        await send_to_targets(
            context.bot,
            target_chat_ids,
            "❌ بررسی خودکار انجام نشد.\n"
            "هیچ خانه‌ای تحت نظر نیست. از منوی «👁 خانه‌های تحت نظر» استفاده کنید.",
        )
        return

    plans, errors = await collect_plans(
        auth_user['token'],
        bill_ids,
        plan_days,
    )
    if errors:
        await send_to_targets(
            context.bot,
            target_chat_ids,
            format_plan_errors(errors),
        )
        return

    application = getattr(context, 'application', context)
    schedule_blackout_reminders(application, admin_id, auth_user, plans)

    today = get_jalali_today()
    digest = snapshot_digest(plans, plan_days)
    target_states = notification_state.setdefault(admin_id, {})

    local_now = datetime.now(pytz.timezone(Config.TIMEZONE))
    sent_count = 0

    for target_chat_id in target_chat_ids:
        previous_state = target_states.get(target_chat_id, {})
        if not isinstance(previous_state, dict):
            previous_state = {}

        first_check_today = previous_state.get('date') != today
        plan_changed = (
            not first_check_today
            and previous_state.get('digest') != digest
        )
        if not first_check_today and not plan_changed:
            continue

        try:
            await send_long_message(
                context.bot,
                telegram_chat_reference(target_chat_id),
                format_plan(
                    plans,
                    bill_ids,
                    plan_days=plan_days,
                    house_titles=house_titles_by_bill_id(auth_user),
                    changed=plan_changed,
                ),
            )
        except Exception:
            logger.exception(
                "Could not send plan snapshot to target %s",
                target_chat_id,
            )
            continue

        target_states[target_chat_id] = {
            "date": today,
            "digest": digest,
            "sent_at": local_now.isoformat(),
        }
        sent_count += 1

    if sent_count:
        logger.info("Sent plan snapshot to %s target chat(s)", sent_count)
    else:
        logger.info("The selected plan range has not changed for any target")


async def check_all_users_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """Run one globally scheduled check or link reminder for each admin."""
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        message = "🔔 زمان بررسی برنامه خاموشی است.\n" + LINK_CHECK_NOTICE
        target_chat_ids = []
        for admin_id in Config.ADMIN_CHAT_IDS:
            auth_user = user_data.get(admin_id)
            if isinstance(auth_user, dict):
                if ensure_user_settings(admin_id, auth_user):
                    save_user_data(user_data)
                target_chat_ids.extend(auth_user['target_chat_ids'])
            else:
                target_chat_ids.append(admin_id)
        await send_to_targets(
            context.bot,
            list(dict.fromkeys(target_chat_ids)),
            message,
        )
        return

    for admin_id in Config.ADMIN_CHAT_IDS:
        auth_user = user_data.get(admin_id)
        if not isinstance(auth_user, dict) or not auth_user.get('token'):
            continue
        changed = ensure_user_settings(admin_id, auth_user)
        if changed:
            save_user_data(user_data)
        try:
            await check_user_and_notify(context, admin_id, auth_user)
        except Exception:
            logger.exception("Unexpected scheduled-check failure for user %s", admin_id)
            await send_to_targets(
                context.bot,
                auth_user['target_chat_ids'],
                "❌ بررسی خودکار به دلیل یک خطای داخلی غیرمنتظره انجام نشد.",
            )


async def check_blackouts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    plan_days: int = 1,
):
    """Run one private, interactive plan lookup without changing automation."""
    if not await require_private_admin(update):
        return

    admin_id = get_admin_id(update)
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        await reply_text_with_retry(
            update,
            LINK_CHECK_NOTICE,
            reply_markup=get_menu_markup(admin_id),
        )
        return

    allowed, remaining, reset_time = check_rate_limit(admin_id)
    if not allowed:
        reset_str = reset_time.strftime("%Y-%m-%d %H:%M:%S") if reset_time else "پس از 24 ساعت"
        await reply_text_with_retry(update,
            f"⚠️ شما به سقف درخواست‌های روزانه ({RATE_LIMIT['daily_limit']}) رسیده‌اید.\n"
            f"⏳ لطفاً پس از {reset_str} دوباره تلاش کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    user = user_data.get(admin_id, {})
    if not user.get('token'):
        await reply_text_with_retry(update,
            "⚠️ نشست ورود پیدا نشد. ابتدا از منوی عضویت وارد برق من شوید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    ensure_user_settings(admin_id, user)
    plan_days = normalize_plan_days(plan_days)
    initialization_error = await initialize_watched_bill_ids(admin_id, user)
    if initialization_error:
        await reply_text_with_retry(update,
            "❌ فهرست خانه‌های تحت نظر هنوز تنظیم نشده است.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {initialization_error}",
            reply_markup=get_menu_markup(admin_id),
        )
        return
    bill_ids = user['watched_bill_ids']
    if not bill_ids:
        await reply_text_with_retry(update,
            "⚠️ هیچ خانه‌ای تحت نظر نیست. از منوی «👁 خانه‌های تحت نظر» استفاده کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    requested_dates = get_plan_dates(plan_days)
    requested_range = (
        requested_dates[0]
        if plan_days == 1
        else f"{requested_dates[0]} تا {requested_dates[-1]}"
    )
    await reply_text_with_retry(update,
        f"🔍 در حال بررسی برنامه {plan_days_description(plan_days)} "
        f"({requested_range}) برای {len(bill_ids)} خانه تحت نظر..."
    )

    plans, errors = await collect_plans(user['token'], bill_ids, plan_days)
    if errors:
        await reply_text_with_retry(update,
            format_plan_errors(errors),
            reply_markup=get_menu_markup(admin_id),
        )
        return

    for chunk in split_message(format_plan(
        plans,
        bill_ids,
        plan_days=plan_days,
        house_titles=house_titles_by_bill_id(user),
    )):
        await reply_text_with_retry(update,
            chunk,
            reply_markup=get_menu_markup(admin_id),
        )

    if remaining is not None and remaining < 3:
        await reply_text_with_retry(update,
            f"ℹ️ شما {remaining} درخواست تا پایان بازه فعلی دارید.",
            reply_markup=get_menu_markup(admin_id),
        )


# --- رابط کاربری تلگرام ---
def telegram_user_display_name(user) -> str:
    if not user:
        return "نامشخص"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def telegram_chat_display_name(chat) -> str:
    title = getattr(chat, 'title', None)
    full_name = getattr(chat, 'full_name', None)
    username = getattr(chat, 'username', None)
    return title or full_name or (f"@{username}" if username else str(chat.id))


def telegram_chat_type_label(chat_type: str) -> str:
    return {
        'private': 'گفت‌وگوی خصوصی',
        'group': 'گروه',
        'supergroup': 'سوپرگروه',
        'channel': 'کانال',
    }.get(chat_type, chat_type or 'نامشخص')


async def require_private_admin(update: Update) -> bool:
    if not is_authorized_admin(update):
        return False
    if update.effective_chat.type != 'private':
        await send_private_text_with_retry(
            update,
            "ℹ️ این فرمان فقط در گفت‌وگوی خصوصی قابل استفاده است.\n"
            "لطفاً ادامه را در همین PV انجام دهید. برای دیدن مشخصات یک گروه، "
            "/chatid را در همان گروه اجرا کنید؛ نتیجه به این PV فرستاده می‌شود.",
        )
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_admin(update):
        return

    admin_id = get_admin_id(update)
    if update.effective_chat.type != 'private':
        await send_private_text_with_retry(
            update,
            "✅ شما مدیر مجاز هستید.\n"
            "پاسخ‌های تعاملی ربات فقط در همین گفت‌وگوی خصوصی ارسال می‌شوند.\n"
            "برای مشخصات گروه یا کانال از /chatid استفاده کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    authenticated_user = user_data.get(admin_id, {})
    authenticated = bool(authenticated_user.get('token'))
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        message = (
            f"سلام {telegram_user_display_name(update.effective_user)}.\n"
            "⏰ ساعت‌های یادآوری بررسی برنامه: "
            + "، ".join(Config.CHECK_TIMES)
            + "\n"
            + LINK_CHECK_NOTICE
        )
    elif authenticated:
        if ensure_user_settings(admin_id, authenticated_user):
            save_user_data(user_data)
        message = (
            f"سلام {telegram_user_display_name(update.effective_user)}.\n"
            "✅ نشست ورود برق من برای شما ثبت شده است.\n"
            "⏰ ساعت‌های بررسی خودکار سراسری: "
            + "، ".join(Config.CHECK_TIMES)
            + "\n📅 بررسی خودکار و یادآوری‌ها: فقط برنامه امروز"
            + "\n"
            + reminder_setting_text()
        )
    else:
        message = (
            f"سلام {telegram_user_display_name(update.effective_user)}.\n"
            "برای استفاده از امکانات، ابتدا احراز هویت کنید."
        )

    await reply_text_with_retry(update,
        message,
        reply_markup=get_menu_markup(admin_id),
    )


async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_admin(update):
        return

    chat = update.effective_chat
    reply = getattr(update.effective_message, 'reply_to_message', None)
    forwarded_chat = None
    if reply:
        origin = getattr(reply, 'forward_origin', None)
        forwarded_chat = getattr(origin, 'chat', None)
        if forwarded_chat is None:
            # Compatibility with forwarded messages received through older
            # Telegram Bot API fields.
            forwarded_chat = getattr(reply, 'forward_from_chat', None)
    if forwarded_chat is not None:
        chat = forwarded_chat

    user = update.effective_user
    username = f"@{chat.username}" if getattr(chat, 'username', None) else "ندارد"
    lines = [
        "ℹ️ مشخصات این مقصد\n"
        f"• نام: {telegram_chat_display_name(chat)}\n"
        f"• نوع: {telegram_chat_type_label(chat.type)}\n"
        f"• Chat ID: {chat.id}\n"
        f"• Username: {username}"
    ]
    if user:
        lines.append(
            "\n👤 فرستنده دستور\n"
            f"• نام: {telegram_user_display_name(user)}\n"
            f"• User ID: {user.id}"
        )
    await send_private_text_with_retry(
        update,
        "".join(lines),
    )


async def show_available_houses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return

    admin_id = get_admin_id(update)
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        await reply_text_with_retry(
            update,
            LINK_CHECK_NOTICE,
            reply_markup=get_menu_markup(admin_id),
        )
        return
    auth_user = user_data.get(admin_id, {})
    if not auth_user.get('token'):
        await reply_text_with_retry(update,
            "⚠️ ابتدا احراز هویت کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    ensure_user_settings(admin_id, auth_user)
    await reply_text_with_retry(update, "🔍 در حال به‌روزرسانی همه خانه‌های حساب...")
    try:
        houses = await refresh_account_houses(admin_id, auth_user)
    except BargheManAPIError as exc:
        await reply_text_with_retry(update,
            "❌ خانه‌های موجود دریافت نشدند.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {exc.user_message}",
            reply_markup=get_menu_markup(admin_id),
        )
        return
    except Exception:
        logger.exception("Unexpected error while listing houses")
        await reply_text_with_retry(update,
            "❌ دریافت خانه‌ها با خطای داخلی مواجه شد.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    if not houses:
        message = "🏠 هیچ خانه یا قبضی در حساب برق من ثبت نشده است."
    else:
        watched = set(auth_user['watched_bill_ids'])
        lines = [
            f"🏠 همه خانه‌های حساب ({len(houses)} مورد)",
            f"👁 خانه‌های تحت نظر ({len(watched)} مورد)",
        ]
        for index, house in enumerate(houses, 1):
            title = house['bill_title']
            identifier = house['bill_identifier']
            watch_status = "👁 تحت نظر" if identifier in watched else "➖ تحت نظر نیست"
            lines.extend([
                "",
                f"{index}. {title}",
                f"شناسه قبض: {identifier}",
                watch_status,
            ])
        message = "\n".join(lines)

    for chunk in split_message(message):
        await reply_text_with_retry(update,
            chunk,
            reply_markup=get_menu_markup(admin_id),
        )


async def show_destinations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    admin_id = get_admin_id(update)
    auth_user = user_data.get(admin_id, {})
    if Config.DOMESTIC_SERVER_AVAILABLE and not auth_user.get('token'):
        await reply_text_with_retry(update,
            "⚠️ ابتدا احراز هویت کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return ConversationHandler.END

    ensure_user_settings(admin_id, auth_user)

    lines = [
        "📨 مقصدهای تنظیم‌شده برای ارسال",
        f"👤 مدیر فعلی: {telegram_user_display_name(update.effective_user)} "
        f"(User ID: {update.effective_user.id})",
    ]

    for index, configured_target in enumerate(auth_user['target_chat_ids'], 1):
        reference = telegram_chat_reference(configured_target)
        try:
            chat = await telegram_request(
                context.bot.get_chat,
                reference,
                operation=f"دریافت مشخصات مقصد {configured_target}",
            )
            username = (
                f"@{chat.username}"
                if getattr(chat, 'username', None)
                else "ندارد"
            )
            lines.extend([
                "",
                f"{index}. {telegram_chat_display_name(chat)}",
                f"نوع: {telegram_chat_type_label(chat.type)}",
                f"Chat ID: {chat.id}",
                f"Username: {username}",
                "وضعیت: ✅ قابل دسترسی",
            ])
        except Exception:
            lines.extend([
                "",
                f"{index}. {configured_target}",
                "وضعیت: ❌ پس از 3 تلاش قابل دسترسی نبود",
            ])

    for chunk in split_message("\n".join(lines)):
        await reply_text_with_retry(update,
            chunk,
        )

    await reply_text_with_retry(update,
        "برای ویرایش، مقصدها را با ویرگول بفرستید.\n"
        "نمونه: pv, -1001234567890, @public_channel\n"
        "عبارت pv یعنی همین گفت‌وگوی خصوصی. برای انصراف /cancel را بزنید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EDIT_TARGETS


async def save_destinations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    admin_id = get_admin_id(update)
    auth_user = user_data.get(admin_id, {})
    if Config.DOMESTIC_SERVER_AVAILABLE and not auth_user.get('token'):
        return ConversationHandler.END
    try:
        targets = parse_target_chat_ids(
            update.effective_message.text,
            str(update.effective_chat.id),
        )
    except ValueError as exc:
        await reply_text_with_retry(update,
            f"⚠️ {exc}\nدوباره وارد کنید یا /cancel را بزنید."
        )
        return EDIT_TARGETS

    if admin_id not in user_data:
        auth_user = {
            'telegram_user_id': update.effective_user.id,
            'telegram_chat_id': update.effective_chat.id,
            'telegram_name': update.effective_user.full_name,
            'telegram_username': update.effective_user.username,
        }
        user_data[admin_id] = auth_user
    ensure_user_settings(admin_id, auth_user)
    auth_user['target_chat_ids'] = targets
    save_user_data(user_data)
    await reply_text_with_retry(update,
        "✅ مقصدهای ارسال ذخیره شدند:\n" + "\n".join(f"• {item}" for item in targets),
        reply_markup=get_menu_markup(admin_id),
    )
    return ConversationHandler.END


async def check_plan_for_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    plan_days: int,
):
    """Fetch one requested range and display it only in the user's PV."""
    if not await require_private_admin(update):
        return
    admin_id = get_admin_id(update)
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        await reply_text_with_retry(
            update,
            LINK_CHECK_NOTICE,
            reply_markup=get_menu_markup(admin_id),
        )
        return
    auth_user = user_data.get(admin_id, {})
    if not auth_user.get('token'):
        await reply_text_with_retry(
            update,
            "⚠️ ابتدا احراز هویت کنید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return

    if ensure_user_settings(admin_id, auth_user):
        save_user_data(user_data)
    await check_blackouts(
        update,
        context,
        plan_days=normalize_plan_days(plan_days),
    )


async def check_today_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_plan_for_days(update, context, 1)


async def check_all_available_plans(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await check_plan_for_days(update, context, ALL_AVAILABLE_PLAN_DAYS)


async def start_edit_watched_houses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END
    admin_id = get_admin_id(update)
    auth_user = user_data.get(admin_id, {})
    if not auth_user.get('token'):
        return ConversationHandler.END
    ensure_user_settings(admin_id, auth_user)
    current = "، ".join(auth_user['watched_bill_ids']) or "تنظیم نشده"
    account_houses = auth_user['account_houses']
    account_lines = []
    for house in account_houses:
        account_lines.append(
            f"• {house['bill_title']}: {house['bill_identifier']}"
        )
    account_summary = (
        "\n".join(account_lines)
        if account_lines
        else "هنوز دریافت نشده؛ ابتدا «🏠 نمایش خانه‌های موجود» را بزنید."
    )
    await reply_text_with_retry(update,
        f"👁 شناسه‌های قبض تحت نظر: {current}\n\n"
        f"🏠 همه خانه‌های ذخیره‌شده حساب:\n{account_summary}\n\n"
        "شناسه‌های موردنظر را با ویرگول بفرستید، یا «همه» را بفرستید تا همه خانه‌های حساب تحت نظر باشند.\n"
        "برای انصراف /cancel را بزنید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EDIT_WATCHED_HOUSES


async def save_watched_houses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END
    admin_id = get_admin_id(update)
    auth_user = user_data.get(admin_id, {})
    if not auth_user.get('token'):
        return ConversationHandler.END

    value = update.effective_message.text.strip()
    if value in ('همه', 'all'):
        try:
            houses = await refresh_account_houses(admin_id, auth_user)
        except BargheManAPIError as exc:
            await reply_text_with_retry(update,
                "❌ خانه‌ها دریافت نشدند.\n"
                f"علت پس از {MAX_RETRIES} تلاش: {exc.user_message}\n"
                "دوباره تلاش کنید، شناسه‌ها را دستی وارد کنید یا /cancel را بزنید."
            )
            return EDIT_WATCHED_HOUSES
        bill_ids = bill_identifiers_from_bills(houses)
        if not bill_ids:
            await reply_text_with_retry(update,
                "⚠️ هیچ شناسه قبض معتبری در خانه‌های حساب پیدا نشد. دستی وارد کنید یا /cancel را بزنید."
            )
            return EDIT_WATCHED_HOUSES
    else:
        try:
            bill_ids = parse_bill_ids(value)
        except ValueError as exc:
            await reply_text_with_retry(update,
                f"⚠️ {exc}\nدوباره وارد کنید یا /cancel را بزنید."
            )
            return EDIT_WATCHED_HOUSES

    auth_user['watched_bill_ids'] = bill_ids
    save_user_data(user_data)
    await reply_text_with_retry(update,
        "✅ خانه‌های تحت نظر ذخیره شدند:\n" + "\n".join(f"• {item}" for item in bill_ids),
        reply_markup=get_menu_markup(admin_id),
    )
    return ConversationHandler.END


# --- شروع یا تجدید احراز هویت ---
async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    admin_id = get_admin_id(update)
    if not Config.DOMESTIC_SERVER_AVAILABLE:
        await reply_text_with_retry(
            update,
            LINK_CHECK_NOTICE,
            reply_markup=get_menu_markup(admin_id),
        )
        return ConversationHandler.END
    reauthenticating = update.effective_message.text == "🔄 احراز هویت مجدد"
    if user_data.get(admin_id, {}).get('token') and not reauthenticating:
        await reply_text_with_retry(update,
            "✅ شما قبلاً احراز هویت شده‌اید.",
            reply_markup=get_menu_markup(admin_id),
        )
        return ConversationHandler.END

    context.user_data['reauthenticating'] = reauthenticating
    await reply_text_with_retry(update,
        "لطفاً شماره موبایل خود را وارد کنید (مثال: 09123456789):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return GET_MOBILE


async def get_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    mobile = update.effective_message.text.strip()
    if not validate_mobile(mobile):
        await reply_text_with_retry(update,
            "⚠️ شماره موبایل نامعتبر است. شماره باید با 09 شروع شود و 11 رقم داشته باشد."
        )
        return GET_MOBILE

    try:
        delivery_confirmed = await send_otp_with_retry(mobile)
    except BargheManAPIError as exc:
        await reply_text_with_retry(update,
            "❌ کد پیامک ارسال نشد.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {exc.user_message}\n\n"
            "پس از رفع مشکل، شماره را دوباره بفرستید یا /cancel را بزنید."
        )
        return GET_MOBILE
    except Exception:
        logger.exception("Unexpected error while sending OTP")
        await reply_text_with_retry(update,
            "❌ کد پیامک پس از سه تلاش ارسال نشد؛ یک خطای داخلی رخ داد."
        )
        return GET_MOBILE

    context.user_data['mobile'] = mobile
    if delivery_confirmed:
        message = (
            "✅ سرویس برق من ارسال کد را تأیید کرد.\n"
            "لطفاً کد 6 رقمی پیامک‌شده را وارد کنید:"
        )
    else:
        message = (
            "⚠️ پاسخ نهایی ارسال کد قطعی نبود، اما ممکن است پیامک ارسال شده باشد.\n"
            "اگر کد را دریافت کرده‌اید، همان کد 6 رقمی را وارد کنید. "
            "در غیر این صورت /cancel را بزنید و کمی بعد دوباره تلاش کنید.\n\n"
            + LINK_CHECK_NOTICE
        )
    await reply_text_with_retry(update,
        message,
        reply_markup=ReplyKeyboardRemove(),
    )
    return GET_OTP


async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    otp = update.effective_message.text.strip()
    if not validate_otp(otp):
        await reply_text_with_retry(update,
            "⚠️ کد نامعتبر است. دقیقاً 6 رقم وارد کنید یا /cancel را بزنید."
        )
        return GET_OTP

    mobile = context.user_data.get('mobile')
    if not mobile:
        await reply_text_with_retry(update,
            "❌ شماره موبایل این گفتگو پیدا نشد. احراز هویت را از ابتدا شروع کنید.",
            reply_markup=get_menu_markup(get_admin_id(update)),
        )
        return ConversationHandler.END

    try:
        result = await safe_api_call(verify_otp, mobile, otp)
        result_data = result.get('data')
        if not isinstance(result_data, dict):
            raise BargheManAPIError(
                "پاسخ تأیید کد فاقد بخش data بود.",
                retryable=False,
            )
        token = result_data.get('Token') or result_data.get('token')
        if not token:
            raise BargheManAPIError(
                "پاسخ تأیید کد فاقد توکن ورود بود.",
                retryable=False,
            )
    except BargheManAPIError as exc:
        await reply_text_with_retry(update,
            "❌ ورود انجام نشد.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {exc.user_message}\n\n"
            "کد دیگری وارد کنید یا /cancel را بزنید و کد جدید بگیرید."
        )
        return GET_OTP
    except Exception:
        logger.exception("Unexpected error while verifying OTP")
        await reply_text_with_retry(update,
            "❌ ورود پس از سه تلاش با خطای داخلی مواجه شد."
        )
        return GET_OTP

    admin_id = get_admin_id(update)
    telegram_user = update.effective_user
    previous_user = user_data.get(admin_id, {})
    if not isinstance(previous_user, dict):
        previous_user = {}
    ensure_user_settings(admin_id, previous_user)
    target_chat_ids = previous_user.get('target_chat_ids')
    if not isinstance(target_chat_ids, list) or not target_chat_ids:
        target_chat_ids = [str(update.effective_chat.id)]

    same_account = previous_user.get('mobile') == mobile
    account_houses = (
        list(previous_user['account_houses']) if same_account else []
    )
    watched_bill_ids = (
        list(previous_user['watched_bill_ids']) if same_account else []
    )

    bills_error = None
    try:
        bills = await safe_api_call(get_user_bills, token)
        account_houses = account_houses_from_bills(bills)
        if not watched_bill_ids:
            watched_bill_ids = bill_identifiers_from_bills(account_houses)
    except BargheManAPIError as exc:
        bills_error = exc.user_message

    user_data[admin_id] = {
        'mobile': mobile,
        'token': token,
        'authenticated_at': datetime.now(pytz.UTC).isoformat(),
        'telegram_user_id': telegram_user.id,
        'telegram_chat_id': update.effective_chat.id,
        'telegram_name': telegram_user.full_name,
        'telegram_username': telegram_user.username,
        'target_chat_ids': target_chat_ids,
        'account_houses': account_houses,
        'watched_bill_ids': watched_bill_ids,
    }
    save_user_data(user_data)
    context.user_data.clear()

    message = (
        "✅ احراز هویت برق من با موفقیت ثبت شد.\n"
        f"🏠 تعداد همه خانه‌های حساب: {len(account_houses)}\n"
        f"👁 تعداد خانه‌های تحت نظر: {len(watched_bill_ids)}\n"
        f"🎯 مقصد پیش‌فرض: {', '.join(target_chat_ids)}\n"
        "📅 بررسی خودکار و یادآوری‌ها: فقط برنامه امروز\n"
        f"⏰ ساعت‌های بررسی خودکار سراسری: {'، '.join(Config.CHECK_TIMES)}\n"
        f"{reminder_setting_text()}"
    )
    if bills_error:
        message += (
            "\n\n⚠️ ورود موفق بود، اما فهرست همه خانه‌های حساب به‌روزرسانی نشد.\n"
            f"علت پس از {MAX_RETRIES} تلاش: {bills_error}\n"
            "از منوی «🏠 نمایش خانه‌های موجود» دوباره تلاش کنید. "
            "فهرست «👁 خانه‌های تحت نظر» مستقل است و می‌توانید آن را دستی تنظیم کنید."
        )
    elif not account_houses:
        message += (
            "\n\n⚠️ هیچ خانه‌ای در حساب پیدا نشد؛ خانه‌های تحت نظر را می‌توانید دستی وارد کنید."
        )

    await reply_text_with_retry(update,
        message,
        reply_markup=get_menu_markup(admin_id),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if not await require_private_admin(update):
        return ConversationHandler.END
    admin_id = get_admin_id(update)
    await reply_text_with_retry(update,
        "عملیات لغو شد.",
        reply_markup=(
            get_menu_markup(admin_id)
            if is_authorized_admin(update)
            else ReplyKeyboardRemove()
        ),
    )
    return ConversationHandler.END


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    admin_id = get_admin_id(update)
    if user_data.get(admin_id, {}).get('token'):
        keyboard = [["✔️ تأیید حذف نشست", "❎ انصراف"]]
        await reply_text_with_retry(update,
            "⚠️ توکن ورود و مشخصات ذخیره‌شده شما حذف می‌شود.\n"
            "پس از حذف، بررسی خودکار مخصوص شما متوقف می‌شود.\n\n"
            "آیا مطمئن هستید؟",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return "CONFIRM_DELETION"

    await reply_text_with_retry(update,
        "نشست ورودی برای شما ثبت نشده است.",
        reply_markup=get_menu_markup(admin_id),
    )
    return ConversationHandler.END


async def confirm_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return ConversationHandler.END

    admin_id = get_admin_id(update)
    if update.effective_message.text == "✔️ تأیید حذف نشست":
        user = user_data.pop(admin_id, None)
        notification_state.pop(admin_id, None)
        cancel_user_reminders(admin_id)
        save_user_data(user_data)
        mobile = user.get('mobile', 'نامشخص') if user else 'نامشخص'
        await reply_text_with_retry(update,
            f"✅ نشست ورود شماره {mobile} حذف شد.",
            reply_markup=get_menu_markup(admin_id),
        )
    else:
        await reply_text_with_retry(update,
            "عملیات حذف لغو شد.",
            reply_markup=get_menu_markup(admin_id),
        )
    return ConversationHandler.END


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private_admin(update):
        return

    text = update.effective_message.text
    if text in ("🔐 احراز هویت", "🔄 احراز هویت مجدد"):
        await start_registration(update, context)
    elif text == "🏠 نمایش خانه‌های موجود":
        await show_available_houses(update, context)
    elif text == "📅 برنامه امروز":
        await check_today_plan(update, context)
    elif text == "🗓 همه روزهای موجود":
        await check_all_available_plans(update, context)
    elif text == "🔗 مشاهده برنامه در وب‌سایت":
        await reply_text_with_retry(
            update,
            LINK_CHECK_NOTICE,
            reply_markup=get_menu_markup(get_admin_id(update)),
        )
    elif text == "🎯 مقصدهای ارسال":
        await show_destinations(update, context)
    elif text == "👁 خانه‌های تحت نظر":
        await start_edit_watched_houses(update, context)
    elif text == "🚪 حذف نشست ورود":
        await delete_account(update, context)


async def setup_scheduler(application: Application):
    """Register one daily job per globally configured check time."""
    job_queue = application.job_queue
    if job_queue:
        local_tz = pytz.timezone(Config.TIMEZONE)
        for check_time in Config.CHECK_TIMES:
            hour, minute = (int(value) for value in check_time.split(':', 1))
            job_queue.run_daily(
                callback=check_all_users_and_notify,
                time=time(hour=hour, minute=minute, tzinfo=local_tz),
                name=f"global_blackout_check_{hour:02d}_{minute:02d}",
                job_kwargs={'misfire_grace_time': 3600},
            )

        # ریست روزانه محدودیت‌ها در نیمه شب
        reset_time = time(hour=0, minute=0, tzinfo=local_tz)
        job_queue.run_daily(
            callback=reset_daily_limits,
            time=reset_time,
            name="reset_rate_limits"
        )

        logger.info(
            "Global blackout checks scheduled at %s (%s)",
            ", ".join(Config.CHECK_TIMES),
            Config.TIMEZONE,
        )
        if not Config.DOMESTIC_SERVER_AVAILABLE:
            logger.info("Link reminders enabled; blackout start-time reminders are disabled")
        elif Config.BLACKOUT_REMINDER_MINUTES:
            logger.info(
                "Blackout reminders enabled %s minute(s) before each start time",
                Config.BLACKOUT_REMINDER_MINUTES,
            )
        else:
            logger.info("Blackout reminders are disabled")
    else:
        logger.warning("JobQueue is unavailable; using the asyncio scheduler")
        asyncio.create_task(manual_scheduler(application))
    logger.info("✅ ربات شروع به کار کرد")

async def manual_scheduler(application: Application):
    """راه‌حل جایگزین زمانی که Job Queue کار نمی‌کند"""
    last_check_key = None
    last_reset_date = None
    local_tz = pytz.timezone(Config.TIMEZONE)

    while True:
        now = datetime.now(local_tz)
        current_date = now.strftime('%Y-%m-%d')
        current_time = now.strftime('%H:%M')
        current_key = f"{current_date} {current_time}"

        if current_time in Config.CHECK_TIMES and current_key != last_check_key:
            await check_all_users_and_notify(application)
            last_check_key = current_key

        if current_time == '00:00' and last_reset_date != current_date:
            await reset_daily_limits(application)
            last_reset_date = current_date

        await asyncio.sleep(20)

# --- تنظیمات اصلی ربات ---
def main():
    Config.validate()
    telegram_api_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )
    telegram_updates_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(telegram_api_request)
        .get_updates_request(telegram_updates_request)
        .post_init(setup_scheduler)
        .build()
    )

    # تنظیم هندلرها
    app.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^(🔐 احراز هویت|🔄 احراز هویت مجدد)$"),
                start_registration,
            ),
            MessageHandler(filters.Regex("^🎯 مقصدهای ارسال$"), show_destinations),
            MessageHandler(filters.Regex("^👁 خانه‌های تحت نظر$"), start_edit_watched_houses),
            CommandHandler("destinations", show_destinations),
            CommandHandler("bills", start_edit_watched_houses),
        ],
        states={
            GET_MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mobile)],
            GET_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            EDIT_TARGETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_destinations)],
            EDIT_WATCHED_HOUSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_watched_houses)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    deletion_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🚪 حذف نشست ورود$"), delete_account)],
        states={
            "CONFIRM_DELETION": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_deletion)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # اضافه کردن هندلرها
    app.add_handler(TypeHandler(Update, enforce_admin_allowlist), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", show_chat_id))
    app.add_handler(CommandHandler("houses", show_available_houses))
    app.add_handler(CommandHandler("check", check_today_plan))
    app.add_handler(conv_handler)
    app.add_handler(deletion_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    app.run_polling()
    logger.info("🛑 ربات متوقف شد")

if __name__ == "__main__":
    main()
