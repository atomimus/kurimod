from kurimod import Client
import pyrogram

bot = Client("my_bot")


@bot.on_message()
async def handler(client, message):
    await message.reply_text("Hello world!")

bot.run()
