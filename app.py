# -*- coding: utf-8 -*-
"""
بوت الرد التلقائي لمتجر ملابس
================================
يرد تلقائياً بالذكاء الاصطناعي (Google Gemini) على:
  1. رسائل Facebook Messenger الخاصة بالصفحة.
  2. تعليقات منشورات صفحة Facebook (رد عام قصير + رسالة خاصة بالتفاصيل Private Reply).
  3. رسائل Instagram Direct للحساب المرتبط بالصفحة.
  4. تعليقات منشورات Instagram (رد عام قصير + رسالة خاصة بالتفاصيل).

مزايا الأمان والجودة:
  - التحقق من توقيع Meta (X-Hub-Signature-256) قبل قبول أي حدث.
  - الرد على Meta فوراً (200) ثم المعالجة في الخلفية.
  - منع معالجة الحدث نفسه مرتين (Meta تعيد الإرسال أحياناً).
  - عدم الرد على تعليقات/رسائل الصفحة نفسها (منع الحلقات اللانهائية).
  - تذكّر سياق المحادثة لكل زبون.
  - التحويل إلى موظف: عند طلب الزبون، أو عند إرسال صورة، أو عندما يرد صاحب الصفحة يدوياً.
  - الذكاء الاصطناعي يجيب فقط من بيانات المتجر المكتوبة أدناه ولا يخترع أسعاراً.
  - حد أقصى للردود في الساعة، وفلترة الروابط والكلمات المسيئة.

التشغيل محلياً:   python app.py
التشغيل على Render: gunicorn app:app --workers 1 --threads 8 --timeout 120
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, Response, jsonify, request

# =============================================================================
# 1) الإعدادات (تُقرأ من متغيرات البيئة — لا تكتب الأسرار داخل الكود أبداً)
# =============================================================================


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "نعم")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- أسرار ومعرّفات Meta ---
VERIFY_TOKEN = env_str("VERIFY_TOKEN")            # كلمة تختارها أنت وتكتبها نفسها في Meta
PAGE_ACCESS_TOKEN = env_str("PAGE_ACCESS_TOKEN")  # رمز وصول الصفحة (Page Access Token)
APP_SECRET = env_str("APP_SECRET")                # App Secret من إعدادات تطبيق Meta (إلزامي للتحقق من التوقيع)
PAGE_ID = env_str("PAGE_ID")                      # معرّف صفحة فيسبوك (رقم)
META_APP_ID = env_str("META_APP_ID")              # اختياري: معرّف تطبيقك لتمييز رسائل البوت عن ردود الموظفين
GRAPH_API_VERSION = env_str("GRAPH_API_VERSION", "v26.0")

# --- Gemini ---
GEMINI_API_KEY = env_str("GEMINI_API_KEY")
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-3.5-flash-lite")

# --- تشغيل/إيقاف كل ميزة على حدة ---
ENABLE_MESSENGER = env_bool("ENABLE_MESSENGER", True)
ENABLE_FB_COMMENTS = env_bool("ENABLE_FB_COMMENTS", True)
ENABLE_INSTAGRAM_DM = env_bool("ENABLE_INSTAGRAM_DM", True)
ENABLE_INSTAGRAM_COMMENTS = env_bool("ENABLE_INSTAGRAM_COMMENTS", True)
PUBLIC_COMMENT_REPLY = env_bool("PUBLIC_COMMENT_REPLY", True)      # رد عام تحت التعليق
PRIVATE_REPLY_ON_COMMENT = env_bool("PRIVATE_REPLY_ON_COMMENT", True)  # رسالة خاصة لصاحب التعليق
REPLY_TO_COMMENT_REPLIES = env_bool("REPLY_TO_COMMENT_REPLIES", False)  # الرد على الردود داخل التعليقات أيضاً
BOT_DISCLOSURE_ENABLED = env_bool("BOT_DISCLOSURE_ENABLED", True)  # إعلام الزبون أنه يتحدث مع مساعد آلي

# --- حدود وأمان ---
ALLOW_UNSIGNED_WEBHOOKS = env_bool("ALLOW_UNSIGNED_WEBHOOKS", False)  # للتجربة المحلية فقط!
MAX_REPLIES_PER_HOUR = env_int("MAX_REPLIES_PER_HOUR", 200)
HANDOFF_PAUSE_HOURS = env_int("HANDOFF_PAUSE_HOURS", 12)  # مدة توقف البوت عن زبون بعد تحويله لموظف
HISTORY_MESSAGES = env_int("HISTORY_MESSAGES", 12)       # عدد الرسائل السابقة التي يتذكرها البوت لكل زبون
PORT = env_int("PORT", 5000)

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
BOT_TAG = "auto_bot_reply"  # علامة نضعها في رسائل البوت لنميّز صداها (echo) عن ردود الموظفين
MESSENGER_MAX_CHARS = 2000
INSTAGRAM_MAX_BYTES = 990  # توثيق Meta: رسالة إنستغرام لا تتجاوز 1000 بايت (الحرف العربي = 2 بايت)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("store-bot")

# =============================================================================
# 2) بيانات المتجر — عدّل هذا القسم ببيانات متجرك الحقيقية
#    الذكاء الاصطناعي لن يذكر أي سعر أو مقاس أو لون غير مكتوب هنا.
# =============================================================================

STORE = {
    "name": "متجر الأناقة للملابس",
    "description": "ملابس رجالية ونسائية بخامات تركية، مع إمكانية فحص الطلب قبل الدفع.",
    "phone": "07700000000",
    "whatsapp": "07700000000",
    "address": "بغداد - الكرادة - شارع 62",
    "working_hours": "يومياً من 10 صباحاً إلى 11 ليلاً، الجمعة من 2 ظهراً",
    "payment_methods": ["الدفع نقداً عند الاستلام", "زين كاش"],
    "return_policy": "يحق للزبون فحص الطلب أمام المندوب قبل الدفع. الاستبدال خلال 3 أيام بشرط بقاء الليبل والقطعة بحالتها الأصلية.",
    "how_to_order": "يرسل الزبون بالخاص: اسم الموديل أو صورته + القياس + اللون + الاسم الثلاثي + رقم الهاتف + المحافظة والعنوان بالتفصيل.",
    "delivery": [
        {"area": "بغداد", "fee": 5000, "duration": "خلال 24 ساعة"},
        {"area": "البصرة", "fee": 6000, "duration": "2 إلى 3 أيام"},
        {"area": "أربيل والسليمانية ودهوك", "fee": 6000, "duration": "2 إلى 3 أيام"},
        {"area": "باقي المحافظات", "fee": 5000, "duration": "1 إلى 2 يوم"},
    ],
    "currency": "دينار عراقي",
    "products": [
        {
            "name": "قميص كتان رجالي",
            "price": 25000,
            "sizes": ["M", "L", "XL", "XXL"],
            "colors": ["أبيض", "أسود", "بيج", "زيتي"],
            "fabric": "كتان تركي 100%، خفيف ومناسب للصيف",
            "in_stock": True,
        },
        {
            "name": "بنطلون كارجو",
            "price": 30000,
            "sizes": ["30", "32", "34", "36", "38"],
            "colors": ["أسود", "زيتي", "رمادي"],
            "fabric": "قطن ثقيل مع جيوب جانبية",
            "in_stock": True,
        },
        {
            "name": "فستان صيفي نسائي",
            "price": 35000,
            "sizes": ["S", "M", "L", "XL"],
            "colors": ["وردي", "سماوي", "أبيض"],
            "fabric": "شيفون مبطن",
            "in_stock": True,
        },
        {
            "name": "هودي شتوي مبطن",
            "price": 40000,
            "sizes": ["L", "XL", "XXL"],
            "colors": ["كحلي", "أسود"],
            "fabric": "قطن مبطن فرو من الداخل",
            "in_stock": False,
        },
    ],
}

HANDOFF_MESSAGE = (
    "تدلل عيني 🌸 حولتك لأحد موظفينا، وراح يرد عليك بأقرب وقت. "
    f"وإذا مستعجل تكدر تتواصل ويانا واتساب: {STORE['whatsapp']}"
)
ATTACHMENT_MESSAGE = (
    "وصلتنا رسالتك 🌸 الصور والملفات يشوفها موظفنا بنفسه وراح يرد عليك بأقرب وقت. "
    "وإذا تكدر اكتبلي اسم الموديل والقياس اللي تريده حتى أساعدك هسه."
)
FALLBACK_MESSAGE = (
    "عذراً عيني، صار عندي خلل بسيط هسه 🙏 حولت سؤالك لموظفنا وراح يرد عليك قريباً. "
    f"أو تواصل ويانا واتساب: {STORE['whatsapp']}"
)
BOT_DISCLOSURE = "(هذا رد من المساعد الآلي للمتجر — اكتب «موظف» إذا تريد تحجي ويا شخص من فريقنا)"

HUMAN_REQUEST_KEYWORDS = [
    "موظف", "شخص حقيقي", "انسان", "بشري", "خدمه الزبائن", "خدمه العملاء",
    "المدير", "مسؤول", "اريد احجي ويا", "اريد اكلم", "ابي اكلم", "عايز اكلم",
    "human", "agent",
]
BLOCKED_WORDS = ["نصاب", "حرامي", "حرامية", "غشاش"]  # تعليقات تحتوي هذه الكلمات لا يرد عليها البوت
LINK_PATTERN = re.compile(r"(https?://|www\.|bit\.ly|t\.me/|wa\.me/)", re.IGNORECASE)


def build_store_facts() -> str:
    """تحويل بيانات المتجر إلى نص يفهمه الذكاء الاصطناعي."""
    lines = [
        f"اسم المتجر: {STORE['name']}",
        f"نبذة: {STORE['description']}",
        f"الهاتف: {STORE['phone']} | واتساب: {STORE['whatsapp']}",
        f"العنوان: {STORE['address']}",
        f"أوقات الدوام: {STORE['working_hours']}",
        f"طرق الدفع: {'، '.join(STORE['payment_methods'])}",
        f"سياسة الاستبدال والإرجاع: {STORE['return_policy']}",
        f"طريقة الطلب: {STORE['how_to_order']}",
        "",
        f"أسعار التوصيل ({STORE['currency']}):",
    ]
    for d in STORE["delivery"]:
        lines.append(f"- {d['area']}: {d['fee']:,} — {d['duration']}")
    lines.append("")
    lines.append(f"المنتجات (الأسعار بالـ{STORE['currency']}):")
    for p in STORE["products"]:
        stock = "متوفر" if p["in_stock"] else "نافد حالياً"
        lines.append(
            f"- {p['name']} | السعر: {p['price']:,} | المقاسات: {'، '.join(p['sizes'])} | "
            f"الألوان: {'، '.join(p['colors'])} | الخامة: {p['fabric']} | الحالة: {stock}"
        )
    return "\n".join(lines)


def build_system_prompt() -> str:
    return f"""أنت مساعد مبيعات لطيف وخبير في الملابس، تعمل لدى "{STORE['name']}" وترد على زبائن فيسبوك وإنستغرام.
