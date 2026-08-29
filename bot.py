import os
import time
import sqlite3
import threading
import base64
import requests
import telebot

from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# =========================
# CONFIG
# =========================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

FREE_LIMIT = 15
MONTHLY_PRICE = 100

DB_FILE = "bossai.db"

bot = telebot.TeleBot(
    TOKEN,
    parse_mode=None
)


# =========================
# STATE
# =========================

busy_users = set()
busy_lock = threading.Lock()

last_request = {}

image_waiting = set()


# =========================
# MODELS
# =========================

CHAT_MODELS = {
    "DeepSeek": "deepseek/deepseek-chat",
    "GPT-4o": "openai/gpt-4o",
    "Claude": "anthropic/claude-3.5-sonnet",
    "Grok": "x-ai/grok-beta"
}

IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"


# =========================
# DATABASE
# =========================

def db():

    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            free_used INTEGER DEFAULT 0,
            free_date TEXT,
            model TEXT DEFAULT 'DeepSeek',
            subscription_until INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            referrals INTEGER DEFAULT 0,
            paid_referrals INTEGER DEFAULT 0,
            created_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        )
    """)

    conn.commit()
    conn.close()


def today():

    return time.strftime("%Y-%m-%d")


def get_user(
    user_id,
    first_name="",
    username=""
):

    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:

        conn.execute("""
            INSERT INTO users
            (
                user_id,
                first_name,
                username,
                free_used,
                free_date,
                created_at
            )
            VALUES (?, ?, ?, 0, ?, ?)
        """, (
            user_id,
            first_name,
            username or "",
            today(),
            int(time.time())
        ))

        conn.commit()

        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    elif row["free_date"] != today():

        conn.execute("""
            UPDATE users
            SET free_used=0,
                free_date=?
            WHERE user_id=?
        """, (
            today(),
            user_id
        ))

        conn.commit()

        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    conn.close()

    return row


# =========================
# SUBSCRIPTION
# =========================

def subscription_active(user):

    return (
        user["subscription_until"] is not None
        and
        user["subscription_until"] > int(time.time())
    )


def get_price(user):

    if (
        user["referrals"] >= 50
        and
        user["paid_referrals"] >= 10
    ):

        return 50

    if user["referrals"] >= 30:

        return 70

    return 100


# =========================
# KEYBOARD
# =========================

def main_keyboard():

    markup = ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    markup.row(
        KeyboardButton("💳 Payment Methods"),
        KeyboardButton("👥 Referral")
    )

    markup.row(
        KeyboardButton("🤖 Models"),
        KeyboardButton("🔄 Restart")
    )

    markup.row(
        KeyboardButton("❓ Help"),
        KeyboardButton("📊 My Account")
    )

    markup.row(
        KeyboardButton("🎨 Create Image"),
        KeyboardButton("🎬 Create Video")
    )

    markup.row(
        KeyboardButton("🎵 Create Music")
    )

    return markup


# =========================
# CONVERSATION MEMORY
# =========================

def save_message(
    user_id,
    role,
    content
):

    conn = db()

    conn.execute("""
        INSERT INTO messages
        (
            user_id,
            role,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        role,
        content,
        int(time.time())
    ))

    conn.commit()
    conn.close()


def get_history(user_id):

    conn = db()

    rows = conn.execute("""
        SELECT role, content
        FROM messages
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
    """, (
        user_id,
    )).fetchall()

    conn.close()

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


# =========================
# TYPING
# =========================

def typing_loop(
    chat_id,
    stop_event
):

    while not stop_event.is_set():

        try:

            bot.send_chat_action(
                chat_id,
                "typing"
            )

        except Exception:

            pass

        stop_event.wait(4)


# =========================
# LONG MESSAGE
# =========================

def send_long_message(
    chat_id,
    text
):

    if not text:

        text = (
            "Sorry, I could not generate a response."
        )

    for i in range(
        0,
        len(text),
        4000
    ):

        bot.send_message(
            chat_id,
            text[i:i + 4000]
        )


# =========================
# SYSTEM PROMPT
# =========================

def system_prompt():

    return """
You are BOSSAI, a natural all-in-one AI assistant.

Default language is English.

If the user speaks Amharic,
respond naturally in Amharic.

If the user speaks another language,
respond naturally in that language.

Do not unnecessarily mention that you are a bot.

Do not use hashtag symbols.

Be helpful, clear and natural.

Remember relevant conversation context.
"""


