import logging
import os
from typing import Optional

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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing")

if not OWNER_PASSWORD:
    raise RuntimeError("OWNER_PASSWORD is missing")

if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is missing")


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
# AUTHENTICATION
# ============================================================

# Stores:
# telegram_user_id -> "OWNER" or "ADMIN"
authenticated_users: dict[int, str] = {}

# Users currently being asked for a password.
# Value is either "OWNER" or "ADMIN".
awaiting_password: dict[int, str] = {}


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

ACCESS_TYPE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "👑 Owner",
                callback_data="login_owner",
            ),
            InlineKeyboardButton(
                "🛡️ Admin",
                callback_data="login_admin",
            ),
        ]
    ]
)


OWNER_DASHBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🏆 Results"],
        ["🚪 Logout"],
    ],
    resize_keyboard=True,
)

ADMIN_DASHBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🏆 Results"],
        ["🚪 Logout"],
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
    return (
        update.effective_chat is not None
        and update.effective_chat.type == "private"
    )


def get_telegram_user_id(update: Update) -> Optional[int]:
    if update.effective_user is None:
        return None

    return update.effective_user.id


def get_authenticated_role(update: Update) -> Optional[str]:
    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return None

    return authenticated_users.get(telegram_user_id)


def is_authenticated(update: Update) -> bool:
    return get_authenticated_role(update) is not None


def is_owner(update: Update) -> bool:
    return get_authenticated_role(update) == "OWNER"


def is_admin(update: Update) -> bool:
    return get_authenticated_role(update) == "ADMIN"


async def get_logged_in_database_user(
    update: Update,
) -> Optional[dict]:
    """
    Returns the database admin record for the current Telegram user.

    Telegram ID is NOT used for password authentication.
    It is only used here to identify the current session's database
    record when an owner/admin performs actions.
    """

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return None

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
        logger.exception(
            "Failed to get current database admin record"
        )

    return None


async def update_admin_profile(update: Update) -> None:
    """
    Keep a lightweight database actor record for the current session.

    Login is still controlled only by the Railway OWNER_PASSWORD or
    ADMIN_PASSWORD. The admins table is used internally because tests.created_by
    requires a database record; it is not an admin-management system.
    """

    telegram_user_id = get_telegram_user_id(update)
    role = get_authenticated_role(update)

    if telegram_user_id is None or role not in ("OWNER", "ADMIN"):
        return

    user = update.effective_user
    display_name = user.full_name if user else "PrepArena User"
    username = user.username if user else None

    try:
        response = (
            supabase.table("admins")
            .select("id,role,is_active")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )

        if response.data:
            existing = response.data[0]
            awaitable = (
                supabase.table("admins")
                .update(
                    {
                        "display_name": display_name,
                        "username": username,
                        "is_active": True,
                    }
                )
                .eq("id", existing["id"])
                .execute()
            )
            _ = awaitable
            return

        if role == "OWNER":
            # Owner should normally already exist from the bootstrap SQL.
            # Do not silently create a second owner record.
            logger.warning(
                "Owner login succeeded but no owner database record exists for %s",
                telegram_user_id,
            )
            return

        supabase.table("admins").insert(
            {
                "telegram_user_id": telegram_user_id,
                "display_name": display_name,
                "username": username,
                "role": "ADMIN",
                "password_hash": None,
                "is_active": True,
            }
        ).execute()

    except Exception:
        logger.exception("Failed to create/update database actor record")


