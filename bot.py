"""Telegram-бот: присылаешь фото файлом — он показывает, что записано в его метаданных."""

import asyncio
import logging
import os
import sys
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Document, Message, PhotoSize
from dotenv import load_dotenv

from exif_report import ExifToolError, build_report, find_exiftool, read_metadata

# Файлы больше 20 МБ Telegram ботам не отдаёт
MAX_FILE_SIZE = 20 * 1024 * 1024

START_TEXT = """\
Привет! Я показываю, что можно узнать о фото по его метаданным: \
где и когда оно сделано, на какое устройство и т.д.

Отправь мне фото <b>файлом</b>, чтобы Telegram его не сжимал:
• с телефона: 📎 → Файл → выбери фото
• с компьютера: перетащи фото в чат и сними галочку «Сжать изображение»

Если отправить обычным фото, Telegram вырежет метаданные — можешь сравнить.

Файлы я не сохраняю: проверка идёт в памяти, и всё сразу забывается."""

dp = Dispatcher()


@dp.message(CommandStart())
@dp.message(Command("help"))
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT)


@dp.message(Command("id"))
async def on_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(F.document)
async def on_document(message: Message, bot: Bot, exiftool: str) -> None:
    await check_file(message, bot, exiftool, message.document, compressed=False)


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot, exiftool: str) -> None:
    # Обычное фото Telegram уже пережал — покажем, что от метаданных осталось
    await check_file(message, bot, exiftool, message.photo[-1], compressed=True)


@dp.message()
async def on_other(message: Message) -> None:
    await message.answer("Пришли фото файлом: 📎 → Файл → выбери фото.")


async def check_file(message: Message, bot: Bot, exiftool: str,
                     file: Document | PhotoSize, compressed: bool) -> None:
    if file.file_size and file.file_size > MAX_FILE_SIZE:
        await message.reply("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие файлы.")
        return
    try:
        data = (await bot.download(file)).getvalue()  # качаем в память, на диск не пишем
    except TelegramBadRequest as e:
        await message.reply(f"Не получилось скачать файл: {escape(e.message)}")
        return
    try:
        tags = await read_metadata(data, exiftool)
    except ExifToolError as e:
        await message.reply(f"ExifTool не смог прочитать файл: {escape(str(e))}")
        return

    report = build_report(tags, len(data), compressed)
    await message.reply(report.text)
    if report.location:
        await message.reply_location(*report.location)


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не задан BOT_TOKEN: создай файл .env по образцу .env.example")
    exiftool = find_exiftool()
    if not exiftool:
        sys.exit("Не найден ExifTool: установи его (см. README.md)")
    # Если список задан, бот молча игнорирует всех остальных
    if allowed := os.getenv("ALLOWED_USERS"):
        user_ids = {int(user_id) for user_id in allowed.split(",") if user_id.strip()}
        dp.message.filter(F.from_user.id.in_(user_ids))

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                  link_preview_is_disabled=True))
    await dp.start_polling(bot, exiftool=exiftool)


if __name__ == "__main__":
    asyncio.run(main())