# =========================
# OPENROUTER CHAT
# =========================

def ask_openrouter(
    user_id,
    text
):

    user = get_user(user_id)

    model_name = user["model"]

    history = get_history(
        user_id
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt()
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": text
    })

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",

        headers={
            "Authorization":
                f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":
                "application/json"
        },

        json={
            "model":
                CHAT_MODELS[model_name],

            "messages":
                messages
        },

        timeout=90
    )

    if not response.ok:

        raise RuntimeError(
            f"OpenRouter error: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    return (
        data["choices"][0]
        ["message"]["content"]
    )


# =========================
# GEMINI FALLBACK
# =========================

def ask_gemini(
    user_id,
    text
):

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "Gemini API key is missing."
        )

    history = get_history(
        user_id
    )

    conversation = ""

    for item in history:

        conversation += (
            item["role"]
            + ": "
            + item["content"]
            + "\n"
        )

    prompt = f"""
{system_prompt()}

Previous conversation:

{conversation}

User:

{text}
"""

    response = requests.post(

        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",

        params={
            "key": GEMINI_API_KEY
        },

        headers={
            "Content-Type":
                "application/json"
        },

        json={
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ]
        },

        timeout=90
    )

    if not response.ok:

        raise RuntimeError(
            f"Gemini error: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    return (
        data["candidates"][0]
        ["content"]["parts"][0]["text"]
    )


# =========================
# AI
# =========================

def ask_ai(
    user_id,
    text
):

    user = get_user(user_id)

    try:

        if user["model"] == "Gemini":

            return ask_gemini(
                user_id,
                text
            )

        return ask_openrouter(
            user_id,
            text
        )

    except Exception as error:

        print(
            "OpenRouter error:",
            error
        )

        if GEMINI_API_KEY:

            return ask_gemini(
                user_id,
                text
            )

        raise


# =========================
# IMAGE GENERATION
# =========================

def generate_image(
    prompt
):

    response = requests.post(

        "https://openrouter.ai/api/v1/images",

        headers={
            "Authorization":
                f"Bearer {OPENROUTER_API_KEY}",

            "Content-Type":
                "application/json"
        },

        json={
            "model":
                IMAGE_MODEL,

            "prompt":
                prompt,

            "n":
                1
        },

        timeout=180
    )

    if not response.ok:

        raise RuntimeError(
            f"Image API error: "
            f"{response.status_code} "
            f"{response.text}"
        )

    data = response.json()

    if "data" not in data:

        raise RuntimeError(
            f"No image returned: {data}"
        )

    image_item = data["data"][0]

    encoded = image_item.get(
        "b64_json"
    )

    if not encoded:

        raise RuntimeError(
            "Image response did not contain b64_json."
        )

    return base64.b64decode(
        encoded
    )


# =========================
# START
# =========================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    get_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username
    )

    name = (
        message.from_user.first_name
        or "there"
    )

    text = f"""
Hello {name}! Welcome to BOSSAI — your all-in-one AI assistant.

Access GPT-4o, Claude, DeepSeek, Grok, and Gemini in one bot.

I can:
• Answer any question
• Write and translate text
• Write and debug code
• Solve math problems
• Analyze supported content
• Remember our conversation
• Generate images

Free: 15 messages per day
Unlimited: 100 ETB/month

Use the buttons below to continue.
"""

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=main_keyboard()
    )


# =========================
# HELP
# =========================

@bot.message_handler(
    commands=["help"]
)
def help_command(message):

    bot.send_message(
        message.chat.id,
        """
BOSSAI Help

Chat
Send your question directly.

Free
15 messages per day.

Unlimited
100 ETB/month.

Payment
Open Payment Methods.

Referral
Open Referral.

Models
Choose an AI model.

Restart
Start a fresh conversation.

Image
Create an image from a text prompt.

Video
Video generation can be connected to a supported video API.

Music
Music generation can be connected to a supported music API.

Support
@Silent_Survivorr
"""
    )


@bot.message_handler(
    func=lambda m:
    m.text == "❓ Help"
)
def help_button(message):

    help_command(message)


# =========================
# PAYMENT
# =========================

@bot.message_handler(
    commands=["menu"]
)
def payment_menu(message):

    show_payment_menu(
        message
    )


