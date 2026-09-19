import logging
import os
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
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
    raise RuntimeError("BOT_TOKEN is not configured.")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not configured.")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured.")

if not OWNER_PASSWORD:
    raise RuntimeError("OWNER_PASSWORD is not configured.")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("preparena-admin")


# ============================================================
# SUPABASE
# ============================================================

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)


# ============================================================
# AUTHENTICATED SESSIONS
# ============================================================

# Telegram user IDs that successfully logged in.
authenticated_users: set[int] = set()

# Users currently being asked for a password.
awaiting_password: set[int] = set()


# ============================================================
# KEYBOARDS
# ============================================================

OWNER_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🔑 Answer Keys", "🏆 Results"],
        ["👥 Manage Admins"],
        ["🚪 Logout"],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Create Test", "📋 Manage Tests"],
        ["🔑 Answer Keys", "🏆 Results"],
        ["🚪 Logout"],
    ],
    resize_keyboard=True,
)


# ============================================================
# HELPERS
# ============================================================

def is_private_chat(update: Update) -> bool:
    """Return True only for private Telegram chats."""
    return bool(
        update.effective_chat
        and update.effective_chat.type == "private"
    )


def get_telegram_user_id(update: Update) -> Optional[int]:
    """Get the Telegram numeric user ID."""
    if not update.effective_user:
        return None

    return update.effective_user.id


