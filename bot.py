import logging
import os
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from supabase import create_client, Client

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing")

if not OWNER_PASSWORD:
    raise RuntimeError("OWNER_PASSWORD is missing")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# SUPABASE
# ============================================================

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)


# ============================================================
# IN-MEMORY AUTHENTICATION
# ============================================================

authenticated_users: set[int] = set()
awaiting_password: set[int] = set()


# ============================================================
# CREATE TEST CONVERSATION STATES
# ============================================================

(
    CREATE_TEST_NAME,
    CREATE_TEST_DESCRIPTION,
    QUESTION_TEXT,
    QUESTION_TYPE,
    OPTION_A,
    OPTION_B,
    OPTION_C,
    OPTION_D,
    QUESTION_MARKS,
    QUESTION_NEGATIVE_MARKS,
    QUESTION_PHOTO_CHOICE,
    QUESTION_PHOTO,
    AFTER_QUESTION,
) = range(13)


# ============================================================
# KEYBOARDS
# ============================================================

OWNER_DASHBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🏆 Results", "👥 Manage Admins"],
        ["🚪 Logout"],
    ],
    resize_keyboard=True,
)

ADMIN_DASHBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🏆 Results", "🚪 Logout"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["❌ Cancel"],
    ],
    resize_keyboard=True,
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def is_private_chat(update: Update) -> bool:
    """Only allow private chats."""
    return (
        update.effective_chat is not None
        and update.effective_chat.type == "private"
    )


def get_telegram_user_id(update: Update) -> Optional[int]:
    if update.effective_user is None:
        return None

    return update.effective_user.id