تكلم باللهجة العراقية الودية المحترمة (هلا بيك، تدلل، عيني، من عيوني)، وإذا كتب الزبون بالفصحى أو بلهجة أخرى فجاوبه بأسلوب عربي بسيط وواضح.

=== بيانات المتجر المعتمدة (المصدر الوحيد للمعلومات) ===
{build_store_facts()}
=== نهاية البيانات ===

قواعد إلزامية:
1. لا تذكر أبداً سعراً أو مقاساً أو لوناً أو خامة أو كلفة توصيل أو مدة غير مكتوبة حرفياً في البيانات أعلاه. إذا لم تجد المعلومة قل بلطف إنك راح تتأكد من الفريق، واجعل needs_human = true.
2. إذا كان المنتج "نافد حالياً" قل ذلك بوضوح ولا تعد بموعد توفر.
3. استخدم سياق المحادثة السابقة، ولا تكرر التحية في كل رسالة.
4. لا تؤكد أبداً أن الطلب تثبّت نهائياً أو أن الدفع وصل. اجمع تفاصيل الطلب ثم قل إن الفريق راح يتواصل لتأكيده.
5. لا تطلب أبداً أرقام بطاقات أو كلمات مرور أو رموز تحقق.
6. الشكاوى، مشاكل طلب سابق، الاسترجاع المالي، أو أي أمر لا تستطيع حله من البيانات: اعتذر بلطف و needs_human = true.
7. اجعل الرد قصيراً ومرتباً ومناسباً للدردشة (عادةً أقل من 500 حرف)، واستخدم 1-2 إيموجي كحد أقصى.
"""


# =============================================================================
# 3) الذاكرة المؤقتة (حالة البوت داخل الذاكرة)
#    ملاحظة: على Render المجاني تُمسح عند إعادة التشغيل أو النوم — وهذا مقبول لبوت بسيط.
# =============================================================================


class TTLSet:
    """مجموعة تحفظ المعرّفات لفترة محددة (لمنع المعالجة المكررة)."""

    def __init__(self, ttl_seconds: int, max_items: int = 10000):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._items: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def add_if_new(self, key: str) -> bool:
        """يضيف المعرّف ويعيد True إذا كان جديداً، و False إذا كان موجوداً مسبقاً."""
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if key in self._items:
                return False
            self._items[key] = now
            return True

    def add(self, key: str) -> None:
        self.add_if_new(key)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            self._cleanup(time.time())
            return key in self._items

    def _cleanup(self, now: float) -> None:
        while self._items:
            oldest_key, ts = next(iter(self._items.items()))
            if now - ts > self.ttl or len(self._items) > self.max_items:
                self._items.popitem(last=False)
            else:
                break


processed_events = TTLSet(ttl_seconds=48 * 3600)  # معرّفات الرسائل والتعليقات المعالجة
sent_message_ids = TTLSet(ttl_seconds=48 * 3600)  # معرّفات رسائل أرسلها البوت (لتجاهل صداها)
disclosed_users = TTLSet(ttl_seconds=24 * 3600)   # زبائن أُعلموا بأنهم يتحدثون مع بوت

state_lock = threading.Lock()
conversation_history: dict = {}  # (platform, user_id) -> deque[(role, text)]
paused_until: dict = {}          # (platform, user_id) -> timestamp
reply_timestamps: deque = deque()


def history_for(key):
    with state_lock:
        if key not in conversation_history:
            conversation_history[key] = deque(maxlen=max(HISTORY_MESSAGES, 2))
        return list(conversation_history[key])


def remember(key, role: str, text: str) -> None:
    if not text:
        return
    with state_lock:
        if key not in conversation_history:
            conversation_history[key] = deque(maxlen=max(HISTORY_MESSAGES, 2))
        conversation_history[key].append((role, text))


def pause_user(key, reason: str) -> None:
    with state_lock:
        paused_until[key] = time.time() + HANDOFF_PAUSE_HOURS * 3600
    log.info("⏸️  تم إيقاف البوت للزبون %s:%s لمدة %s ساعة. السبب: %s", key[0], key[1], HANDOFF_PAUSE_HOURS, reason)


def is_paused(key) -> bool:
    with state_lock:
        until = paused_until.get(key)
        if until and until > time.time():
            return True
        paused_until.pop(key, None)
        return False


def allow_reply_now() -> bool:
    """حد أقصى عام للردود في الساعة (حماية من الاستهلاك المفرط أو الحلقات)."""
    now = time.time()
    with state_lock:
        while reply_timestamps and now - reply_timestamps[0] > 3600:
            reply_timestamps.popleft()
        if len(reply_timestamps) >= MAX_REPLIES_PER_HOUR:
            return False
        reply_timestamps.append(now)
        return True


# =============================================================================
# 4) أدوات مساعدة للنص
# =============================================================================


def normalize_arabic(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[ً-ٰٟـ]", "", text)  # التشكيل والتطويل
    text = re.sub(r"[أإآ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text).strip()


def asks_for_human(text: str) -> bool:
    normalized = normalize_arabic(text)
    return any(normalize_arabic(k) in normalized for k in HUMAN_REQUEST_KEYWORDS)


def is_spam_or_offensive(text: str) -> bool:
    if LINK_PATTERN.search(text or ""):
        return True
    normalized = normalize_arabic(text)
    return any(normalize_arabic(w) in normalized for w in BLOCKED_WORDS)


def split_by_utf8_bytes(text: str, max_bytes: int) -> list:
    """تقسيم النص إلى أجزاء لا يتجاوز كل منها عدداً محدداً من البايتات (لرسائل إنستغرام)."""
    parts, current = [], ""
    for word in text.split(" "):
        candidate = (current + " " + word).strip() if current else word
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current:
            parts.append(current)
        # كلمة طويلة جداً وحدها: نقطعها حرفاً حرفاً
        while len(word.encode("utf-8")) > max_bytes:
            cut = word
            while len(cut.encode("utf-8")) > max_bytes:
                cut = cut[:-1]
            parts.append(cut)
            word = word[len(cut):]
        current = word
    if current:
        parts.append(current)
    return parts


def split_for_platform(text: str, platform: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    if platform == "instagram":
        parts = split_by_utf8_bytes(text, INSTAGRAM_MAX_BYTES)
    else:
        parts = [text[i:i + MESSENGER_MAX_CHARS] for i in range(0, len(text), MESSENGER_MAX_CHARS)]
    return parts[:3]  # لا نرسل أكثر من 3 رسائل متتالية


# =============================================================================
# 5) Gemini — توليد الردود
# =============================================================================

_gemini_client = None
_gemini_lock = threading.Lock()


def get_gemini():
    """إنشاء عميل Gemini مرة واحدة فقط (عند أول استخدام)."""
    global _gemini_client
    if _gemini_client is None:
        with _gemini_lock:
            if _gemini_client is None:
                if not GEMINI_API_KEY:
                    raise RuntimeError("GEMINI_API_KEY غير مضبوط")
                from google import genai  # استيراد متأخر حتى يعمل التطبيق حتى لو كانت المكتبة غير مثبتة بعد
                _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _call_gemini(contents, extra_instruction: str) -> dict:
    from google.genai import types

    client = get_gemini()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt() + "\n" + extra_instruction,
            temperature=0.3,
            max_output_tokens=800,
            response_mime_type="application/json",
        ),
    )
    return _parse_json(response.text)


def _history_to_contents(history: list, latest_text: str):
    """تحويل سجل المحادثة إلى صيغة Gemini (user/model) مع دمج الرسائل المتتالية من نفس الطرف."""
    from google.genai import types

    turns = []
    for role, text in history + [("user", latest_text)]:
        gemini_role = "user" if role == "user" else "model"
        if turns and turns[-1][0] == gemini_role:
            turns[-1] = (gemini_role, turns[-1][1] + "\n" + text)
        else:
            turns.append((gemini_role, text))
    while turns and turns[0][0] != "user":  # يجب أن تبدأ المحادثة برسالة من الزبون
        turns.pop(0)
    return [types.Content(role=r, parts=[types.Part.from_text(text=t)]) for r, t in turns]


def generate_dm_reply(history: list, user_text: str) -> dict:
    """رد على رسالة خاصة. يعيد: {"reply": str, "needs_human": bool}"""
    instruction = (
        'أعد JSON فقط بهذا الشكل: {"reply": "نص الرد على الزبون", "needs_human": true أو false}'
    )
    try:
        data = _call_gemini(_history_to_contents(history, user_text), instruction)
        reply = str(data.get("reply") or "").strip()
        needs_human = bool(data.get("needs_human"))
        if not reply:
            return {"reply": FALLBACK_MESSAGE, "needs_human": True}
        return {"reply": reply, "needs_human": needs_human}
    except Exception as exc:  # أي خطأ في Gemini: لا نخترع رداً، نحوّل لموظف
        log.error("فشل Gemini في الرد على رسالة: %s", str(exc)[:300])
        return {"reply": FALLBACK_MESSAGE, "needs_human": True}


def generate_comment_replies(comment_text: str, commenter_name: str, platform: str) -> dict:
    """رد على تعليق. يعيد: {"ignore": bool, "public_reply": str, "private_message": str}"""
    platform_name = "إنستغرام" if platform == "instagram" else "فيسبوك"
    instruction = f"""المهمة الآن: زبون اسمه "{commenter_name or 'زبون'}" كتب تعليقاً عاماً على منشور في {platform_name}.