async def send_dashboard(
    update: Update,
) -> None:

    role = get_authenticated_role(update)

    if role == "OWNER":

        await update.effective_message.reply_text(
            "🏟️ PrepArena Owner Panel\n\n"
            "👑 Access: Owner\n\n"
            "Choose an option below.",
            reply_markup=OWNER_DASHBOARD,
        )

    elif role == "ADMIN":

        await update.effective_message.reply_text(
            "🏟️ PrepArena Admin Panel\n\n"
            "🛡️ Access: Admin\n\n"
            "Choose an option below.",
            reply_markup=ADMIN_DASHBOARD,
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    # If already logged in, don't ask for password again.
    if telegram_user_id in authenticated_users:

        await send_dashboard(update)

        return

    # Clear any previous pending login choice.
    awaiting_password.pop(
        telegram_user_id,
        None,
    )

    await update.message.reply_text(
        "🏟️ Welcome to PrepArena Admin Bot.\n\n"
        "Choose your access type:",
        reply_markup=ACCESS_TYPE_KEYBOARD,
    )


# ============================================================
# ACCESS TYPE SELECTION
# ============================================================

async def select_login_type(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
        return

    await query.answer()

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    if telegram_user_id in authenticated_users:

        await query.message.reply_text(
            "You are already logged in."
        )

        await send_dashboard(update)

        return

    if query.data == "login_owner":

        awaiting_password[
            telegram_user_id
        ] = "OWNER"

        await query.message.reply_text(
            "👑 Owner Login\n\n"
            "🔐 Enter the Owner Password:",
        )

        return

    if query.data == "login_admin":

        awaiting_password[
            telegram_user_id
        ] = "ADMIN"

        await query.message.reply_text(
            "🛡️ Admin Login\n\n"
            "🔐 Enter the Admin Password:",
        )

        return


# ============================================================
# PASSWORD LOGIN
# ============================================================

async def receive_password(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    login_type = awaiting_password.get(
        telegram_user_id
    )

    if not login_type:
        return

    if not update.message or not update.message.text:
        return

    password = update.message.text.strip()

    # --------------------------------------------------------
    # OWNER PASSWORD
    # --------------------------------------------------------

    if login_type == "OWNER":

        if password == OWNER_PASSWORD:

            authenticated_users[
                telegram_user_id
            ] = "OWNER"

            awaiting_password.pop(
                telegram_user_id,
                None,
            )

            await update_admin_profile(update)

            await update.message.reply_text(
                "✅ Owner login successful."
            )

            await send_dashboard(update)

            return

        await update.message.reply_text(
            "❌ Incorrect Owner Password.\n\n"
            "Please try again."
        )

        return

    # --------------------------------------------------------
    # ADMIN PASSWORD
    # --------------------------------------------------------

    if login_type == "ADMIN":

        if password == ADMIN_PASSWORD:

            authenticated_users[
                telegram_user_id
            ] = "ADMIN"

            awaiting_password.pop(
                telegram_user_id,
                None,
            )

            await update_admin_profile(update)

            await update.message.reply_text(
                "✅ Admin login successful."
            )

            await send_dashboard(update)

            return

        await update.message.reply_text(
            "❌ Incorrect Admin Password.\n\n"
            "Please try again."
        )

        return


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

    authenticated_users.pop(
        telegram_user_id,
        None,
    )

    awaiting_password.pop(
        telegram_user_id,
        None,
    )

    context.user_data.clear()

    await update.message.reply_text(
        "🚪 You have been logged out.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# CREATE TEST HELPERS
# ============================================================

async def delete_draft_test(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    test_id = context.user_data.get(
        "create_test_id"
    )

    if not test_id:
        return

    try:

        (
            supabase.table("tests")
            .delete()
            .eq(
                "id",
                test_id,
            )
            .execute()
        )

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
        context.user_data.pop(
            key,
            None,
        )


async def cancel_create_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    await delete_draft_test(
        context
    )

    clear_create_test_data(
        context
    )

    await update.effective_message.reply_text(
        "❌ Create Test cancelled.\n\n"
        "Any unfinished draft created during this process has been removed."
    )

    await send_dashboard(
        update
    )

    return ConversationHandler.END


# ============================================================
# CREATE TEST - START
# ============================================================

async def begin_create_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    role = get_authenticated_role(update)

    if role not in ("OWNER", "ADMIN"):

        await update.message.reply_text(
            "⛔ Please login first."
        )

        return ConversationHandler.END

    clear_create_test_data(
        context
    )

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
        return await cancel_create_test(
            update,
            context,
        )

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

    context.user_data[
        "create_test_name"
    ] = text

    await update.message.reply_text(
        "Step 2/2\n\n"
        "Enter the test description.\n\n"
        "Example:\n"
        "JEE Main level mock test."
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
        return await cancel_create_test(
            update,
            context,
        )

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

    context.user_data[
        "create_test_description"
    ] = text

    telegram_user_id = get_telegram_user_id(
        update
    )

    if telegram_user_id is None:
        return ConversationHandler.END

    # --------------------------------------------------------
    # Find database creator record if available.
    #
    # Existing database schema has created_by as FK.
    # The current owner record was already bootstrapped.
    # For other admin users, their Telegram account can be
    # represented in the admins table if needed.
    # --------------------------------------------------------

    database_admin = await get_logged_in_database_user(
        update
    )

    if not database_admin:

        await update.message.reply_text(
            "❌ Your login is valid, but your database admin "
            "record could not be found.\n\n"
            "Please make sure the current admin/owner record "
            "exists in Supabase before creating tests."
        )

        return ConversationHandler.END

    try:

        response = (
            supabase.table("tests")
            .insert(
                {
                    "name": context.user_data[
                        "create_test_name"
                    ],
                    "description": text,
                    "status": "DRAFT",
                    "created_by": database_admin["id"],
                }
            )
            .execute()
        )

        if not response.data:

            raise RuntimeError(
                "Supabase did not return created test."
            )

        test = response.data[0]

        context.user_data[
            "create_test_id"
        ] = test["id"]

        context.user_data[
            "question_count"
        ] = 0

        context.user_data[
            "total_marks"
        ] = 0.0

    except Exception:

        logger.exception(
            "Failed to create draft test"
        )

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
        return await cancel_create_test(
            update,
            context,
        )

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

    context.user_data[
        "current_question_text"
    ] = text

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

    if query.data == "qtype_mcq":
        question_type = "MCQ"

    elif query.data == "qtype_multi":
        question_type = "MULTIPLE_CORRECT"

    elif query.data == "qtype_numeric":
        question_type = "NUMERICAL"

    else:
        return QUESTION_TYPE

    context.user_data[
        "current_question_type"
    ] = question_type

    if question_type in (
        "MCQ",
        "MULTIPLE_CORRECT",
    ):

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
# OPTION A
# ============================================================

async def receive_option_a(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_A

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(
            update,
            context,
        )

    if not text:

        await update.message.reply_text(
            "❌ Option A cannot be empty."
        )

        return OPTION_A

    if len(text) > 2000:

        await update.message.reply_text(
            "❌ Option A is too long."
        )

        return OPTION_A

    context.user_data[
        "current_option_a"
    ] = text

    await update.message.reply_text(
        "Enter option B:"
    )

    return OPTION_B


# ============================================================
# OPTION B
# ============================================================

async def receive_option_b(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_B

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(
            update,
            context,
        )

    if not text:

        await update.message.reply_text(
            "❌ Option B cannot be empty."
        )

        return OPTION_B

    if len(text) > 2000:

        await update.message.reply_text(
            "❌ Option B is too long."
        )

        return OPTION_B

    context.user_data[
        "current_option_b"
    ] = text

    await update.message.reply_text(
        "Enter option C:"
    )

    return OPTION_C


# ============================================================
# OPTION C
# ============================================================

async def receive_option_c(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_C

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(
            update,
            context,
        )

    if not text:

        await update.message.reply_text(
            "❌ Option C cannot be empty."
        )

        return OPTION_C

    if len(text) > 2000:

        await update.message.reply_text(
            "❌ Option C is too long."
        )

        return OPTION_C

    context.user_data[
        "current_option_c"
    ] = text

    await update.message.reply_text(
        "Enter option D:"
    )

    return OPTION_D


# ============================================================
# OPTION D
# ============================================================

async def receive_option_d(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if not update.message or not update.message.text:
        return OPTION_D

    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await cancel_create_test(
            update,
            context,
        )

    if not text:

        await update.message.reply_text(
            "❌ Option D cannot be empty."
        )

        return OPTION_D

    if len(text) > 2000:

        await update.message.reply_text(
            "❌ Option D is too long."
        )

        return OPTION_D

    context.user_data[
        "current_option_d"
    ] = text

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
        return await cancel_create_test(
            update,
            context,
        )

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

    context.user_data[
        "current_question_marks"
    ] = marks

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
        return await cancel_create_test(
            update,
            context,
        )

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
            "❌ Negative marks cannot be below 0."
        )

        return QUESTION_NEGATIVE_MARKS

    if negative_marks > 100000:

        await update.message.reply_text(
            "❌ Negative marks value is too large."
        )

        return QUESTION_NEGATIVE_MARKS

    context.user_data[
        "current_question_negative_marks"
    ] = negative_marks

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

        context.user_data[
            "current_question_photo_file_id"
        ] = None

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

    photo = update.message.photo[-1]

    context.user_data[
        "current_question_photo_file_id"
    ] = photo.file_id

    await update.message.reply_text(
        "✅ Photo received successfully."
    )

    return await save_current_question_after_photo(
        update.message,
        context,
    )


# ============================================================
# SAVE QUESTION
# ============================================================

async def save_current_question_after_photo(
    message,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    test_id = context.user_data.get(
        "create_test_id"
    )

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
    # QUESTION NUMBER
    # --------------------------------------------------------

    try:

        existing_questions = (
            supabase.table("questions")
            .select("question_number")
            .eq(
                "test_id",
                test_id,
            )
            .order(
                "question_number",
                desc=True,
            )
            .limit(1)
            .execute()
        )

        if existing_questions.data:

            question_number = (
                existing_questions.data[0][
                    "question_number"
                ]
                + 1
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

    question_id = None

    try:

        # ----------------------------------------------------
        # INSERT QUESTION
        # ----------------------------------------------------

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

            (
                supabase.table("question_options")
                .insert(options)
                .execute()
            )

    except Exception:

        logger.exception(
            "Failed to save question"
        )

        if question_id:

            try:

                (
                    supabase.table("questions")
                    .delete()
                    .eq(
                        "id",
                        question_id,
                    )
                    .execute()
                )

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
    # UPDATE SUMMARY
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

    context.user_data[
        "question_count"
    ] = question_count

    context.user_data[
        "total_marks"
    ] = total_marks

    # --------------------------------------------------------
    # CLEAR CURRENT QUESTION DATA
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

        context.user_data.pop(
            key,
            None,
        )

    # --------------------------------------------------------
    # NEXT ACTION
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
# FORMAT NUMBER
# ============================================================

def format_number(
    value: float,
) -> str:

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

    test_id = context.user_data.get(
        "create_test_id"
    )

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

    if question_count <= 0:

        await message.reply_text(
            "❌ You cannot finish a test without "
            "adding at least one question.\n\n"
            "Add a question first."
        )

        return AFTER_QUESTION

    try:

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

        actual_questions = (
            stats_response.data or []
        )

        actual_count = len(
            actual_questions
        )

        actual_total_marks = sum(
            float(
                row.get("marks") or 0
            )
            for row in actual_questions
        )

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
            "⚠️ Test was created, but I could not load "
            "the final summary.\n\n"
            "The draft is still saved."
        )

    clear_create_test_data(
        context
    )

    # Return to dashboard.
    telegram_user_id = (
        message.from_user.id
        if message.from_user
        else None
    )

    if telegram_user_id:

        role = authenticated_users.get(
            telegram_user_id
        )

        if role == "OWNER":

            await message.reply_text(
                "🏟️ Owner Dashboard",
                reply_markup=OWNER_DASHBOARD,
            )

        elif role == "ADMIN":

            await message.reply_text(
                "🏟️ Admin Dashboard",
                reply_markup=ADMIN_DASHBOARD,
            )

    return ConversationHandler.END


# ============================================================
# CREATE TEST CANCEL COMMAND
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    if context.user_data.get(
        "create_test_id"
    ):

        return await cancel_create_test(
            update,
            context,
        )

    return ConversationHandler.END


# ============================================================
# DASHBOARD - MANAGE TESTS
# ============================================================

async def dashboard_manage_tests(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    role = get_authenticated_role(update)

    if role not in ("OWNER", "ADMIN"):
        await update.effective_message.reply_text(
            "⛔ Please login first."
        )
        return

    try:
        response = (
            supabase.table("tests")
            .select(
                "id,name,status,scheduled_at,duration_seconds,created_at"
            )
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )

        tests = response.data or []

    except Exception:
        logger.exception("Failed to load tests")
        await update.effective_message.reply_text(
            "❌ Could not load tests right now.\n\nPlease try again."
        )
        return

    if not tests:
        await update.effective_message.reply_text(
            "📋 Manage Tests\n\n"
            "No tests have been created yet.\n\n"
            "Use /create to create your first test.",
            reply_markup=OWNER_DASHBOARD if role == "OWNER" else ADMIN_DASHBOARD,
        )
        return

    lines = ["📋 Manage Tests", ""]

    for index, test in enumerate(tests, start=1):
        name = test.get("name") or "Unnamed Test"
        status = test.get("status") or "UNKNOWN"
        scheduled_at = test.get("scheduled_at")
        duration_seconds = test.get("duration_seconds")

        details = [f"{index}. {name}", f"   Status: {status}"]
        if scheduled_at:
            details.append(f"   Scheduled: {scheduled_at}")
        if duration_seconds:
            minutes = int(duration_seconds) // 60
            details.append(f"   Duration: {minutes} min")
        lines.append("\n".join(details))
        lines.append("")

    lines.append("Use /create to create another test.")

    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=OWNER_DASHBOARD if role == "OWNER" else ADMIN_DASHBOARD,
    )


# ============================================================
# DASHBOARD - RESULTS
# ============================================================

async def dashboard_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    role = get_authenticated_role(update)

    if role not in ("OWNER", "ADMIN"):
        await update.effective_message.reply_text(
            "⛔ Please login first."
        )
        return

    try:
        response = (
            supabase.table("results")
            .select(
                "score,total_marks,correct_count,wrong_count,unattempted_count,percentage,rank,calculated_at,participants(test_id,students(display_name,username),tests(name))"
            )
            .order("calculated_at", desc=True)
            .limit(20)
            .execute()
        )
        results = response.data or []
    except Exception:
        logger.exception("Failed to load results")
        await update.effective_message.reply_text(
            "❌ Could not load results right now.\n\nPlease try again."
        )
        return

    if not results:
        await update.effective_message.reply_text(
            "🏆 Results\n\n"
            "No calculated results are available yet.\n\n"
            "Results will appear here after a test is completed and its answer key is processed.",
            reply_markup=OWNER_DASHBOARD if role == "OWNER" else ADMIN_DASHBOARD,
        )
        return

    lines = ["🏆 Latest Results", ""]

    for index, result in enumerate(results, start=1):
        participant = result.get("participants") or {}
        student = participant.get("students") or {}
        test = participant.get("tests") or {}
        display_name = student.get("display_name") or "Unknown Student"
        test_name = test.get("name") or "Unknown Test"
        rank = result.get("rank")
        rank_text = str(rank) if rank is not None else "Not ranked"

        lines.append(
            f"{index}. {display_name} — {test_name}\n"
            f"   Score: {result.get('score', 0)}/{result.get('total_marks', 0)}\n"
            f"   Correct: {result.get('correct_count', 0)} | Wrong: {result.get('wrong_count', 0)} | Unattempted: {result.get('unattempted_count', 0)}\n"
            f"   Percentage: {result.get('percentage', 0)}% | Rank: {rank_text}"
        )
        lines.append("")

    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=OWNER_DASHBOARD if role == "OWNER" else ADMIN_DASHBOARD,
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

    telegram_user_id = get_telegram_user_id(
        update
    )

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

        await update.message.reply_text(
            "⛔ Please use /start to login."
        )

        return

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

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

    if text == "🚪 Logout":

        await logout(
            update,
            context,
        )

        return

    if text == "➕ Create Test":

        # Normally handled by ConversationHandler.
        # This branch is kept as a safe fallback.
        await begin_create_test(
            update,
            context,
        )

        return

    if text == "❌ Cancel":

        await cancel_create_test(
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

    logger.error(
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
    # START
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # --------------------------------------------------------
    # LOGIN TYPE SELECTION
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            select_login_type,
            pattern=r"^login_(owner|admin)$",
        )
    )

    # --------------------------------------------------------
    # CREATE TEST CONVERSATION
    # --------------------------------------------------------

    create_test_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "create",
                begin_create_test,
            ),
            MessageHandler(
                filters.Regex(
                    r"^➕ Create Test$"
                ),
                begin_create_test,
            ),
        ],

        states={

            CREATE_TEST_NAME: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_test_name,
                )
            ],

            CREATE_TEST_DESCRIPTION: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_test_description,
                )
            ],

            QUESTION_TEXT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
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
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_option_a,
                )
            ],

            OPTION_B: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_option_b,
                )
            ],

            OPTION_C: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_option_c,
                )
            ],

            OPTION_D: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_option_d,
                )
            ],

            QUESTION_MARKS: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_question_marks,
                )
            ],

            QUESTION_NEGATIVE_MARKS: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
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
                filters.Regex(
                    r"^❌ Cancel$"
                ),
                cancel_create_test,
            ),
        ],

        allow_reentry=False,
    )

    application.add_handler(
        create_test_conversation
    )

    # --------------------------------------------------------
    # DASHBOARD COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "tests",
            dashboard_manage_tests,
        )
    )

    application.add_handler(
        CommandHandler(
            "results",
            dashboard_results,
        )
    )

    application.add_handler(
        CommandHandler(
            "logout",
            logout,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    # --------------------------------------------------------
    # MANAGE ADMINS CALLBACKS
    # --------------------------------------------------------

    # --------------------------------------------------------
    # LOGOUT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Regex(
                r"^🚪 Logout$"
            ),
            logout,
        )
    )

    # --------------------------------------------------------
    # GENERAL TEXT HANDLER
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
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
    # START POLLING
    # --------------------------------------------------------

    logger.info(
        "PrepArena Admin Bot starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
