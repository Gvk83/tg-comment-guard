"""Локальный спам-фильтр: оценивает сообщение по сумме признаков.

Ни одно правило само по себе не приводит к блокировке — решение принимается
только по сочетанию признаков. Все веса и словари лежат в rules.yml.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .normalize import normalize, squash

RULES_PATH = Path(__file__).with_name("rules.yml")

# Суммы денег: «10 000», «4000», «16к», «5 900». Годы и время сюда не попадают.
MONEY_RE = re.compile(
    r"(?<![\d.,:/])"
    r"(?:(\d{1,3}(?:[   .]\d{3})+)"          # 10 000
    r"|(\d{1,3})\s?(?:к|тыс\.?|тысяч|k)(?![а-яa-z])"   # 16к
    r"|(\d{4,}))"                                       # 6300
    r"(?![\d.,]?\d)"
)

# Единицы измерения, после которых число деньгами не является.
NOT_MONEY_UNIT = re.compile(r"^\s*(?:км|км\.|кг|м2|м²|мл|гр|г|шт|штук|литр|мин|сек|год|года|лет)\b")

# Период оплаты рядом с суммой.
PER_PERIOD_RE = re.compile(
    r"(в день|за день|/\s?день|в смену|за смену|за выход|на руки|в час|за час|"
    r"за пару часов|в неделю|за неделю|в сутки|за сутки|ежедневно|день\b)"
)

TELEGRAM_LINK_RE = re.compile(r"(t\.me/|telegram\.me/|@[a-z0-9_]{4,})")
URL_RE = re.compile(r"https?://|www\.")
SCHEDULE_RE = re.compile(r"(\b[2-7]\s?/\s?[1-3]\b|\b\d{1,2}[:.]\d{2}\s?[-–—]\s?\d{1,2}[:.]\d{2})")

# «Пиши» как самостоятельный призыв: отдельной строкой или в самом конце.
STANDALONE_TAIL_RE = re.compile(r"(?:^|[\n.!,])\s*({words})\s*[.!)]*\s*$")


@dataclass
class Verdict:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    action: str = "none"  # none | delete | ban

    @property
    def reason_text(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "нет признаков"


class SpamFilter:
    def __init__(self, rules_path: Path = RULES_PATH, custom_path: Path | None = None):
        self.rules_path = rules_path
        self.custom_path = custom_path
        self.reload()

    def reload(self) -> None:
        with open(self.rules_path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        # Правила, добавленные из Telegram, дополняют основные.
        if self.custom_path and self.custom_path.exists():
            with open(self.custom_path, encoding="utf-8") as f:
                custom = yaml.safe_load(f) or {}
            rules["thresholds"].update(custom.get("thresholds") or {})
            for group, words in (custom.get("patterns") or {}).items():
                rules["patterns"].setdefault(group, []).extend(words)
        self.thresholds = rules["thresholds"]
        self.scores = rules["scores"]
        self.patterns = {k: [normalize(p) for p in v] for k, v in rules["patterns"].items()}
        self.context_required = set(rules["context_required"])
        self.max_without_context = rules["max_score_without_context"]
        self.safe_phrases = [normalize(p) for p in rules.get("safe_phrases", [])]
        self._standalone_re = re.compile(
            STANDALONE_TAIL_RE.pattern.format(
                words="|".join(re.escape(w) for w in self.patterns.get("contact_cta_standalone", []))
            ),
            re.MULTILINE,
        )

    # ------------------------------------------------------------------ поиск

    def _find(self, group: str, norm: str, sq: str) -> tuple[bool, bool]:
        """Возвращает (найдено, найдено_только_в_сжатом_виде).

        Второй флаг означает, что слово было намеренно искажено.
        """
        for phrase in self.patterns.get(group, ()):
            if phrase in norm:
                return True, False
        for phrase in self.patterns.get(group, ()):
            if squash(phrase) and squash(phrase) in sq:
                return True, True
        return False, False

    def _money(self, norm: str) -> tuple[int, bool]:
        """Максимальная найденная сумма и признак «сумма за период»."""
        best = 0
        per_period = False
        for m in MONEY_RE.finditer(norm):
            tail = norm[m.end():]
            if NOT_MONEY_UNIT.match(tail):
                continue
            grouped, thousands, plain = m.groups()
            if grouped:
                value = int(re.sub(r"\D", "", grouped))
            elif thousands:
                value = int(thousands) * 1000
            else:
                value = int(plain)
                if 1900 <= value <= 2100:  # похоже на год
                    continue
            if value < 1000:
                continue
            best = max(best, value)
            head = norm[max(0, m.start() - 25):m.start()]
            if PER_PERIOD_RE.search(tail[:25]) or PER_PERIOD_RE.search(head):
                per_period = True
        return best, per_period

    # ---------------------------------------------------------------- решение

    def check(
        self,
        text: str,
        *,
        is_duplicate: bool = False,
        is_new_member: bool = False,
        is_reply_to_user: bool = False,
        hidden_links: Iterable[str] = (),
        is_forwarded: bool = False,
        spam_name: bool = False,
        known_spam: bool = False,
    ) -> Verdict:
        v = Verdict()
        hidden_links = list(hidden_links)
        # Сообщение может быть без видимого текста — только картинка со ссылкой.
        norm = normalize(text) or normalize(" ".join(hidden_links))
        if not norm:
            return v
        sq = squash(norm)

        obfuscated = False
        hit_context = False

        def add(key: str, label: str) -> None:
            v.score += self.scores[key]
            v.reasons.append(label)

        # --- деньги
        amount, per_period = self._money(norm)
        if amount:
            if amount >= 10000:
                add("money_large", f"крупная сумма {amount}")
            else:
                add("money", f"сумма {amount}")
            if per_period:
                add("money_per_period", "оплата за период")

        # --- словарные группы
        for group, label in (
            ("job_offer", "предложение работы"),
            ("contact_cta", "призыв написать в личные"),
            ("easy_money", "обещание лёгкого дохода"),
            ("send_material", "предлагает прислать материал в личные"),
            ("age_bait", "«без опыта» / возрастная приманка"),
        ):
            found, only_squashed = self._find(group, norm, sq)
            if found:
                add(group, label)
                obfuscated = obfuscated or only_squashed
                if group in self.context_required:
                    hit_context = True

        # --- одиночное «Пиши» в конце сообщения
        if "призыв написать в личные" not in v.reasons and self._standalone_re.search(norm):
            add("contact_cta", "призыв «пиши» в конце сообщения")
            hit_context = True

        # --- ссылки и контакты
        if TELEGRAM_LINK_RE.search(norm) or URL_RE.search(norm):
            add("telegram_link", "ссылка или username")
            hit_context = True

        # Ссылка, спрятанная под текстом («подробности» с адресом внутри),
        # — приём, которым обычные участники почти не пользуются.
        for url in hidden_links:
            low = normalize(url)
            if TELEGRAM_LINK_RE.search(low):
                add("hidden_telegram_link", "скрытая ссылка на Telegram-аккаунт")
                hit_context = True
                break
            if URL_RE.search(low):
                add("hidden_link", "скрытая ссылка под текстом")
                hit_context = True
                break

        if is_forwarded:
            add("forwarded", "переслано из другого чата или канала")

        if spam_name:
            add("spam_name", "спам в имени или @username отправителя")
            hit_context = True

        # --- график работы
        if SCHEDULE_RE.search(norm):
            add("schedule", "рабочий график")

        # --- короткая заманка без деталей
        if len(norm) < 70 and self._find("easy_money", norm, sq)[0]:
            add("short_earn_pitch", "короткая заманка про заработок")

        if obfuscated:
            add("obfuscation", "искажённое написание слов")

        if known_spam:
            add("known_spam", "совпало с сохранённым образцом спама")
            hit_context = True

        if is_duplicate:
            add("duplicate", "повтор недавнего сообщения")
        if is_new_member:
            add("new_member", "первое сообщение участника")
        if is_reply_to_user:
            add("reply_to_user", "ответ участнику")
        if norm.rstrip().endswith("?"):
            add("question", "вопрос")

        # Без «контекстных» признаков одна лишь сумма спамом не считается.
        if not hit_context:
            v.score = min(v.score, self.max_without_context)

        v.score = max(v.score, 0)
        if v.score >= self.thresholds["ban"]:
            v.action = "ban"
        elif v.score >= self.thresholds["delete"]:
            v.action = "delete"
        return v
