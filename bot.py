import os
import time
import sqlite3
import threading
import base64
import requests
import telebot

from google import genai

from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# ==================================================
# CONFIGURATION
# ==================================================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

FREE_LIMIT = 15
MONTHLY_PRICE = 100

DB_FILE = "bossai.db"

bot = telebot.TeleBot(
    TOKEN,
    parse_mode=None
)


# ==================================================
# DATABASE
# ==================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_database():

    conn = get_db()

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


def current_date():
    return time.strftime("%Y-%m-%d")


def get_user(
    user_id,
    first_name="",
    username=""
):

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if user is None:

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
            first_name or "",
            username or "",
            current_date(),
            int(time.time())
        ))

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    elif user["free_date"] != current_date():

        conn.execute("""
            UPDATE users
            SET free_used=0,
                free_date=?
            WHERE user_id=?
        """, (
            current_date(),
            user_id
        ))

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    conn.close()

    return user


# ==================================================
# SUBSCRIPTION
# ==================================================

def subscription_active(user):

    return (
        user["subscription_until"]
        and
        user["subscription_until"] > int(time.time())
    )


def get_subscription_price(user):

    if (
        user["referrals"] >= 50
        and
        user["paid_referrals"] >= 10
    ):
        return 50

    if user["referrals"] >= 30:
        return 70

    return 100


# ==================================================
# MAIN KEYBOARD
# ==================================================

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


# ==================================================
# CHAT MEMORY
# ==================================================

def save_message(
    user_id,
    role,
    content
):

    conn = get_db()

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

    conn = get_db()

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


# ==================================================
# SYSTEM INSTRUCTION
# ==================================================

def system_prompt():

    return """
You are BOSSAI, a natural all-in-one AI assistant.

Speak naturally.

Default language is English.

If the user speaks Amharic,
respond naturally in Amharic.

If the user speaks another language,
respond naturally in that language.

Do not unnecessarily say that you are a bot.

Do not use hashtag symbols.

Be helpful, clear and natural.

Remember relevant conversation context.

If the user sends a follow-up question,
understand the previous conversation.
"""


# ==================================================
# OPENROUTER CHAT
# ==================================================

CHAT_MODELS = {

    "DeepSeek":
        "deepseek/deepseek-chat",

    "GPT-4o":
        "openai/gpt-4o",

    "Claude":
        "anthropic/claude-3.5-sonnet",

    "Grok":
        "x-ai/grok-beta"
}


def ask_openrouter(
    user_id,
    text
):

    if not OPENROUTER_API_KEY:

        raise RuntimeError(
            "OPENROUTER_API_KEY is missing."
        )

    user = get_user(user_id)

    model = user["model"]

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
                CHAT_MODELS[model],

            "messages":
                messages

        },

        timeout=90
    )

    if not response.ok:

        raise RuntimeError(
            f"OpenRouter {response.status_code}: "
            f"{response.text}"
        )

    data = response.json()

    return (
        data["choices"][0]
        ["message"]["content"]
    )


# ==================================================
# GEMINI CHAT
# ==================================================

def ask_gemini(
    user_id,
    text
):

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "GEMINI_API_KEY is missing."
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

Current user message:

{text}
"""

    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    response = client.models.generate_content(

        model="gemini-3.7-flash",

        contents=prompt

    )

    if not response.text:

        raise RuntimeError(
            "Gemini returned an empty response."
        )

    return response.text


# ==================================================
# AI ROUTER
# ==================================================

def ask_ai(
    user_id,
    text
):

    user = get_user(user_id)

    if user["model"] == "Gemini":

        return ask_gemini(
            user_id,
            text
        )

    try:

        return ask_openrouter(
            user_id,
            text
        )

    except Exception as openrouter_error:

        print(
            "OpenRouter failed:",
            openrouter_error
        )

        if GEMINI_API_KEY:

            return ask_gemini(
                user_id,
                text
            )

        raise


# ==================================================
# TYPING INDICATOR
# ==================================================

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


# ==================================================
# SEND LONG MESSAGE
# ==================================================

def send_long_message(
    chat_id,
    text
):

    if not text:

        text = (
            "Sorry, I could not generate "
            "a response."
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


# ==================================================
# START
# ==================================================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    user = get_user(

        message.from_user.id,

        message.from_user.first_name,

        message.from_user.username

    )

    name = (
        message.from_user.first_name
        or
        "there"
    )

    bot.send_message(

        message.chat.id,

        f"""
Hello {name}! Welcome to BOSSAI — your all-in-one AI assistant.

Access GPT-4o, Claude, DeepSeek, Grok, and Gemini in one bot.