@bot.message_handler(
    func=lambda m:
    m.text == "💳 Payment Methods"
)
def payment_button(message):

    show_payment_menu(
        message
    )


def show_payment_menu(message):

    user = get_user(
        message.from_user.id
    )

    price = get_price(
        user
    )

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            f"💳 Telebirr — {price} ETB/month",
            callback_data="telebirr"
        )
    )

    markup.add(
        InlineKeyboardButton(
            "🌍 Payoneer",
            callback_data="payoneer"
        )
    )

    markup.add(
        InlineKeyboardButton(
            "🅿️ PayPal",
            callback_data="paypal"
        )
    )

    bot.send_message(
        message.chat.id,
        "Choose your payment method:",
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call:
    call.data in [
        "telebirr",
        "payoneer",
        "paypal"
    ]
)
def payment_callback(call):

    bot.answer_callback_query(
        call.id
    )

    if call.data == "telebirr":

        user = get_user(
            call.from_user.id
        )

        price = get_price(
            user
        )

        bot.send_message(
            call.message.chat.id,
            f"""
Telebirr Payment

Amount:
{price} ETB/month

Receiver:
Hussen

Telebirr number:
0964990206

Telegram:
@Silent_Survivorr

After payment, send your payment receipt screenshot directly here.

After verification, your monthly unlimited access will be activated.
"""
        )

    elif call.data == "payoneer":

        bot.send_message(
            call.message.chat.id,
            "Payoneer: Soon Available."
        )

    elif call.data == "paypal":

        bot.send_message(
            call.message.chat.id,
            "PayPal: Soon Available."
        )


# =========================
# PAYMENT RECEIPT
# =========================

@bot.message_handler(
    content_types=["photo"]
)
def payment_receipt(message):

    if ADMIN_ID == 0:

        bot.reply_to(
            message,
            "Receipt received, but admin verification is not configured."
        )

        return

    user = get_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username
    )

    price = get_price(
        user
    )

    conn = db()

    cursor = conn.execute("""
        INSERT INTO payments
        (
            user_id,
            amount,
            status,
            created_at
        )
        VALUES (?, ?, 'pending', ?)
    """, (
        message.from_user.id,
        price,
        int(time.time())
    ))

    payment_id = cursor.lastrowid

    conn.commit()
    conn.close()

    markup = InlineKeyboardMarkup()

    markup.add(

        InlineKeyboardButton(
            "✅ Approve",
            callback_data=
            f"approve:{payment_id}:{message.from_user.id}"
        ),

        InlineKeyboardButton(
            "❌ Reject",
            callback_data=
            f"reject:{payment_id}:{message.from_user.id}"
        )
    )

    caption = f"""
Payment Receipt

Payment ID:
{payment_id}

User:
{message.from_user.first_name}

Username:
@{message.from_user.username or 'none'}

User ID:
{message.from_user.id}

Amount:
{price} ETB

Status:
Pending
"""

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=caption,
        reply_markup=markup
    )

    bot.reply_to(
        message,
        "Your receipt has been sent for verification. Please wait for approval."
    )


# =========================
# APPROVE / REJECT
# =========================