أعد JSON فقط بهذا الشكل:
{{"ignore": false, "public_reply": "...", "private_message": "..."}}
- public_reply: رد عام قصير جداً (أقل من 150 حرفاً) فيه ترحيب وإشارة إلى أنك أرسلت له التفاصيل بالخاص. لا تكتب فيه معلومات شخصية.
- private_message: رسالة خاصة مفصلة تجيب على سؤاله من بيانات المتجر فقط (السعر، المقاسات، الألوان، التوصيل، طريقة الطلب حسب سؤاله).
- إذا كان التعليق سباماً أو إعلاناً أو مسيئاً أو لا علاقة له بالمتجر (مثل الإشارة لأصدقاء فقط): ignore = true والباقي فارغ."""
    contents = f"نص التعليق: {comment_text}"
    try:
        data = _call_gemini(contents, instruction)
        return {
            "ignore": bool(data.get("ignore")),
            "public_reply": str(data.get("public_reply") or "").strip(),
            "private_message": str(data.get("private_message") or "").strip(),
        }
    except Exception as exc:
        log.error("فشل Gemini في الرد على تعليق: %s", str(exc)[:300])
        return {
            "ignore": False,
            "public_reply": "هلا بيك عيني 🌸 دزينالك التفاصيل بالخاص.",
            "private_message": (
                f"هلا بيك عيني 🌸 شكراً لتعليقك على صفحة {STORE['name']}. "
                f"موظفنا راح يجاوبك هنا بأقرب وقت، أو تواصل ويانا واتساب: {STORE['whatsapp']}"
            ),
        }


# =============================================================================
# 6) Meta Graph API — الإرسال
# =============================================================================


def _appsecret_proof() -> str:
    """إثبات إضافي يربط الطلب بتطبيقك (توصية أمان من Meta)."""
    if not APP_SECRET or not PAGE_ACCESS_TOKEN:
        return ""
    return hmac.new(APP_SECRET.encode(), PAGE_ACCESS_TOKEN.encode(), hashlib.sha256).hexdigest()


def graph_post(path: str, payload: dict) -> tuple:
    """طلب POST إلى Graph API. يعيد (نجح؟, البيانات أو رسالة الخطأ). لا يطبع رمز الوصول أبداً."""
    if not PAGE_ACCESS_TOKEN:
        return False, "PAGE_ACCESS_TOKEN غير مضبوط"
    body = dict(payload)
    body["access_token"] = PAGE_ACCESS_TOKEN
    proof = _appsecret_proof()
    if proof:
        body["appsecret_proof"] = proof
    try:
        resp = requests.post(f"{GRAPH_URL}/{path}", json=body, timeout=15)
        data = resp.json() if resp.content else {}
    except requests.RequestException as exc:
        return False, f"خطأ اتصال: {exc.__class__.__name__}"
    except ValueError:
        return False, f"رد غير مفهوم من Meta (HTTP {resp.status_code})"
    if resp.ok and "error" not in data:
        return True, data
    err = data.get("error", {}) if isinstance(data, dict) else {}
    return False, f"Meta error {err.get('code')}/{err.get('error_subcode')}: {err.get('message', resp.status_code)}"


def send_text(platform: str, recipient_id: str, text: str) -> bool:
    """إرسال رسالة نصية في Messenger أو Instagram Direct."""
    sender_path = f"{PAGE_ID}/messages" if PAGE_ID else "me/messages"
    all_ok = True
    for part in split_for_platform(text, platform):
        message = {"text": part}
        if platform == "messenger":
            message["metadata"] = BOT_TAG
        ok, data = graph_post(sender_path, {
            "recipient": {"id": recipient_id},
            "messaging_type": "RESPONSE",
            "message": message,
        })
        if ok:
            if data.get("message_id"):
                sent_message_ids.add(data["message_id"])
        else:
            all_ok = False
            log.error("❌ فشل إرسال رسالة %s: %s", platform, data)
            break
    return all_ok


def send_private_reply(comment_id: str, text: str, platform: str) -> bool:
    """رسالة خاصة لصاحب التعليق (Private Reply): رسالة واحدة فقط لكل تعليق خلال 7 أيام."""
    sender_path = f"{PAGE_ID}/messages" if PAGE_ID else "me/messages"
    parts = split_for_platform(text, platform)
    if not parts:
        return False
    ok, data = graph_post(sender_path, {
        "recipient": {"comment_id": comment_id},
        "message": {"text": parts[0]},
    })
    if ok and data.get("message_id"):
        sent_message_ids.add(data["message_id"])
    if not ok:
        log.error("❌ فشل الرد الخاص على تعليق %s: %s", platform, data)
    return ok


def reply_to_facebook_comment(comment_id: str, text: str) -> bool:
    ok, data = graph_post(f"{comment_id}/comments", {"message": text[:1000]})
    if not ok:
        log.error("❌ فشل الرد العام على تعليق فيسبوك: %s", data)
    return ok


def reply_to_instagram_comment(comment_id: str, text: str) -> bool:
    ok, data = graph_post(f"{comment_id}/replies", {"message": text[:1000]})
    if not ok:
        log.error("❌ فشل الرد العام على تعليق إنستغرام: %s", data)
    return ok


# =============================================================================
# 7) معالجة الأحداث
# =============================================================================


def handle_echo(platform: str, event: dict) -> None:
    """صدى رسالة أرسلتها الصفحة. إذا لم يرسلها البوت فهذا يعني أن موظفاً رد يدوياً ← نوقف البوت لهذا الزبون."""
    message = event.get("message", {})
    mid = message.get("mid", "")
    customer_id = event.get("recipient", {}).get("id", "")
    from_bot = (
        (mid and mid in sent_message_ids)
        or message.get("metadata") == BOT_TAG
        or (META_APP_ID and str(message.get("app_id", "")) == META_APP_ID)
    )
    if from_bot or not customer_id:
        return
    key = (platform, customer_id)
    remember(key, "model", f"[رد من موظف المتجر]: {message.get('text', '')}")
    pause_user(key, "صاحب الصفحة رد يدوياً على الزبون")


def handle_direct_message(platform: str, account_id: str, event: dict) -> None:
    """رسالة خاصة من زبون (Messenger أو Instagram Direct)."""
    if platform == "messenger" and not ENABLE_MESSENGER:
        return
    if platform == "instagram" and not ENABLE_INSTAGRAM_DM:
        return

    message = event.get("message") or {}
    postback = event.get("postback") or {}

    if message.get("is_echo"):
        handle_echo(platform, event)
        return
    if message.get("is_deleted") or message.get("is_unsupported"):
        return

    sender_id = event.get("sender", {}).get("id", "")
    if not sender_id or sender_id == account_id or (PAGE_ID and sender_id == PAGE_ID):
        return

    if not message and not postback:
        return  # أحداث أخرى مثل القراءة والتوصيل

    event_id = message.get("mid") or postback.get("mid") or f"pb-{sender_id}-{event.get('timestamp')}"
    if not processed_events.add_if_new(f"{platform}:{event_id}"):
        log.info("↩️  تم تجاهل حدث مكرر (%s)", platform)
        return

    text = (message.get("text") or postback.get("title") or postback.get("payload") or "").strip()
    has_attachment = bool(message.get("attachments"))
    key = (platform, sender_id)

    if is_paused(key):
        remember(key, "user", text or "[مرفق]")
        log.info("🙋 الزبون %s محوّل لموظف؛ البوت لن يرد.", sender_id)
        return

    # صورة أو صوت أو ملف بدون نص: يحتاج عين بشرية
    if not text and has_attachment:
        if allow_reply_now() and send_text(platform, sender_id, ATTACHMENT_MESSAGE):
            remember(key, "user", "[أرسل الزبون مرفقاً]")
            remember(key, "model", ATTACHMENT_MESSAGE)
        pause_user(key, "أرسل الزبون صورة أو مرفقاً")
        return
    if not text:
        return

    # الزبون يطلب موظفاً
    if asks_for_human(text):
        remember(key, "user", text)
        if allow_reply_now():
            send_text(platform, sender_id, HANDOFF_MESSAGE)
        pause_user(key, "الزبون طلب موظفاً")
        return

    if not allow_reply_now():
        log.warning("⚠️ تم بلوغ الحد الأقصى للردود في الساعة (%s). لم يُرسل رد.", MAX_REPLIES_PER_HOUR)
        return

    history = history_for(key)
    result = generate_dm_reply(history, text)

    # ربما رد صاحب الصفحة يدوياً أثناء انتظار Gemini
    if is_paused(key):
        return

    reply = result["reply"]
    if BOT_DISCLOSURE_ENABLED and disclosed_users.add_if_new(f"{platform}:{sender_id}"):
        reply = f"{reply}\n\n{BOT_DISCLOSURE}"

    if send_text(platform, sender_id, reply):
        remember(key, "user", text)
        remember(key, "model", result["reply"])
        log.info("✅ تم الرد على رسالة %s.", platform)
    if result["needs_human"]:
        pause_user(key, "الذكاء الاصطناعي طلب تدخل موظف")


def handle_comment(platform: str, comment_id: str, commenter_id: str, commenter_name: str, text: str) -> None:
    """المنطق المشترك للرد على تعليق في فيسبوك أو إنستغرام."""
    if not processed_events.add_if_new(f"{platform}-comment:{comment_id}"):
        log.info("↩️  تعليق مكرر تم تجاهله (%s)", platform)
        return
    if not text or is_spam_or_offensive(text):
        log.info("🚫 تم تجاهل تعليق (فارغ أو رابط أو كلمات مسيئة).")
        return
    if not allow_reply_now():
        log.warning("⚠️ تم بلوغ الحد الأقصى للردود في الساعة. لم يُرد على التعليق.")
        return

    result = generate_comment_replies(text, commenter_name, platform)
    if result["ignore"]:
        log.info("🚫 الذكاء الاصطناعي قرر تجاهل التعليق.")
        return

    if PUBLIC_COMMENT_REPLY and result["public_reply"]:
        if platform == "instagram":
            reply_to_instagram_comment(comment_id, result["public_reply"])
        else:
            reply_to_facebook_comment(comment_id, result["public_reply"])

    if PRIVATE_REPLY_ON_COMMENT and result["private_message"]:
        private_text = result["private_message"]
        if BOT_DISCLOSURE_ENABLED:
            private_text = f"{private_text}\n\n{BOT_DISCLOSURE}"
        if send_private_reply(comment_id, private_text, platform) and commenter_id:
            # نحفظ السياق حتى إذا رد الزبون على الرسالة الخاصة يكمل البوت من نفس الموضوع
            dm_platform = "instagram" if platform == "instagram" else "messenger"
            key = (dm_platform, commenter_id)
            remember(key, "user", f"[تعليق على منشور]: {text}")
            remember(key, "model", result["private_message"])
            disclosed_users.add(f"{dm_platform}:{commenter_id}")
    log.info("✅ تمت معالجة تعليق %s.", platform)


def handle_facebook_feed_change(page_id: str, value: dict) -> None:
    if not ENABLE_FB_COMMENTS:
        return
    if value.get("item") != "comment" or value.get("verb") != "add":
        return
    sender = value.get("from") or {}
    if sender.get("id") in (page_id, PAGE_ID):
        return  # تعليق الصفحة نفسها (بما فيها ردود البوت) — منع الحلقة
    comment_id = value.get("comment_id")
    if not comment_id:
        return
    parent_id, post_id = value.get("parent_id"), value.get("post_id")
    if parent_id and parent_id != post_id and not REPLY_TO_COMMENT_REPLIES:
        return  # رد داخل سلسلة تعليقات، وليس تعليقاً جديداً على المنشور
    handle_comment("facebook", comment_id, sender.get("id", ""), sender.get("name", ""), (value.get("message") or "").strip())


def handle_instagram_comment_change(ig_account_id: str, value: dict) -> None:
    if not ENABLE_INSTAGRAM_COMMENTS:
        return
    sender = value.get("from") or {}
    if sender.get("id") == ig_account_id:
        return  # تعليق الحساب نفسه (بما فيها ردود البوت) — منع الحلقة
    comment_id = value.get("comment_id") or value.get("id")
    if not comment_id:
        return
    if value.get("parent_id") and not REPLY_TO_COMMENT_REPLIES:
        return
    handle_comment("instagram", comment_id, sender.get("id", ""), sender.get("username", ""), (value.get("text") or "").strip())


def process_payload(body: dict) -> None:
    """توزيع جميع أحداث Webhook على المعالجات المناسبة."""
    obj = body.get("object")
    for entry in body.get("entry", []) or []:
        account_id = str(entry.get("id", ""))
        try:
            if obj == "page":
                for event in entry.get("messaging", []) or []:
                    handle_direct_message("messenger", account_id, event)
                for change in entry.get("changes", []) or []:
                    if change.get("field") == "feed":
                        handle_facebook_feed_change(account_id, change.get("value") or {})
            elif obj == "instagram":
                for event in entry.get("messaging", []) or []:
                    handle_direct_message("instagram", account_id, event)
                for change in entry.get("changes", []) or []:
                    if change.get("field") == "comments":
                        handle_instagram_comment_change(account_id, change.get("value") or {})
        except Exception:
            log.exception("خطأ غير متوقع أثناء معالجة حدث %s", obj)


# =============================================================================
# 8) خادم Flask
# =============================================================================

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4)


def signature_is_valid(raw_body: bytes, header: str) -> bool:
    if not APP_SECRET or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.get("/webhook")
def verify_webhook():
    """مصافحة Meta عند إعداد الـ Webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token or "", VERIFY_TOKEN):
        log.info("✅ تم التحقق من Webhook بنجاح.")
        return Response(challenge, status=200, mimetype="text/plain")
    log.warning("❌ فشل التحقق من Webhook: رمز التحقق غير مطابق.")
    return Response("Forbidden", status=403)


