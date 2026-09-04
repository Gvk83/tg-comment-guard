"""Бот-модератор чата комментариев: удаляет спам и банит спамеров."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .config import BASE_DIR, MODES, Config, load_config
from .normalize import normalize
from .spamfilter import SpamFilter
from .settings import add_phrase, remove_phrase, set_threshold, update_env
from .storage import Storage

log = logging.getLogger("moderator")

ADMIN_CACHE_TTL = 300  # секунд

# Служебные отправители Telegram, которых нельзя трогать.
# 1087968824 — @GroupAnonymousBot: под этим id приходят сообщения анонимных
# администраторов группы, в списке администраторов чата их нет.
# 777000 — служебный аккаунт Telegram.
SYSTEM_SENDERS = frozenset({1087968824, 777000})


def user_id_or_none(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None

# Постоянные кнопки под полем ввода — чтобы не набирать команды руками.
BTN_STATUS = "📊 Состояние"
BTN_STATS = "📈 Статистика"
BTN_LAST = "📋 Последние"
BTN_SAMPLES = "🚫 Образцы"
BTN_MODE = "⚙️ Режим"
BTN_HELP = "❓ Команды"

REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_LAST), KeyboardButton(text=BTN_SAMPLES)],
        [KeyboardButton(text=BTN_MODE), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите кнопку или введите команду",
)

# Список для кнопки «Меню» у поля ввода.
BOT_COMMANDS = [
    BotCommand(command="menu", description="Меню с кнопками"),
    BotCommand(command="status", description="Режим и настройки"),
    BotCommand(command="stats", description="Статистика за неделю"),
    BotCommand(command="last", description="Последние срабатывания"),
    BotCommand(command="samples", description="Образцы спама"),
    BotCommand(command="spam", description="Запомнить образец: /spam текст"),
    BotCommand(command="check", description="Проверить текст: /check текст"),
    BotCommand(command="mode", description="Режим: observe, soft, enforce"),
    BotCommand(command="pause", description="Приостановить проверку"),
    BotCommand(command="resume", description="Возобновить проверку"),
    BotCommand(command="restart", description="Перезапустить бота"),
    BotCommand(command="whitelist", description="Белый список"),
    BotCommand(command="unban", description="Разблокировать: /unban ID"),
    BotCommand(command="threshold", description="Пороги срабатывания"),
    BotCommand(command="addword", description="Добавить фразу в правила"),
    BotCommand(command="delword", description="Убрать добавленную фразу"),
    BotCommand(command="reload", description="Перечитать правила"),
    BotCommand(command="delsample", description="Удалить образец: /delsample 5"),
    BotCommand(command="purge", description="Стереть тексты из журнала"),
    BotCommand(command="media", description="Кружки и голосовые от новичков"),
    BotCommand(command="service", description="Сообщения о входе в группу"),
    BotCommand(command="help", description="Все команды"),
]

MODE_NAMES = {
    "observe": "наблюдение — ничего не удаляет",
    "soft": "мягкий — удаляет спам, не банит",
    "enforce": "боевой — удаляет и банит",
}


class Moderator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.custom_rules = cfg.db_path.parent / "custom_rules.yml"
        self.filter = SpamFilter(custom_path=self.custom_rules)
        self.store = Storage(cfg.db_path, store_text=cfg.store_message_text)
        self.bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher()
        self._admins: dict[int, tuple[float, set[int]]] = {}
        self._me_id: int | None = None
        self.paused = False
        self._warned: dict[str, float] = {}
        self._native_antispam: bool | None = None
        self._pending_sample: str = ""
        self._register()

    # ------------------------------------------------------------ маршруты

    def _register(self) -> None:
        dp = self.dp
        private_admin = F.chat.type == "private"

        for command, handler in (
            (("start", "help", "menu"), self.cmd_start),
            (("status",), self.cmd_status),
            (("mode",), self.cmd_mode),
            (("pause",), self.cmd_pause),
            (("resume",), self.cmd_resume),
            (("restart",), self.cmd_restart),
            (("reload",), self.cmd_reload),
            (("stats",), self.cmd_stats),
            (("last",), self.cmd_last),
            (("check",), self.cmd_check),
            (("purge",), self.cmd_purge),
            (("whitelist", "wl"), self.cmd_whitelist),
            (("unban",), self.cmd_unban),
            (("addword",), self.cmd_addword),
            (("delword",), self.cmd_delword),
            (("threshold",), self.cmd_threshold),
            (("service",), self.cmd_service),
            (("media",), self.cmd_media),
            (("spam",), self.cmd_spam),
            (("samples",), self.cmd_samples),
            (("delsample",), self.cmd_delsample),
        ):
            dp.message.register(handler, Command(*command), private_admin)

        dp.message.register(
            self.on_reply_button,
            private_admin,
            F.text.in_({BTN_STATUS, BTN_STATS, BTN_LAST, BTN_SAMPLES, BTN_MODE, BTN_HELP}),
        )
        # Любой другой текст или пересылка в личке — разбор сообщения.
        # Регистрируем последним, чтобы не перехватывать команды и кнопки.
        dp.message.register(self.on_private_text, private_admin)

        dp.callback_query.register(self.on_button)

        group = F.chat.type.in_({"group", "supergroup"})
        dp.message.register(self.cmd_id, Command("id"), group)
        # Служебные сообщения («X теперь в группе») проверяем до обычных.
        dp.message.register(
            self.on_service_message,
            group,
            F.new_chat_members | F.left_chat_member | F.new_chat_title
            | F.new_chat_photo | F.delete_chat_photo,
        )
        dp.message.register(self.on_group_message, group)
        dp.edited_message.register(self.on_group_message, group)

    # ------------------------------------------------- вспомогательные вещи

    def _is_owner(self, message: Message) -> bool:
        return self.cfg.admin_id is not None and message.from_user is not None \
            and message.from_user.id == self.cfg.admin_id

    def is_protected(
        self,
        user_id: int | None,
        chat_admins: Iterable[int],
        *,
        sender_chat_id: int | None = None,
        chat_id: int | None = None,
        linked_chat_id: int | None = None,
    ) -> bool:
        """Отправители, которых бот не трогает ни при каких условиях."""
        # Анонимный администратор пишет от имени самой группы, а связанный
        # канал — от имени канала. В списке администраторов их нет.
        if sender_chat_id is not None and sender_chat_id in (chat_id, linked_chat_id):
            return True
        if user_id is None:
            return False
        if user_id in SYSTEM_SENDERS:
            return True
        if user_id == self._me_id:
            return True
        if user_id == self.cfg.admin_id:
            return True
        if user_id in self.cfg.whitelist_ids:
            return True
        return user_id in chat_admins

    async def _chat_admins(self, chat_id: int) -> set[int]:
        cached = self._admins.get(chat_id)
        now = time.monotonic()
        if cached and now - cached[0] < ADMIN_CACHE_TTL:
            return cached[1]
        try:
            members = await self.bot.get_chat_administrators(chat_id)
            ids = {m.user.id for m in members}
        except TelegramAPIError as e:
            log.warning("Не удалось получить админов чата %s: %s", chat_id, e)
            ids = cached[1] if cached else set()
        self._admins[chat_id] = (now, ids)
        return ids

    async def _notify(
        self, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> None:
        if not self.cfg.notify_admin or self.cfg.admin_id is None:
            return
        try:
            await self.bot.send_message(
                self.cfg.admin_id, text,
                disable_web_page_preview=True, reply_markup=keyboard,
            )
        except TelegramAPIError as e:
            log.warning("Не удалось отправить уведомление админу: %s", e)

    @staticmethod
    def _incident_buttons(
        user_id: int | None, banned: bool, incident_id: int | None = None
    ) -> InlineKeyboardMarkup | None:
        """Кнопки под уведомлением — чтобы исправить ошибку в один тап."""
        if user_id is None:
            return None
        row = [InlineKeyboardButton(text="✅ Не спам", callback_data=f"notspam:{user_id}")]
        if banned:
            row.append(
                InlineKeyboardButton(text="🔓 Разблокировать", callback_data=f"unban:{user_id}")
            )
        rows = [row]
        if incident_id is not None:
            rows.append([
                InlineKeyboardButton(
                    text="🚫 Запомнить как спам", callback_data=f"remember:{incident_id}"
                )
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _hidden_links(message: Message) -> list[str]:
        """Адреса, спрятанные под текстом сообщения, и упомянутые профили."""
        urls: list[str] = []
        for entity in (message.entities or []) + (message.caption_entities or []):
            if entity.type == "text_link" and entity.url:
                urls.append(entity.url)
            elif entity.type == "text_mention" and entity.user:
                urls.append(f"@{entity.user.username or entity.user.id}")
        return urls

    def _name_looks_spammy(self, message: Message) -> bool:
        """Спамеры часто пишут объявление прямо в имени профиля."""
        u = message.from_user
        if u is None:
            return False
        name = " ".join(filter(None, (u.full_name, u.username)))
        return len(name) > 8 and self.filter.check(name).action != "none"

    @staticmethod
    def _who(message: Message) -> str:
        u = message.from_user
        if u is None:
            chat = message.sender_chat
            return f"канал «{html.escape(chat.title or '')}» (id {chat.id})" if chat else "неизвестно"
        name = html.escape(u.full_name)
        handle = f" @{u.username}" if u.username else ""
        return f"{name}{handle} (id {u.id})"

    # ------------------------------------------- служебные сообщения чата

    async def on_service_message(self, message: Message) -> None:
        """Убирает «X теперь в группе» и подобные системные заметки.

        Скрыть их настройками Telegram нельзя — удалить может только бот
        с правом удаления сообщений.
        """
        if self.paused or not self.cfg.delete_service_messages:
            return
        if self.cfg.chat_ids and message.chat.id not in self.cfg.chat_ids:
            return
        try:
            await message.delete()
        except TelegramAPIError as e:
            log.warning("Не удалось удалить служебное сообщение в %s: %s", message.chat.id, e)

    async def _handle_newcomer_media(self, message: Message) -> None:
        """Кружочек, голосовое или история от участника, который ещё не писал."""
        kind = ("видео-кружок" if message.video_note
                else "голосовое" if message.voice else "история")
        deleted = False
        if self.cfg.may_delete:
            try:
                await message.delete()
                deleted = True
            except TelegramAPIError as e:
                log.warning("Не удалось удалить %s: %s", kind, e)
        user_id = user_id_or_none(message)
        self.store.log_incident(
            chat_id=message.chat.id, user_id=user_id, score=0,
            reason=f"{kind} от нового участника", action="delete",
            deleted=deleted, banned=False,
        )
        await self._notify(
            f"🎥 <b>{kind.capitalize()} от нового участника</b>\n"
            f"Отправитель: {self._who(message)}\n"
            f"Действие: {'удалено' if deleted else 'режим наблюдения — ничего не сделано'}\n\n"
            "Отключить эту проверку: /media выкл",
            keyboard=self._incident_buttons(user_id, banned=False),
        )

    async def _delete_previous(
        self, chat_id: int, user_id: int | None, current_id: int
    ) -> int:
        """Удаляет остальные сообщения участника за последние двое суток."""
        ids = [m for m in self.store.user_message_ids(chat_id, user_id) if m != current_id]
        if not ids:
            return 0
        removed = 0
        # Telegram умеет удалять пачкой, но не больше ста за раз.
        for chunk in (ids[i:i + 100] for i in range(0, len(ids), 100)):
            try:
                await self.bot.delete_messages(chat_id, chunk)
                removed += len(chunk)
            except TelegramAPIError:
                # Пачкой не вышло — пробуем по одному, часть могла быть уже удалена.
                for message_id in chunk:
                    try:
                        await self.bot.delete_message(chat_id, message_id)
                        removed += 1
                    except TelegramAPIError:
                        pass
        self.store.forget_user_messages(chat_id, user_id)
        if removed:
            log.info("Удалено прошлых сообщений участника %s: %s", user_id, removed)
        return removed

    # ------------------------------------------------- основная проверка

    async def on_group_message(self, message: Message) -> None:
        cfg = self.cfg

        if self.paused:
            return

        if cfg.chat_ids and message.chat.id not in cfg.chat_ids:
            return

        # Пост канала, автоматически пересланный в группу обсуждений, — не трогаем.
        if message.is_automatic_forward or message.from_user is None and message.sender_chat \
                and message.chat.linked_chat_id == message.sender_chat.id:
            return

        if self.cfg.block_newcomer_media and (
            message.video_note or message.voice or message.story
        ):
            if self.store.is_new_member(message.chat.id, user_id_or_none(message)):
                await self._handle_newcomer_media(message)
                return

        text = message.text or message.caption or ""
        hidden = self._hidden_links(message)
        if not text.strip() and not hidden:
            return

        user_id = message.from_user.id if message.from_user else None
        sender_chat_id = message.sender_chat.id if message.sender_chat else None

        if self._me_id is None:
            self._me_id = (await self.bot.me()).id
        if self.is_protected(
            user_id,
            await self._chat_admins(message.chat.id),
            sender_chat_id=sender_chat_id,
            chat_id=message.chat.id,
            linked_chat_id=message.chat.linked_chat_id,
        ):
            return

        # Номер сообщения запоминаем всегда — включая те, что фильтр пропустил.
        # Если человек окажется спамером, удалим всё, что он успел написать.
        self.store.remember_message(message.chat.id, user_id, message.message_id)

        norm = normalize(text)
        is_reply_to_user = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and not message.reply_to_message.is_automatic_forward
        )
        verdict = self.filter.check(
            text,
            is_duplicate=self.store.check_duplicate(norm, user_id),
            is_new_member=self.store.is_new_member(message.chat.id, user_id),
            is_reply_to_user=is_reply_to_user,
            hidden_links=hidden,
            is_forwarded=message.forward_origin is not None,
            spam_name=self._name_looks_spammy(message),
            known_spam=self.store.match_sample(norm),
        )
        if verdict.action == "none":
            return

        deleted = banned = False
        also_deleted = 0
        errors: list[str] = []

        if cfg.may_delete:
            try:
                await message.delete()
                deleted = True
            except TelegramAPIError as e:
                if "not found" in str(e).lower():
                    # Обычно означает, что сообщение уже снял антиспам Telegram.
                    deleted = True
                    errors.append("сообщение уже было удалено (вероятно, антиспамом Telegram)")
                else:
                    errors.append(f"удаление не удалось: {e}")

            # Убираем и всё, что этот человек успел написать раньше:
            # фильтр мог пропустить его первые сообщения.
            also_deleted = await self._delete_previous(message.chat.id, user_id, message.message_id)

        if verdict.action == "ban" and cfg.may_ban:
            try:
                if user_id is not None:
                    await self.bot.ban_chat_member(
                        message.chat.id, user_id, revoke_messages=True
                    )
                elif sender_chat_id is not None:
                    await self.bot.ban_chat_sender_chat(message.chat.id, sender_chat_id)
                banned = True
                self.store.forget_user_messages(message.chat.id, user_id)
            except TelegramAPIError as e:
                errors.append(f"блокировка не удалась: {e}")

        incident_id = self.store.log_incident(
            chat_id=message.chat.id,
            user_id=user_id or sender_chat_id,
            score=verdict.score,
            reason=verdict.reason_text,
            action=verdict.action,
            deleted=deleted,
            banned=banned,
            text=text,
        )
        log.info(
            "chat=%s user=%s score=%s action=%s deleted=%s banned=%s reason=%s",
            message.chat.id, user_id, verdict.score, verdict.action, deleted, banned,
            verdict.reason_text,
        )

        if cfg.mode == "observe":
            done = "режим наблюдения — ничего не сделано"
        else:
            done = ("удалено" if deleted else "не удалено") + \
                   (", забанен" if banned else (", без бана" if verdict.action == "ban" else ""))
            if also_deleted:
                done += f", удалено прошлых сообщений: {also_deleted}"

        preview = html.escape(text.strip())[:300]
        await self._notify(
            f"🚫 <b>Спам</b> ({verdict.score} баллов, {verdict.action})\n"
            f"Чат: {html.escape(message.chat.title or str(message.chat.id))}\n"
            f"Отправитель: {self._who(message)}\n"
            f"Признаки: {html.escape(verdict.reason_text)}\n"
            f"Действие: {done}\n"
            + (f"Ошибки: {html.escape('; '.join(errors))}\n" if errors else "")
            + f"\n<blockquote>{preview}</blockquote>",
            keyboard=self._incident_buttons(user_id, banned, incident_id),
        )

    # ---------------------------------------------------- команды владельца

    def _menu(self) -> InlineKeyboardMarkup:
        """Кнопки действий. Просмотр вынесен на клавиатуру под полем ввода."""
        rows = [
            [
                InlineKeyboardButton(
                    text=("👀 Наблюдение" + (" ✅" if self.cfg.mode == "observe" else "")),
                    callback_data="mode:observe",
                ),
                InlineKeyboardButton(
                    text=("🧹 Мягкий" + (" ✅" if self.cfg.mode == "soft" else "")),
                    callback_data="mode:soft",
                ),
                InlineKeyboardButton(
                    text=("🛡 Боевой" + (" ✅" if self.cfg.mode == "enforce" else "")),
                    callback_data="mode:enforce",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Возобновить" if self.paused else "⏸ Приостановить",
                    callback_data="resume" if self.paused else "pause",
                ),
                InlineKeyboardButton(text="🔄 Перезапустить", callback_data="restart"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    HELP_TEXT = (
        "<b>Управление ботом</b>\n\n"
        "/menu — меню с кнопками\n"
        "/status — режим и настройки\n"
        "/stats — сводка за 7 дней\n"
        "/last — последние 10 срабатываний\n\n"
        "<b>Режимы</b>\n"
        "/mode observe — только уведомления\n"
        "/mode soft — удалять, не банить\n"
        "/mode enforce — удалять и банить\n"
        "/pause — приостановить проверку\n"
        "/resume — возобновить\n"
        "/restart — перезапустить бота\n\n"
        "<b>Образцы спама</b>\n"
        "Перешлите мне сюда любое сообщение — разберу его и предложу\n"
        "запомнить как образец. Это самый быстрый способ.\n"
        "/samples — сохранённые образцы\n"
        "/spam текст — запомнить образец вручную\n"
        "/delsample 5 — удалить образец\n"
        "  Похожие сообщения удаляются сразу, цифры не важны.\n\n"
        "<b>Правила</b>\n"
        "/check текст — проверить текст\n"
        "/addword группа фраза — добавить фразу\n"
        "    группы: job_offer, contact_cta, easy_money,\n"
        "    send_material, age_bait\n"
        "/delword фраза — убрать добавленную фразу\n"
        "/threshold ban 7 — порог блокировки\n"
        "/threshold delete 5 — порог удаления\n"
        "/reload — перечитать правила\n\n"
        "<b>Люди</b>\n"
        "/whitelist — показать белый список\n"
        "/whitelist add 12345 — не трогать этого человека\n"
        "/whitelist del 12345 — убрать из списка\n"
        "/unban 12345 — разблокировать в чате\n\n"
        "<b>Прочее</b>\n"
        "/service вкл|выкл — удалять ли «X теперь в группе»\n"
        "/media вкл|выкл — кружки и голосовые от новичков\n"
        "/purge — стереть сохранённые тексты из журнала"
    )

    async def cmd_start(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer(
            "Кнопки просмотра — под полем ввода. "
            "Слева от него кнопка «Меню» со списком всех команд.",
            reply_markup=REPLY_KEYBOARD,
        )
        await message.answer(
            f"Режим: <b>{self.cfg.mode}</b> — {MODE_NAMES[self.cfg.mode]}",
            reply_markup=self._menu(),
        )

    async def cmd_id(self, message: Message) -> None:
        """Показывает ID чата — нужен один раз при настройке."""
        admins = await self._chat_admins(message.chat.id)
        if not message.from_user or message.from_user.id not in admins:
            return
        await message.reply(
            f"ID этого чата: <code>{message.chat.id}</code>\n"
            f"Ваш ID: <code>{message.from_user.id}</code>"
        )

    def _status_text(self) -> str:
        cfg = self.cfg
        native = {
            True: "включён",
            False: "выключен",
            None: "неизвестно",
        }[self._native_antispam]
        return (
            (f"⏸ <b>Работа приостановлена</b>\n\n" if self.paused else "")
            + f"Режим: <b>{cfg.mode}</b> — {MODE_NAMES[cfg.mode]}\n"
            f"Чаты: {', '.join(map(str, cfg.chat_ids)) or 'все, куда добавлен'}\n"
            f"Порог удаления: {self.filter.thresholds['delete']}, "
            f"порог блокировки: {self.filter.thresholds['ban']}\n"
            f"Уведомления: {'вкл' if cfg.notify_admin else 'выкл'}\n"
            f"Белый список: {', '.join(map(str, cfg.whitelist_ids)) or 'пуст'}\n"
            f"Образцов спама: {self.store.count_samples()}\n"
            f"Хранение текстов сработавших сообщений: "
            f"{'вкл' if cfg.store_message_text else 'выкл'}\n"
            f"Служебные сообщения о входе: "
            f"{'удаляются' if cfg.delete_service_messages else 'остаются'}\n"
            f"Антиспам самого Telegram: {native}"
        )

    async def cmd_status(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer(self._status_text())

    async def _set_mode(self, mode: str) -> str:
        self.cfg.mode = mode
        update_env(BASE_DIR / ".env", "MODE", mode)
        self.paused = False
        self._warned: dict[str, float] = {}
        self._native_antispam: bool | None = None
        self._pending_sample: str = ""
        return f"Режим: <b>{mode}</b> — {MODE_NAMES[mode]}\nНастройка сохранена."

    async def cmd_mode(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1] not in MODES:
            await message.answer(
                f"Режим: <b>{self.cfg.mode}</b> — {MODE_NAMES[self.cfg.mode]}",
                reply_markup=self._menu(),
            )
            return
        await message.answer(await self._set_mode(parts[1]), reply_markup=self._menu())

    async def cmd_pause(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        self.paused = True
        await message.answer(
            "⏸ Проверка приостановлена. Бот запущен, но ничего не проверяет и не удаляет.\n"
            "Вернуть: /resume",
            reply_markup=self._menu(),
        )

    async def cmd_resume(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        self.paused = False
        self._warned: dict[str, float] = {}
        self._native_antispam: bool | None = None
        self._pending_sample: str = ""
        await message.answer("▶️ Проверка возобновлена.", reply_markup=self._menu())

    async def cmd_restart(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer("🔄 Перезапускаюсь, вернусь через несколько секунд…")
        log.info("Перезапуск по команде владельца")
        # Служба systemd поднимет процесс заново (Restart=always).
        asyncio.get_running_loop().call_later(1, lambda: os._exit(0))

    async def cmd_reload(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        try:
            self.filter.reload()
            await message.answer("Правила перечитаны.")
        except Exception as e:  # noqa: BLE001 — показываем владельцу причину
            await message.answer(f"Ошибка в правилах: {html.escape(str(e))}")

    async def cmd_stats(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer(self._stats_text())

    def _stats_text(self) -> str:
        total, deleted, banned = self.store.stats(7)
        day_total, day_deleted, day_banned = self.store.stats(1)
        return (
            f"<b>За сутки:</b> срабатываний {day_total}, "
            f"удалено {day_deleted}, заблокировано {day_banned}\n"
            f"<b>За 7 дней:</b> срабатываний {total}, "
            f"удалено {deleted}, заблокировано {banned}"
        )

    def _last_text(self, limit: int = 10) -> str:
        rows = self.store.recent(limit)
        if not rows:
            return "Срабатываний пока не было."
        lines = ["<b>Последние срабатывания</b>"]
        for ts, user_id, score, reason, deleted, banned, sample in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(ts))
            did = "удалено" if deleted else "не удалено"
            if banned:
                did += ", заблокирован"
            block = (
                f"\n<code>{when}</code> · id {user_id} · {score} баллов\n"
                f"{html.escape(reason)}\n{did}"
            )
            if sample:
                block += f"\n<blockquote>{html.escape(sample[:200])}</blockquote>"
            else:
                block += "\n<i>текст не сохранён</i>"
            lines.append(block)
        text = "\n".join(lines)
        return text[:3900] + ("…" if len(text) > 3900 else "")

    async def cmd_last(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer(self._last_text())

    async def cmd_check(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Пришлите: /check текст сообщения")
            return
        v = self.filter.check(text)
        decision = {
            "none": "пропустить",
            "delete": "удалить без блокировки",
            "ban": "удалить и заблокировать",
        }[v.action]
        await message.answer(
            f"Баллы: <b>{v.score}</b>\nРешение: <b>{decision}</b>\n"
            f"Признаки: {html.escape(v.reason_text)}"
        )

    async def cmd_purge(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        n = self.store.purge_samples()
        await message.answer(f"Тексты стёрты из {n} записей журнала.")

    async def cmd_whitelist(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) == 1:
            ids = ", ".join(map(str, sorted(self.cfg.whitelist_ids))) or "пуст"
            await message.answer(
                f"Белый список: {ids}\n\n"
                "Добавить: /whitelist add 123456789\n"
                "Убрать: /whitelist del 123456789\n\n"
                "Администраторы чата и вы защищены всегда, их добавлять не нужно."
            )
            return
        if len(parts) < 3 or parts[1] not in ("add", "del") or not parts[2].isdigit():
            await message.answer("Формат: /whitelist add 123456789")
            return
        user_id = int(parts[2])
        if parts[1] == "add":
            self.cfg.whitelist_ids.add(user_id)
            verb = "добавлен в белый список"
        else:
            self.cfg.whitelist_ids.discard(user_id)
            verb = "убран из белого списка"
        update_env(
            BASE_DIR / ".env", "WHITELIST_IDS",
            ",".join(map(str, sorted(self.cfg.whitelist_ids))),
        )
        await message.answer(f"Пользователь {user_id} {verb}. Настройка сохранена.")

    async def cmd_unban(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Формат: /unban 123456789\nID виден в уведомлении о спаме.")
            return
        user_id = int(parts[1])
        chats = self.cfg.chat_ids or set()
        if not chats:
            await message.answer("Не задан чат — разблокировать вручную не получится.")
            return
        results = []
        for chat_id in chats:
            try:
                await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                results.append(f"чат {chat_id}: разблокирован")
            except TelegramAPIError as e:
                results.append(f"чат {chat_id}: {e}")
        await message.answer(
            html.escape("\n".join(results))
            + "\n\nДобавить его в белый список: "
            + f"/whitelist add {user_id}"
        )

    async def cmd_addword(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split(maxsplit=2)
        groups = ("job_offer", "contact_cta", "easy_money", "send_material", "age_bait")
        if len(parts) < 3 or parts[1] not in groups:
            await message.answer(
                "Формат: /addword группа фраза\n\n"
                "Например: /addword contact_cta свяжитесь со мной\n\n"
                "Группы:\n"
                "job_offer — предложение работы\n"
                "contact_cta — призыв написать в личные\n"
                "easy_money — обещание лёгкого дохода\n"
                "send_material — предлагает прислать материал\n"
                "age_bait — «без опыта», «с 16 лет»"
            )
            return
        phrase = normalize(parts[2])
        added = add_phrase(self.custom_rules, parts[1], phrase)
        self.filter.reload()
        await message.answer(
            (f"Фраза «{html.escape(phrase)}» добавлена в {parts[1]}."
             if added else "Такая фраза уже есть.")
            + "\nПравила применены сразу."
        )

    async def cmd_delword(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        phrase = normalize((message.text or "").partition(" ")[2])
        if not phrase:
            await message.answer("Формат: /delword фраза")
            return
        removed = remove_phrase(self.custom_rules, phrase)
        self.filter.reload()
        await message.answer(
            f"Фраза убрана из: {', '.join(removed)}." if removed
            else "Такой добавленной фразы нет. Встроенные правила правятся в файле rules.yml."
        )

    async def cmd_threshold(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 3 or parts[1] not in ("ban", "delete") or not parts[2].isdigit():
            await message.answer(
                "Формат: /threshold ban 7  или  /threshold delete 5\n\n"
                f"Сейчас: удаление от {self.filter.thresholds['delete']} баллов, "
                f"блокировка от {self.filter.thresholds['ban']}.\n"
                "Больше число — реже срабатывания."
            )
            return
        set_threshold(self.custom_rules, parts[1], int(parts[2]))
        self.filter.reload()
        await message.answer(
            f"Порог «{parts[1]}» теперь {parts[2]}.\n"
            f"Удаление от {self.filter.thresholds['delete']}, "
            f"блокировка от {self.filter.thresholds['ban']}."
        )

    async def cmd_service(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1].lower() not in ("вкл", "выкл", "on", "off"):
            await message.answer(
                "Удалять служебные сообщения «X теперь в группе»?\n"
                "/service вкл — удалять\n/service выкл — оставлять\n\n"
                f"Сейчас: {'удаляются' if self.cfg.delete_service_messages else 'остаются'}"
            )
            return
        on = parts[1].lower() in ("вкл", "on")
        self.cfg.delete_service_messages = on
        update_env(BASE_DIR / ".env", "DELETE_SERVICE_MESSAGES", "true" if on else "false")
        await message.answer(
            f"Служебные сообщения теперь {'удаляются' if on else 'остаются'}. Сохранено."
        )

    async def cmd_media(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1].lower() not in ("вкл", "выкл", "on", "off"):
            await message.answer(
                "Удалять видео-кружки, голосовые и истории от тех, кто ещё "
                "ни разу не писал в чате? Через них тоже рассылают рекламу.\n\n"
                "/media вкл — удалять\n/media выкл — не трогать\n\n"
                f"Сейчас: {'удаляются' if self.cfg.block_newcomer_media else 'не трогаются'}"
            )
            return
        on = parts[1].lower() in ("вкл", "on")
        self.cfg.block_newcomer_media = on
        update_env(BASE_DIR / ".env", "BLOCK_NEWCOMER_MEDIA", "true" if on else "false")
        await message.answer(
            f"Кружки, голосовые и истории от новичков теперь "
            f"{'удаляются' if on else 'не трогаются'}. Сохранено."
        )

    async def cmd_spam(self, message: Message) -> None:
        """Добавить образец спама вручную: /spam текст объявления."""
        if not self._is_owner(message):
            return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer(
                "Пришлите: /spam текст спам-сообщения\n\n"
                "Похожие сообщения после этого будут удаляться сразу. "
                "Цифры не важны — «5000 в день» и «7000 в день» считаются "
                "одним и тем же объявлением."
            )
            return
        if self.store.add_sample(normalize(text), text):
            await message.answer(
                f"🚫 Образец сохранён. Всего образцов: {self.store.count_samples()}.\n"
                "Посмотреть: /samples"
            )
        else:
            await message.answer(
                "Не сохранил: текст слишком короткий или похожий образец уже есть."
            )

    def _samples_text(self) -> str:
        rows = self.store.list_samples(20)
        if not rows:
            return (
                "Образцов пока нет.\n\n"
                "Добавляйте их кнопкой «🚫 Запомнить как спам» под уведомлением "
                "или командой /spam текст."
            )
        lines = [f"<b>Образцы спама</b> (всего {self.store.count_samples()})"]
        for sample_id, preview, ts in rows:
            when = time.strftime("%d.%m", time.localtime(ts))
            lines.append(
                f"\n<code>#{sample_id}</code> · {when}\n"
                f"<blockquote>{html.escape(preview[:150])}</blockquote>"
            )
        lines.append("\nУдалить образец: /delsample 5")
        text = "\n".join(lines)
        return text[:3900] + ("…" if len(text) > 3900 else "")

    async def cmd_samples(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        await message.answer(self._samples_text())

    async def cmd_delsample(self, message: Message) -> None:
        if not self._is_owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
            await message.answer("Формат: /delsample 5\nНомер образца виден в /samples")
            return
        sample_id = int(parts[1].lstrip("#"))
        if self.store.delete_sample(sample_id):
            await message.answer(f"Образец #{sample_id} удалён.")
        else:
            await message.answer(f"Образца #{sample_id} нет.")

    async def on_reply_button(self, message: Message) -> None:
        """Нажатия постоянных кнопок под полем ввода."""
        text = (message.text or "").strip()
        if text == BTN_STATUS:
            await message.answer(self._status_text())
        elif text == BTN_STATS:
            await message.answer(self._stats_text())
        elif text == BTN_LAST:
            await message.answer(self._last_text())
        elif text == BTN_SAMPLES:
            await message.answer(self._samples_text())
        elif text == BTN_MODE:
            await message.answer(
                f"Режим: <b>{self.cfg.mode}</b> — {MODE_NAMES[self.cfg.mode]}"
                + ("\n⏸ Проверка приостановлена." if self.paused else ""),
                reply_markup=self._menu(),
            )
        elif text == BTN_HELP:
            await message.answer(self.HELP_TEXT)

    async def on_private_text(self, message: Message) -> None:
        """Разбирает сообщение, которое владелец прислал или переслал в личку.

        Так удобнее всего добавлять пропущенный спам: не нужно копировать
        текст в команду — достаточно переслать само сообщение.
        """
        if not self._is_owner(message):
            return
        text = message.text or message.caption or ""
        hidden = self._hidden_links(message)
        if not text.strip() and not hidden:
            await message.answer(
                "Пришлите текст или перешлите сюда сообщение из чата — "
                "разберу его и предложу запомнить как образец спама."
            )
            return

        verdict = self.filter.check(
            text,
            hidden_links=hidden,
            is_forwarded=message.forward_origin is not None,
            known_spam=self.store.match_sample(normalize(text)),
        )
        decision = {
            "none": "пропустил бы",
            "delete": "удалил бы без блокировки",
            "ban": "удалил бы и заблокировал",
        }[verdict.action]

        delete_at = self.filter.thresholds["delete"]
        gap = ""
        if verdict.action == "none":
            gap = f"\nНе хватило баллов: {delete_at - verdict.score}"

        # Автор пересланного сообщения виден, только если он не скрыл профиль.
        origin = message.forward_origin
        author_id = getattr(getattr(origin, "sender_user", None), "id", None)

        rows = [[InlineKeyboardButton(
            text="🚫 Запомнить как спам", callback_data="remember_text"
        )]]
        if author_id and not self.is_protected(author_id, set()):
            rows.append([InlineKeyboardButton(
                text="🔨 Заблокировать автора", callback_data=f"banuser:{author_id}"
            )])
        self._pending_sample = text

        await message.answer(
            f"Баллы: <b>{verdict.score}</b> из {delete_at} нужных\n"
            f"Решение: <b>{decision}</b>{gap}\n"
            f"Признаки: {html.escape(verdict.reason_text)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # ------------------------------------------------------------- кнопки

    async def on_button(self, call: CallbackQuery) -> None:
        if self.cfg.admin_id is None or call.from_user.id != self.cfg.admin_id:
            await call.answer("Недоступно", show_alert=True)
            return
        data = call.data or ""

        if data == "remember_text":
            text = getattr(self, "_pending_sample", "")
            if not text:
                await call.answer("Текст потерялся, пришлите заново", show_alert=True)
                return
            added = self.store.add_sample(normalize(text), text)
            await call.answer("Запомнил" if added else "Такой образец уже есть")
            if added:
                await call.message.answer(
                    "🚫 Образец сохранён. Похожие сообщения теперь будут удаляться "
                    f"сразу.\n\nВсего образцов: {self.store.count_samples()}\n"
                    "Посмотреть: /samples"
                )
            else:
                await call.message.answer(
                    "Такой образец уже сохранён — похожие сообщения и так удаляются.\n"
                    "Если это сообщение всё равно проходит, пришлите его мне, "
                    "и я подскажу, какого признака не хватает."
                )
            return

        if data.startswith("banuser:"):
            user_id = int(data.split(":", 1)[1])
            lines = []
            for chat_id in self.cfg.chat_ids:
                try:
                    await self.bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
                    removed = await self._delete_previous(chat_id, user_id, 0)
                    lines.append(
                        f"Заблокирован в чате {chat_id}"
                        + (f", удалено сообщений: {removed}" if removed else "")
                    )
                except TelegramAPIError as e:
                    lines.append(f"Чат {chat_id}: {e}")
            await call.answer("Готово")
            await call.message.answer(html.escape("\n".join(lines)))
            return

        if data.startswith("remember:"):
            incident_id = int(data.split(":", 1)[1])
            raw = self.store.incident_text(incident_id)
            if not raw:
                await call.answer("Текст не сохранён", show_alert=True)
                return
            added = self.store.add_sample(normalize(raw), raw)
            await call.answer("Запомнил" if added else "Такой образец уже есть")
            await call.message.answer(
                ("🚫 Образец сохранён. Похожие сообщения теперь будут удаляться сразу, "
                 "даже если баллов не хватает.\n\n"
                 f"Всего образцов: {self.store.count_samples()}\n"
                 "Посмотреть: /samples"
                 ) if added else
                "Такой образец уже сохранён — похожие сообщения и так удаляются."
            )
            return

        # Кнопки под уведомлением о спаме
        if data.startswith(("unban:", "notspam:")):
            action, _, raw = data.partition(":")
            user_id = int(raw)
            lines = []
            for chat_id in self.cfg.chat_ids:
                try:
                    await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                    lines.append("Разблокирован в чате.")
                except TelegramAPIError as e:
                    lines.append(f"Разблокировать не удалось: {e}")
            if action == "notspam":
                self.cfg.whitelist_ids.add(user_id)
                update_env(
                    BASE_DIR / ".env", "WHITELIST_IDS",
                    ",".join(map(str, sorted(self.cfg.whitelist_ids))),
                )
                lines.append(f"Добавлен в белый список — больше его не трогаю.")
                lines.append(
                    "Если такие сообщения ловятся часто, поднимите порог: /threshold delete 5"
                )
            await call.answer("Готово")
            await call.message.answer(html.escape("\n".join(lines)))
            return

        if data.startswith("mode:"):
            text = await self._set_mode(data.split(":", 1)[1])
        elif data == "pause":
            self.paused = True
            text = "⏸ Проверка приостановлена. Бот запущен, но ничего не проверяет."
        elif data == "resume":
            self.paused = False
            text = "▶️ Проверка возобновлена."
        elif data == "restart":
            await call.answer("Перезапускаюсь…")
            await call.message.answer("🔄 Перезапускаюсь, вернусь через несколько секунд…")
            asyncio.get_running_loop().call_later(1, lambda: os._exit(0))
            return
        else:
            await call.answer()
            return

        await call.answer()
        try:
            await call.message.edit_text(text, reply_markup=self._menu())
        except TelegramAPIError:
            # Текст не изменился — Telegram отклоняет правку, это не ошибка.
            pass

    # --------------------------------------------------------- самоконтроль

    async def _check_rights(self) -> None:
        """Проверяет, что боту не урезали права в чате."""
        if self._me_id is None:
            return
        if not self.cfg.chat_ids:
            # Без списка чатов проверять нечего — предупредим об этом один раз.
            await self._warn_once(
                "no-chat-ids",
                "⚠️ Не задан CHAT_IDS: бот работает во всех группах, куда его "
                "добавили, и не может следить за своими правами.\n"
                "Напишите /id в нужном чате и впишите его ID в настройки.",
            )
            return
        for chat_id in self.cfg.chat_ids:
            try:
                me = await self.bot.get_chat_member(chat_id, self._me_id)
            except TelegramAPIError as e:
                await self._warn_once(f"rights:{chat_id}", f"Не вижу чат {chat_id}: {e}")
                continue
            if me.status != "administrator":
                await self._warn_once(
                    f"rights:{chat_id}",
                    f"⚠️ Бот больше не администратор в чате {chat_id} — "
                    "удалять спам не сможет. Верните права в настройках чата.",
                )
                continue
            missing = []
            if not getattr(me, "can_delete_messages", False):
                missing.append("удаление сообщений")
            if not getattr(me, "can_restrict_members", False):
                missing.append("блокировка пользователей")
            try:
                chat = await self.bot.get_chat(chat_id)
                self._native_antispam = chat.has_aggressive_anti_spam_enabled
            except TelegramAPIError:
                pass
            if missing:
                await self._warn_once(
                    f"rights:{chat_id}",
                    f"⚠️ Боту не хватает прав в чате {chat_id}: {', '.join(missing)}.\n"
                    "Включите их в настройках чата → Администраторы → Модератор.",
                )
            else:
                self._warned.pop(f"rights:{chat_id}", None)

    async def _warn_once(self, key: str, text: str) -> None:
        """Одно и то же предупреждение — не чаще раза в 6 часов."""
        now = time.time()
        if now - self._warned.get(key, 0.0) < 6 * 3600:
            return
        self._warned[key] = now
        log.warning(text)
        await self._notify(text)

    async def _background(self) -> None:
        """Раз в час: контроль прав, чистка журнала, дневная сводка."""
        sent_on: str | None = None
        while True:
            try:
                await self._check_rights()
                self.store.cleanup_old(self.cfg.log_retention_days)

                if self.cfg.daily_report_hour is not None:
                    now = time.localtime()
                    today = time.strftime("%Y-%m-%d", now)
                    if now.tm_hour == self.cfg.daily_report_hour and sent_on != today:
                        sent_on = today
                        await self._notify(
                            f"☀️ Бот работает. Режим: <b>{self.cfg.mode}</b>\n\n"
                            + self._stats_text()
                        )
            except Exception as e:  # noqa: BLE001 — фоновая задача не должна падать
                log.warning("Сбой фоновой задачи: %s", e)
            await asyncio.sleep(3600)

    # ------------------------------------------------------------- запуск

    async def run(self) -> None:
        me = await self.bot.me()
        self._me_id = me.id
        log.info("Запущен как @%s, режим=%s", me.username, self.cfg.mode)
        if self.cfg.admin_id is not None:
            try:
                # Список команд виден только владельцу — в группе бот молчит.
                await self.bot.set_my_commands(
                    BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=self.cfg.admin_id)
                )
            except TelegramAPIError as e:
                log.warning("Не удалось задать список команд: %s", e)
        await self._notify(f"✅ Бот запущен. Режим: <b>{self.cfg.mode}</b>")
        asyncio.create_task(self._background())
        await self.dp.start_polling(
            self.bot,
            # Без callback_query Telegram не присылает нажатия кнопок меню.
            allowed_updates=["message", "edited_message", "callback_query"],
            handle_signals=True,
        )


async def _main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    moderator = Moderator(cfg)
    while True:
        try:
            await moderator.run()
            return
        except (TelegramRetryAfter, TelegramAPIError, OSError) as e:
            # Сеть или Telegram недоступны — ждём и пробуем снова.
            delay = getattr(e, "retry_after", 15)
            log.warning("Сбой связи (%s). Повтор через %s с.", e, delay)
            await asyncio.sleep(delay)


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