def get_admin_by_telegram_id(telegram_user_id: int):
    """Fetch active admin/owner by Telegram ID."""
    response = (
        supabase.table("admins")
        .select(
            "id, telegram_user_id, display_name, username, role, "
            "password_hash, is_active"
        )
        .eq("telegram_user_id", telegram_user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def update_admin_profile(update: Update, admin_id: str) -> None:
    """Keep display name and username up to date."""
    user = update.effective_user

    if not user:
        return

    display_name = user.full_name or "PrepArena Admin"
    username = user.username

    supabase.table("admins").update(
        {
            "display_name": display_name,
            "username": username,
        }
    ).eq("id", admin_id).execute()


def get_logged_in_admin(update: Update):
    """Return logged-in admin record, or None."""
    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return None

    if telegram_user_id not in authenticated_users:
        return None

    try:
        admin = get_admin_by_telegram_id(telegram_user_id)

        if not admin:
            authenticated_users.discard(telegram_user_id)
            return None

        update_admin_profile(update, admin["id"])

        return admin

    except Exception:
        logger.exception("Failed to verify authenticated admin.")
        return None


async def send_dashboard(update: Update, admin) -> None:
    """Show the correct dashboard based on role."""
    if admin["role"] == "OWNER":
        keyboard = OWNER_KEYBOARD
        title = "👑 PrepArena Owner Dashboard"
    else:
        keyboard = ADMIN_KEYBOARD
        title = "🛠️ PrepArena Admin Dashboard"

    await update.message.reply_text(
        f"{title}\n\n"
        f"Welcome, {admin.get('display_name') or 'Admin'}.\n\n"
        "Choose an option below.",
        reply_markup=keyboard,
    )


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    if not is_private_chat(update):
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    try:
        admin = get_admin_by_telegram_id(telegram_user_id)

    except Exception:
        logger.exception("Supabase error during /start.")
        await update.message.reply_text(
            "⚠️ Unable to connect to PrepArena right now. "
            "Please try again later."
        )
        return

    if not admin:
        await update.message.reply_text(
            "❌ Access Denied.\n\n"
            "You are not registered as a PrepArena Owner or Admin."
        )
        return

    if telegram_user_id in authenticated_users:
        await send_dashboard(update, admin)
        return

    awaiting_password.add(telegram_user_id)

    role_text = "Owner" if admin["role"] == "OWNER" else "Admin"

    await update.message.reply_text(
        f"🔐 PrepArena {role_text} Login\n\n"
        "Please enter your password."
    )


# ============================================================
# PASSWORD LOGIN
# ============================================================

async def handle_password(update: Update) -> None:
    """Process a password entered during login."""
    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    if telegram_user_id not in awaiting_password:
        return

    if not update.message or not update.message.text:
        return

    password = update.message.text

    # Remove login state immediately.
    awaiting_password.discard(telegram_user_id)

    try:
        admin = get_admin_by_telegram_id(telegram_user_id)

    except Exception:
        logger.exception("Supabase error during login.")
        await update.message.reply_text(
            "⚠️ Login failed because the database could not be reached."
        )
        return

    if not admin:
        await update.message.reply_text(
            "❌ Access Denied."
        )
        return

    authenticated = False

    # Owner authentication uses Railway OWNER_PASSWORD.
    if admin["role"] == "OWNER":
        authenticated = password == OWNER_PASSWORD

    # Normal Admin authentication uses bcrypt hash in Supabase.
    elif admin["role"] == "ADMIN":
        password_hash = admin.get("password_hash")

        if password_hash:
            try:
                authenticated = bcrypt.checkpw(
                    password.encode("utf-8"),
                    password_hash.encode("utf-8"),
                )
            except Exception:
                logger.exception("Admin password verification failed.")

    if not authenticated:
        await update.message.reply_text(
            "❌ Incorrect password.\n\n"
            "Use /start to try again."
        )
        return

    authenticated_users.add(telegram_user_id)

    try:
        update_admin_profile(update, admin["id"])
    except Exception:
        logger.exception("Could not update admin profile.")

    await update.message.reply_text(
        "✅ Login successful."
    )

    # Refresh admin data after login.
    try:
        admin = get_admin_by_telegram_id(telegram_user_id)
    except Exception:
        pass

    await send_dashboard(update, admin)


# ============================================================
# LOGOUT
# ============================================================

async def logout(update: Update) -> None:
    """Log the current admin out."""
    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    authenticated_users.discard(telegram_user_id)
    awaiting_password.discard(telegram_user_id)

    await update.message.reply_text(
        "🚪 You have been logged out.\n\n"
        "Use /start whenever you want to log in again."
    )


# ============================================================
# DASHBOARD BUTTONS
# ============================================================

async def handle_dashboard_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle dashboard menu buttons."""
    if not is_private_chat(update):
        return

    if not update.message or not update.message.text:
        return

    telegram_user_id = get_telegram_user_id(update)

    if telegram_user_id is None:
        return

    # Password input must be processed before dashboard buttons.
    if telegram_user_id in awaiting_password:
        await handle_password(update)
        return

    text = update.message.text

    if text == "🚪 Logout":
        await logout(update)
        return

    admin = get_logged_in_admin(update)

    if not admin:
        await update.message.reply_text(
            "🔐 You are not logged in.\n\n"
            "Use /start to log in."
        )
        return

    # --------------------------------------------------------
    # These features will be implemented in later phases.
    # --------------------------------------------------------

    if text == "➕ Create Test":
        await update.message.reply_text(
            "➕ Create Test\n\n"
            "Test creation module will be added next."
        )
        return

    if text == "📋 Manage Tests":
        await update.message.reply_text(
            "📋 Manage Tests\n\n"
            "Test management module will be added next."
        )
        return

    if text == "🔑 Answer Keys":
        await update.message.reply_text(
            "🔑 Answer Keys\n\n"
            "Answer-key module will be added later."
        )
        return

    if text == "🏆 Results":
        await update.message.reply_text(
            "🏆 Results\n\n"
            "Results and ranking module will be added later."
        )
        return

    if text == "👥 Manage Admins":
        if admin["role"] != "OWNER":
            await update.message.reply_text(
                "❌ Owner access required."
            )
            return

        await update.message.reply_text(
            "👥 Manage Admins\n\n"
            "Admin-management module will be added later."
        )
        return

    await update.message.reply_text(
        "Please use the dashboard buttons."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log unexpected errors without exposing secrets."""
    logger.exception(
        "Unhandled bot error:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    """Start the Telegram bot."""
    logger.info("Starting PrepArena Admin Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_dashboard_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("PrepArena Admin Bot is running.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