async def get_admin_by_telegram_id(
    telegram_user_id: int,
) -> Optional[dict]:
    """Get active admin/owner record."""

    try:
        response = (
            supabase.table("admins")
            .select("*")
            .eq("telegram_user_id", telegram_user_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )

        if response.data:
            return response.data[0]

    except Exception:
        logger.exception("Failed to get admin")

    return None


async def update_admin_profile(update: Update) -> None:
    """Keep display name and username updated."""

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    user = update.effective_user

    display_name = user.full_name if user else "Admin"
    username = user.username if user else None

    try:
        supabase.table("admins").update(
            {
                "display_name": display_name,
                "username": username,
            }
        ).eq(
            "telegram_user_id",
            telegram_user_id,
        ).execute()

    except Exception:
        logger.exception("Failed to update admin profile")


async def get_logged_in_admin(update: Update) -> Optional[dict]:
    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return None

    if telegram_user_id not in authenticated_users:
        return None

    return await get_admin_by_telegram_id(telegram_user_id)


async def send_dashboard(
    update: Update,
    admin: Optional[dict] = None,
) -> None:

    if admin is None:
        admin = await get_logged_in_admin(update)

    if not admin:
        return

    role = admin.get("role")

    if role == "OWNER":
        keyboard = OWNER_DASHBOARD
        role_text = "👑 OWNER"
    else:
        keyboard = ADMIN_DASHBOARD
        role_text = "🛡 ADMIN"

    await update.effective_message.reply_text(
        f"🏟️ PrepArena Admin Panel\n\n"
        f"Role: {role_text}\n\n"
        f"Choose an option below.",
        reply_markup=keyboard,
    )


# ============================================================
# START / LOGIN
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    admin = await get_admin_by_telegram_id(telegram_user_id)

    if not admin:
        await update.message.reply_text(
            "⛔ You are not authorized to use the PrepArena Admin Bot."
        )
        return

    await update_admin_profile(update)

    if telegram_user_id in authenticated_users:
        await send_dashboard(update, admin)
        return

    awaiting_password.add(telegram_user_id)

    if admin.get("role") == "OWNER":
        await update.message.reply_text(
            "🔐 Owner authentication required.\n\n"
            "Please enter the owner password."
        )
    else:
        await update.message.reply_text(
            "🔐 Admin authentication required.\n\n"
            "Please enter your admin password."
        )


async def receive_password(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    if telegram_user_id not in awaiting_password:
        return

    if not update.message or not update.message.text:
        return

    password = update.message.text.strip()

    admin = await get_admin_by_telegram_id(telegram_user_id)

    if not admin:
        awaiting_password.discard(telegram_user_id)

        await update.message.reply_text(
            "⛔ You are not authorized."
        )
        return

    role = admin.get("role")

    authenticated = False

    # --------------------------------------------------------
    # OWNER LOGIN
    # --------------------------------------------------------

    if role == "OWNER":

        if password == OWNER_PASSWORD:
            authenticated = True

    # --------------------------------------------------------
    # ADMIN LOGIN
    # --------------------------------------------------------

    elif role == "ADMIN":

        password_hash = admin.get("password_hash")

        if password_hash:

            try:
                authenticated = bcrypt.checkpw(
                    password.encode("utf-8"),
                    password_hash.encode("utf-8"),
                )
            except Exception:
                logger.exception("Admin password verification failed")

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    if authenticated:

        authenticated_users.add(telegram_user_id)
        awaiting_password.discard(telegram_user_id)

        await update_admin_profile(update)

        await update.message.reply_text(
            "✅ Login successful."
        )

        await send_dashboard(update, admin)

    else:

        await update.message.reply_text(
            "❌ Incorrect password.\n\n"
            "Please try again."
        )


# ============================================================
# LOGOUT
# ============================================================

async def logout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    authenticated_users.discard(telegram_user_id)
    awaiting_password.discard(telegram_user_id)

    # Remove any unfinished create-test conversation data.
    context.user_data.clear()

    await update.message.reply_text(
        "🚪 You have been logged out.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# CREATE TEST HELPERS
# ============================================================

async def delete_draft_test(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Delete the draft test created during the current conversation.

    Because questions/options/etc. use ON DELETE CASCADE,
    related data is removed automatically.
    """

    test_id = context.user_data.get("create_test_id")

    if not test_id:
        return

    try:
        supabase.table("tests").delete().eq(
            "id",
            test_id,
        ).execute()

        logger.info(
            "Deleted cancelled draft test %s",
            test_id,
        )

    except Exception:
        logger.exception(
            "Failed to delete draft test %s",
            test_id,
        )


def clear_create_test_data(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    keys = [
        "create_test_id",
        "create_test_name",
        "create_test_description",
        "current_question_text",
        "current_question_type",
        "current_option_a",
        "current_option_b",
        "current_option_c",
        "current_option_d",
        "current_question_marks",
        "current_question_negative_marks",
        "current_question_photo_file_id",
        "question_count",
        "total_marks",
    ]

    for key in keys:
        context.user_data.pop(key, None)


async def cancel_create_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    await delete_draft_test(context)

    clear_create_test_data(context)

    await update.effective_message.reply_text(
        "❌ Create Test cancelled.\n\n"
        "Any unfinished draft created during this process has been removed."
    )

    admin = await get_logged_in_admin(update)

    if admin:
        await send_dashboard(update, admin)

    return ConversationHandler.END


# ============================================================
# CREATE TEST - START
# ============================================================

async def begin_create_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    admin = await get_logged_in_admin(update)

    if not admin:
        await update.message.reply_text(
            "⛔ Please login first."
        )
        return ConversationHandler.END

    clear_create_test_data(context)

    await update.message.reply_text(
        "➕ Create Test\n\n"
        "Step 1/2\n\n"
        "Enter the test name.\n\n"
        "Example:\n"
        "JEE Main Mock Test 01",
        reply_markup=CANCEL_KEYBOARD,
    )

    return CREATE_TEST_NAME


# ============================================================
# CREATE TEST - NAME
# ============================================================

async def receive_test_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return CREATE_TEST_NAME

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Test name cannot be empty.\n\n"
            "Please enter the test name."
        )
        return CREATE_TEST_NAME

    if len(text) > 200:
        await update.message.reply_text(
            "❌ Test name is too long.\n\n"
            "Please keep it under 200 characters."
        )
        return CREATE_TEST_NAME

    context.user_data["create_test_name"] = text

    await update.message.reply_text(
        "Step 2/2\n\n"
        "Enter the test description.\n\n"
        "You can write what this test is about.\n\n"
        "Example:\n"
        "JEE Main level Physics, Chemistry and Mathematics mock test."
    )

    return CREATE_TEST_DESCRIPTION


# ============================================================
# CREATE TEST - DESCRIPTION
# ============================================================

async def receive_test_description(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return CREATE_TEST_DESCRIPTION

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Description cannot be empty.\n\n"
            "Please enter the test description."
        )
        return CREATE_TEST_DESCRIPTION

    if len(text) > 4000:
        await update.message.reply_text(
            "❌ Description is too long.\n\n"
            "Please keep it under 4000 characters."
        )
        return CREATE_TEST_DESCRIPTION

    context.user_data["create_test_description"] = text

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return ConversationHandler.END

    admin = await get_admin_by_telegram_id(telegram_user_id)

    if not admin:
        await update.message.reply_text(
            "⛔ Admin account not found."
        )
        return ConversationHandler.END

    # --------------------------------------------------------
    # CREATE DRAFT TEST
    # --------------------------------------------------------

    try:

        response = (
            supabase.table("tests")
            .insert(
                {
                    "name": context.user_data["create_test_name"],
                    "description": text,
                    "status": "DRAFT",
                    "created_by": admin["id"],
                }
            )
            .execute()
        )

        if not response.data:
            raise RuntimeError("Supabase did not return created test.")

        test = response.data[0]

        context.user_data["create_test_id"] = test["id"]
        context.user_data["question_count"] = 0
        context.user_data["total_marks"] = 0.0

    except Exception:
        logger.exception("Failed to create draft test")

        await update.message.reply_text(
            "❌ Could not create the test.\n\n"
            "Please try again later."
        )

        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Test draft created.\n\n"
        "Now we will add questions.\n\n"
        "Question 1\n\n"
        "Send the question text.",
        reply_markup=CANCEL_KEYBOARD,
    )

    return QUESTION_TEXT


# ============================================================
# QUESTION - TEXT
# ============================================================

async def receive_question_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return QUESTION_TEXT

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Question text cannot be empty.\n\n"
            "Please send the question text."
        )
        return QUESTION_TEXT

    if len(text) > 8000:
        await update.message.reply_text(
            "❌ Question is too long.\n\n"
            "Please keep it under 8000 characters."
        )
        return QUESTION_TEXT

    context.user_data["current_question_text"] = text

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔘 MCQ",
                    callback_data="qtype_mcq",
                ),
                InlineKeyboardButton(
                    "☑️ Multiple Correct",
                    callback_data="qtype_multi",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔢 Numerical",
                    callback_data="qtype_numeric",
                )
            ],
        ]
    )

    await update.message.reply_text(
        "Select the question type:",
        reply_markup=keyboard,
    )

    return QUESTION_TYPE


# ============================================================
# QUESTION - TYPE
# ============================================================

async def receive_question_type(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    if not query:
        return QUESTION_TYPE

    await query.answer()

    data = query.data

    if data == "qtype_mcq":
        question_type = "MCQ"

    elif data == "qtype_multi":
        question_type = "MULTIPLE_CORRECT"

    elif data == "qtype_numeric":
        question_type = "NUMERICAL"

    else:
        return QUESTION_TYPE

    context.user_data["current_question_type"] = question_type

    if question_type in ("MCQ", "MULTIPLE_CORRECT"):

        await query.message.reply_text(
            "Enter option A:",
            reply_markup=CANCEL_KEYBOARD,
        )

        return OPTION_A

    await query.message.reply_text(
        "Enter the marks for this question.\n\n"
        "Example: 4"
    )

    return QUESTION_MARKS


# ============================================================
# QUESTION - OPTION A
# ============================================================

async def receive_option_a(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_A

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Option A cannot be empty.\n\n"
            "Please enter option A."
        )
        return OPTION_A

    if len(text) > 2000:
        await update.message.reply_text(
            "❌ Option A is too long."
        )
        return OPTION_A

    context.user_data["current_option_a"] = text

    await update.message.reply_text(
        "Enter option B:"
    )

    return OPTION_B


# ============================================================
# QUESTION - OPTION B
# ============================================================

async def receive_option_b(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_B

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Option B cannot be empty.\n\n"
            "Please enter option B."
        )
        return OPTION_B

    if len(text) > 2000:
        await update.message.reply_text(
            "❌ Option B is too long."
        )
        return OPTION_B

    context.user_data["current_option_b"] = text

    await update.message.reply_text(
        "Enter option C:"
    )

    return OPTION_C


# ============================================================
# QUESTION - OPTION C
# ============================================================

async def receive_option_c(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_C

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Option C cannot be empty.\n\n"
            "Please enter option C."
        )
        return OPTION_C

    if len(text) > 2000:
        await update.message.reply_text(
            "❌ Option C is too long."
        )
        return OPTION_C

    context.user_data["current_option_c"] = text

    await update.message.reply_text(
        "Enter option D:"
    )

    return OPTION_D


# ============================================================
# QUESTION - OPTION D
# ============================================================

async def receive_option_d(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_D

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    if not text:
        await update.message.reply_text(
            "❌ Option D cannot be empty.\n\n"
            "Please enter option D."
        )
        return OPTION_D

    if len(text) > 2000:
        await update.message.reply_text(
            "❌ Option D is too long."
        )
        return OPTION_D

    context.user_data["current_option_d"] = text

    await update.message.reply_text(
        "Enter the marks for this question.\n\n"
        "Example: 4"
    )

    return QUESTION_MARKS


# ============================================================
# QUESTION - MARKS
# ============================================================

async def receive_question_marks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return QUESTION_MARKS

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    try:
        marks = float(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid marks.\n\n"
            "Please enter a number.\n"
            "Example: 4"
        )
        return QUESTION_MARKS

    if marks <= 0:
        await update.message.reply_text(
            "❌ Marks must be greater than 0."
        )
        return QUESTION_MARKS

    if marks > 100000:
        await update.message.reply_text(
            "❌ Marks value is too large."
        )
        return QUESTION_MARKS

    context.user_data["current_question_marks"] = marks

    await update.message.reply_text(
        "Enter negative marks.\n\n"
        "If there is no negative marking, enter 0.\n\n"
        "Example:\n"
        "1"
    )

    return QUESTION_NEGATIVE_MARKS


# ============================================================
# QUESTION - NEGATIVE MARKS
# ============================================================

async def receive_question_negative_marks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return QUESTION_NEGATIVE_MARKS

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(update, context)

    try:
        negative_marks = float(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid negative marks.\n\n"
            "Please enter a number.\n\n"
            "Use 0 if there is no negative marking."
        )
        return QUESTION_NEGATIVE_MARKS

    if negative_marks < 0:
        await update.message.reply_text(
            "❌ Negative marks cannot be below 0.\n\n"
            "Example: 1"
        )
        return QUESTION_NEGATIVE_MARKS

    if negative_marks > 100000:
        await update.message.reply_text(
            "❌ Negative marks value is too large."
        )
        return QUESTION_NEGATIVE_MARKS

    context.user_data["current_question_negative_marks"] = negative_marks

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📷 Add Photo",
                    callback_data="photo_yes",
                ),
                InlineKeyboardButton(
                    "➡️ No Photo",
                    callback_data="photo_no",
                ),
            ]
        ]
    )

    await update.message.reply_text(
        "Does this question have a photo?",
        reply_markup=keyboard,
    )

    return QUESTION_PHOTO_CHOICE


# ============================================================
# QUESTION - PHOTO CHOICE
# ============================================================

async def receive_photo_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    if not query:
        return QUESTION_PHOTO_CHOICE

    await query.answer()

    if query.data == "photo_yes":

        await query.message.reply_text(
            "📷 Please send the question photo now.\n\n"
            "Only send the photo.\n"
            "The photo will be saved using Telegram's file ID."
        )

        return QUESTION_PHOTO

    if query.data == "photo_no":

        context.user_data["current_question_photo_file_id"] = None

        return await save_current_question_after_photo(
            query.message,
            context,
        )

    return QUESTION_PHOTO_CHOICE


# ============================================================
# QUESTION - PHOTO
# ============================================================

async def receive_question_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message:
        return QUESTION_PHOTO

    if not update.message.photo:
        await update.message.reply_text(
            "❌ Please send a photo.\n\n"
            "If you want to cancel the test creation, press ❌ Cancel."
        )
        return QUESTION_PHOTO

    # Highest resolution Telegram photo.
    photo = update.message.photo[-1]

    context.user_data["current_question_photo_file_id"] = photo.file_id

    await update.message.reply_text(
        "✅ Photo received successfully."
    )

    return await save_current_question_after_photo(
        update.message,
        context,
    )


# ============================================================
# SAVE CURRENT QUESTION
# ============================================================

async def save_current_question_after_photo(
    message,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    test_id = context.user_data.get("create_test_id")

    if not test_id:
        await message.reply_text(
            "❌ Test draft was not found.\n\n"
            "Please start Create Test again."
        )
        return ConversationHandler.END

    question_text = context.user_data.get(
        "current_question_text"
    )

    question_type = context.user_data.get(
        "current_question_type"
    )

    marks = context.user_data.get(
        "current_question_marks"
    )

    negative_marks = context.user_data.get(
        "current_question_negative_marks"
    )

    photo_file_id = context.user_data.get(
        "current_question_photo_file_id"
    )

    if not question_text or not question_type:
        await message.reply_text(
            "❌ Question information is incomplete."
        )
        return ConversationHandler.END

    # --------------------------------------------------------
    # DETERMINE QUESTION NUMBER
    # --------------------------------------------------------

    try:

        existing_questions = (
            supabase.table("questions")
            .select("question_number")
            .eq("test_id", test_id)
            .order(
                "question_number",
                desc=True,
            )
            .limit(1)
            .execute()
        )

        if existing_questions.data:
            question_number = (
                existing_questions.data[0]["question_number"] + 1
            )
        else:
            question_number = 1

    except Exception:
        logger.exception(
            "Failed to determine question number"
        )

        await message.reply_text(
            "❌ Could not determine the question number."
        )

        return ConversationHandler.END

    # --------------------------------------------------------
    # INSERT QUESTION
    # --------------------------------------------------------

    question_id = None

    try:

        question_response = (
            supabase.table("questions")
            .insert(
                {
                    "test_id": test_id,
                    "question_number": question_number,
                    "question_text": question_text,
                    "question_type": question_type,
                    "marks": marks,
                    "negative_marks": negative_marks,
                    "photo_file_id": photo_file_id,
                }
            )
            .execute()
        )

        if not question_response.data:
            raise RuntimeError(
                "Supabase did not return created question."
            )

        question_id = question_response.data[0]["id"]

        # ----------------------------------------------------
        # INSERT OPTIONS
        # ----------------------------------------------------

        if question_type in (
            "MCQ",
            "MULTIPLE_CORRECT",
        ):

            options = [
                {
                    "question_id": question_id,
                    "option_key": "A",
                    "option_text": context.user_data[
                        "current_option_a"
                    ],
                    "display_order": 1,
                },
                {
                    "question_id": question_id,
                    "option_key": "B",
                    "option_text": context.user_data[
                        "current_option_b"
                    ],
                    "display_order": 2,
                },
                {
                    "question_id": question_id,
                    "option_key": "C",
                    "option_text": context.user_data[
                        "current_option_c"
                    ],
                    "display_order": 3,
                },
                {
                    "question_id": question_id,
                    "option_key": "D",
                    "option_text": context.user_data[
                        "current_option_d"
                    ],
                    "display_order": 4,
                },
            ]

            supabase.table("question_options").insert(
                options
            ).execute()

    except Exception:
        logger.exception(
            "Failed to save question"
        )

        # If question was inserted but options failed,
        # delete the question. Options cascade automatically.
        if question_id:
            try:
                supabase.table("questions").delete().eq(
                    "id",
                    question_id,
                ).execute()
            except Exception:
                logger.exception(
                    "Failed to clean up incomplete question"
                )

        await message.reply_text(
            "❌ Could not save this question.\n\n"
            "Nothing was partially kept from this question."
        )

        return ConversationHandler.END

    # --------------------------------------------------------
    # UPDATE LOCAL SUMMARY
    # --------------------------------------------------------

    question_count = int(
        context.user_data.get(
            "question_count",
            0,
        )
    )

    total_marks = float(
        context.user_data.get(
            "total_marks",
            0.0,
        )
    )

    question_count += 1
    total_marks += float(marks)

    context.user_data["question_count"] = question_count
    context.user_data["total_marks"] = total_marks

    # --------------------------------------------------------
    # CLEAR CURRENT QUESTION
    # --------------------------------------------------------

    current_question_keys = [
        "current_question_text",
        "current_question_type",
        "current_option_a",
        "current_option_b",
        "current_option_c",
        "current_option_d",
        "current_question_marks",
        "current_question_negative_marks",
        "current_question_photo_file_id",
    ]

    for key in current_question_keys:
        context.user_data.pop(key, None)

    # --------------------------------------------------------
    # ASK NEXT ACTION
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add Another Question",
                    callback_data="question_add",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Finish Test",
                    callback_data="question_finish",
                )
            ],
        ]
    )

    await message.reply_text(
        f"✅ Question {question_number} saved.\n\n"
        f"📊 Questions: {question_count}\n"
        f"🧮 Total Marks: {format_number(total_marks)}\n\n"
        f"What do you want to do next?",
        reply_markup=keyboard,
    )

    return AFTER_QUESTION


# ============================================================
# AFTER QUESTION
# ============================================================

async def after_question_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    if not query:
        return AFTER_QUESTION

    await query.answer()

    if query.data == "question_add":

        question_count = int(
            context.user_data.get(
                "question_count",
                0,
            )
        )

        next_number = question_count + 1

        await query.message.reply_text(
            f"Question {next_number}\n\n"
            f"Send the question text.",
            reply_markup=CANCEL_KEYBOARD,
        )

        return QUESTION_TEXT

    if query.data == "question_finish":

        return await finish_create_test(
            query.message,
            context,
        )

    return AFTER_QUESTION


# ============================================================
# FORMAT NUMBERS
# ============================================================

def format_number(value: float) -> str:

    if float(value).is_integer():
        return str(int(value))

    return f"{value:g}"


# ============================================================
# FINISH CREATE TEST
# ============================================================

async def finish_create_test(
    message,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    test_id = context.user_data.get("create_test_id")

    if not test_id:
        await message.reply_text(
            "❌ Test draft was not found."
        )
        return ConversationHandler.END

    question_count = int(
        context.user_data.get(
            "question_count",
            0,
        )
    )

    total_marks = float(
        context.user_data.get(
            "total_marks",
            0.0,
        )
    )

    if question_count <= 0:

        await message.reply_text(
            "❌ You cannot finish a test without adding at least one question.\n\n"
            "Add a question first."
        )

        return AFTER_QUESTION

    try:

        # Recalculate statistics directly from DB
        # so the summary is based on actual saved questions.
        stats_response = (
            supabase.table("questions")
            .select(
                "id,marks",
                count="exact",
            )
            .eq(
                "test_id",
                test_id,
            )
            .execute()
        )

        actual_questions = stats_response.data or []

        actual_count = len(actual_questions)

        actual_total_marks = sum(
            float(row.get("marks") or 0)
            for row in actual_questions
        )

        # ----------------------------------------------------
        # Keep draft status.
        #
        # Publishing will be a separate step later.
        # ----------------------------------------------------

        await message.reply_text(
            "🎉 Test creation completed!\n\n"
            f"📝 Test: "
            f"{context.user_data.get('create_test_name', 'Unnamed')}\n"
            f"❓ Questions: {actual_count}\n"
            f"🧮 Total Marks: "
            f"{format_number(actual_total_marks)}\n"
            f"📌 Status: DRAFT\n\n"
            "The test is saved as a draft.\n"
            "Publishing, scheduling and duration will be configured from Manage Tests."
        )

    except Exception:
        logger.exception(
            "Failed to finalize test summary"
        )

        await message.reply_text(
            "⚠️ Test was created, but I could not load the final summary.\n\n"
            "The draft is still saved."
        )

    clear_create_test_data(context)

    # Return to dashboard.
    telegram_user_id = message.from_user.id if message.from_user else None

    if telegram_user_id:

        admin = await get_admin_by_telegram_id(
            telegram_user_id
        )

        if admin:

            if admin.get("role") == "OWNER":
                keyboard = OWNER_DASHBOARD
            else:
                keyboard = ADMIN_DASHBOARD

            await message.reply_text(
                "🏟️ Admin Dashboard",
                reply_markup=keyboard,
            )

    return ConversationHandler.END


# ============================================================
# CREATE TEST ERROR/CANCEL COMMAND
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await cancel_create_test(
        update,
        context,
    )


# ============================================================
# DASHBOARD HANDLERS
# ============================================================

async def dashboard_create_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await begin_create_test(
        update,
        context,
    )


async def dashboard_manage_tests(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    admin = await get_logged_in_admin(update)

    if not admin:
        await update.message.reply_text(
            "⛔ Please login first."
        )
        return

    await update.message.reply_text(
        "📋 Manage Tests\n\n"
        "Test management module will be added here next."
    )


async def dashboard_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    admin = await get_logged_in_admin(update)

    if not admin:
        await update.message.reply_text(
            "⛔ Please login first."
        )
        return

    await update.message.reply_text(
        "🏆 Results\n\n"
        "Results module will be added here next."
    )


async def dashboard_manage_admins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    admin = await get_logged_in_admin(update)

    if not admin:
        await update.message.reply_text(
            "⛔ Please login first."
        )
        return

    if admin.get("role") != "OWNER":
        await update.message.reply_text(
            "⛔ Only the Owner can manage admins."
        )
        return

    await update.message.reply_text(
        "👥 Manage Admins\n\n"
        "Admin management module will be added here next."
    )


# ============================================================
# GENERIC DASHBOARD MESSAGE HANDLER
# ============================================================

async def dashboard_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_private_chat(update):
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    # --------------------------------------------------------
    # PASSWORD HANDLING
    # --------------------------------------------------------

    if telegram_user_id in awaiting_password:

        await receive_password(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # AUTH CHECK
    # --------------------------------------------------------

    if telegram_user_id not in authenticated_users:
        return

    # --------------------------------------------------------
    # DASHBOARD BUTTONS
    # --------------------------------------------------------

    if text == "➕ Create Test":
        return

    if text == "📋 Manage Tests":

        await dashboard_manage_tests(
            update,
            context,
        )
        return

    if text == "🏆 Results":

        await dashboard_results(
            update,
            context,
        )
        return

    if text == "👥 Manage Admins":

        await dashboard_manage_admins(
            update,
            context,
        )
        return

    if text == "🚪 Logout":

        await logout(
            update,
            context,
        )
        return

    await update.message.reply_text(
        "Please use the buttons in the dashboard."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.exception(
        "Unhandled exception while processing update",
        exc_info=context.error,
    )

    try:

        if isinstance(update, Update):

            if update.effective_message:

                await update.effective_message.reply_text(
                    "⚠️ Something went wrong while processing that request.\n\n"
                    "Please try again."
                )

    except Exception:
        logger.exception(
            "Failed to send error message"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # CREATE TEST CONVERSATION
    #
    # IMPORTANT:
    # ConversationHandler is added before the generic
    # dashboard message handler.
    # --------------------------------------------------------

    create_test_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"^➕ Create Test$"),
                begin_create_test,
            )
        ],
        states={

            CREATE_TEST_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_test_name,
                )
            ],

            CREATE_TEST_DESCRIPTION: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_test_description,
                )
            ],

            QUESTION_TEXT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_question_text,
                )
            ],

            QUESTION_TYPE: [
                CallbackQueryHandler(
                    receive_question_type,
                    pattern=r"^qtype_(mcq|multi|numeric)$",
                )
            ],

            OPTION_A: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_option_a,
                )
            ],

            OPTION_B: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_option_b,
                )
            ],

            OPTION_C: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_option_c,
                )
            ],

            OPTION_D: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_option_d,
                )
            ],

            QUESTION_MARKS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_question_marks,
                )
            ],

            QUESTION_NEGATIVE_MARKS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_question_negative_marks,
                )
            ],

            QUESTION_PHOTO_CHOICE: [
                CallbackQueryHandler(
                    receive_photo_choice,
                    pattern=r"^photo_(yes|no)$",
                )
            ],

            QUESTION_PHOTO: [
                MessageHandler(
                    filters.PHOTO,
                    receive_question_photo,
                )
            ],

            AFTER_QUESTION: [
                CallbackQueryHandler(
                    after_question_action,
                    pattern=r"^question_(add|finish)$",
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_command,
            ),
            MessageHandler(
                filters.Regex(r"^❌ Cancel$"),
                cancel_create_test,
            ),
        ],

        allow_reentry=False,
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    # --------------------------------------------------------
    # CREATE TEST CONVERSATION
    # --------------------------------------------------------

    application.add_handler(
        create_test_conversation
    )

    # --------------------------------------------------------
    # LOGOUT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Regex(r"^🚪 Logout$"),
            logout,
        )
    )

    # --------------------------------------------------------
    # GENERIC DASHBOARD HANDLER
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            dashboard_message_handler,
        )
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # START BOT
    # --------------------------------------------------------

    logger.info("PrepArena Admin Bot starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
