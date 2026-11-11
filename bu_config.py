"""
bu_config.py — чтение и запись конфигурации backupUtil. Только стандартная библиотека.

Файлы (все лежат рядом со скриптами, в «домашней» папке утилиты):
  backup.conf          список директорий:  путь [ | имя [ | опции ] ]
  backup.env           настройки для backup.sh (VOLUME_SIZE, ZSTD_LEVEL, …) — подключается через `source`
  telegram.toml        api_id / api_hash / группа / сопоставление топиков / метки autoback (chmod 600)
  excludes.txt         ваши исключения для tar; если файла нет — используется excludes.default.txt

Модуль используется интерфейсом `tui.py` и автотестами; backup.sh и tg_upload.py читают те же файлы
независимо, поэтому формат здесь обязан совпадать с тем, что они разбирают.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Имена, которые нельзя использовать для папок архивов (совпадает с RESERVED в backup.sh)
RESERVED = {".secrets", ".state", ".venv", "__pycache__", ".git", ".github", "docs", "tests", "examples"}

ENV_DEFAULTS = {
    "VOLUME_SIZE": "1800M",
    "ZSTD_LEVEL": "3",
    "SPLIT": "1",
    "ONE_FS": "0",
    "AGE_RECIPIENT": "",
    "AGE_RECIPIENTS_FILE": "",
}
AUTOBACK_DEFAULTS = {
    "date_format": "%d.%m.%Y",
    "log_message": "{date} autoback#{n}",
    "dir_message": "autoback#{n}",
}
TG_VOLUME_LIMIT_MIB = 2000  # лимит Telegram для обычного аккаунта


# Имена папок, похожие на «секретные»: без шифрования такие архивы не стоит отправлять в Telegram.
# (Должно совпадать с SENSITIVE_RE в tg_upload.py — это проверяет тест.)
SENSITIVE_RE = re.compile(
    r"(ssh|gnupg|gpg|secret|token|passw|pass-|keys?\b|wallet|keepass|aws|kube|\.config|"
    r"mozilla|thunderbird|bitwarden|1password)", re.I)


def human_size(n: int) -> str:
    v = float(n)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if v < 1024 or unit == "ТиБ":
            return f"{v:.0f} {unit}" if unit == "Б" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} Б"


def norm(s: str) -> str:
    """Сравнение имён папок и топиков: без регистра, без ведущей точки, без пробелов/_/-.
    (Должно совпадать с norm() в tg_upload.py — это проверяет тест.)"""
    return re.sub(r"[\s_\-]+", "", str(s).casefold().lstrip("."))


def expand(path: str) -> Path:
    p = path.strip()
    home = str(Path.home())
    if p == "~" or p.startswith("~/"):
        p = home + p[1:]
    p = p.replace("$HOME", home)
    return Path(os.path.realpath(p))


# --------------------------------------------------------------------------- источники
@dataclass
class Source:
    path: str                 # как записано в конфиге (~/code)
    name: str = ""            # пусто = имя по умолчанию (последнее имя пути)
    encrypt: bool = False
    whole: bool = False       # вся директория одним архивом
    sudo: bool = False        # принудительный sudo (для путей вне $HOME он включается сам)

    def effective_name(self) -> str:
        return self.name.strip() or expand(self.path).name

    def auto_sudo(self) -> bool:
        p = expand(self.path)
        home = Path(os.path.realpath(str(Path.home())))
        return not (p == home or home in p.parents)

    def needs_sudo(self) -> bool:
        return self.sudo or self.auto_sudo()

    def options(self) -> list[str]:
        return [o for o, on in (("encrypt", self.encrypt), ("whole", self.whole), ("sudo", self.sudo)) if on]


def parse_sources(text: str) -> list[Source]:
    out: list[Source] = []
    for line in text.splitlines():
        if re.match(r"^\s*(#|$)", line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        name = parts[1] if len(parts) > 1 else ""
        opts = {o.strip() for o in (parts[2] if len(parts) > 2 else "").split(",") if o.strip()}
        out.append(Source(parts[0], name, "encrypt" in opts, "whole" in opts, "sudo" in opts))
    return out


CONF_HEADER = """\
# Список директорий для бэкапа. Управляется через TUI (./backup.sh tui), можно править руками.
# Формат:   путь [ | имя_папки [ | опции ] ]
#   имя_папки — подпапка в ~/backupUtil/ (и топик в Telegram, регистр не важен);
#               по умолчанию = последнее имя пути
#   опции     — через запятую:  encrypt | whole | sudo
#       encrypt  зашифровать age перед нарезкой на тома
#       whole    вся директория одним архивом (по умолчанию каждый элемент — отдельный архив)
#       sudo     читать через sudo (для путей вне $HOME включается само)
"""


def format_sources(sources: list[Source]) -> str:
    lines = [CONF_HEADER]
    for s in sources:
        opts = ",".join(s.options())
        if opts:
            lines.append(f"{s.path} | {s.name} | {opts}")
        elif s.name:
            lines.append(f"{s.path} | {s.name}")
        else:
            lines.append(s.path)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- backup.env
_ENV_LINE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)=(.*)$')


def _env_value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        q = raw[0]
        end = raw.find(q, 1)
        return raw[1:end] if end != -1 else raw[1:]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = _env_value(m.group(2))
    return out


def check_env_value(key: str, value: str) -> None:
    """backup.env подключается через `source` — значения не должны исполнять код."""
    if any(c in value for c in ('"', "`", "\\", "\n", "\r")) or "$(" in value:
        raise ValueError(f"{key}: недопустимые символы (кавычки, обратный слеш, `, $( )")


def update_env(text: str, values: dict[str, str]) -> str:
    """Подставляет значения в существующий текст backup.env, сохраняя прочие строки и комментарии."""
    for k, v in values.items():
        check_env_value(k, v)
    numeric = {"ZSTD_LEVEL", "SPLIT", "ONE_FS"}
    rendered = {k: (v if k in numeric else f'"{v}"') for k, v in values.items()}
    seen: set[str] = set()
    out = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in rendered and m.group(1) not in seen:
            out.append(f"{m.group(1)}={rendered[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    if not out:
        out = ["# Настройки backup.sh. Управляются через TUI (./backup.sh tui), можно править руками."]
    for k, v in rendered.items():
        if k not in seen:
            out.append(f"{k}={v}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- telegram.toml
def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _toml_scalar(v: str):
    """'-1001' → число, всё остальное → строка (id группы/топика или @имя/название)."""
    s = str(v).strip()
    return s if not re.fullmatch(r"-?\d+", s) else int(s)


def dump_telegram_toml(tg: dict[str, str], topics: dict[str, int], ab: dict[str, str], default_session: str = "") -> str:
    def val(v):
        return str(v) if isinstance(v, int) else _toml_str(v)

    out = ["# Секреты: файл должен иметь права 600 и НЕ должен попадать в git/бэкапы.",
           "# Управляется через TUI (./backup.sh tui), можно править руками.", "", "[telegram]"]
    if str(tg.get("api_id", "")).strip():
        out.append(f"api_id = {int(str(tg['api_id']).strip())}")
    if tg.get("api_hash", "").strip():
        out.append(f"api_hash = {_toml_str(tg['api_hash'].strip())}")
    if str(tg.get("group", "")).strip():
        out.append(f"group = {val(_toml_scalar(tg['group']))}")
    if tg.get("session", "").strip() and tg["session"].strip() != default_session:  # по умолчанию ключ не пишем
        out.append(f"session = {_toml_str(tg['session'].strip())}")
    out += ["", "# имя папки (регистр не важен) → id топика. Нужно только для исключений:",
            "# обычно топик находится автоматически по названию.", "[topics]"]
    for k in sorted(topics, key=str.casefold):
        out.append(f"{_toml_str(k)} = {int(topics[k])}")
    out += ["", "# Метки «точек» в чате (полный бэкап — всё из backup.conf)", "[autoback]"]
    lt = str(ab.get("log_topic", "")).strip()
    if lt:
        out.append(f"log_topic = {val(_toml_scalar(lt))}")
    for k, default in AUTOBACK_DEFAULTS.items():
        v = ab.get(k, default)
        if v and v != default:
            out.append(f"{k} = {_toml_str(v)}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- модель
def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Запись через временный файл + rename; mode=0o600 для секретов."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


class Model:
    """Всё состояние конфигурации в памяти + загрузка/сохранение в файлы."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.conf = self.root / "backup.conf"
        self.env_file = self.root / "backup.env"
        self.toml = self.root / "telegram.toml"
        self.excl = self.root / "excludes.txt"
        self.excl_default = self.root / "excludes.default.txt"
        self.sources: list[Source] = []
        self.env: dict[str, str] = dict(ENV_DEFAULTS)
        self.default_session = str(self.root / ".secrets" / "tg.session")  # так же решает tg_upload.py
        self.tg: dict[str, str] = {"api_id": "", "api_hash": "", "group": "", "session": self.default_session}
        self.topics: dict[str, int] = {}
        self.ab: dict[str, str] = {"log_topic": "", **AUTOBACK_DEFAULTS}
        self.excludes = ""
        self.dirty = False

    # ---- загрузка
    def load(self) -> None:
        if self.conf.exists():
            self.sources = parse_sources(self.conf.read_text(encoding="utf-8"))
        if self.env_file.exists():
            self.env = {**ENV_DEFAULTS, **{k: v for k, v in parse_env(self.env_file.read_text(encoding="utf-8")).items()
                                           if k in ENV_DEFAULTS}}
        if self.toml.exists():
            raw = tomllib.loads(self.toml.read_text(encoding="utf-8"))
            tg = raw.get("telegram", {})
            self.tg = {
                "api_id": str(tg.get("api_id", "")),
                "api_hash": str(tg.get("api_hash", "")),
                "group": str(tg.get("group", "")),
                "session": str(tg.get("session", self.default_session)),
            }
            self.topics = {str(k): int(v) for k, v in raw.get("topics", {}).items()}
            ab = raw.get("autoback", {})
            self.ab = {"log_topic": str(ab.get("log_topic", "")),
                       **{k: str(ab.get(k, d)) for k, d in AUTOBACK_DEFAULTS.items()}}
        src = self.excl if self.excl.exists() else self.excl_default
        self.excludes = src.read_text(encoding="utf-8") if src.exists() else ""
        self.dirty = False

    # ---- проверки
    def validate(self) -> list[str]:
        problems: list[str] = []
        names: dict[str, str] = {}
        for s in self.sources:
            n = s.effective_name()
            if not s.path.strip():
                problems.append("Пустой путь у одного из источников")
                continue
            if not n or n in (".", "..") or "/" in n:
                problems.append(f"{s.path}: недопустимое имя папки «{n}» — задайте имя явно")
            elif n in RESERVED:
                problems.append(f"{s.path}: имя «{n}» зарезервировано утилитой — задайте другое")
            if n in names:
                problems.append(f"Имя «{n}» повторяется ({names[n]} и {s.path}) — задайте уникальное")
            names[n] = s.path
            if "|" in s.path or "|" in s.name:
                problems.append(f"{s.path}: символ «|» нельзя использовать в пути и имени")
        vs = self.env.get("VOLUME_SIZE", "")
        m = re.fullmatch(r"(\d+)([KMG])", vs)
        if not m:
            problems.append("Размер тома: число и суффикс K/M/G, например 1800M")
        else:
            mib = int(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]
            if mib >= TG_VOLUME_LIMIT_MIB:
                problems.append(f"Размер тома {vs} не проходит в Telegram (нужно меньше {TG_VOLUME_LIMIT_MIB}M)")
            if mib <= 0:
                problems.append("Размер тома должен быть больше нуля")
        if not re.fullmatch(r"([1-9]|1[0-9])", self.env.get("ZSTD_LEVEL", "")):
            problems.append("Уровень zstd: число от 1 до 19")
        for k in ("SPLIT", "ONE_FS"):
            if self.env.get(k) not in ("0", "1"):
                problems.append(f"{k}: допустимо 0 или 1")
        for k in ("AGE_RECIPIENT", "AGE_RECIPIENTS_FILE"):
            try:
                check_env_value(k, self.env.get(k, ""))
            except ValueError as e:
                problems.append(str(e))
        if any(s.encrypt for s in self.sources) and not (self.env.get("AGE_RECIPIENT") or self.env.get("AGE_RECIPIENTS_FILE")):
            problems.append("Включено шифрование, но не задан ключ age (вкладка «Параметры»)")
        if self.tg.get("api_id", "").strip() and not re.fullmatch(r"\d+", self.tg["api_id"].strip()):
            problems.append("api_id должен быть числом")
        return problems

    # ---- сохранение
    def save(self) -> list[Path]:
        problems = self.validate()
        if problems:
            raise ValueError("\n".join(problems))
        written: list[Path] = []

        new_conf = format_sources(self.sources)
        if not self.conf.exists() or self.conf.read_text(encoding="utf-8") != new_conf:
            if self.conf.exists():
                atomic_write(self.conf.with_name("backup.conf.bak"), self.conf.read_text(encoding="utf-8"))
            atomic_write(self.conf, new_conf)
            written.append(self.conf)

        old_env = self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else ""
        new_env = update_env(old_env, self.env)
        if new_env != old_env:
            atomic_write(self.env_file, new_env, 0o600)
            written.append(self.env_file)

        new_toml = dump_telegram_toml(self.tg, self.topics, self.ab, self.default_session)
        old_toml = self.toml.read_text(encoding="utf-8") if self.toml.exists() else None
        if new_toml != old_toml:
            atomic_write(self.toml, new_toml, 0o600)
            written.append(self.toml)

        default_text = self.excl_default.read_text(encoding="utf-8") if self.excl_default.exists() else ""
        current_on_disk = self.excl.read_text(encoding="utf-8") if self.excl.exists() else default_text
        if self.excludes != current_on_disk:
            atomic_write(self.excl, self.excludes)
            written.append(self.excl)
        self.dirty = False
        return written

    def save_telegram(self) -> None:
        """Записать только telegram.toml (перед сетевыми действиями в TUI): то же, что читает tg_upload.py."""
        tg_id = self.tg.get("api_id", "").strip()
        if tg_id and not re.fullmatch(r"\d+", tg_id):
            raise ValueError("api_id должен быть числом")
        text = dump_telegram_toml(self.tg, self.topics, self.ab, self.default_session)
        old = self.toml.read_text(encoding="utf-8") if self.toml.exists() else None
        if text != old:
            atomic_write(self.toml, text, 0o600)

    def archive_status(self, name: str) -> dict:
        """Что собрано в папке архива и сколько томов уже отмечено в журнале выгрузки."""
        d = self.root / name
        mf = d / "MANIFEST.txt"
        if not mf.exists():
            return {"built": False}
        info: dict[str, str] = {}
        for line in mf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                info.setdefault(k.strip(), v.strip())
        vols: dict[str, str] = {}
        shas = sorted(d.glob("*.sha256"))
        if shas:
            for line in shas[0].read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line)
                if m:
                    vols[m.group(2)] = m.group(1)
        sent: dict[str, str] = {}
        journal = self.root / ".state" / "uploaded.jsonl"
        if journal.exists():
            for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                    sent[rec["key"].split("|", 2)[2]] = rec["sha256"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        date = info.get("DATE", "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = f"{date[8:]}.{date[5:7]}.{date[:4]}"
        return {
            "built": True, "date": date, "bytes": int(info.get("ARCHIVE_BYTES", "0") or 0),
            "volumes": len(vols), "uploaded": sum(1 for fn, sha in vols.items() if sent.get(f"{name}/{fn}") == sha),
        }

    # ---- вспомогательное для интерфейса
    def telegram_cfg(self) -> dict:
        """Конфиг в формате, который ждёт tg_upload (для запросов из TUI, без записи на диск)."""
        if not self.tg["api_id"].strip() or not self.tg["api_hash"].strip():
            raise ValueError("Заполните api_id и api_hash на вкладке «Telegram»")
        grp = self.tg["group"].strip()
        return {
            "api_id": int(self.tg["api_id"]),
            "api_hash": self.tg["api_hash"].strip(),
            "group": _toml_scalar(grp) if grp else None,
            "session": Path(os.path.expanduser(self.tg["session"].strip() or self.default_session)),
        }

    def explicit_topic(self, name: str) -> int | None:
        n = norm(name)
        for k, v in self.topics.items():
            if norm(k) == n:
                return v
        return None

    def topic_key(self, name: str) -> str | None:
        n = norm(name)
        for k in self.topics:
            if norm(k) == n:
                return k
        return None

    def resolve_topic(self, name: str, titles: dict[int, str]) -> tuple[int | None, str]:
        """Как tg_upload.resolve_topic: сначала [topics], потом совпадение с названием топика."""
        ex = self.explicit_topic(name)
        if ex is not None:
            return ex, "вручную"
        hits = [(i, t) for i, t in titles.items() if norm(t) == norm(name)]
        if len(hits) == 1:
            return hits[0][0], f"авто → «{hits[0][1]}»"
        if len(hits) > 1:
            return None, "неоднозначно"
        return None, ("нет топика" if titles else "авто по названию")
