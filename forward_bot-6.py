# -*- coding: utf-8 -*-
"""
بوت إعادة نشر حسابات ببجي - Multi-tenant Forward Bot
------------------------------------------------------
الفكرة:
  - كل تاجر بيحدد جروب المصدر الخاص بيه (اللي فيه عروض الحسابات: فيديو + وصف)
    وقناته الخاصة اللي عايز ينشر فيها.
  - البوت لازم يكون عضو (مش لازم Admin) في جروب المصدر، بشرط إن Privacy Mode
    يكون متعطل من BotFather عشان يقدر يقرا كل الرسائل.
  - البوت لازم يكون Admin بصلاحية نشر في قناة الوجهة بتاعة كل تاجر.
  - الاشتراك 5$/شهر، دفع يدوي خارج البوت، وموافقة الأدمن يدوية من لوحة /admin.

الإعداد قبل التشغيل (غيّر القيم دي في الأسفل أو حطها Environment Variables):
  BOT_TOKEN        - توكن البوت من BotFather
  ADMIN_ID         - رقم التليجرام آيدي بتاعك (مش اليوزر)
  ADMIN_USERNAME   - يوزرك للتواصل (بدون @) - بيظهر للتاجر عشان يدفع

ملحوظة مهمة: من BotFather -> اختار بوتك -> Bot Settings -> Group Privacy -> Turn off
عشان البوت يقدر يقرا رسائل أي جروب مصدر هو عضو فيه من غير ما يبقى Admin.
"""

