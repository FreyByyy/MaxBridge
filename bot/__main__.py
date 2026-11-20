"""Запуск: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, LinkPreviewOptions

from .app import App
from .config import load_config
from .db import Database
from .forwarder import Forwarder
from .max_client import MaxService
from .rules import RulesStore
from .settings import Settings
from .tg import callbacks, membership, private

logger = logging.getLogger("bot")

OWNER_COMMANDS = [
    BotCommand(command="menu", description="Меню настроек"),
    BotCommand(command="status", description="Состояние"),
    BotCommand(command="chats", description="Чаты Telegram и источники"),
    BotCommand(command="login", description="Войти в MAX"),
    BotCommand(command="kw", description="Ключевые слова"),
    BotCommand(command="ai", description="ИИ для спорных сообщений"),
    BotCommand(command="pending", description="Сообщения на модерации"),
    BotCommand(command="stats", description="Статистика за 7 дней"),
    BotCommand(command="help", description="Все команды"),
]
TRUSTED_COMMANDS = [
    BotCommand(command="chats", description="Мои чаты и что в них пересылать"),
]


async def cleanup_loop(db: Database) -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await db.cleanup()
        except Exception:  # noqa: BLE001
            logger.exception("cleanup failed")


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Файлы с сессией MAX и настройками доступны только владельцу процесса.
    os.umask(0o077)

    db = Database(cfg.db_path)
    await db.open()
    rules = RulesStore(cfg.rules_path)
    settings = Settings(db, cfg)
    await settings.init_defaults()

    bot = Bot(
        cfg.bot_token,
        default=DefaultBotProperties(parse_mode="HTML", link_preview=LinkPreviewOptions(is_disabled=True)),
    )
    forwarder = Forwarder(bot, db, rules, settings, cfg)
    max_service = MaxService(
        work_dir=cfg.max_dir,
        notify_owner=lambda text: forwarder.notify_owner(text),
        on_message=forwarder.on_new,
        on_edit=forwarder.on_edit,
        on_delete=forwarder.on_delete,
        show_online=cfg.max_show_online,
    )
    forwarder.attach(max_service)
    app = App(cfg, bot, db, rules, settings, forwarder, max_service)

    dp = Dispatcher(app=app)
    dp.include_routers(membership.router, private.router, callbacks.router)

    await bot.set_my_commands(TRUSTED_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=cfg.owner_id))

    phone = await settings.max_phone()
    if phone and max_service.has_session:
        await max_service.start(phone)
    else:
        await forwarder.notify_owner("👋 Бот запущен. Чтобы подключить MAX: /login")

    cleanup = asyncio.create_task(cleanup_loop(db))
    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query", "my_chat_member"]
        )
    finally:
        cleanup.cancel()
        await max_service.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