@bot.callback_query_handler(
    func=lambda call:
    call.data.startswith("approve:")
    or
    call.data.startswith("reject:")
)
def payment_decision(call):

    if call.from_user.id != ADMIN_ID:

        bot.answer_callback_query(
            call.id,
            "Not authorized.",
            show_alert=True
        )

        return

    bot.answer_callback_query(
        call.id
    )

    parts = call.data.split(":")

    action = parts[0]

    payment_id = int(
        parts[1]
    )

    user_id = int(
        parts[2]
    )

    if action == "approve":

        until = (
            int(time.time())
            +
            30 * 24 * 60 * 60
        )

        conn = db()

        conn.execute("""
            UPDATE payments
            SET status='approved'
            WHERE id=?
        """, (
            payment_id,
        ))

        conn.execute("""
            UPDATE users
            SET subscription_until=?
            WHERE user_id=?
        """, (
            until,
            user_id
        ))

        referrer = conn.execute(
            """
            SELECT referred_by
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if (
            referrer
            and
            referrer["referred_by"]
        ):

            conn.execute("""
                UPDATE users
                SET paid_referrals =
                    paid_referrals + 1
                WHERE user_id=?
            """, (
                referrer["referred_by"],
            ))

        conn.commit()
        conn.close()

        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )

        bot.send_message(
            user_id,
            """
Payment approved.

Your unlimited subscription is active for 30 days.

Thank you for using BOSSAI.
"""
        )

    else:

        conn = db()

        conn.execute("""
            UPDATE payments
            SET status='rejected'
            WHERE id=?
        """, (
            payment_id,
        ))

        conn.commit()
        conn.close()

        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )

        bot.send_message(
            user_id,
            """
Your payment receipt was not approved.

Please check the payment and send a valid receipt again.

Support:
@Silent_Survivorr
"""
        )


# =========================
# REFERRAL
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "👥 Referral"
)
def referral(message):

    user = get_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username
    )

    username = bot.get_me().username

    link = (
        f"https://t.me/{username}"
        f"?start=ref_{message.from_user.id}"
    )

    price = get_price(
        user
    )

    bot.send_message(
        message.chat.id,
        f"""
Referral Program

Your referral link:

{link}

30 referrals
→ 70 ETB/month

50 referrals + 10 paid referrals
→ 50 ETB/month

Your referrals:
{user["referrals"]}

Paid referrals:
{user["paid_referrals"]}

Current monthly price:
{price} ETB
"""
    )


# =========================
# MODELS
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "🤖 Models"
)
def models(message):

    markup = InlineKeyboardMarkup()

    for name in CHAT_MODELS:

        markup.add(
            InlineKeyboardButton(
                name,
                callback_data=
                f"model:{name}"
            )
        )

    markup.add(
        InlineKeyboardButton(
            "Gemini",
            callback_data="model:Gemini"
        )
    )

    user = get_user(
        message.from_user.id
    )

    bot.send_message(
        message.chat.id,
        f"""
Current model:
{user["model"]}

Choose a model:
""",
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call:
    call.data.startswith("model:")
)
def model_callback(call):

    bot.answer_callback_query(
        call.id
    )

    model = call.data.split(
        ":",
        1
    )[1]

    if (
        model not in CHAT_MODELS
        and
        model != "Gemini"
    ):

        return

    conn = db()

    conn.execute("""
        UPDATE users
        SET model=?
        WHERE user_id=?
    """, (
        model,
        call.from_user.id
    ))

    conn.commit()
    conn.close()

    bot.send_message(
        call.message.chat.id,
        f"Model changed to {model}."
    )


# =========================
# ACCOUNT
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "📊 My Account"
)
def account(message):

    user = get_user(
        message.from_user.id
    )

    remaining = max(
        0,
        FREE_LIMIT -
        user["free_used"]
    )

    if subscription_active(user):

        days = max(
            1,
            int(
                (
                    user["subscription_until"]
                    -
                    int(time.time())
                )
                /
                86400
            )
        )

        status = (
            "Unlimited active\n"
            f"Approximately {days} days remaining"
        )

    else:

        status = "Free plan"

    conn = db()

    total_users = conn.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    paid_users = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE subscription_until > ?
        """,
        (int(time.time()),)
    ).fetchone()["c"]

    conn.close()

    bot.send_message(
        message.chat.id,
        f"""
My Account

User ID:
{message.from_user.id}

Plan:
{status}

Free messages remaining today:
{remaining}

Current model:
{user["model"]}

Referrals:
{user["referrals"]}

Paid referrals:
{user["paid_referrals"]}

Registered users:
{total_users}

Active paid users:
{paid_users}
"""
    )


# =========================
# RESTART
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "🔄 Restart"
)
def restart(message):

    conn = db()

    conn.execute("""
        DELETE FROM messages
        WHERE user_id=?
    """, (
        message.from_user.id,
    ))

    conn.commit()
    conn.close()

    bot.send_message(
        message.chat.id,
        "Conversation restarted. You can start a new chat.",
        reply_markup=main_keyboard()
    )


# =========================
# IMAGE BUTTON
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "🎨 Create Image"
)
def image_button(message):

    image_waiting.add(
        message.from_user.id
    )

    bot.send_message(
        message.chat.id,
        """
🎨 Create Image

Send a description of the image you want.

Example:

A cinematic futuristic city at night, realistic lighting, detailed buildings, dramatic sky.
"""
    )


# =========================
# IMAGE GENERATION MESSAGE
# =========================

