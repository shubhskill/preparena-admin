# PrepArena Admin Bot

Admin and Owner management bot for PrepArena.

Built with Python, Telegram Bot API, Supabase, GitHub and Railway.

## Authentication

- Owner login uses `OWNER_PASSWORD` from Railway.
- Admin login uses the common `ADMIN_PASSWORD` from Railway.
- No Telegram ID allowlist is used for login.
- Admin-management UI has been removed.

## Bot commands

- `/start` — Start the bot and choose Owner/Admin access
- `/create` — Create a new test
- `/tests` — View created tests
- `/results` — View calculated results
- `/logout` — Log out
- `/cancel` — Cancel the current operation