@app.post("/webhook")
def receive_webhook():
    raw = request.get_data()
    if APP_SECRET:
        if not signature_is_valid(raw, request.headers.get("X-Hub-Signature-256", "")):
            log.warning("🔒 تم رفض طلب Webhook: توقيع غير صالح.")
            return Response("Invalid signature", status=403)
    elif not ALLOW_UNSIGNED_WEBHOOKS:
        log.error("🔒 تم رفض طلب Webhook: APP_SECRET غير مضبوط.")
        return Response("APP_SECRET not configured", status=403)

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response("Bad request", status=400)

    # نرد على Meta فوراً، والمعالجة (Gemini + الإرسال) تتم في الخلفية
    executor.submit(process_payload, body)
    return Response("EVENT_RECEIVED", status=200)


@app.get("/")
@app.get("/health")
def health():
    """فحص سريع لحالة البوت (لا يعرض أي سر)."""
    return jsonify({
        "status": "ok",
        "store": STORE["name"],
        "config": {
            "verify_token": bool(VERIFY_TOKEN),
            "page_access_token": bool(PAGE_ACCESS_TOKEN),
            "app_secret": bool(APP_SECRET),
            "page_id": bool(PAGE_ID),
            "gemini_api_key": bool(GEMINI_API_KEY),
            "gemini_model": GEMINI_MODEL,
            "graph_api_version": GRAPH_API_VERSION,
        },
        "features": {
            "messenger": ENABLE_MESSENGER,
            "facebook_comments": ENABLE_FB_COMMENTS,
            "instagram_dm": ENABLE_INSTAGRAM_DM,
            "instagram_comments": ENABLE_INSTAGRAM_COMMENTS,
        },
    })