import os
import re
import logging
import sqlite3
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ----------------------- الإعدادات -----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "429325696"))  # حط آيدي التليجرام بتاعك
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Bobeka11")
SUBSCRIPTION_DAYS = 30
DB_PATH = os.environ.get("DB_PATH", "traders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# محادثة التسجيل - حالات
ASK_USERNAME, ASK_SOURCE_GROUP, ASK_CHANNEL, WAIT_CONFIRM = range(4)


# ----------------------- قاعدة البيانات -----------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            trader_username TEXT,
            source_group_id TEXT,
            source_group_title TEXT,
            channel_id TEXT,
            channel_title TEXT,
            status TEXT DEFAULT 'pending',   -- pending / active / expired
            created_at TEXT,
            expires_at TEXT,
            pending_username TEXT,
            pending_source_group_id TEXT,
            pending_source_group_title TEXT,
            pending_channel_id TEXT,
            pending_channel_title TEXT,
            has_pending_update INTEGER DEFAULT 0
        )
        """
    )
    # ترقية القواعد القديمة اللي اتعملت قبل إضافة الأعمدة دي
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(traders)")}
    upgrades = [
        ("source_group_id", "ALTER TABLE traders ADD COLUMN source_group_id TEXT"),
        ("source_group_title", "ALTER TABLE traders ADD COLUMN source_group_title TEXT"),
        ("pending_username", "ALTER TABLE traders ADD COLUMN pending_username TEXT"),
        ("pending_source_group_id", "ALTER TABLE traders ADD COLUMN pending_source_group_id TEXT"),
        ("pending_source_group_title", "ALTER TABLE traders ADD COLUMN pending_source_group_title TEXT"),
        ("pending_channel_id", "ALTER TABLE traders ADD COLUMN pending_channel_id TEXT"),
        ("pending_channel_title", "ALTER TABLE traders ADD COLUMN pending_channel_title TEXT"),
        ("has_pending_update", "ALTER TABLE traders ADD COLUMN has_pending_update INTEGER DEFAULT 0"),
    ]
    for col, ddl in upgrades:
        if col not in existing_cols:
            conn.execute(ddl)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_state (
            source_key TEXT PRIMARY KEY,
            last_post_id INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_trader(telegram_id, trader_username, source_group_id, source_group_title, channel_id, channel_title):
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO traders
            (telegram_id, trader_username, source_group_id, source_group_title,
             channel_id, channel_title, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            trader_username=excluded.trader_username,
            source_group_id=excluded.source_group_id,
            source_group_title=excluded.source_group_title,
            channel_id=excluded.channel_id,
            channel_title=excluded.channel_title,
            status='pending'
        """,
        (telegram_id, trader_username, source_group_id, source_group_title,
         channel_id, channel_title, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_trader(trader_db_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM traders WHERE id=?", (trader_db_id,)).fetchone()
    conn.close()
    return row


def get_trader_by_telegram_id(telegram_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM traders WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row


def get_traders_by_status(status):
    conn = db_connect()
    rows = conn.execute("SELECT * FROM traders WHERE status=?", (status,)).fetchall()
    conn.close()
    return rows


def get_active_traders_by_source(source_group_id):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM traders WHERE status='active' AND source_group_id=?",
        (str(source_group_id),),
    ).fetchall()
    conn.close()
    return rows


def get_traders_with_pending_update():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM traders WHERE has_pending_update=1").fetchall()
    conn.close()
    return rows


def set_status(trader_db_id, status, extend=False):
    conn = db_connect()
    if extend:
        expires_at = (datetime.utcnow() + timedelta(days=SUBSCRIPTION_DAYS)).isoformat()
        conn.execute(
            "UPDATE traders SET status=?, expires_at=? WHERE id=?",
            (status, expires_at, trader_db_id),
        )
    else:
        conn.execute("UPDATE traders SET status=? WHERE id=?", (status, trader_db_id))
    conn.commit()
    conn.close()


def set_pending_update(telegram_id, pending_username, pending_source_group_id,
                        pending_source_group_title, pending_channel_id, pending_channel_title):
    conn = db_connect()
    conn.execute(
        """
        UPDATE traders
        SET pending_username=?, pending_source_group_id=?, pending_source_group_title=?,
            pending_channel_id=?, pending_channel_title=?, has_pending_update=1
        WHERE telegram_id=?
        """,
        (pending_username, pending_source_group_id, pending_source_group_title,
         pending_channel_id, pending_channel_title, telegram_id),
    )
    conn.commit()
    conn.close()


def apply_pending_update(trader_db_id):
    conn = db_connect()
    conn.execute(
        """
        UPDATE traders
        SET trader_username=pending_username,
            source_group_id=pending_source_group_id,
            source_group_title=pending_source_group_title,
            channel_id=pending_channel_id,
            channel_title=pending_channel_title,
            pending_username=NULL, pending_source_group_id=NULL, pending_source_group_title=NULL,
            pending_channel_id=NULL, pending_channel_title=NULL,
            has_pending_update=0
        WHERE id=?
        """,
        (trader_db_id,),
    )
    conn.commit()
    conn.close()


def clear_pending_update(trader_db_id):
    conn = db_connect()
    conn.execute(
        """
        UPDATE traders
        SET pending_username=NULL, pending_source_group_id=NULL, pending_source_group_title=NULL,
            pending_channel_id=NULL, pending_channel_title=NULL, has_pending_update=0
        WHERE id=?
        """,
        (trader_db_id,),
    )
    conn.commit()
    conn.close()


# ----------------------- أدوات مساعدة -----------------------
def days_left(expires_at_str):
    if not expires_at_str:
        return None
    try:
        expires = datetime.fromisoformat(expires_at_str)
    except ValueError:
        return None
    remaining = (expires - datetime.utcnow()).days
    return max(remaining, 0)


def parse_chat_input(text: str) -> str:
    """
    يقبل: رابط t.me، يوزر بـ@، آيدي رقمي سالب (-100...)، أو نص عادي.
    يرجع الصيغة الصح اللي البوت هيستخدمها.
    """
    text = text.strip()
    if "t.me/" in text:
        text = text.split("t.me/")[-1].strip("/ ")
        return "@" + text if not text.startswith("@") else text
    # آيدي رقمي (زي اللي بيجيبه GetIDs Bot بعد إضافة -100)
    if text.lstrip("-").isdigit():
        return text
    if not text.startswith("@"):
        text = "@" + text
    return text


# ----------------------- تسجيل / تحديث بيانات التاجر -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    existing = get_trader_by_telegram_id(telegram_id)

    if existing:
        if existing["status"] == "active":
            remaining = days_left(existing["expires_at"])
            msg = (
                "اشتراكك شغال ✅\n"
                f"مصدر: {existing['source_group_title'] or existing['source_group_id']}\n"
                f"قناتك: {existing['channel_title']}\n"
                f"باقيلك {remaining} يوم على انتهاء الاشتراك.\n\n"
                "لو عايز تغيّر بياناتك، ابعت كلمة 'تحديث'."
            )
        elif existing["status"] == "pending":
            msg = "طلبك لسه تحت مراجعة الأدمن، هيتفعّل بعد الدفع مباشرة."
        else:  # expired
            msg = (
                f"اشتراكك خلص. كلم الأدمن للتجديد: @{ADMIN_USERNAME}\n\n"
                "لو عايز تغيّر بياناتك برضو، ابعت كلمة 'تحديث'."
            )
        await update.message.reply_text(msg)
        return ConversationHandler.END

    context.user_data["mode"] = "register"
    await update.message.reply_text(
        "أهلاً بيك 👋\n"
        "البوت ده بينقل منشورات حسابات ببجي أوتوماتيك من جروب المصدر بتاعك لقناتك.\n\n"
        "الاشتراك: 5$ شهريًا.\n\n"
        "عشان نبدأ، ابعتلي اليوزر بتاعك (اللي عايز يظهر مع منشوراتك):"
    )
    return ASK_USERNAME


async def start_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    existing = get_trader_by_telegram_id(telegram_id)
    if not existing:
        await update.message.reply_text("مش مسجل عندنا لسه. ابعت /start عشان تسجل الأول.")
        return ConversationHandler.END

    context.user_data["mode"] = "update"
    await update.message.reply_text(
        "تمام، هنعمل تحديث لبياناتك (هيتبعت للأدمن يوافق عليه قبل ما يتفعّل).\n\n"
        "ابعتلي اليوزر الجديد بتاعك:"
    )
    return ASK_USERNAME


async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trader_username"] = update.message.text.strip()
    await update.message.reply_text(
        "تمام ✅\n\n"
        "دلوقتي ابعتلي جروب المصدر اللي هتتسحب منه عروض الحسابات:\n"
        "- ابعت رابطه (https://t.me/xxx) أو يوزره (لو Public)\n"
        "- أو لو Private، هاته من GetIDs Bot وابعتلي الرقم (زي -1001234567890)\n\n"
        "⚠️ متنساش تضيف البوت كعضو عادي في الجروب ده (مش لازم Admin)، "
        "بس تأكد إن Privacy Mode متعطل من BotFather."
    )
    return ASK_SOURCE_GROUP


async def ask_source_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("ابعت رابط الجروب أو آيديه كنص. جرب تاني:")
        return ASK_SOURCE_GROUP

    source_group_id = parse_chat_input(update.message.text)
    context.user_data["source_group_id"] = source_group_id
    context.user_data["source_group_title"] = update.message.text.strip()

    await update.message.reply_text(
        "تمام ✅\n\n"
        "دلوقتي ابعتلي رابط أو يوزر قناتك اللي هينشر فيها (مثال: https://t.me/BOBEKA12):"
    )
    return ASK_CHANNEL


async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = None
    channel_title = None

    forward_origin = update.message.forward_origin
    if forward_origin and getattr(forward_origin, "chat", None):
        channel_id = str(forward_origin.chat.id)
        channel_title = forward_origin.chat.title
    elif update.message.text:
        channel_id = parse_chat_input(update.message.text)
        channel_title = update.message.text.strip()

    if not channel_id:
        await update.message.reply_text("محتاج رابط القناة أو يوزرها أو رسالة محولة منها. جرب تاني:")
        return ASK_CHANNEL

    context.user_data["channel_id"] = channel_id
    context.user_data["channel_title"] = channel_title or channel_id

    await update.message.reply_text(
        "خطوة أخيرة:\n"
        "1) روح لقناتك (الوجهة)\n"
        "2) ضيفني Admin فيها (نفس يوزر البوت ده)\n"
        "3) فعّل صلاحية 'نشر الرسائل' بس\n\n"
        "لما تخلص ابعت كلمة 'تم'"
    )
    return WAIT_CONFIRM


async def confirm_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() not in ("تم", "تمام", "done", "Done"):
        await update.message.reply_text("ابعت 'تم' لما تخلص إضافة البوت كـ Admin في القناة.")
        return WAIT_CONFIRM

    telegram_id = update.effective_user.id
    trader_username = context.user_data.get("trader_username")
    source_group_id = context.user_data.get("source_group_id")
    source_group_title = context.user_data.get("source_group_title")
    channel_id = context.user_data.get("channel_id")
    channel_title = context.user_data.get("channel_title")
    mode = context.user_data.get("mode", "register")

    if mode == "update":
        set_pending_update(
            telegram_id, trader_username, source_group_id, source_group_title,
            channel_id, channel_title,
        )
        await update.message.reply_text(
            "تم إرسال طلب التحديث ✅\nهيتفعّل بعد ما الأدمن يوافق عليه."
        )
        if ADMIN_ID:
            existing = get_trader_by_telegram_id(telegram_id)
            await context.bot.send_message(
                ADMIN_ID,
                "🔄 طلب تحديث بيانات من تاجر:\n"
                f"القديم: {existing['trader_username']} | مصدر: {existing['source_group_title']} | قناة: {existing['channel_title']}\n"
                f"الجديد: {trader_username} | مصدر: {source_group_title} | قناة: {channel_title}\n"
                "استخدم /admin → التحديثات المعلقة للموافقة أو الرفض.",
            )
        return ConversationHandler.END

    upsert_trader(telegram_id, trader_username, source_group_id, source_group_title, channel_id, channel_title)

    await update.message.reply_text(
        "تسجيلك خلص ✅\n\n"
        f"للاشتراك (5$/شهر) كلم الأدمن مباشرة: @{ADMIN_USERNAME}\n"
        "وبعد الدفع هيتم تفعيل حسابك ويبدأ النشر أوتوماتيك."
    )

    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            "🆕 تاجر جديد سجّل بياناته:\n"
            f"يوزر: {trader_username}\n"
            f"مصدر: {source_group_title} ({source_group_id})\n"
            f"قناة: {channel_title} ({channel_id})\n"
            "استخدم /admin لمراجعته وتفعيله بعد الدفع.",
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء. ابعت /start تاني لما تحب تسجل.")
    return ConversationHandler.END


# ----------------------- لوحة تحكم الأدمن -----------------------
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


def build_admin_keyboard(status_filter):
    rows = get_traders_by_status(status_filter)
    buttons = []
    for r in rows:
        label = f"{r['trader_username']} | {r['channel_title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"view_{r['id']}")])
    return buttons, rows


def main_menu_keyboard():
    return [
        [InlineKeyboardButton("🆕 طلبات جديدة (Pending)", callback_data="list_pending")],
        [InlineKeyboardButton("✅ مشتركين نشطين (Active)", callback_data="list_active")],
        [InlineKeyboardButton("⛔ منتهية صلاحيتهم (Expired)", callback_data="list_expired")],
        [InlineKeyboardButton("🔄 التحديثات المعلقة", callback_data="list_updates")],
    ]


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "📋 لوحة تحكم التجار", reply_markup=InlineKeyboardMarkup(main_menu_keyboard())
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("مش مسموح لك.", show_alert=True)
        return
    await query.answer()

    data = query.data

    if data.startswith("list_") and data != "list_updates":
        status = data.split("_", 1)[1]
        buttons, rows = build_admin_keyboard(status)
        if not rows:
            buttons = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]
            await query.edit_message_text(f"مفيش تجار بحالة '{status}' دلوقتي.", reply_markup=InlineKeyboardMarkup(buttons))
            return
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(
            f"قائمة التجار ({status}):", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "back_main":
        await query.edit_message_text("📋 لوحة تحكم التجار", reply_markup=InlineKeyboardMarkup(main_menu_keyboard()))

    elif data == "list_updates":
        rows = get_traders_with_pending_update()
        if not rows:
            buttons = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]
            await query.edit_message_text("مفيش تحديثات معلقة دلوقتي.", reply_markup=InlineKeyboardMarkup(buttons))
            return
        buttons = [
            [InlineKeyboardButton(f"{r['trader_username']} → {r['pending_username']}", callback_data=f"view_update_{r['id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text("التحديثات المعلقة:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("view_update_"):
        trader_id = int(data.split("_", 2)[2])
        t = get_trader(trader_id)
        if not t or not t["has_pending_update"]:
            await query.edit_message_text("مفيش تحديث معلق للتاجر ده.")
            return
        text = (
            f"يوزر: {t['trader_username']} → {t['pending_username']}\n"
            f"مصدر: {t['source_group_title']} → {t['pending_source_group_title']}\n"
            f"قناة: {t['channel_title']} → {t['pending_channel_title']}"
        )
        buttons = [
            [
                InlineKeyboardButton("✅ قبول التحديث", callback_data=f"approve_update_{trader_id}"),
                InlineKeyboardButton("❌ رفض التحديث", callback_data=f"reject_update_{trader_id}"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="list_updates")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("approve_update_"):
        trader_id = int(data.split("_", 2)[2])
        apply_pending_update(trader_id)
        t = get_trader(trader_id)
        await context.bot.send_message(t["telegram_id"], "تم قبول تحديث بياناتك ✅")
        await query.edit_message_text(f"تم تحديث بيانات {t['trader_username']} ✅")

    elif data.startswith("reject_update_"):
        trader_id = int(data.split("_", 2)[2])
        clear_pending_update(trader_id)
        t = get_trader(trader_id)
        await context.bot.send_message(t["telegram_id"], "تم رفض طلب التحديث. تواصل مع الأدمن لو محتاج توضيح.")
        await query.edit_message_text(f"تم رفض تحديث {t['trader_username']} ❌")

    elif data.startswith("view_"):
        trader_id = int(data.split("_", 1)[1])
        t = get_trader(trader_id)
        if not t:
            await query.edit_message_text("التاجر ده مش موجود.")
            return
        text = (
            f"يوزر: {t['trader_username']}\n"
            f"مصدر: {t['source_group_title']} ({t['source_group_id']})\n"
            f"قناة: {t['channel_title']} ({t['channel_id']})\n"
            f"الحالة: {t['status']}\n"
            f"ينتهي: {t['expires_at'] or '-'}"
        )
        buttons = [
            [
                InlineKeyboardButton("✅ تفعيل/تجديد", callback_data=f"activate_{trader_id}"),
                InlineKeyboardButton("⛔ إيقاف", callback_data=f"deactivate_{trader_id}"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("activate_"):
        trader_id = int(data.split("_", 1)[1])
        set_status(trader_id, "active", extend=True)
        t = get_trader(trader_id)
        await context.bot.send_message(
            t["telegram_id"],
            f"تم تفعيل اشتراكك ✅ صالح لمدة {SUBSCRIPTION_DAYS} يوم. هيبدأ النشر في قناتك حالًا.",
        )
        await query.edit_message_text(f"تم تفعيل {t['trader_username']} ✅")

    elif data.startswith("deactivate_"):
        trader_id = int(data.split("_", 1)[1])
        set_status(trader_id, "expired")
        t = get_trader(trader_id)
        await context.bot.send_message(
            t["telegram_id"], "تم إيقاف اشتراكك. كلم الأدمن لو عايز تجدد."
        )
        await query.edit_message_text(f"تم إيقاف {t['trader_username']} ⛔")


# ----------------------- سحب المنشورات من صفحة المعاينة العامة (بدون عضوية) -----------------------
def get_last_post_id(source_key):
    conn = db_connect()
    row = conn.execute("SELECT last_post_id FROM source_state WHERE source_key=?", (source_key,)).fetchone()
    conn.close()
    return row["last_post_id"] if row else 0


def set_last_post_id(source_key, post_id):
    conn = db_connect()
    conn.execute(
        "INSERT INTO source_state (source_key, last_post_id) VALUES (?, ?) "
        "ON CONFLICT(source_key) DO UPDATE SET last_post_id=excluded.last_post_id",
        (source_key, post_id),
    )
    conn.commit()
    conn.close()


def fetch_public_posts(username: str):
    """يجيب آخر المنشورات من صفحة المعاينة العامة t.me/s/username (فيديو + كابشن فقط)."""
    clean = username.lstrip("@")
    url = f"https://t.me/s/{clean}"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"فشل تحميل صفحة المعاينة العامة لـ {clean}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for block in soup.select("div.tgme_widget_message"):
        data_post = block.get("data-post", "")
        if "/" not in data_post:
            continue
        try:
            post_id = int(data_post.split("/")[-1])
        except ValueError:
            continue

        video_tag = block.select_one("video.tgme_widget_message_video")
        if not video_tag or not video_tag.get("src"):
            continue  # مش منشور فيديو، تجاهله

        text_tag = block.select_one("div.tgme_widget_message_text")
        caption = text_tag.get_text("\n", strip=True) if text_tag else None
        if not caption:
            continue  # مفيش وصف، تجاهله

        posts.append({"id": post_id, "video_url": video_tag["src"], "caption": caption})

    return posts


async def scrape_public_sources(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    rows = conn.execute(
        "SELECT DISTINCT source_group_id FROM traders WHERE status='active' AND source_group_id LIKE '@%'"
    ).fetchall()
    conn.close()

    for row in rows:
        source_key = row["source_group_id"]
        posts = fetch_public_posts(source_key)
        if not posts:
            continue

        last_seen = get_last_post_id(source_key)
        new_posts = [p for p in posts if p["id"] > last_seen]
        if not new_posts:
            continue
        new_posts.sort(key=lambda p: p["id"])  # الأقدم الأول

        matching_traders = get_active_traders_by_source(source_key)

        for post in new_posts:
            for t in matching_traders:
                try:
                    await context.bot.send_video(
                        chat_id=t["channel_id"],
                        video=post["video_url"],
                        caption=post["caption"][:1024],
                    )
                except Exception as e:
                    logger.warning(f"فشل إرسال منشور {post['id']} لقناة {t['channel_id']}: {e}")
            set_last_post_id(source_key, post["id"])
async def relay_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    # فلتر: لازم فيديو + وصف (كابشن) عشان يعتبر "عرض حساب"
    if not msg.video or not msg.caption:
        return

    matching_traders = get_active_traders_by_source(msg.chat_id)
    for t in matching_traders:
        try:
            await context.bot.copy_message(
                chat_id=t["channel_id"],
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            logger.warning(f"فشل النشر لقناة {t['channel_id']}: {e}")


# ----------------------- فحص الاشتراكات المنتهية (يومي) -----------------------
async def check_expired(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    now = datetime.utcnow().isoformat()
    rows = conn.execute(
        "SELECT * FROM traders WHERE status='active' AND expires_at < ?", (now,)
    ).fetchall()
    for t in rows:
        conn.execute("UPDATE traders SET status='expired' WHERE id=?", (t["id"],))
        try:
            await context.bot.send_message(
                t["telegram_id"],
                f"⚠️ اشتراكك خلص. كلم @{ADMIN_USERNAME} عشان تجدد وترجع تستقبل المنشورات.",
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


# ----------------------- تشغيل البوت -----------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(تحديث|update)$"), start_update),
        ],
        states={
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_username)],
            ASK_SOURCE_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_source_group)],
            ASK_CHANNEL: [MessageHandler(filters.TEXT | filters.FORWARDED, ask_channel)],
            WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_channel)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(reg_conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    # يسمع لكل رسائل الجروبات (المصادر بتتفلتر جوه relay_source_post حسب قاعدة البيانات)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, relay_source_post))

    if app.job_queue:
        app.job_queue.run_repeating(check_expired, interval=3600, first=10)
        app.job_queue.run_repeating(scrape_public_sources, interval=60, first=15)

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
