"""Диагностика: проверяет всё, что обычно ломается. Токен не печатает.

Запуск на сервере:
    /opt/moderator/.venv/bin/python -m deploy.diagnose
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.main import BOT_COMMANDS  # noqa: E402
from bot.spamfilter import SpamFilter  # noqa: E402
from bot.storage import Storage  # noqa: E402

OK, FAIL, WARN = "  OK  ", " ОШИБКА ", " ВНИМАНИЕ "
problems: list[str] = []


def report(status: str, title: str, detail: str = "") -> None:
    print(f"[{status}] {title}" + (f" — {detail}" if detail else ""))
    if status is FAIL:
        problems.append(title)


async def main() -> int:
    cfg = load_config()
    bot = Bot(cfg.token)

    try:
        me = await bot.me()
        report(OK, "Токен принят Telegram", f"@{me.username}")
    except TelegramAPIError as e:
        report(FAIL, "Токен не принят", str(e))
        await bot.session.close()
        return 1

    # Права в чатах
    admin_everywhere = bool(cfg.chat_ids)
    if not cfg.chat_ids:
        report(WARN, "CHAT_IDS не задан", "бот работает во всех группах, куда добавлен")
    for chat_id in cfg.chat_ids:
        try:
            member = await bot.get_chat_member(chat_id, me.id)
        except TelegramAPIError as e:
            report(FAIL, f"Чат {chat_id} недоступен", str(e))
            admin_everywhere = False
            continue
        if member.status != "administrator":
            report(FAIL, f"Бот не администратор в чате {chat_id}", member.status)
            admin_everywhere = False
            continue
        missing = [
            name for name, flag in (
                ("удаление сообщений", getattr(member, "can_delete_messages", False)),
                ("блокировка пользователей", getattr(member, "can_restrict_members", False)),
            ) if not flag
        ]
        if missing:
            report(FAIL, f"Не хватает прав в чате {chat_id}", ", ".join(missing))
        else:
            report(OK, f"Права в чате {chat_id}", "удаление и блокировка есть")

    # Режим приватности. Администратор группы видит все сообщения и с включённым
    # режимом — это исключение описано в документации Telegram. Но если права
    # админа снимут, бот молча ослепнет, поэтому режим лучше выключить.
    privacy_off = getattr(me, "can_read_all_group_messages", None)
    if privacy_off is True:
        report(OK, "Режим приватности выключен", "бот видит все сообщения")
    elif privacy_off is False:
        if admin_everywhere:
            report(
                WARN, "Режим приватности включён",
                "сейчас не мешает — администратор видит все сообщения, — но если "
                "права админа снимут, бот перестанет видеть комментарии. "
                "@BotFather → Bot Settings → Group Privacy → Turn off",
            )
        else:
            report(
                FAIL, "Включён режим приватности, а бот не администратор",
                "бот не видит обычные сообщения. @BotFather → Bot Settings → "
                "Group Privacy → Turn off, затем удалить и заново добавить бота в чат",
            )

    # Антиспам самого Telegram — работает параллельно с ботом
    for chat_id in cfg.chat_ids:
        try:
            chat = await bot.get_chat(chat_id)
            count = await bot.get_chat_member_count(chat_id)
        except TelegramAPIError:
            continue
        if chat.has_aggressive_anti_spam_enabled:
            report(OK, "Антиспам Telegram включён", f"участников {count}, работает вместе с ботом")
        elif count >= 200:
            report(
                WARN, "Антиспам Telegram выключен",
                f"участников {count} — можно включить в настройках чата, "
                "он ловит часть спама до бота",
            )
        else:
            report(
                WARN, "Антиспам Telegram недоступен",
                f"участников {count}, нужно от 200",
            )

    # Уведомления владельцу
    if cfg.admin_id is None:
        report(WARN, "ADMIN_ID не задан", "уведомления и управление недоступны")
    else:
        try:
            await bot.get_chat(cfg.admin_id)
            report(OK, "Личка с владельцем доступна", f"id {cfg.admin_id}")
        except TelegramAPIError:
            report(
                FAIL, "Владелец не начал диалог с ботом",
                "откройте бота и нажмите Start — Telegram не даёт писать первым",
            )

    # Список команд у кнопки «Меню»
    try:
        from aiogram.types import BotCommandScopeChat

        scope = BotCommandScopeChat(chat_id=cfg.admin_id) if cfg.admin_id else None
        registered = await bot.get_my_commands(scope=scope)
        if len(registered) >= len(BOT_COMMANDS):
            report(OK, "Список команд зарегистрирован", f"{len(registered)} шт.")
        else:
            report(WARN, "Список команд не полон", f"{len(registered)} из {len(BOT_COMMANDS)}")
    except TelegramAPIError as e:
        report(WARN, "Не удалось прочитать список команд", str(e))

    # Фильтр и база
    try:
        spam_filter = SpamFilter(custom_path=cfg.db_path.parent / "custom_rules.yml")
        spam = spam_filter.check("Работа на дому, 5000 в день, пиши в лс")
        clean = spam_filter.check("Спасибо за пост, очень интересно")
        if spam.action != "none" and clean.action == "none":
            report(OK, "Фильтр работает", f"спам {spam.score} баллов, обычное {clean.score}")
        else:
            report(FAIL, "Фильтр ведёт себя неверно", f"{spam.action} / {clean.action}")
    except Exception as e:  # noqa: BLE001
        report(FAIL, "Ошибка в правилах фильтра", str(e))

    try:
        store = Storage(cfg.db_path, store_text=cfg.store_message_text)
        total, deleted, banned = store.stats(7)
        report(
            OK, "База доступна",
            f"за 7 дней: {total} срабатываний, образцов {store.count_samples()}",
        )
        store.close()
    except Exception as e:  # noqa: BLE001
        report(FAIL, "База недоступна", str(e))

    # Возможность сохранять настройки из Telegram
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        try:
            with open(env_path, "a", encoding="utf-8"):
                pass
            report(OK, "Настройки можно менять из Telegram", "файл .env доступен на запись")
        except OSError as e:
            report(FAIL, "Настройки не сохранятся", str(e))

    report(OK, "Режим работы", f"{cfg.mode}")
    await bot.session.close()

    print()
    if problems:
        print(f"Проблем: {len(problems)}")
        for item in problems:
            print(f"  • {item}")
        return 1
    print("Всё в порядке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
