# -*- coding: utf-8 -*-
"""Проверки спам-фильтра, хранилища и защиты администраторов."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.normalize import normalize, squash  # noqa: E402
from bot.spamfilter import SpamFilter  # noqa: E402
from bot.storage import Storage  # noqa: E402
from tests.samples import (  # noqa: E402
    CLEAN,
    CLEAN_TRICKY,
    SPAM,
    SPAM_MISSED,
    SPAM_NEW_WORDING,
    SPAM_OBFUSCATED,
)


@pytest.fixture(scope="module")
def spam_filter():
    return SpamFilter()


@pytest.mark.parametrize("text", SPAM, ids=range(len(SPAM)))
def test_real_spam_is_caught(spam_filter, text):
    verdict = spam_filter.check(text)
    assert verdict.action != "none", f"пропущен спам ({verdict.score}): {text[:60]}"


@pytest.mark.parametrize("text", SPAM_OBFUSCATED, ids=range(len(SPAM_OBFUSCATED)))
def test_obfuscated_spam_is_caught(spam_filter, text):
    assert spam_filter.check(text).action == "ban"


@pytest.mark.parametrize("text", CLEAN, ids=range(len(CLEAN)))
def test_clean_messages_survive(spam_filter, text):
    verdict = spam_filter.check(text)
    assert verdict.action == "none", f"ложное срабатывание ({verdict.reason_text}): {text[:60]}"


def test_single_keyword_is_not_enough(spam_filter):
    for text in ("Работа", "Деньги", "Доход", "Хорошая работа у вас"):
        assert spam_filter.check(text).action == "none"


def test_amount_alone_is_not_spam(spam_filter):
    assert spam_filter.check("Отдал за ремонт 45000 рублей").action == "none"


def test_zero_width_and_homoglyphs_are_normalized():
    assert squash(normalize("Р​А​Б​О​Т​А")) == "работа"
    assert squash(normalize("рaб0тa")) == "работа"
    assert normalize("  много   пробелов  ") == "много пробелов"


def test_duplicate_raises_score(spam_filter):
    text = "Ищем помощника на объект, оплата достойная, обращайтесь"
    plain = spam_filter.check(text).score
    with_dup = spam_filter.check(text, is_duplicate=True).score
    assert with_dup > plain


def test_reply_to_user_lowers_score(spam_filter):
    text = "Работа есть, пишите"
    assert spam_filter.check(text, is_reply_to_user=True).score < spam_filter.check(text).score


def test_thresholds_are_ordered(spam_filter):
    assert spam_filter.thresholds["ban"] > spam_filter.thresholds["delete"]


# ------------------------------------------------------------------ хранилище


def test_storage_duplicates_and_new_members(tmp_path):
    store = Storage(tmp_path / "t.db")
    text = "одинаковое сообщение достаточной длины для проверки"
    assert store.check_duplicate(text, user_id=1) is False
    assert store.check_duplicate(text, user_id=1) is False   # тот же автор — не дубль
    assert store.check_duplicate(text, user_id=2) is True    # другой автор — дубль

    assert store.is_new_member(-100, 555) is True
    assert store.is_new_member(-100, 555) is False
    store.close()


def test_storage_does_not_keep_text_by_default(tmp_path):
    store = Storage(tmp_path / "t.db", store_text=False)
    store.log_incident(
        chat_id=-100, user_id=1, score=9, reason="тест", action="ban",
        deleted=True, banned=True, text="секретный текст",
    )
    rows = store.db.execute("SELECT sample FROM incidents").fetchall()
    assert rows == [(None,)]
    assert store.stats(7) == (1, 1, 1)
    store.close()


def test_storage_purge_clears_samples(tmp_path):
    store = Storage(tmp_path / "t.db", store_text=True)
    store.log_incident(
        chat_id=-100, user_id=1, score=9, reason="тест", action="ban",
        deleted=True, banned=True, text="сохранённый текст",
    )
    assert store.purge_samples() == 1
    assert store.db.execute("SELECT sample FROM incidents").fetchone() == (None,)
    store.close()


# --------------------------------------------------- защита администраторов


def test_admins_and_whitelist_are_protected(tmp_path, monkeypatch):
    from bot.config import Config
    from bot.main import Moderator

    cfg = Config(
        token="1:test", mode="enforce", admin_id=777,
        whitelist_ids={42}, db_path=tmp_path / "t.db",
    )
    monkeypatch.setattr("bot.main.Bot", lambda *a, **kw: object())
    moderator = Moderator.__new__(Moderator)
    moderator.cfg = cfg
    moderator._me_id = 999

    assert moderator.is_protected(777, set()) is True    # владелец
    assert moderator.is_protected(999, set()) is True    # сам бот
    assert moderator.is_protected(42, set()) is True     # белый список
    assert moderator.is_protected(5, {5, 6}) is True     # админ чата
    assert moderator.is_protected(123, {5, 6}) is False  # обычный участник


def test_modes_control_actions():
    from bot.config import Config

    assert Config(token="1:t", mode="observe").may_delete is False
    assert Config(token="1:t", mode="observe").may_ban is False
    assert Config(token="1:t", mode="soft").may_delete is True
    assert Config(token="1:t", mode="soft").may_ban is False
    assert Config(token="1:t", mode="enforce").may_ban is True


def test_service_message_deletion_flag():
    from bot.config import Config

    assert Config(token="1:t").delete_service_messages is True
    assert Config(token="1:t", delete_service_messages=False).delete_service_messages is False


def test_service_handler_is_registered_before_spam_handler():
    """Служебные сообщения должны разбираться раньше обычной проверки текста."""
    from bot.config import Config
    from bot.main import Moderator

    moderator = Moderator.__new__(Moderator)
    moderator.cfg = Config(token="1:t")
    moderator._me_id = 1
    names = []
    from aiogram import Dispatcher
    moderator.dp = Dispatcher()
    moderator._register()
    for handler in moderator.dp.message.handlers:
        names.append(handler.callback.__name__)
    assert "on_service_message" in names
    assert names.index("on_service_message") < names.index("on_group_message")


# ------------------------------------------------- настройки из Telegram


def test_update_env_replaces_only_target_line(tmp_path):
    from bot.settings import update_env

    env = tmp_path / ".env"
    env.write_text("# комментарий\nMODE=observe\nADMIN_ID=1\n", encoding="utf-8")
    update_env(env, "MODE", "enforce")
    text = env.read_text(encoding="utf-8")
    assert "MODE=enforce" in text
    assert "ADMIN_ID=1" in text
    assert "# комментарий" in text

    update_env(env, "NEW_KEY", "value")   # ключа нет — добавляется
    assert "NEW_KEY=value" in env.read_text(encoding="utf-8")


def test_custom_rules_extend_builtin_ones(tmp_path):
    from bot.settings import add_phrase, set_threshold
    from bot.spamfilter import SpamFilter

    custom = tmp_path / "custom_rules.yml"
    text = "Свяжитесь со мной, отдам 12000 за помощь"

    assert SpamFilter(custom_path=custom).check(text).action == "none"

    add_phrase(custom, "contact_cta", "свяжитесь со мной")
    assert SpamFilter(custom_path=custom).check(text).action != "none"

    set_threshold(custom, "ban", 99)
    assert SpamFilter(custom_path=custom).thresholds["ban"] == 99


def test_remove_phrase_reverts_change(tmp_path):
    from bot.settings import add_phrase, remove_phrase
    from bot.spamfilter import SpamFilter

    custom = tmp_path / "custom_rules.yml"
    add_phrase(custom, "contact_cta", "свяжитесь со мной")
    assert remove_phrase(custom, "свяжитесь со мной") == ["contact_cta"]
    assert remove_phrase(custom, "свяжитесь со мной") == []
    assert "свяжитесь со мной" not in SpamFilter(custom_path=custom).patterns["contact_cta"]


def test_recent_returns_newest_first(tmp_path):
    store = Storage(tmp_path / "t.db")
    for score in (5, 9):
        store.log_incident(
            chat_id=-100, user_id=score, score=score, reason="тест",
            action="ban", deleted=True, banned=False,
        )
    rows = store.recent(10)
    assert len(rows) == 2
    assert rows[0][2] == 9    # сначала последнее
    store.close()


def test_all_owner_commands_are_registered():
    from bot.config import Config
    from bot.main import Moderator
    from aiogram import Dispatcher

    moderator = Moderator.__new__(Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()
    names = {h.callback.__name__ for h in moderator.dp.message.handlers}
    for expected in (
        "cmd_start", "cmd_status", "cmd_mode", "cmd_pause", "cmd_resume",
        "cmd_restart", "cmd_last", "cmd_whitelist", "cmd_unban",
        "cmd_addword", "cmd_delword", "cmd_threshold", "cmd_service",
    ):
        assert expected in names, f"команда {expected} не зарегистрирована"
    assert moderator.dp.callback_query.handlers, "кнопки меню не подключены"


# ----------------------------------------------- ссылки, пересылки, профиль


def test_hidden_link_is_detected(spam_filter):
    """Ссылка, спрятанная под словом «тут», раньше не проверялась вообще."""
    v = spam_filter.check("Подробности тут", hidden_links=["https://t.me/scamchannel"])
    assert v.action != "none"
    assert "скрытая ссылка" in v.reason_text


def test_hidden_link_without_text(spam_filter):
    """Картинка без подписи, но со спрятанной ссылкой на Telegram."""
    assert spam_filter.check("", hidden_links=["https://t.me/scam"]).action != "none"


def test_ordinary_hidden_link_is_not_enough(spam_filter):
    """Обычная ссылка на статью под текстом — не повод удалять."""
    v = spam_filter.check("Смотрите тут интересная статья", hidden_links=["https://habr.com/x"])
    assert v.action == "none"


def test_spam_in_profile_name_counts(spam_filter):
    text = "Всем привет"
    assert spam_filter.check(text).action == "none"
    assert spam_filter.check(text, spam_name=True).score > spam_filter.check(text).score


def test_forwarded_message_raises_score(spam_filter):
    text = "Ищем сотрудников, оплата 7000 за смену"
    assert spam_filter.check(text, is_forwarded=True).score > spam_filter.check(text).score


# ------------------------------------------------ журнал и фоновая уборка


def test_cleanup_removes_old_incidents(tmp_path):
    import time as _time

    store = Storage(tmp_path / "t.db")
    store.log_incident(
        chat_id=-100, user_id=1, score=9, reason="старое", action="ban",
        deleted=True, banned=True,
    )
    store.db.execute("UPDATE incidents SET ts = ?", (int(_time.time()) - 200 * 86400,))
    store.db.commit()
    store.log_incident(
        chat_id=-100, user_id=2, score=9, reason="свежее", action="ban",
        deleted=True, banned=True,
    )
    assert store.cleanup_old(90) == 1
    rows = store.recent(10)
    assert len(rows) == 1 and rows[0][3] == "свежее"
    assert store.cleanup_old(0) == 0   # 0 — хранить бессрочно
    store.close()


def test_daily_report_hour_parsing(monkeypatch, tmp_path):
    from bot.config import load_config

    env = tmp_path / ".env"
    for raw, expected in (("10", 10), ("выкл", None), ("", None), ("25", 1)):
        env.write_text(
            f"BOT_TOKEN=1:test\nDAILY_REPORT_HOUR={raw}\n", encoding="utf-8"
        )
        monkeypatch.delenv("DAILY_REPORT_HOUR", raising=False)
        assert load_config(env).daily_report_hour == expected, raw


def test_newcomer_media_flag_and_command_registered():
    from bot.config import Config
    from bot.main import Moderator
    from aiogram import Dispatcher

    assert Config(token="1:t").block_newcomer_media is False
    moderator = Moderator.__new__(Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()
    names = {h.callback.__name__ for h in moderator.dp.message.handlers}
    assert "cmd_media" in names


def test_polling_subscribes_to_button_presses():
    """Без callback_query в allowed_updates кнопки меню молча не работают."""
    import inspect
    from bot import main as bot_main

    source = inspect.getsource(bot_main.Moderator.run)
    assert "callback_query" in source, "бот не подписан на нажатия кнопок"
    assert "edited_message" in source


# ---------------------------------------------------- образцы спама


def test_sample_matches_similar_message(tmp_path):
    from bot.normalize import normalize

    store = Storage(tmp_path / "t.db")
    original = "Работа на дому, 5000 в день, пиши в лс"
    assert store.add_sample(normalize(original), original) is True

    # Цифры поменялись — образец всё равно узнаётся.
    assert store.match_sample(normalize("Работа на дому, 7000 в день, пиши в лс")) is True
    # Совсем другой текст — нет.
    assert store.match_sample(normalize("Спасибо за статью, было очень интересно читать")) is False
    # Повторно тот же образец не добавляется.
    assert store.add_sample(normalize("Работа на дому, 6000 в день, пиши в лс"), "x") is False
    store.close()


def test_short_text_is_not_stored_as_sample(tmp_path):
    store = Storage(tmp_path / "t.db")
    assert store.add_sample("привет", "привет") is False
    assert store.count_samples() == 0
    store.close()


def test_known_sample_triggers_ban(spam_filter):
    """Сообщение без явных признаков, но совпавшее с образцом, удаляется."""
    text = "Интересное предложение для вас, подробности внутри"
    assert spam_filter.check(text).action == "none"
    assert spam_filter.check(text, known_spam=True).action == "ban"


def test_samples_can_be_listed_and_deleted(tmp_path):
    from bot.normalize import normalize

    store = Storage(tmp_path / "t.db")
    text = "Ищем сотрудников на склад, оплата 6000 за смену, пишите"
    store.add_sample(normalize(text), text)
    rows = store.list_samples()
    assert len(rows) == 1
    sample_id = rows[0][0]
    assert store.delete_sample(sample_id) is True
    assert store.delete_sample(sample_id) is False
    assert store.count_samples() == 0
    store.close()


def test_incident_text_is_saved_and_readable(tmp_path):
    store = Storage(tmp_path / "t.db", store_text=True)
    incident_id = store.log_incident(
        chat_id=-100, user_id=1, score=9, reason="тест", action="ban",
        deleted=True, banned=True, text="текст спама",
    )
    assert isinstance(incident_id, int)
    assert store.incident_text(incident_id) == "текст спама"
    assert store.recent(1)[0][6] == "текст спама"
    store.close()


def test_last_marks_records_without_text(tmp_path):
    """Записи, сделанные до включения хранения, не должны выглядеть поломкой."""
    from bot.config import Config
    from bot.main import Moderator

    moderator = Moderator.__new__(Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.store = Storage(tmp_path / "t.db", store_text=False)
    moderator.store.log_incident(
        chat_id=-100, user_id=1, score=9, reason="тест", action="ban",
        deleted=True, banned=False, text="этот текст не сохранится",
    )
    assert "текст не сохранён" in moderator._last_text()
    moderator.store.close()


# ------------------------------------------------- удобство управления


def test_reply_keyboard_buttons_are_handled():
    """Каждая постоянная кнопка должна иметь обработчик."""
    from bot import main as bot_main
    from bot.config import Config
    from aiogram import Dispatcher

    moderator = bot_main.Moderator.__new__(bot_main.Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()
    names = [h.callback.__name__ for h in moderator.dp.message.handlers]
    assert "on_reply_button" in names
    # Кнопки разбираются раньше, чем сообщение попадёт в общий обработчик.
    assert names.index("on_reply_button") < len(names)

    import inspect
    source = inspect.getsource(bot_main.Moderator.on_reply_button)
    for name in ("BTN_STATUS", "BTN_STATS", "BTN_LAST", "BTN_SAMPLES",
                 "BTN_MODE", "BTN_HELP"):
        assert name in source, f"кнопка {name} не обрабатывается"

    # Все кнопки клавиатуры должны быть среди обработанных.
    on_keyboard = {
        b.text for row in bot_main.REPLY_KEYBOARD.keyboard for b in row
    }
    assert on_keyboard == {
        bot_main.BTN_STATUS, bot_main.BTN_STATS, bot_main.BTN_LAST,
        bot_main.BTN_SAMPLES, bot_main.BTN_MODE, bot_main.BTN_HELP,
    }


def test_bot_commands_match_registered_handlers():
    """Список для кнопки «Меню» не должен обещать несуществующих команд."""
    from bot import main as bot_main
    from bot.config import Config
    from aiogram import Dispatcher

    moderator = bot_main.Moderator.__new__(bot_main.Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()

    registered = set()
    for handler in moderator.dp.message.handlers:
        for flt in handler.filters or ():
            commands = getattr(flt.callback, "commands", None)
            if commands:
                registered.update(commands)

    for command in bot_main.BOT_COMMANDS:
        assert command.command in registered, f"/{command.command} нет среди обработчиков"


def test_menu_does_not_duplicate_keyboard():
    """Кнопки в сообщении и под полем ввода не должны повторять друг друга."""
    from bot import main as bot_main
    from bot.config import Config

    moderator = bot_main.Moderator.__new__(bot_main.Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.paused = False

    inline = {
        b.text for row in moderator._menu().inline_keyboard for b in row
    }
    keyboard = {
        b.text for row in bot_main.REPLY_KEYBOARD.keyboard for b in row
    }

    def words(texts):
        return {t.split(" ", 1)[-1].replace(" ✅", "") for t in texts}

    assert not (words(inline) & words(keyboard)), (
        f"кнопки дублируются: {words(inline) & words(keyboard)}"
    )
    # В сообщении остаются только действия.
    assert words(inline) == {
        "Наблюдение", "Мягкий", "Боевой", "Приостановить", "Перезапустить",
    }


def test_several_chats_are_supported():
    """Один бот может обслуживать несколько чатов комментариев."""
    from bot.config import Config, _ids

    ids = _ids("-1001111111111, -1002222222222,-1003333333333")
    assert ids == {-1001111111111, -1002222222222, -1003333333333}

    cfg = Config(token="1:t", chat_ids=ids)
    # Все три чата обслуживаются, посторонний — нет.
    for chat_id in ids:
        assert chat_id in cfg.chat_ids
    assert -1009999999999 not in cfg.chat_ids


def test_anonymous_admin_and_linked_channel_are_protected(tmp_path):
    """Анонимный админ пишет от имени группы, канал — от имени канала.

    В списке администраторов их нет, поэтому нужна отдельная проверка.
    """
    from bot.config import Config
    from bot.main import Moderator

    moderator = Moderator.__new__(Moderator)
    moderator.cfg = Config(token="1:t", admin_id=777, whitelist_ids={42})
    moderator._me_id = 999

    chat_id, linked_id = -1001111111111, -1002222222222

    # Анонимный админ: from_user = @GroupAnonymousBot, sender_chat = сама группа
    assert moderator.is_protected(
        1087968824, set(), sender_chat_id=chat_id,
        chat_id=chat_id, linked_chat_id=linked_id,
    ) is True

    # Сообщение от имени связанного канала
    assert moderator.is_protected(
        None, set(), sender_chat_id=linked_id,
        chat_id=chat_id, linked_chat_id=linked_id,
    ) is True

    # Служебный аккаунт Telegram
    assert moderator.is_protected(777000, set()) is True

    # А вот спам от имени ПОСТОРОННЕГО канала защищать не надо
    assert moderator.is_protected(
        None, set(), sender_chat_id=-1009999999999,
        chat_id=chat_id, linked_chat_id=linked_id,
    ) is False

    # Обычный участник по-прежнему проверяется
    assert moderator.is_protected(
        123, {5, 6}, sender_chat_id=None,
        chat_id=chat_id, linked_chat_id=linked_id,
    ) is False


# ------------------------------------- проверка на новых формулировках


@pytest.mark.parametrize("text", SPAM_NEW_WORDING, ids=range(len(SPAM_NEW_WORDING)))
def test_spam_with_unseen_wording(spam_filter, text):
    """Фильтр не должен быть заточен под конкретные присланные тексты."""
    verdict = spam_filter.check(text)
    assert verdict.action != "none", f"пропущен спам ({verdict.score}): {text[:60]}"


@pytest.mark.parametrize("text", CLEAN_TRICKY, ids=range(len(CLEAN_TRICKY)))
def test_tricky_clean_messages_survive(spam_filter, text):
    """Личные объявления и рассказы о покупках — не спам."""
    verdict = spam_filter.check(text)
    assert verdict.action == "none", f"ложное срабатывание ({verdict.reason_text}): {text[:60]}"


def test_word_boundaries_are_respected(spam_filter):
    """«заработался» — не «работа», «на смену масла» — не предложение работы."""
    for text in (
        "Я вчера заработался до ночи",
        "Пора на смену масла записаться",
        "Переработка отходов — интересная тема",
        "Подработаю дома над проектом",
    ):
        v = spam_filter.check(text)
        assert "предложение работы" not in v.reason_text, f"{text}: {v.reason_text}"


def test_plain_link_is_not_a_context_signal(spam_filter):
    """Обычная ссылка не должна снимать защиту от ложных срабатываний."""
    v = spam_filter.check("Отдал за ремонт 45000 рублей, вот сайт https://remont.ru")
    assert v.action == "none", v.reason_text
    # А ссылка на Telegram-аккаунт — признак объявления.
    v = spam_filter.check("Ищем сотрудников, 8000 за смену, пиши @somebody")
    assert v.action == "ban"


def test_squashed_search_only_for_obfuscated_text():
    """Сжатый поиск включается только при подозрении на обход фильтра."""
    from bot.normalize import looks_obfuscated, normalize

    for text in ("Я вчера заработался до ночи", "Обычный текст про работу"):
        assert looks_obfuscated(text, normalize(text)) is False
    for text in ("р а б о т а удаленно", "рaб0тa без опыта", "Р\u200bА\u200bБ\u200bО\u200bТ\u200bА"):
        assert looks_obfuscated(text, normalize(text)) is True


def test_rights_check_warns_when_no_chats_configured():
    """Раньше самоконтроль прав молча ничего не делал при пустом CHAT_IDS."""
    import inspect
    from bot import main as bot_main

    source = inspect.getsource(bot_main.Moderator._check_rights)
    assert "if not self.cfg.chat_ids" in source, "нет ветки для пустого списка чатов"
    assert "_warn_once" in source


def test_env_example_lists_every_setting():
    """Каждая настройка из конфигурации должна быть описана в .env.example."""
    from pathlib import Path

    env = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for key in (
        "BOT_TOKEN", "CHAT_IDS", "MODE", "NOTIFY_ADMIN", "ADMIN_ID",
        "WHITELIST_IDS", "DELETE_SERVICE_MESSAGES", "STORE_MESSAGE_TEXT",
        "DAILY_REPORT_HOUR", "LOG_RETENTION_DAYS", "BLOCK_NEWCOMER_MEDIA",
        "DB_PATH", "LOG_LEVEL",
    ):
        assert f"{key}=" in env, f"{key} не описан в .env.example"


# ------------------------------ пропущенное на боевом канале


@pytest.mark.parametrize("text", SPAM_MISSED, ids=range(len(SPAM_MISSED)))
def test_previously_missed_spam_is_caught(spam_filter, text):
    """Эти сообщения бот пропустил в реальном чате — больше не должен."""
    verdict = spam_filter.check(text)
    assert verdict.action != "none", f"снова пропущено ({verdict.score}): {text[:60]}"


def test_code_word_request_is_case_sensitive(spam_filter):
    """«Пиши СКЛАД» — приём вербовки. «пишите в личку» — обычная просьба."""
    assert "кодовое слово" in spam_filter.check("Есть работа. Пиши «СКЛАД».").reason_text
    assert "кодовое слово" in spam_filter.check("Работа есть, напиши ПЛЮС").reason_text
    assert "кодовое слово" not in spam_filter.check("Пишите в личку, отвечу").reason_text
    assert "кодовое слово" not in spam_filter.check("Напишите мне пожалуйста").reason_text


def test_payment_per_period_is_enough_context(spam_filter):
    """«4300 ₽ за день» — объявление, даже без слов из словаря."""
    v = spam_filter.check("Помощник кладовщика. 4300 ₽ за день. Есть обучение.")
    assert v.action != "none"
    # А обычная фраза с суммой без периода — не спам.
    assert spam_filter.check("Заплатил за ремонт 45000 рублей").action == "none"


def test_message_ids_are_remembered_for_cleanup(tmp_path):
    """Номера сообщений хранятся без текста, чтобы удалить их при бане."""
    store = Storage(tmp_path / "t.db")
    for message_id in (11, 12, 13):
        store.remember_message(-100, 55, message_id)
    store.remember_message(-100, 66, 20)

    assert store.user_message_ids(-100, 55) == [13, 12, 11]
    assert store.user_message_ids(-100, 66) == [20]

    # В таблице только номера — восстановить текст нельзя.
    columns = [c[1] for c in store.db.execute("PRAGMA table_info(user_messages)")]
    assert "text" not in columns and "sample" not in columns

    store.forget_user_messages(-100, 55)
    assert store.user_message_ids(-100, 55) == []
    assert store.user_message_ids(-100, 66) == [20]
    store.close()


def test_ban_deletes_previous_messages():
    """При бане бот должен убирать и прошлые сообщения спамера."""
    import inspect
    from bot import main as bot_main

    source = inspect.getsource(bot_main.Moderator.on_group_message)
    assert "_delete_previous" in source
    assert "revoke_messages=True" in source
    assert "remember_message" in source


# --------------------------- добавление образца пересылкой


def test_forwarded_message_handler_is_registered_last():
    """Разбор пересылок не должен перехватывать команды и кнопки."""
    from bot import main as bot_main
    from bot.config import Config
    from aiogram import Dispatcher

    moderator = bot_main.Moderator.__new__(bot_main.Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()
    names = [h.callback.__name__ for h in moderator.dp.message.handlers]

    assert "on_private_text" in names
    # Все команды и кнопки разбираются раньше.
    last = names.index("on_private_text")
    for earlier in ("cmd_start", "cmd_status", "cmd_spam", "on_reply_button"):
        assert names.index(earlier) < last, f"{earlier} перехватывается разбором текста"
    # А сообщения из группы он не трогает вовсе.
    assert names.index("on_group_message") > last or "on_group_message" in names


def test_sample_buttons_are_wired():
    """Кнопки «Запомнить как спам» и «Заблокировать автора» обработаны."""
    import inspect
    from bot import main as bot_main

    handler = inspect.getsource(bot_main.Moderator.on_button)
    assert '"remember_text"' in handler
    assert '"banuser:"' in handler

    sender = inspect.getsource(bot_main.Moderator.on_private_text)
    assert "remember_text" in sender
    assert "forward_origin" in sender
    # Защищённых пользователей блокировать не предлагаем.
    assert "is_protected" in sender


def test_manual_covers_every_command():
    """Руководство должно описывать все команды, которые есть в боте."""
    import re
    from pathlib import Path
    from aiogram import Dispatcher
    from bot import main as bot_main
    from bot.config import Config

    manual = (Path(__file__).resolve().parent.parent / "РУКОВОДСТВО.md").read_text(
        encoding="utf-8"
    )

    moderator = bot_main.Moderator.__new__(bot_main.Moderator)
    moderator.cfg = Config(token="1:t")
    moderator.dp = Dispatcher()
    moderator._register()

    registered = set()
    for handler in moderator.dp.message.handlers:
        for flt in handler.filters or ():
            registered.update(getattr(flt.callback, "commands", None) or ())

    described = set(re.findall(r"`/(\w+)", manual))
    missing = registered - described - {"wl", "help"}   # /wl и /help — синонимы
    assert not missing, f"не описаны в руководстве: {sorted(missing)}"


def test_manual_covers_every_setting_and_rule_group():
    """И все настройки, и все группы правил."""
    import re
    import yaml
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent
    manual = (base / "РУКОВОДСТВО.md").read_text(encoding="utf-8")

    settings = set(re.findall(r"^([A-Z_]+)=", (base / ".env.example").read_text(encoding="utf-8"), re.M))
    assert not settings - set(re.findall(r"`([A-Z_]+)`", manual)), (
        f"не описаны настройки: {sorted(settings - set(re.findall(r'`([A-Z_]+)`', manual)))}"
    )

    rules = yaml.safe_load((base / "bot" / "rules.yml").read_text(encoding="utf-8"))
    groups = set(rules["patterns"]) - {"contact_cta_standalone"}
    described = set(re.findall(r"`(\w+)`", manual))
    assert not groups - described, f"не описаны группы правил: {sorted(groups - described)}"