I can:
• Answer questions
• Write and translate text
• Write and debug code
• Solve math problems
• Remember conversations
• Generate images

Free: 15 messages per day
Unlimited: 100 ETB/month

Use the buttons below.
""",

        reply_markup=main_keyboard()

    )


# ==================================================
# HELP
# ==================================================

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

Payment Methods
Choose Telebirr, Payoneer or PayPal.

Referral
Invite users and receive discounts.

Models
Choose your AI model.

Restart
Clear your current conversation.

Create Image
Generate an image from a description.

Create Video
Video generation can be connected later.

Create Music
Music generation can be connected later.

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


# ==================================================
# PAYMENT MENU
# ==================================================

def show_payment_menu(message):

    user = get_user(
        message.from_user.id
    )

    price = get_subscription_price(
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


@bot.message_handler(
    commands=["menu"]
)
def menu_command(message):

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


# ==================================================
# PAYMENT CALLBACK
# ==================================================

@bot.callback_query_handler(
    func=lambda call:
    call.data in
    [
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

        price = get_subscription_price(
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

Telebirr:
0964990206

Telegram:
@Silent_Survivorr

After payment, send your payment receipt screenshot here.

Your subscription will be activated after manual verification.
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


# ==================================================
# PAYMENT RECEIPT
# ==================================================

@bot.message_handler(
    content_types=["photo"]
)
def payment_receipt(message):

    if ADMIN_ID == 0:

        bot.reply_to(

            message,

            "Receipt received. Admin verification is not configured yet."

        )

        return

    user = get_user(

        message.from_user.id,

        message.from_user.first_name,

        message.from_user.username

    )

    price = get_subscription_price(
        user
    )

    conn = get_db()

    cursor = conn.execute(

        """
        INSERT INTO payments
        (
            user_id,
            amount,
            status,
            created_at
        )
        VALUES (?, ?, 'pending', ?)
        """,

        (
            message.from_user.id,
            price,
            int(time.time())
        )

    )

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


# ==================================================
# APPROVE / REJECT PAYMENT
# ==================================================

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

        conn = get_db()

        conn.execute(

            """
            UPDATE payments
            SET status='approved'
            WHERE id=?
            """,

            (payment_id,)

        )

        conn.execute(

            """
            UPDATE users
            SET subscription_until=?
            WHERE user_id=?
            """,

            (
                until,
                user_id
            )

        )

        referral = conn.execute(

            """
            SELECT referred_by
            FROM users
            WHERE user_id=?
            """,

            (user_id,)

        ).fetchone()

        if referral and referral["referred_by"]:

            conn.execute(

                """
                UPDATE users
                SET paid_referrals =
                    paid_referrals + 1
                WHERE user_id=?
                """,

                (
                    referral["referred_by"],
                )

            )

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

        conn = get_db()

        conn.execute(

            """
            UPDATE payments
            SET status='rejected'
            WHERE id=?
            """,

            (payment_id,)

        )

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
Your payment receipt was rejected.

Please send a valid receipt again.

Support:
@Silent_Survivorr
"""

        )


# ==================================================
# REFERRAL
# ==================================================

@bot.message_handler(
    func=lambda m:
    m.text == "👥 Referral"
)
def referral(message):

    user = get_user(
        message.from_user.id
    )

    bot_username = (
        bot.get_me().username
    )

    referral_link = (

        f"https://t.me/"
        f"{bot_username}"
        f"?start=ref_"
        f"{message.from_user.id}"

    )

    price = get_subscription_price(
        user
    )

    bot.send_message(

        message.chat.id,

        f"""
Referral Program

Your referral link:

{referral_link}

30 referrals
→ 70 ETB/month

50 referrals + 10 paid referrals
→ 50 ETB/month

Your referrals:
{user["referrals"]}

Paid referrals:
{user["paid_referrals"]}

Current price:
{price} ETB/month
"""

    )


