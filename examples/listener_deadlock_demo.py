import asyncio
import os

from kurimod import Client
from pyrogram import filters

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv()

API_ID = os.getenv("PYROGRAM_API_ID")
API_HASH = os.getenv("PYROGRAM_API_HASH")
BOT_TOKEN = os.getenv("PYROGRAM_BOT_TOKEN")

WORKERS = int(os.getenv("PYROGRAM_WORKERS", "1"))

bot = Client(
    "listener-deadlock-demo",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=WORKERS,
    in_memory=True,
)


@bot.on_message(filters.command("deadlock"))
async def deadlock_handler(client, message):
    await message.reply_text(
        "Opening two listeners with only "
        f"{client.workers} worker(s) are available.\n"
        "Send /deadlock from another account before completing these listeners. "
        "The other account should still get this reply."
    )

    await asyncio.gather(
        message.chat.listen(user_id=message.from_user.id),
        message.chat.listen(user_id=message.from_user.id),
    )

    await message.reply_text(
        "Both listeners completed."
    )


if __name__ == "__main__":
    bot.run()