@app.get("/privacy")
def privacy_policy():
    """صفحة سياسة خصوصية بسيطة — تطلب Meta رابط سياسة خصوصية لنشر التطبيق. راجعها وعدّلها حسب متجرك."""
    html = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>سياسة الخصوصية - {STORE['name']}</title>
<style>body{{font-family:Tahoma,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;line-height:1.9;color:#222}}</style>
</head><body>
<h1>سياسة الخصوصية — {STORE['name']}</h1>
<p>يستخدم {STORE['name']} مساعداً آلياً للرد على رسائل وتعليقات الزبائن في فيسبوك وإنستغرام.</p>
<h2>البيانات التي نستخدمها</h2>
<p>نص الرسالة أو التعليق، ومعرّف الحساب الذي يوفره فيسبوك/إنستغرام، والاسم الظاهر. نستخدمها فقط للرد على استفسارك عن منتجاتنا وطلباتك.</p>
<h2>معالجة الردود</h2>
<p>تُرسل نصوص الرسائل إلى خدمة Google Gemini لتوليد الرد. لا نبيع بياناتك ولا نشاركها مع أي جهة أخرى لأغراض تسويقية.</p>
<h2>مدة الحفظ</h2>
<p>يحتفظ المساعد بسياق المحادثة مؤقتاً في ذاكرة الخادم فقط، ويُحذف تلقائياً عند إعادة تشغيل الخادم.</p>
<h2>حذف البيانات</h2>
<p>لطلب حذف بياناتك تواصل معنا على الرقم {STORE['phone']} أو عبر رسالة خاصة للصفحة.</p>
<hr><h2 dir="ltr" style="text-align:left">Privacy Policy (English)</h2>
<p dir="ltr" style="text-align:left">{STORE['name']} uses an automated assistant to reply to customer messages and comments on Facebook and Instagram.
We process message text, the platform-provided user ID and display name only to answer your questions. Message text is sent to Google Gemini to generate replies.
We do not sell or share your data. Conversation context is kept temporarily in server memory only.
To request deletion of your data, contact us at {STORE['phone']} or message our Page.</p>
</body></html>"""
    return Response(html, mimetype="text/html")


def log_startup_warnings() -> None:
    missing = [name for name, value in [
        ("VERIFY_TOKEN", VERIFY_TOKEN),
        ("PAGE_ACCESS_TOKEN", PAGE_ACCESS_TOKEN),
        ("APP_SECRET", APP_SECRET),
        ("PAGE_ID", PAGE_ID),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
    ] if not value]
    if missing:
        log.warning("⚠️ متغيرات بيئة ناقصة: %s", ", ".join(missing))
    if not APP_SECRET and ALLOW_UNSIGNED_WEBHOOKS:
        log.warning("⚠️ ALLOW_UNSIGNED_WEBHOOKS مفعّل: أي شخص يستطيع إرسال أحداث مزيفة. للتجربة المحلية فقط!")
    log.info("🤖 بوت %s جاهز | Gemini: %s | Graph API: %s", STORE["name"], GEMINI_MODEL, GRAPH_API_VERSION)


log_startup_warnings()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