# ==================================================
# MODELS
# ==================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🤖 Models"
)
def models(message):

    markup = InlineKeyboardMarkup()

    for model in CHAT_MODELS:

        markup.add(

            InlineKeyboardButton(

                model,

                callback_data=
                f"model:{model}"

            )

        )

    markup.add(

        InlineKeyboardButton(

            "Gemini",

            callback_data=
            "model:Gemini"

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

    conn = get_db()

    conn.execute(

        """
        UPDATE users
        SET model=?
        WHERE user_id=?
        """,

        (
            model,
            call.from_user.id
        )

    )

    conn.commit()
    conn.close()

    bot.send_message(

        call.message.chat.id,

        f"Model changed to {model}."

    )


# ==================================================
# ACCOUNT
# ==================================================

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

        plan = (
            "Unlimited active\n"
            f"Approximately {days} days remaining"
        )

    else:

        plan = "Free plan"

    conn = get_db()

    total_users = conn.execute(

        "SELECT COUNT(*) AS count FROM users"

    ).fetchone()["count"]

    paid_users = conn.execute(

        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE subscription_until > ?
        """,

        (int(time.time()),)

    ).fetchone()["count"]

    conn.close()

    bot.send_message(

        message.chat.id,

        f"""
My Account

Plan:
{plan}

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


# ==================================================
# RESTART
# ==================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🔄 Restart"
)
def restart(message):

    conn = get_db()

    conn.execute(

        """
        DELETE FROM messages
        WHERE user_id=?
        """,

        (
            message.from_user.id,
        )

    )

    conn.commit()
    conn.close()

    bot.send_message(

        message.chat.id,

        "Conversation restarted. You can start a new chat.",

        reply_markup=main_keyboard()

    )


# ==================================================
# IMAGE GENERATION
# ==================================================

IMAGE_MODEL = (
    "google/gemini-3.1-flash-image-preview"
)


def generate_image(prompt):

    if not OPENROUTER_API_KEY:

        raise RuntimeError(
            "OPENROUTER_API_KEY is missing."
        )

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

            f"Image API {response.status_code}: "
            f"{response.text}"

        )

    data = response.json()

    image_data = (
        data["data"][0]
        .get("b64_json")
    )

    if not image_data:

        raise RuntimeError(
            "No image data returned."
        )

    return base64.b64decode(
        image_data
    )


image_waiting = set()


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
Create Image

Send me a description of the image you want.

Example:

A futuristic city at night, cinematic lighting, realistic, highly detailed.
"""

    )


def process_image_prompt(message):

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

    stop_event = threading.Event()

    thread = threading.Thread(

        target=typing_loop,

        args=(

            message.chat.id,

            stop_event

        ),

        daemon=True

    )

    thread.start()

    try:

        bot.send_message(

            message.chat.id,

            "🎨 Creating your image..."

        )

        image = generate_image(
            prompt
        )

        bot.send_photo(

            message.chat.id,

            image,

            caption=
            "Generated by BOSSAI"

        )

    except Exception as error:

        print(
            "IMAGE ERROR:",
            error
        )

        bot.send_message(

            message.chat.id,

            "Sorry, image generation is unavailable right now."

        )

    finally:

        stop_event.set()


# ==================================================
# VIDEO
# ==================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🎬 Create Video"
)
def video_button(message):

    bot.send_message(

        message.chat.id,

        """
🎬 Create Video

The video menu is ready.

A dedicated video-generation API still needs to be connected before BOSSAI can generate videos.
"""

    )


# ==================================================
# MUSIC
# ==================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🎵 Create Music"
)
def music_button(message):

    bot.send_message(

        message.chat.id,

        """
🎵 Create Music

The music menu is ready.

A dedicated music-generation API still needs to be connected before BOSSAI can generate music.
"""

    )


# ==================================================
# FILES
# ==================================================

@bot.message_handler(
    content_types=[
        "document",
        "voice",
        "audio"
    ]
)
def file_handler(message):

    bot.reply_to(

        message,

        """
I received your file.

File and voice analysis can be connected to the appropriate processing service.
"""

    )


# ==================================================
# MAIN CHAT
# ==================================================

busy_users = set()
busy_lock = threading.Lock()

last_request = {}


@bot.message_handler(
    content_types=["text"]
)
def chat(message):

    text = (
        message.text.strip()
    )

    if not text:

        return

    if text.startswith("/"):

        return

    user_id = (
        message.from_user.id
    )

    # Image prompt
    if user_id in image_waiting:

        process_image_prompt(
            message
        )

        return

    # Two-second protection
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

        if user["free_used"] >= FREE_LIMIT:

            bot.reply_to(

                message,

                """
You have used all 15 free messages for today.

Unlimited access is 100 ETB/month.

Open Payment Methods to continue.
"""

            )

            return

        conn = get_db()

        conn.execute(

            """
            UPDATE users
            SET free_used=free_used+1
            WHERE user_id=?
            """,

            (user_id,)

        )

        conn.commit()
        conn.close()

    # Busy protection
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

    stop_event = threading.Event()

    typing_thread = threading.Thread(

        target=typing_loop,

        args=(

            message.chat.id,

            stop_event

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

        stop_event.set()

        with busy_lock:

            busy_users.discard(
                user_id
            )


# ==================================================
# RUN BOT
# ==================================================

def main():

    init_database()

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