def handle_image_prompt(
    message
):

    user_id = message.from_user.id

    image_waiting.discard(
        user_id
    )

    prompt = (
        message.text.strip()
    )

    if not prompt:

        bot.send_message(
            message.chat.id,
            "Please describe the image you want."
        )

        return

    stop_typing = threading.Event()

    thread = threading.Thread(
        target=typing_loop,
        args=(
            message.chat.id,
            stop_typing
        ),
        daemon=True
    )

    thread.start()

    try:

        bot.send_message(
            message.chat.id,
            "🎨 Creating your image..."
        )

        image_bytes = generate_image(
            prompt
        )

        bot.send_photo(
            message.chat.id,
            image_bytes,
            caption="Generated by BOSSAI"
        )

    except Exception as error:

        print(
            "IMAGE ERROR:",
            error
        )

        bot.send_message(
            message.chat.id,
            "Sorry, I could not generate the image right now. Please try again."
        )

    finally:

        stop_typing.set()


# =========================
# VIDEO
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "🎬 Create Video"
)
def video_button(message):

    bot.send_message(
        message.chat.id,
        """
🎬 Video Generation

The Video button is ready in the menu.

A video-generation API still needs to be connected before BOSSAI can generate the actual video.
"""
    )


# =========================
# MUSIC
# =========================

@bot.message_handler(
    func=lambda m:
    m.text == "🎵 Create Music"
)
def music_button(message):

    bot.send_message(
        message.chat.id,
        """
🎵 Music Generation

The Music button is ready in the menu.

A music-generation API still needs to be connected before BOSSAI can generate actual music.
"""
    )


# =========================
# DOCUMENT / VOICE
# =========================

@bot.message_handler(
    content_types=[
        "document",
        "voice",
        "audio"
    ]
)
def media_message(message):

    bot.reply_to(
        message,
        """
I received your file/audio.

Document and voice processing needs the corresponding transcription/document API to be connected.
"""
    )


# =========================
# MAIN CHAT
# =========================

@bot.message_handler(
    content_types=["text"]
)
def chat(message):

    text = message.text.strip()

    if not text:

        return

    if text.startswith("/"):

        return

    user_id = message.from_user.id

    # Image prompt mode
    if user_id in image_waiting:

        handle_image_prompt(
            message
        )

        return

    # 2 second protection
    now = time.time()

    previous = last_request.get(
        user_id,
        0
    )

    if (
        now - previous
        < 2
    ):

        bot.reply_to(
            message,
            "⏳ Wait another 2 seconds before submitting your next question . . ."
        )

        return

    last_request[user_id] = now

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )

    # Free limit
    if not subscription_active(user):

        if (
            user["free_used"]
            >= FREE_LIMIT
        ):

            bot.reply_to(
                message,
                """
You have used all 15 free messages for today.

Unlimited access is 100 ETB/month.

Open Payment Methods to continue.
"""
            )

            return

        conn = db()

        conn.execute("""
            UPDATE users
            SET free_used=free_used+1
            WHERE user_id=?
        """, (
            user_id,
        ))

        conn.commit()
        conn.close()

    # Prevent simultaneous questions
    with busy_lock:

        if user_id in busy_users:

            bot.reply_to(
                message,
                "⏳ Wait another 2 seconds before submitting your next question . . ."
            )

            return

        busy_users.add(
            user_id
        )

    stop_typing = threading.Event()

    typing_thread = threading.Thread(
        target=typing_loop,
        args=(
            message.chat.id,
            stop_typing
        ),
        daemon=True
    )

    typing_thread.start()

    try:

        save_message(
            user_id,
            "user",
            text
        )

        answer = ask_ai(
            user_id,
            text
        )

        save_message(
            user_id,
            "assistant",
            answer
        )

        send_long_message(
            message.chat.id,
            answer
        )

    except Exception as error:

        print(
            "CHAT ERROR:",
            error
        )

        bot.reply_to(
            message,
            "Sorry, I could not connect to the AI service right now. Please try again."
        )

    finally:

        stop_typing.set()

        with busy_lock:

            busy_users.discard(
                user_id
            )


# =========================
# RUN
# =========================

def main():

    init_db()

    print(
        "BOSSAI is running..."
    )

    while True:

        try:

            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )

        except Exception as error:

            print(
                "Polling error:",
                error
            )

            time.sleep(5)


if __name__ == "__main__":

    main()
