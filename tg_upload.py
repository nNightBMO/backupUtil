#!/usr/bin/env python3
"""
tg_upload.py — выгрузка архивов в Telegram-группу с топиками от лица ЛИЧНОГО аккаунта
(MTProto, Telethon). Bot API не используется.

Команды:
  login    авторизация: телефон → код → пароль 2FA (если есть); сессия сохраняется (chmod 600)
  groups   список ваших супергрупп (id, forum/group, название)
  topics   список топиков форумной группы + готовые строки для telegram.toml
  upload   загрузка из ~/backupUtil/<имя>/ в топик с таким же названием (регистр не важен)

Метки (только при upload):
  полный бэкап (--full: все директории из backup.conf)
      1) в топик логов:                  «07.11.2026 autoback#1»
      2) после архивов каждой директории: «autoback#1»  (в её топике)
  частичный (одна или несколько директорий)
      после архивов каждой директории:   «07.11.2026»   (в её топике), больше нигде ничего
Дата — дата СОЗДАНИЯ бэкапа (из MANIFEST.txt). Номер autoback# растёт на 1 с каждым
завершённым полным бэкапом (.state/autoback.json); прерванный полный бэкап при повторном
запуске продолжается под тем же номером (--fresh — начать новый).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

os.umask(0o077)  # все создаваемые файлы (сессия, журнал, лог) — 600

try:
    import telethon
    from telethon import TelegramClient, errors, functions
except ImportError:
    sys.exit(
        "Не установлен telethon. Выполните:\n"
        "  python -m venv ~/backupUtil/.venv\n"
        "  ~/backupUtil/.venv/bin/pip install -r ~/backupUtil/requirements.txt"
    )

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
JOURNAL = STATE_DIR / "uploaded.jsonl"
AUTOBACK = STATE_DIR / "autoback.json"
LOGFILE = ROOT / "backup.log"

MAX_FILE = 2000 * 1024 * 1024  # лимит Telegram для обычного аккаунта (2000 МиБ)
MAX_RETRIES = 8
FATAL_RPC = {
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "UserBannedInChannelError",
    "TopicClosedError", "TopicDeletedError", "MessageIdInvalidError", "ChannelPrivateError",
    "FilePartsInvalidError", "FileTooBigError", "AuthKeyUnregisteredError",
}
SENSITIVE_RE = re.compile(
    r"(ssh|gnupg|gpg|secret|token|passw|pass-|keys?\b|wallet|keepass|aws|kube|\.config|"
    r"mozilla|thunderbird|bitwarden|1password)", re.I)


# ------------------------------ вспомогательное --------------------------------
def log(level: str, msg: str) -> None:
    line = f"{time.strftime('%F %T')} [{level}] tg: {msg}"
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line if level != "INFO" else msg, file=sys.stderr if level == "ERR" else sys.stdout, flush=True)


def mib(n: float) -> float:
    return n / 1048576


def fmt_time(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes", "д", "да")


def norm(s: str) -> str:
    """Сравнение имён папок и топиков: без регистра, без ведущей точки, без пробелов/_/-."""
    return re.sub(r"[\s_\-]+", "", str(s).casefold().lstrip("."))


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Нет файла {path}. Скопируйте telegram.toml.example → telegram.toml и заполните.")
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
        print(f"[!] Права на {path} были слишком широкими — установлены 600")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        tg = raw["telegram"]
        ab = raw.get("autoback", {})
        return {
            "api_id": int(tg["api_id"]),
            "api_hash": str(tg["api_hash"]),
            "group": tg.get("group"),
            "session": Path(os.path.expanduser(tg.get("session", str(ROOT / ".secrets" / "tg.session")))),
            "topics": {str(k): int(v) for k, v in raw.get("topics", {}).items()},
            "log_topic": ab.get("log_topic"),
            "date_format": ab.get("date_format", "%d.%m.%Y"),
            "log_message": ab.get("log_message", "{date} autoback#{n}"),
            "dir_message": ab.get("dir_message", "autoback#{n}"),
        }
    except KeyError as e:
        sys.exit(f"В {path} не хватает поля {e}")


def make_client(cfg: dict) -> TelegramClient:
    cfg["session"].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return TelegramClient(
        str(cfg["session"]), cfg["api_id"], cfg["api_hash"],
        connection_retries=10, retry_delay=5, request_retries=10,
        auto_reconnect=True, flood_sleep_threshold=60,
    )


def lock_session_files(cfg: dict) -> None:
    for p in cfg["session"].parent.glob(cfg["session"].name + "*"):
        p.chmod(0o600)
    cfg["session"].parent.chmod(0o700)


async def connect_authorized(cfg: dict) -> TelegramClient:
    client = make_client(cfg)
    await client.connect()
    lock_session_files(cfg)
    if not await client.is_user_authorized():
        await client.disconnect()
        sys.exit("Сессия не авторизована. Сначала выполните: ./backup.sh tg login")
    return client


async def resolve_group(client: TelegramClient, group):
    if group is None:
        sys.exit("В telegram.toml не задан group (id, @username или точное название)")
    try:
        return await client.get_entity(group)
    except (ValueError, errors.RPCError):
        pass
    await client.get_dialogs()  # прогреваем кэш сущностей — с числовым id без этого не найдёт
    try:
        return await client.get_entity(group)
    except (ValueError, errors.RPCError):
        if isinstance(group, str):
            async for d in client.iter_dialogs():
                if d.name == group:
                    return d.entity
    sys.exit(f"Не нашёл группу {group!r}. Посмотрите id командой: ./backup.sh tg groups")


# ------------------------------ топики -------------------------------------------
async def topics_from_service_messages(client, entity) -> dict[int, str]:
    """Запасной путь: собрать названия топиков из сервисных сообщений группы (медленнее)."""
    log("INFO", "Собираю названия топиков из истории группы (может занять время)…")
    topics: dict[int, str] = {}
    async for m in client.iter_messages(entity):  # от новых к старым: переименование побеждает создание
        act = getattr(m, "action", None)
        if act is None:
            continue
        kind = type(act).__name__
        if kind == "MessageActionTopicCreate":
            topics.setdefault(m.id, act.title)
        elif kind == "MessageActionTopicEdit" and getattr(act, "title", None):
            rt = getattr(m, "reply_to", None)
            tid = (getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)) if rt else None
            if tid:
                topics.setdefault(tid, act.title)
    topics.setdefault(1, "General")
    return topics


async def fetch_topics(client, entity) -> dict[int, str]:
    """id → название. Основной путь: messages.GetForumTopics (в Telethon он в functions.messages)."""
    api = getattr(functions.messages, "GetForumTopicsRequest", None)
    if api is not None:
        try:
            topics: dict[int, str] = {}
            offset_date, offset_id, offset_topic = None, 0, 0
            for _ in range(100):
                r = await client(api(peer=entity, offset_date=offset_date, offset_id=offset_id,
                                     offset_topic=offset_topic, limit=100))
                for t in r.topics:
                    if hasattr(t, "title"):  # удалённые топики (ForumTopicDeleted) без названия
                        topics[t.id] = t.title
                if len(r.topics) < 100:
                    break
                last = r.topics[-1]
                offset_topic, offset_id = last.id, last.top_message
                m = next((m for m in r.messages if m.id == last.top_message), None)
                offset_date = m.date if m else None
            return topics
        except (errors.RPCError, TypeError, AttributeError) as e:
            log("WARN", f"GetForumTopics не сработал ({type(e).__name__}: {e}) — пробую обход")
    else:
        log("WARN", f"В вашей версии Telethon ({telethon.__version__}) нет GetForumTopicsRequest — "
                    "использую обход. Лучше обновить: pip install -U telethon")
    return await topics_from_service_messages(client, entity)


def resolve_topic(name: str, cfg: dict, titles: dict[int, str]):
    """(id, как_найден) или (None, причина). Регистр, ведущая точка, пробелы/_/- не важны."""
    n = norm(name)
    for k, v in cfg["topics"].items():
        if norm(k) == n:
            return v, f"telegram.toml: «{k}»"
    hits = [(i, t) for i, t in titles.items() if norm(t) == n]
    if len(hits) == 1:
        return hits[0][0], f"по названию топика «{hits[0][1]}»"
    if len(hits) > 1:
        return None, "несколько топиков подходят: " + ", ".join(f"«{t}» ({i})" for i, t in hits) + " — укажите id в [topics]"
    return None, "топик не найден (нет записи в [topics] и нет топика с таким названием)"


# ------------------------------ журнал и autoback --------------------------------
class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    self.data[rec["key"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue

    @staticmethod
    def key(group, topic: int, rel: str) -> str:
        return f"{group}|{topic}|{rel}"

    def done(self, key: str, sha: str) -> bool:
        rec = self.data.get(key)
        return bool(rec) and rec.get("sha256") == sha

    def add(self, key: str, sha: str, size: int, msg_id: int) -> None:
        rec = {"key": key, "sha256": sha, "size": size, "message_id": msg_id, "ts": int(time.time())}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.data[key] = rec


def ab_load() -> dict:
    if AUTOBACK.exists():
        try:
            return json.loads(AUTOBACK.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"last": 0, "current": None}


def ab_save(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTOBACK.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, AUTOBACK)


def manifest_datetime(d: Path, mani: dict) -> datetime:
    try:
        return datetime.fromisoformat(mani["DATE"])
    except (KeyError, ValueError):
        mf = d / "MANIFEST.txt"
        return datetime.fromtimestamp((mf if mf.exists() else d).stat().st_mtime)


# ------------------------------ прогресс и отправка ----------------------------
class Progress:
    def __init__(self, label: str):
        self.label, self.t0, self.last = label, time.time(), 0.0

    def __call__(self, sent: int, total: int) -> None:
        now = time.time()
        if now - self.last < 0.5 and sent < total:
            return
        self.last = now
        speed = sent / max(now - self.t0, 1e-6)
        eta = (total - sent) / speed if speed else 0
        print(f"\r  ↑ {self.label}: {sent / total * 100:5.1f}%  {mib(sent):8.1f}/{mib(total):.1f} МиБ  "
              f"{mib(speed):5.2f} МиБ/с  осталось {fmt_time(eta)}   ", end="", flush=True)

    def finish(self) -> None:
        print()


async def _retry(label: str, op):
    """FloodWait — ждать и продолжать; обрывы — повтор с нарастающей паузой; фатальное — наверх."""
    attempt = 0
    while True:
        try:
            return await op()
        except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
            wait = int(getattr(e, "seconds", 30)) + 2
            log("WARN", f"{label}: FloodWait/SlowMode — жду {wait} с и продолжаю")
            await asyncio.sleep(wait)  # не считается неудачной попыткой
        except (ConnectionError, OSError, asyncio.TimeoutError, errors.RPCError) as e:
            if type(e).__name__ in FATAL_RPC:
                raise
            attempt += 1
            if attempt > MAX_RETRIES:
                raise
            delay = min(300, 5 * 2 ** attempt)
            log("WARN", f"{label}: {type(e).__name__}: {e} — повтор {attempt}/{MAX_RETRIES} через {delay} с")
            await asyncio.sleep(delay)


async def send_file(client, entity, topic: int, path: Path, caption: str, label: str):
    size = path.stat().st_size

    async def op():
        prog = Progress(label)
        try:
            msg = await client.send_file(entity, str(path), caption=caption, force_document=True,
                                         reply_to=topic, part_size_kb=512, progress_callback=prog)
        finally:
            prog.finish()
        got = getattr(getattr(msg, "file", None), "size", None)
        if got is not None and got != size:
            raise IOError(f"Telegram сообщил размер {got}, локально {size}")
        return msg

    return await _retry(label, op)


async def send_text(client, entity, topic: int, text: str, label: str):
    return await _retry(label, lambda: client.send_message(entity, text, reply_to=topic))


# ------------------------------ чтение каталога архива -------------------------
def read_manifest(d: Path) -> dict[str, str]:
    res: dict[str, str] = {}
    mf = d / "MANIFEST.txt"
    if mf.exists():
        for line in mf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                res.setdefault(k.strip(), v.strip())
    return res


def parse_sha(p: Path) -> list[tuple[str, str]]:
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line)
        if m:
            out.append((m.group(2), m.group(1)))
    return sorted(out)


def set_key(fn: str) -> str:
    return re.sub(r"\.\d{3}$", "", fn)


# ------------------------------ команды -----------------------------------------
async def cmd_login(cfg: dict, args) -> int:
    client = make_client(cfg)
    await client.start()  # спросит телефон, код из Telegram и пароль 2FA (если включён)
    lock_session_files(cfg)
    me = await client.get_me()
    print(f"Готово: вошли как {me.first_name} (@{me.username}), сессия: {cfg['session']} (права 600)")
    await client.disconnect()
    return 0


async def cmd_groups(cfg: dict, args) -> int:
    client = await connect_authorized(cfg)
    try:
        print("id\t\t\tтип\tназвание")
        async for d in client.iter_dialogs():
            e = d.entity
            if getattr(e, "megagroup", False):
                print(f"{d.id}\t{'forum' if getattr(e, 'forum', False) else 'group'}\t{d.name}")
        print("\nНужна строка с типом forum. Её id → поле group в telegram.toml.")
    finally:
        await client.disconnect()
    return 0


async def cmd_topics(cfg: dict, args) -> int:
    client = await connect_authorized(cfg)
    try:
        entity = await resolve_group(client, args.group if args.group is not None else cfg["group"])
        if not getattr(entity, "forum", False):
            print("Эта группа не форум (темы не включены) — топиков нет.")
            return 1
        topics = await fetch_topics(client, entity)
        print(f"Группа: {getattr(entity, 'title', entity)}\n\nID\tНазвание")
        for i, t in sorted(topics.items()):
            print(f"{i}\t{t}")
        print("\nСопоставление с папками делается автоматически, БЕЗ учёта регистра "
              "(папка «code» ↔ топик «Code»), поэтому [topics] нужен только для исключений:\n[topics]")
        for i, t in sorted(topics.items()):
            print(f'"{t.lower()}" = {i}')
    finally:
        await client.disconnect()
    return 0


async def upload_dir(client, entity, cfg, journal: Journal, d: Path, topic: int, ab_n, args) -> bool:
    """Загрузить все тома директории, затем sha256/MANIFEST, затем метку. ab_n — номер autoback или None."""
    name = d.name
    shas = sorted(d.glob("*.sha256"))
    if not shas:
        log("ERR", f"{name}: нет *.sha256 — сначала соберите архив (./backup.sh build {name})")
        return False
    sha_path = shas[0]
    vols = parse_sha(sha_path)
    if not vols:
        log("ERR", f"{name}: {sha_path.name} пуст или не разобран")
        return False
    mani = read_manifest(d)
    if mani.get("ENCRYPTED") != "1" and SENSITIVE_RE.search(name):
        log("WARN", f"{name}: имя похоже на папку с ключами/токенами/паролями, а архив НЕ зашифрован. "
                    "Чаты Telegram не имеют сквозного шифрования — лучше пометить её encrypt в backup.conf.")
        if not confirm("Всё равно загрузить без шифрования?", args.yes):
            log("ERR", f"{name}: пропущено (нет подтверждения)")
            return False

    group_key = cfg["group"]
    sizes = {}
    for fn, _ in vols:  # заранее проверяем, что всё, что не загружено, на месте и влезает в лимит
        key = Journal.key(group_key, topic, f"{name}/{fn}")
        sha = dict(vols)[fn]
        if journal.done(key, sha):
            continue
        p = d / fn
        if not p.exists():
            log("ERR", f"{name}: нет файла {fn}, а в журнале он не отмечен — соберите архив заново")
            return False
        sizes[fn] = p.stat().st_size
        if sizes[fn] > MAX_FILE:
            log("ERR", f"{fn}: {mib(sizes[fn]):.0f} МиБ больше лимита Telegram ({mib(MAX_FILE):.0f}). Соберите с меньшим --volume-size")
            return False

    per_set: dict[str, list] = {}
    for fn, sha in vols:
        per_set.setdefault(set_key(fn), []).append((fn, sha))
    log("INFO", f"── {name} → топик {topic}: архивов {len(per_set)}, файлов {len(vols)}")

    for sk, items in per_set.items():
        for i, (fn, sha) in enumerate(items, 1):
            key = Journal.key(group_key, topic, f"{name}/{fn}")
            if journal.done(key, sha):
                log("INFO", f"  = {fn}: уже загружен")
                continue
            if args.dry_run:
                log("INFO", f"  [dry-run] загрузил бы {fn} ({mib(sizes[fn]):.1f} МиБ)")
                continue
            caption = f"{fn} — {i} из {len(items)}\nsha256: {sha}"
            msg = await send_file(client, entity, topic, d / fn, caption, f"{name}/{fn}")
            journal.add(key, sha, sizes[fn], msg.id)
            log("INFO", f"  ✓ {fn} ({i}/{len(items)}) → сообщение {msg.id}")

    # Контрольные суммы и манифест — после архивов: их наличие в топике = набор загружен целиком
    manifest_sha = ""
    for extra in (sha_path, d / "MANIFEST.txt"):
        if not extra.exists():
            continue
        sha = sha256_file(extra)
        if extra.name == "MANIFEST.txt":
            manifest_sha = sha
        key = Journal.key(group_key, topic, f"{name}/{extra.name}")
        if journal.done(key, sha):
            continue
        if args.dry_run:
            log("INFO", f"  [dry-run] загрузил бы {extra.name}")
            continue
        msg = await send_file(client, entity, topic, extra, f"{name}: {extra.name}", f"{name}/{extra.name}")
        journal.add(key, sha, extra.stat().st_size, msg.id)
        log("INFO", f"  ✓ {extra.name}")

    # Метка после архивов
    when = manifest_datetime(d, mani).strftime(cfg["date_format"])
    if ab_n is not None:
        text, msha = cfg["dir_message"].format(n=ab_n, date=when), f"autoback#{ab_n}"
    else:
        text, msha = when, f"date:{manifest_sha}"
    mkey = Journal.key(group_key, topic, f"{name}/@marker")
    if not journal.done(mkey, msha):
        if args.dry_run:
            log("INFO", f"  [dry-run] написал бы в топик: «{text}»")
        else:
            msg = await send_text(client, entity, topic, text, f"{name}/метка")
            journal.add(mkey, msha, 0, msg.id)
            log("INFO", f"  ✓ метка «{text}»")

    if args.delete_after_upload and not args.dry_run:
        present = [d / fn for fn, sha in vols
                   if (d / fn).exists() and journal.done(Journal.key(group_key, topic, f"{name}/{fn}"), sha)]
        if present:
            tot = sum(p.stat().st_size for p in present)
            if confirm(f"Удалить {len(present)} локальных файл(ов) {name} ({mib(tot):.0f} МиБ)? В журнале они отмечены как загруженные", args.yes):
                for p in present:
                    p.unlink()
                log("INFO", f"  локальные тома {name} удалены (sha256 и MANIFEST оставлены)")
    return True


async def cmd_upload(cfg: dict, args) -> int:
    if args.names:
        dirs = [ROOT / n for n in args.names]
        missing = [str(d) for d in dirs if not (d / "MANIFEST.txt").exists()]
        if missing:
            sys.exit("Нет собранных архивов (нет MANIFEST.txt) в: " + ", ".join(missing) +
                     "\nСначала: ./backup.sh build <имя>")
    else:
        dirs = sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "MANIFEST.txt").exists())
    if not dirs:
        print("Нечего загружать: нет собранных архивов (нужны папки с MANIFEST.txt).")
        return 1
    full = args.full
    if full and cfg["log_topic"] is None:
        sys.exit("Полный бэкап пишет метки в топик логов: добавьте в telegram.toml\n"
                 "[autoback]\nlog_topic = \"Logs\"   # id или название топика")

    journal = Journal(JOURNAL)
    client = entity = None
    titles: dict[int, str] = {}
    if not args.dry_run:
        client = await connect_authorized(cfg)
    failed: list[str] = []
    try:
        if client:
            entity = await resolve_group(client, cfg["group"])
            if not getattr(entity, "forum", False):
                log("WARN", "Группа не является форумом — reply_to на id топика может не сработать")
            titles = await fetch_topics(client, entity)

        # --- заранее определяем топики (до первой отправки) ---
        plan: list[tuple[Path, int]] = []
        for d in dirs:
            tid, how = resolve_topic(d.name, cfg, titles)
            if tid is None:
                if args.dry_run and not titles:
                    log("INFO", f"  [dry-run] {d.name}: топик определится по названию при загрузке")
                    plan.append((d, 0))
                    continue
                log("ERR", f"{d.name}: {how}")
                failed.append(d.name)
            else:
                log("INFO", f"  {d.name} → топик {tid} ({how})")
                plan.append((d, tid))
        log_tid = None
        if full:
            lt = cfg["log_topic"]
            if isinstance(lt, int):
                log_tid = lt
            else:
                log_tid, how = resolve_topic(str(lt), {"topics": {}}, titles)
                if log_tid is None and not args.dry_run:
                    log("ERR", f"топик логов «{lt}»: {how}")
                    failed.append("log_topic")
            if failed:
                log("ERR", "Полный бэкап не начат: сначала исправьте ошибки выше (метки не отправлялись)")
                return 1

        # --- состояние autoback ---
        st = ab_load()
        ab_n = None
        if full:
            when_all = max(manifest_datetime(d, read_manifest(d)) for d, _ in plan).strftime(cfg["date_format"])
            cur = st.get("current")
            if cur and not args.fresh:
                log("INFO", f"Продолжаю незавершённый autoback#{cur['n']} (--fresh — начать новый номер)")
            else:
                cur = {"n": st.get("last", 0) + 1, "date": when_all, "start_sent": False}
                if not args.dry_run:
                    st["current"] = cur
                    ab_save(st)
            ab_n = cur["n"]
            start_text = cfg["log_message"].format(n=ab_n, date=cur["date"])
            if args.dry_run:
                log("INFO", f"[dry-run] в топик логов написал бы: «{start_text}»")
            elif not cur.get("start_sent"):
                await send_text(client, entity, log_tid, start_text, "метка старта")
                cur["start_sent"] = True
                st["current"] = cur
                ab_save(st)
                log("INFO", f"✓ топик логов: «{start_text}»")

        for d, tid in plan:
            if not await upload_dir(client, entity, cfg, journal, d, tid, ab_n, args):
                failed.append(d.name)

        if full and not failed and not args.dry_run:
            st["last"], st["current"] = ab_n, None
            ab_save(st)
            log("INFO", f"autoback#{ab_n} завершён")
    finally:
        if client:
            await client.disconnect()
    if failed:
        log("ERR", "Не завершено: " + ", ".join(failed) + " — исправьте и запустите снова: загруженное пропустится")
        return 1
    log("INFO", "Загрузка завершена успешно.")
    return 0


# ------------------------------ вход ---------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "telegram.toml", help="путь к telegram.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="авторизоваться и сохранить сессию")
    sub.add_parser("groups", help="показать ваши супергруппы")
    tp = sub.add_parser("topics", help="показать топики форумной группы")
    tp.add_argument("--group", default=None, help="переопределить group из конфига (id или @username)")
    up = sub.add_parser("upload", help="загрузить собранные архивы")
    up.add_argument("names", nargs="*", help="имена папок (по умолчанию — все собранные)")
    up.add_argument("--full", action="store_true",
                    help="полный бэкап (все директории из конфига): метки autoback#N. backup.sh ставит сам")
    up.add_argument("--fresh", action="store_true", help="не продолжать прерванный autoback#N, начать новый номер")
    up.add_argument("-n", "--dry-run", action="store_true", help="только показать план")
    up.add_argument("--delete-after-upload", action="store_true",
                    help="удалить локальные тома после успешной выгрузки (по умолчанию ВЫКЛ)")
    up.add_argument("-y", "--yes", action="store_true", help="не задавать вопросов")
    args = ap.parse_args()

    cfg = load_config(args.config)
    handler = {"login": cmd_login, "groups": cmd_groups, "topics": cmd_topics, "upload": cmd_upload}[args.cmd]
    try:
        return asyncio.run(handler(cfg, args))
    except KeyboardInterrupt:
        print("\nПрервано. Перезапустите команду — уже загруженные тома будут пропущены.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
