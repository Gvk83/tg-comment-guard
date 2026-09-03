"""Сохранение настроек, сделанных из Telegram, чтобы они пережили перезапуск.

Режим, белый список и прочие переключатели пишутся в .env.
Добавленные из чата правила — в отдельный файл data/custom_rules.yml,
чтобы обновление кода их не затирало.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def update_env(env_path: Path, key: str, value: str) -> None:
    """Меняет одну строку в .env, сохраняя комментарии и порядок."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_custom_rules(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_custom_rules(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def add_phrase(path: Path, group: str, phrase: str) -> bool:
    """Добавляет фразу в группу правил. False, если она уже есть."""
    data = load_custom_rules(path)
    patterns = data.setdefault("patterns", {})
    words = patterns.setdefault(group, [])
    if phrase in words:
        return False
    words.append(phrase)
    save_custom_rules(path, data)
    return True


def remove_phrase(path: Path, phrase: str) -> list[str]:
    """Убирает фразу из всех групп. Возвращает группы, где она нашлась."""
    data = load_custom_rules(path)
    removed = []
    for group, words in (data.get("patterns") or {}).items():
        if phrase in words:
            words.remove(phrase)
            removed.append(group)
    if removed:
        save_custom_rules(path, data)
    return removed


def set_threshold(path: Path, name: str, value: int) -> None:
    data = load_custom_rules(path)
    data.setdefault("thresholds", {})[name] = value
    save_custom_rules(path, data)
