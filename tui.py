#!/usr/bin/env python3
"""
tui.py — терминальный интерфейс backupUtil (Textual).

Запуск:  ./backup.sh tui      (или  .venv/bin/python tui.py)

Вкладки:
  Источники   какие директории бэкапить: дерево каталогов, имя, режим (элементы/целиком), шифрование, sudo
  Топики      в какой топик Telegram какая директория уходит (список топиков берётся из группы)
  Telegram    api_id / api_hash / группа / сессия, вход в аккаунт, диагностика соединения
  Параметры   размер тома, уровень zstd, нарезка, ключ age, формат меток autoback
  Исключения  редактор excludes.txt
  Запуск      собрать / загрузить в Telegram / собрать и загрузить; статус архивов по директориям
  Журнал      живой вывод сборки и загрузки (TUI не закрывается), история, остановка процесса

Принцип: TUI — оболочка, а работу делают те же программы, что и в командной строке
(backup.sh, tg_upload.py). Они запускаются дочерними процессами, их вывод идёт в «Журнал»
и в файл .state/tui.log — поэтому поведение совпадает с CLI, а любая ошибка видна и сохраняется.

Ctrl+S — сохранить всё, Ctrl+Q — выйти, F5 — журнал, F1 — помощь.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from functools import partial
from pathlib import Path

from bu_config import SENSITIVE_RE, Model, Source, expand, human_size

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, DataTable, DirectoryTree, Footer, Header, Input, Label, OptionList,
                                 RadioButton, RadioSet, RichLog, SelectionList, Static, Switch, TabbedContent,
                                 TabPane, TextArea)
    from textual.widgets.option_list import Option
    from textual.worker import Worker, WorkerState
except ImportError:  # pragma: no cover
    sys.exit("Не установлен textual. Выполните:\n"
             "  python -m venv .venv && .venv/bin/pip install -r requirements.txt")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOG_LIMIT = 2 * 1024 * 1024  # размер .state/tui.log до ротации
STOP_GRACE_INT = 6.0   # с: сколько ждать после SIGINT (мягкая остановка: trap в backup.sh чистит временные файлы)
STOP_GRACE_TERM = 4.0  # с: сколько ждать после SIGTERM, затем SIGKILL


# =========================================================================== процессы
class ProcessRunner:
    """Запускает команду и построчно отдаёт её вывод. Строки, оканчивающиеся на «\\r» (прогресс pv и загрузки),
    уходят в on_progress, а не в журнал. Процесс — в своей группе, чтобы остановка снимала весь конвейер."""

    def __init__(self, on_line, on_progress):
        self.on_line, self.on_progress = on_line, on_progress
        self.proc: asyncio.subprocess.Process | None = None
        self.stopped = False
        self._last_progress = False

    async def run(self, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
        full = {**os.environ, "PYTHONUNBUFFERED": "1", "BACKUPUTIL_NONINTERACTIVE": "1", "BACKUPUTIL_PV_FORCE": "1",
                **(env or {})}
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=full, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, process_group=0)
        pump = asyncio.create_task(self._pump(self.proc.stdout))
        try:
            # Process.wait() в asyncio ждёт ещё и закрытия pipe: пока потомок держит вывод, оно зависает,
            # хотя сам процесс уже завершился. Поэтому ждём по returncode, а хвост вывода — с ограничением.
            while self.proc.returncode is None:
                await asyncio.sleep(0.05)
            try:
                await asyncio.wait_for(asyncio.shield(pump), 2)
            except (asyncio.TimeoutError, TimeoutError):
                # потомки (например, фоновый цикл sudo) держат pipe открытым после выхода основного процесса
                self.on_line("[WARN] после завершения остались фоновые процессы — останавливаю их")
                self._kill_group()
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
        except asyncio.CancelledError:
            self.stop(hard=True)
            raise
        return self.proc.returncode if self.proc.returncode is not None else -1

    def _kill_group(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self, hard: bool = False) -> None:
        """Мягко: SIGINT → через STOP_GRACE_INT SIGTERM → через STOP_GRACE_TERM SIGKILL (всей группе процессов).
        hard=True начинает сразу с SIGTERM. Лестница нужна, если SIGINT унаследован как «игнорируемый»
        (запуск из лаунчера/nohup): тогда первый сигнал не подействует."""
        if self.proc is None or self.proc.returncode is not None:
            return
        self.stopped = True
        first = signal.SIGTERM if hard else signal.SIGINT
        try:
            os.killpg(self.proc.pid, first)
        except (ProcessLookupError, PermissionError):
            return
        loop = asyncio.get_running_loop()
        if hard:
            loop.call_later(STOP_GRACE_TERM, self._escalate, signal.SIGKILL, None)
        else:
            loop.call_later(STOP_GRACE_INT, self._escalate, signal.SIGTERM, STOP_GRACE_TERM)

    def _escalate(self, sig: int, then_after: float | None) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if then_after:
            asyncio.get_running_loop().call_later(then_after, self._escalate, signal.SIGKILL, None)

    def _emit(self, seg: bytes, progress: bool) -> None:
        text = ANSI_RE.sub("", seg.decode("utf-8", errors="replace"))
        if progress:
            if text.strip():
                self._last_progress = True
                self.on_progress(text.strip())
            return
        if not text.strip() and self._last_progress:  # пустая строка после «\r»-прогресса — не нужна
            self._last_progress = False
            return
        self._last_progress = False
        self.on_progress("")
        self.on_line(text.rstrip())

    async def _pump(self, stream) -> None:
        buf = b""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                seg, sep, buf = buf[:m.start()], buf[m.start():m.end()], buf[m.end():]
                if sep == b"\n":
                    self._emit(seg, False)
                elif buf.startswith(b"\n"):  # «\r\n»
                    buf = buf[1:]
                    self._emit(seg, False)
                else:
                    self._emit(seg, True)
        if buf.strip():
            self._emit(buf, False)
        self.on_progress("")


async def run_capture(argv: list[str], cwd: Path, on_stderr, timeout: float) -> tuple[int, str]:
    """Запуск команды: stdout собирается целиком (JSON-ответ), stderr по строкам уходит в on_stderr."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env={**os.environ, "PYTHONUNBUFFERED": "1"}, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, process_group=0)

    async def read_err() -> None:
        async for raw in proc.stderr:
            on_stderr(ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n"))

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    err_task = asyncio.create_task(read_err())
    try:
        out = await asyncio.wait_for(proc.stdout.read(), timeout)
        await asyncio.wait_for(proc.wait(), 10)
        await err_task
    except (asyncio.TimeoutError, TimeoutError):
        kill()
        raise RuntimeError(f"Процесс не ответил за {int(timeout)} с и был остановлен") from None
    except asyncio.CancelledError:
        kill()
        raise
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


# =========================================================================== Telegram (через tg_upload.py)
class Remote:
    """Обращения к Telegram. Выполняет ТОТ ЖЕ tg_upload.py, что и командная строка, дочерним процессом:
    поведение идентично CLI, а шаги диагностики попадают в журнал. Подменяется в тестах."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _argv(self, model: Model, *args: str) -> list[str]:
        return [sys.executable, str(self.root / "tg_upload.py"), "--config", str(model.toml), *args]

    def login_argv(self, model: Model) -> list[str]:
        return self._argv(model, "login")

    async def _call(self, model: Model, args: list[str], log, timeout: float = 80) -> dict:
        model.save_telegram()  # tg_upload читает файл: сохраняем то, что введено на вкладке «Telegram»
        argv = self._argv(model, *args)
        log("$ " + shlex.join(argv))
        rc, out = await run_capture(argv, self.root, log, timeout)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            raise RuntimeError(f"tg_upload.py не вернул ответа (код {rc}); подробности — в журнале")
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            raise RuntimeError(f"Неожиданный ответ tg_upload.py (код {rc}): {lines[-1][:200]}") from None
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "неизвестная ошибка")
        return data

    async def check(self, model: Model, log) -> str:
        return (await self._call(model, ["check"], log))["text"]

    async def topics(self, model: Model, log) -> dict[int, str]:
        data = await self._call(model, ["topics", "--json"], log)
        return {int(k): v for k, v in data["topics"].items()}

    async def groups(self, model: Model, log) -> list[tuple[int, str, str]]:
        data = await self._call(model, ["groups", "--json"], log)
        return [(g["id"], g["kind"], g["title"]) for g in data["groups"]]


def generate_age_key(root: Path) -> str:
    """age-keygen → .secrets/age.key; возвращает публичный ключ."""
    key = root / ".secrets" / "age.key"
    if key.exists():
        raise RuntimeError(f"{key} уже существует — не перезаписываю. Возьмите ключ: age-keygen -y {key}")
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        r = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("age-keygen не найден: sudo pacman -S age") from None
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "age-keygen завершился с ошибкой")
    key.chmod(0o600)
    m = re.search(r"(age1[0-9a-z]+)", r.stdout + r.stderr)
    if not m:
        raise RuntimeError("Не удалось прочитать публичный ключ из вывода age-keygen")
    return m.group(1)


def to_tilde(path: str | Path) -> str:
    p, home = str(path), str(Path.home())
    if p == home:
        return "~"
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def build_command(root: Path, mode: str, layout: str, dry: bool, delete_after: bool, encrypt_all: bool,
                  single: bool, fresh: bool, selected: list[str], all_names: list[str], yes: bool = False) -> list[str]:
    """Команда backup.sh по выбранным на вкладке «Запуск» опциям."""
    cmd = [str(root / "backup.sh"), mode]
    if dry:
        cmd.append("--dry-run")
    if mode in ("build", "all"):
        if layout in ("items", "whole"):
            cmd.append("--" + layout)
        if single:
            cmd.append("--single")
        if encrypt_all:
            cmd.append("--encrypt")
    if mode in ("upload", "all"):
        if delete_after:
            cmd.append("--delete-after-upload")
        if fresh:
            cmd.append("--fresh")
    if yes:
        cmd.append("--yes")
    if selected and set(selected) != set(all_names):
        cmd += ["--", *selected]
    return cmd


def infer_level(text: str) -> str:
    if "[ERR]" in text or text.startswith(("Traceback", "FAIL")) or "СТОП" in text:
        return "err"
    if "[WARN]" in text:
        return "warn"
    if "✓" in text or text.startswith("[OK]") or "готово —" in text:
        return "ok"
    return "info"


# =========================================================================== модальные окна
class PromptModal(ModalScreen):
    """Однострочный ввод. Результат: строка или None."""
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, value: str = "", placeholder: str = "", password: bool = False):
        super().__init__()
        self.title_text, self.value, self.placeholder, self.password = title, value, placeholder, password

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="dlg-title", markup=False)
            yield Input(value=self.value, placeholder=self.placeholder, password=self.password, id="prompt-input")
            with Horizontal(classes="btnrow"):
                yield Button("OK", id="ok", variant="primary")
                yield Button("Отмена", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted, "#prompt-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value if event.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickModal(ModalScreen):
    """Выбор из списка. Результат: кортеж (значение,) или None при отмене."""
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, options: list[tuple[str, object]], hint: str = ""):
        super().__init__()
        self.title_text, self.options, self.hint = title, options, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Label(self.title_text, classes="dlg-title", markup=False)
            if self.hint:
                yield Label(self.hint, markup=False)
            yield OptionList(*[Option(Text(label), id=str(i)) for i, (label, _) in enumerate(self.options)],
                             id="pick-list")
            with Horizontal(classes="btnrow"):
                yield Button("Отмена", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#pick-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#pick-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss((self.options[int(event.option.id)][1],))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen):
    """Вопрос с кнопками. Результат: ключ нажатой кнопки или None."""
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, message: str, buttons: list[tuple[str, str, str]]):
        super().__init__()
        self.title_text, self.message, self.buttons = title, message, buttons

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="dlg-title", markup=False)
            yield Label(self.message, markup=False)
            with Horizontal(classes="btnrow"):
                for label, key, variant in self.buttons:
                    yield Button(label, id=f"btn-{key}", variant=variant)  # type: ignore[arg-type]

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss((event.button.id or "")[4:])

    def action_cancel(self) -> None:
        self.dismiss(None)


class DirsTree(DirectoryTree):
    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir()]


class DirPickerModal(ModalScreen):
    """Выбор директории в дереве. Результат: путь строкой или None."""
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, start: str = "~"):
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Label("Выберите директорию: Enter по папке подставит путь, затем «Добавить»", classes="dlg-title",
                        markup=False)
            yield Input(value=self.start, id="dir-input", placeholder="путь, можно вписать вручную")
            with Horizontal(classes="btnrow"):
                yield Button("Дерево: ~", id="root-home")
                yield Button("Дерево: /", id="root-slash")
            yield DirsTree(Path.home(), id="dir-tree")
            with Horizontal(classes="btnrow"):
                yield Button("Добавить", id="ok", variant="primary")
                yield Button("Отмена", id="cancel")

    @on(DirectoryTree.DirectorySelected)
    def _selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#dir-input", Input).value = to_tilde(event.path)

    @on(Input.Submitted, "#dir-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "root-home":
            self.query_one("#dir-tree", DirsTree).path = Path.home()
        elif bid == "root-slash":
            self.query_one("#dir-tree", DirsTree).path = Path("/")
        elif bid == "ok":
            self.dismiss(self.query_one("#dir-input", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SourceEditModal(ModalScreen):
    """Свойства источника. Результат: Source или None."""
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, src: Source):
        super().__init__()
        self.src = src

    def compose(self) -> ComposeResult:
        s = self.src
        with Vertical(id="dialog"):
            yield Label("Источник", classes="dlg-title")
            yield Label("Путь")
            yield Input(value=s.path, id="ed-path")
            yield Label("Имя папки архива / топика (пусто — по имени директории)", markup=False)
            yield Input(value=s.name, placeholder=expand(s.path).name if s.path else "", id="ed-name")
            with Horizontal(classes="swrow"):
                yield Switch(value=s.whole, id="sw-whole")
                yield Label(" Вся директория одним архивом (иначе каждый элемент — свой архив)", markup=False)
            with Horizontal(classes="swrow"):
                yield Switch(value=s.encrypt, id="sw-encrypt")
                yield Label(" Шифровать age (ключи, токены, пароли)", markup=False)
            with Horizontal(classes="swrow"):
                yield Switch(value=s.sudo, id="sw-sudo")
                yield Label(" Читать через sudo (вне $HOME включается само)", markup=False)
            with Horizontal(classes="btnrow"):
                yield Button("Сохранить", id="ok", variant="primary")
                yield Button("Отмена", id="cancel")

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        path = self.query_one("#ed-path", Input).value.strip()
        if not path:
            self.notify("Путь не может быть пустым", severity="error")
            return
        self.dismiss(Source(path=path, name=self.query_one("#ed-name", Input).value.strip(),
                            encrypt=self.query_one("#sw-encrypt", Switch).value,
                            whole=self.query_one("#sw-whole", Switch).value,
                            sudo=self.query_one("#sw-sudo", Switch).value))

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
Ctrl+S  сохранить всё      Ctrl+Q  выйти      F5  журнал
Вкладки: Источники · Топики · Telegram · Параметры · Исключения · Запуск · Журнал

Источники:  a — добавить из дерева, p — по пути, e — изменить, d — удалить
Топики:     «Загрузить топики» подтягивает список из группы; клавиша t или кнопка «Назначить топик».
            Если ничего не назначено, топик ищется по названию папки без учёта регистра.
Telegram:   «Войти» на время открывает терминал (код и пароль 2FA вводятся там, как в ./backup.sh tg login);
            «Проверить» пошагово диагностирует сеть, сессию, аккаунт и группу — шаги видны в «Журнале».
Запуск:     собрать / загрузить в Telegram / всё сразу. TUI не закрывается: вывод идёт в «Журнал»,
            остановить процесс можно кнопкой там же.

Всё, что делает TUI, пишется в .state/tui.log — приложите его к сообщению об ошибке.
Файлы: backup.conf, backup.env, telegram.toml (chmod 600), excludes.txt — лежат рядом со скриптами.
"""

INPUT_FIELDS = {
    "tg-api-id": ("tg", "api_id"), "tg-api-hash": ("tg", "api_hash"),
    "tg-group": ("tg", "group"), "tg-session": ("tg", "session"),
    "p-volume": ("env", "VOLUME_SIZE"), "p-level": ("env", "ZSTD_LEVEL"),
    "p-age": ("env", "AGE_RECIPIENT"), "p-agefile": ("env", "AGE_RECIPIENTS_FILE"),
    "p-datefmt": ("ab", "date_format"), "p-logmsg": ("ab", "log_message"),
    "p-dirmsg": ("ab", "dir_message"), "p-logtopic": ("ab", "log_topic"),
}
SWITCH_FIELDS = {"p-split": "SPLIT", "p-onefs": "ONE_FS"}
LOG_STYLES = {"err": "bold red", "warn": "yellow", "ok": "green", "dim": "dim", "info": ""}
RUN_MODES = {"run-build": "build", "run-upload": "upload", "run-all": "all"}


def cell(text: object) -> Text:
    return Text(str(text))


# =========================================================================== приложение
class BackupUtilApp(App):
    TITLE = "backupUtil"
    CSS = """
    ModalScreen { align: center middle; }
    #dialog { width: 78; height: auto; max-height: 92%; border: thick $primary; background: $surface; padding: 1 2; }
    #dialog-wide { width: 92%; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    .dlg-title { text-style: bold; margin-bottom: 1; }
    .btnrow { height: auto; margin-top: 1; }
    .btnrow Button { margin-right: 1; }
    .swrow { height: 3; }
    .hint { color: $text-muted; margin-top: 1; }
    .field { margin-top: 1; }
    DataTable { height: 1fr; min-height: 6; }
    #dir-tree { height: 1fr; border: round $primary-darken-2; }
    #pick-list { height: 1fr; }
    #excl-text { height: 1fr; }
    #run-src { height: 10; }
    #log-view { height: 1fr; border: round $primary-darken-2; }
    #log-progress { height: auto; color: $warning; }
    #cols { height: 1fr; }
    #cols > Vertical { width: 1fr; height: 100%; padding-right: 1; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "Сохранить"),
        Binding("ctrl+q", "request_quit", "Выход"),
        Binding("f1", "help", "Помощь"),
        Binding("f5", "show_log", "Журнал"),
        Binding("a", "src_add_tree", "Добавить"),
        Binding("p", "src_add_path", "По пути"),
        Binding("e", "src_edit", "Изменить"),
        Binding("d", "src_delete", "Удалить"),
        Binding("t", "top_assign", "Назначить топик"),
    ]

    def __init__(self, root: Path, remote: Remote | None = None):
        super().__init__()
        self.model = Model(root)
        self.model.load()
        self.remote = remote or Remote(root)
        self.titles: dict[int, str] = {}
        self.sub_title = ""
        self.busy = False          # идёт сборка/загрузка
        self.net_busy = False      # идёт запрос к Telegram
        self.runner: ProcessRunner | None = None
        self.log_history: list[tuple[str, str]] = []
        self.status_texts: dict[str, str] = {}
        self.log_path = Path(root) / ".state" / "tui.log"

    # ------------------------------------------------------------------ раскладка
    def compose(self) -> ComposeResult:
        m = self.model
        yield Header()
        with TabbedContent(initial="tab-src"):
            with TabPane("Источники", id="tab-src"):
                yield DataTable(id="src-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="btnrow"):
                    yield Button("Добавить из дерева (a)", id="src-add-tree", variant="primary")
                    yield Button("Добавить по пути (p)", id="src-add-path")
                    yield Button("Изменить (e)", id="src-edit")
                    yield Button("Удалить (d)", id="src-del", variant="error")
                yield Static("Режим items: каждый элемент директории — отдельный архив; whole: вся директория одним архивом.",
                             classes="hint", markup=False)
            with TabPane("Топики", id="tab-top"):
                with Horizontal(id="cols"):
                    with Vertical():
                        yield Label("Директории → топики", classes="dlg-title")
                        yield DataTable(id="top-table", cursor_type="row", zebra_stripes=True)
                    with Vertical():
                        yield Label("Топики группы", classes="dlg-title")
                        yield DataTable(id="grp-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="btnrow"):
                    yield Button("Загрузить топики из Telegram", id="top-fetch", variant="primary")
                    yield Button("Назначить топик (t)", id="top-assign")
                    yield Button("Сбросить на авто", id="top-reset")
                    yield Button("Топик логов (autoback)", id="top-log")
                yield Static(id="top-status", classes="hint", markup=False)
            with TabPane("Telegram", id="tab-tg"):
                with VerticalScroll():
                    yield Label("api_id (my.telegram.org → API development tools)", classes="field")
                    yield Input(value=m.tg["api_id"], id="tg-api-id")
                    yield Label("api_hash (секрет)", classes="field")
                    yield Input(value=m.tg["api_hash"], id="tg-api-hash", password=True)
                    yield Label("Группа с топиками: id (-100…), @username или точное название", classes="field")
                    yield Input(value=m.tg["group"], id="tg-group")
                    yield Label("Файл сессии (права 600, вне архивов)", classes="field")
                    yield Input(value=m.tg["session"], id="tg-session")
                    with Horizontal(classes="btnrow"):
                        yield Button("Показать/скрыть api_hash", id="tg-show-hash")
                        yield Button("Войти в аккаунт", id="tg-login", variant="primary")
                        yield Button("Проверить соединение", id="tg-check", variant="success")
                        yield Button("Выбрать группу", id="tg-pick-group")
                    yield Static(id="tg-status", classes="hint", markup=False)
                    yield Static("Шаги проверки и ошибки подробно — на вкладке «Журнал» (F5) и в файле .state/tui.log.",
                                 classes="hint", markup=False)
            with TabPane("Параметры", id="tab-par"):
                with VerticalScroll():
                    yield Label("Размер тома (K/M/G; для Telegram меньше 2000M)", classes="field")
                    yield Input(value=m.env["VOLUME_SIZE"], id="p-volume")
                    yield Label("Уровень zstd 1–19 (3 — баланс скорости и сжатия)", classes="field")
                    yield Input(value=m.env["ZSTD_LEVEL"], id="p-level")
                    with Horizontal(classes="swrow"):
                        yield Switch(value=m.env["SPLIT"] == "1", id="p-split")
                        yield Label(" Нарезать на тома (иначе каждый архив одним файлом)", markup=False)
                    with Horizontal(classes="swrow"):
                        yield Switch(value=m.env["ONE_FS"] == "1", id="p-onefs")
                        yield Label(" Не заходить в другие файловые системы / подтома", markup=False)
                    yield Label("Шифрование age: публичный ключ (age1…)", classes="field")
                    yield Input(value=m.env["AGE_RECIPIENT"], id="p-age")
                    yield Label("…или файл с публичными ключами", classes="field")
                    yield Input(value=m.env["AGE_RECIPIENTS_FILE"], id="p-agefile")
                    with Horizontal(classes="btnrow"):
                        yield Button("Сгенерировать ключ age", id="p-genkey")
                    yield Label("Метки autoback — топик логов (id или название)", classes="field")
                    yield Input(value=m.ab["log_topic"], id="p-logtopic")
                    yield Label("Формат даты (strftime)", classes="field")
                    yield Input(value=m.ab["date_format"], id="p-datefmt")
                    yield Label("Сообщение в топик логов: {date}, {n}", classes="field")
                    yield Input(value=m.ab["log_message"], id="p-logmsg")
                    yield Label("Сообщение в топик директории: {date}, {n}", classes="field")
                    yield Input(value=m.ab["dir_message"], id="p-dirmsg")
                    yield Static(id="p-preview", classes="hint", markup=False)
            with TabPane("Исключения", id="tab-exc"):
                yield Static("Секции: [*] — для всех директорий, [имя] — только для директории с таким именем. "
                             "Строки с # — комментарии.", classes="hint", markup=False)
                yield TextArea(m.excludes, id="excl-text")
                with Horizontal(classes="btnrow"):
                    yield Button("Вернуть список по умолчанию", id="excl-default")
            with TabPane("Запуск", id="tab-run"):
                with VerticalScroll():
                    yield Label("Что сделать (ход работы — на вкладке «Журнал»)", classes="dlg-title")
                    with Horizontal(classes="btnrow"):
                        yield Button("Собрать архивы", id="run-build", variant="primary")
                        yield Button("Загрузить в Telegram", id="run-upload", variant="success")
                        yield Button("Собрать и загрузить", id="run-all", variant="warning")
                    yield Label("Раскладка архивов", classes="dlg-title field")
                    with RadioSet(id="run-layout"):
                        yield RadioButton("как в backup.conf", id="lay-config", value=True)
                        yield RadioButton("--items: каждый элемент — свой архив", id="lay-items")
                        yield RadioButton("--whole: вся директория одним архивом", id="lay-whole")
                    for sid, text in (("run-dry", "Пробный запуск (--dry-run): ничего не писать и не отправлять"),
                                      ("run-single", "Без нарезки на тома (--single)"),
                                      ("run-encrypt", "Шифровать всё (--encrypt)"),
                                      ("run-delete", "Удалить локальные тома после выгрузки (--delete-after-upload)"),
                                      ("run-fresh", "Начать новый номер autoback#N (--fresh)")):
                        with Horizontal(classes="swrow"):
                            yield Switch(value=False, id=sid)
                            yield Label(" " + text, markup=False)
                    yield Label("Директории и их состояние (все выбраны = полный бэкап с метками autoback#N)",
                                classes="dlg-title field")
                    yield SelectionList[str](id="run-src")
                    yield Static(id="run-cmd", classes="hint", markup=False)
            with TabPane("Журнал", id="tab-log"):
                yield RichLog(id="log-view", wrap=True, markup=False, highlight=False, max_lines=20000)
                yield Static(id="log-progress", markup=False)
                with Horizontal(classes="btnrow"):
                    yield Button("Остановить", id="log-stop", variant="error", disabled=True)
                    yield Button("Очистить", id="log-clear")
                    yield Button("Показать backup.log", id="log-load")
                with Horizontal(classes="swrow"):
                    yield Switch(value=True, id="log-follow")
                    yield Label(" Автопрокрутка", markup=False)
                yield Static("Ожидание", id="log-status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#src-table", DataTable).add_columns("Имя", "Путь", "Режим", "Шифр.", "sudo", "Топик")
        self.query_one("#top-table", DataTable).add_columns("Имя", "Топик", "Как определён")
        self.query_one("#grp-table", DataTable).add_columns("ID", "Название")
        self.refresh_all()
        self._update_title()
        try:
            asyncio.get_running_loop().set_exception_handler(self._loop_exception)
        except RuntimeError:
            pass
        self.log_line("dim", f"backupUtil TUI запущен · рабочая папка {self.model.root} · журнал: {self.log_path}")

    # ------------------------------------------------------------------ журнал
    def _file_log(self, level: str, text: str) -> None:
        try:
            self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%F %T')} [{level}] {text}\n")
        except OSError:
            pass

    def log_line(self, level: str, text: str) -> None:
        """Строка в вкладку «Журнал», в историю и в .state/tui.log."""
        self.log_history.append((level, text))
        del self.log_history[:-5000]
        self._file_log(level, text)
        try:
            self.query_one("#log-view", RichLog).write(Text(text, style=LOG_STYLES.get(level, "")))
        except Exception:  # noqa: BLE001 — интерфейс ещё не собран или уже закрывается
            pass

    def _proc_line(self, text: str) -> None:
        self.log_line(infer_level(text), text)

    def _proc_progress(self, text: str) -> None:
        try:
            self.query_one("#log-progress", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    def _loop_exception(self, loop, context) -> None:
        exc = context.get("exception")
        self.log_line("err", f"Необработанная ошибка asyncio: {context.get('message')} {exc!r}")
        if exc:
            self.log_line("err", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def _handle_exception(self, error: Exception) -> None:  # type: ignore[override]
        self._file_log("err", "СБОЙ TUI:\n" + "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        super()._handle_exception(error)

    @on(Worker.StateChanged)
    def _worker_state(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR and event.worker.error is not None:
            err = event.worker.error
            self.log_line("err", "Сбой фоновой задачи:\n" + "".join(traceback.format_exception(type(err), err, err.__traceback__)))

    def action_show_log(self) -> None:
        self.show_tab("tab-log")

    def show_tab(self, tab_id: str) -> None:
        """Переключить вкладку. TabbedContent сам возвращается на вкладку, где остался фокус (например, на
        нажатой или только что отключённой кнопке) — поэтому фокус переводим внутрь целевой вкладки и
        несколько раз проверяем, что она удержалась."""
        self._goto_tab(tab_id)
        self.set_timer(0.05, partial(self._recheck_tab, tab_id, 8))

    def _goto_tab(self, tab_id: str) -> None:
        tc = self.query_one(TabbedContent)
        tc.active = tab_id
        try:
            for w in tc.get_pane(tab_id).query("*"):
                if w.can_focus:
                    w.focus()
                    return
        except Exception:  # noqa: BLE001
            pass
        self.set_focus(None)

    def _recheck_tab(self, tab_id: str, left: int) -> None:
        if left > 0 and self.query_one(TabbedContent).active != tab_id:
            self._goto_tab(tab_id)
            self.set_timer(0.1, partial(self._recheck_tab, tab_id, left - 1))

    def set_status(self, widget_id: str, text: str) -> None:
        self.status_texts[widget_id] = text
        try:
            self.query_one(widget_id, Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    def say(self, text: str, severity: str = "information") -> None:
        self.notify(text, severity=severity, markup=False)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ обновление данных
    def touch(self) -> None:
        self.model.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        parts = []
        if self.busy:
            parts.append("⏳ идёт процесс")
        parts.append("● есть несохранённые изменения (Ctrl+S)" if self.model.dirty else "сохранено")
        self.sub_title = " · ".join(parts)

    def refresh_all(self) -> None:
        self.refresh_sources()
        self.refresh_topics()
        self.refresh_run()
        self.refresh_preview()

    def _topic_cell(self, s: Source) -> str:
        tid, how = self.model.resolve_topic(s.effective_name(), self.titles)
        return f"{tid} ({how})" if tid is not None else how

    def refresh_sources(self) -> None:
        t = self.query_one("#src-table", DataTable)
        row = t.cursor_row
        t.clear()
        for i, s in enumerate(self.model.sources):
            t.add_row(cell(s.effective_name()), cell(s.path), cell("whole" if s.whole else "items"),
                      cell("да" if s.encrypt else ""), cell("да" if s.needs_sudo() else ""),
                      cell(self._topic_cell(s)), key=str(i))
        if self.model.sources:
            t.move_cursor(row=min(row, len(self.model.sources) - 1))

    def refresh_topics(self) -> None:
        t = self.query_one("#top-table", DataTable)
        row = t.cursor_row
        t.clear()
        for i, s in enumerate(self.model.sources):
            tid, how = self.model.resolve_topic(s.effective_name(), self.titles)
            t.add_row(cell(s.effective_name()), cell(tid if tid is not None else "—"), cell(how), key=str(i))
        if self.model.sources:
            t.move_cursor(row=min(row, len(self.model.sources) - 1))
        g = self.query_one("#grp-table", DataTable)
        g.clear()
        for tid, title in sorted(self.titles.items()):
            g.add_row(cell(tid), cell(title), key=str(tid))
        lt = self.model.ab.get("log_topic", "")
        if "#top-status" not in self.status_texts or self.status_texts["#top-status"].startswith("Топиков загружено"):
            self.set_status("#top-status",
                            f"Топиков загружено: {len(self.titles)}. Топик логов: {lt or 'не задан (нужен для полного бэкапа)'}")

    def _src_label(self, s: Source) -> str:
        st = self.model.archive_status(s.effective_name())
        if not st["built"]:
            state = "не собрано"
        else:
            up = f"загружено {st['uploaded']}/{st['volumes']}" if st["volumes"] else "—"
            state = f"собрано {st['date']} · {human_size(st['bytes'])} · {up}"
        return f"{s.effective_name()}  ←  {s.path}   [{state}]"

    def refresh_run(self) -> None:
        sl = self.query_one("#run-src", SelectionList)
        keep = set(sl.selected) if sl.option_count else None
        sl.clear_options()
        sl.add_options([(Text(self._src_label(s)), s.effective_name(), keep is None or s.effective_name() in keep)
                        for s in self.model.sources])
        self.update_run_preview()

    def refresh_preview(self) -> None:
        ab = self.model.ab
        try:
            when = datetime(2026, 11, 7).strftime(ab["date_format"])
            fmt = dict(n=7, date=when)
            text = (f"Пример меток: топик логов → «{ab['log_message'].format(**fmt)}», "
                    f"топик директории → «{ab['dir_message'].format(**fmt)}», при частичном бэкапе → «{when}»")
        except (KeyError, ValueError, IndexError) as e:
            text = f"Ошибка в формате меток: {e!r}"
        self.query_one("#p-preview", Static).update(text)

    # ------------------------------------------------------------------ ввод в полях
    @on(Input.Changed)
    def _input_changed(self, event: Input.Changed) -> None:
        spec = INPUT_FIELDS.get(event.input.id or "")
        if not spec:
            return
        store = getattr(self.model, spec[0])
        if store.get(spec[1], "") != event.value:
            store[spec[1]] = event.value
            self.touch()
            if spec[0] == "ab":
                self.refresh_preview()
                self.refresh_topics()

    @on(Switch.Changed)
    def _switch_changed(self, event: Switch.Changed) -> None:
        sid = event.switch.id or ""
        if sid in SWITCH_FIELDS:
            new = "1" if event.value else "0"
            if self.model.env.get(SWITCH_FIELDS[sid]) != new:
                self.model.env[SWITCH_FIELDS[sid]] = new
                self.touch()
        elif sid.startswith("run-"):
            self.update_run_preview()
        elif sid == "log-follow":
            self.query_one("#log-view", RichLog).auto_scroll = event.value

    @on(TextArea.Changed, "#excl-text")
    def _excl_changed(self, event: TextArea.Changed) -> None:
        if self.model.excludes != event.text_area.text:
            self.model.excludes = event.text_area.text
            self.touch()

    @on(RadioSet.Changed)
    def _radio_changed(self, event: RadioSet.Changed) -> None:
        self.update_run_preview()

    @on(SelectionList.SelectedChanged)
    def _sel_changed(self, event: SelectionList.SelectedChanged) -> None:
        self.update_run_preview()

    # ------------------------------------------------------------------ вкладка «Запуск»
    def _radio_value(self, rs_id: str, prefix: str) -> str:
        pressed = self.query_one(rs_id, RadioSet).pressed_button
        return (pressed.id or "")[len(prefix):] if pressed else ""

    def run_command(self, mode: str = "build", yes: bool = False) -> list[str]:
        def sw(i: str) -> bool:
            return self.query_one(i, Switch).value
        selected = list(self.query_one("#run-src", SelectionList).selected)
        names = [s.effective_name() for s in self.model.sources]
        return build_command(self.model.root, mode, self._radio_value("#run-layout", "lay-") or "config", sw("#run-dry"),
                             sw("#run-delete"), sw("#run-encrypt"), sw("#run-single"), sw("#run-fresh"),
                             selected, names, yes)

    def update_run_preview(self) -> None:
        try:
            sel = self.query_one("#run-src", SelectionList).selected
            if not sel:
                self.query_one("#run-cmd", Static).update("Выберите хотя бы одну директорию")
                return
            lines = [f"{label}: " + " ".join(shlex.quote(c) for c in self.run_command(mode))
                     for label, mode in (("Собрать", "build"), ("Загрузить", "upload"), ("Всё", "all"))]
            self.query_one("#run-cmd", Static).update("\n".join(lines))
        except Exception:  # noqa: BLE001 — интерфейс ещё не собран
            return

    # ------------------------------------------------------------------ действия: общие
    def _active_tab(self) -> str:
        return self.query_one(TabbedContent).active

    def action_help(self) -> None:
        self.push_screen(ConfirmModal("Справка", HELP_TEXT, [("Закрыть", "close", "primary")]))

    def action_save(self) -> bool:
        try:
            written = self.model.save()
        except ValueError as e:
            self.push_screen(ConfirmModal("Не сохранено: исправьте ошибки", str(e), [("Понятно", "ok", "primary")]))
            return False
        except OSError as e:
            self.say(f"Ошибка записи: {e}", "error")
            return False
        self._update_title()
        self.say("Сохранено: " + (", ".join(p.name for p in written) if written else "изменений нет"))
        return True

    def action_request_quit(self) -> None:
        def finish() -> None:
            if self.runner:
                self.runner.stop(hard=True)
            self.exit()

        def done_busy(key: str | None) -> None:
            if key == "stop":
                finish()

        def done_dirty(key: str | None) -> None:
            if key == "save":
                if self.action_save():
                    finish()
            elif key == "discard":
                finish()

        if self.busy:
            self.push_screen(ConfirmModal("Выход", "Сейчас идёт процесс (сборка или загрузка). Остановить его и выйти?",
                                          [("Остановить и выйти", "stop", "error"), ("Остаться", "stay", "primary")]),
                             done_busy)
        elif self.model.dirty:
            self.push_screen(ConfirmModal("Выход", "Есть несохранённые изменения.",
                                          [("Сохранить и выйти", "save", "success"),
                                           ("Выйти без сохранения", "discard", "error"), ("Отмена", "cancel", "default")]),
                             done_dirty)
        else:
            finish()

    # ------------------------------------------------------------------ действия: источники
    def _cursor_source(self, table_id: str = "#src-table") -> int | None:
        t = self.query_one(table_id, DataTable)
        if not self.model.sources or t.row_count == 0:
            return None
        return t.cursor_row

    def _add_source(self, path: str | None) -> None:
        if not path:
            return
        new = Source(path=path)
        target = expand(path)
        if any(expand(s.path) == target for s in self.model.sources):
            self.say("Эта директория уже в списке", "warning")
            return
        if not target.is_dir():
            self.say(f"Внимание: {target} сейчас не существует (запись всё равно добавлена)", "warning")
        self.model.sources.append(new)
        self.touch()
        self.refresh_all()

    def action_src_add_tree(self) -> None:
        if self._active_tab() == "tab-src":
            self.push_screen(DirPickerModal("~"), self._add_source)

    def action_src_add_path(self) -> None:
        if self._active_tab() == "tab-src":
            self.push_screen(PromptModal("Путь к директории", value="~/", placeholder="~/projects"), self._add_source)

    def action_src_edit(self) -> None:
        if self._active_tab() != "tab-src":
            return
        idx = self._cursor_source()
        if idx is None:
            return
        old = self.model.sources[idx]

        def done(new: Source | None) -> None:
            if new is None:
                return
            old_name, new_name = old.effective_name(), new.effective_name()
            if old_name != new_name:  # ручной топик переезжает вместе с именем
                key = self.model.topic_key(old_name)
                if key is not None:
                    self.model.topics[new_name] = self.model.topics.pop(key)
            self.model.sources[idx] = new
            self.touch()
            self.refresh_all()

        self.push_screen(SourceEditModal(old), done)

    def action_src_delete(self) -> None:
        if self._active_tab() != "tab-src":
            return
        idx = self._cursor_source()
        if idx is None:
            return
        s = self.model.sources[idx]

        def done(key: str | None) -> None:
            if key == "yes":
                del self.model.sources[idx]
                self.touch()
                self.refresh_all()

        self.push_screen(ConfirmModal("Убрать из списка?", f"{s.path}\n(сами файлы и ранее собранные архивы не затрагиваются)",
                                      [("Убрать", "yes", "error"), ("Отмена", "no", "default")]), done)

    @on(Button.Pressed, "#src-add-tree")
    def _b1(self) -> None:
        self.push_screen(DirPickerModal("~"), self._add_source)

    @on(Button.Pressed, "#src-add-path")
    def _b2(self) -> None:
        self.push_screen(PromptModal("Путь к директории", value="~/", placeholder="~/projects"), self._add_source)

    @on(Button.Pressed, "#src-edit")
    def _b3(self) -> None:
        self.action_src_edit()

    @on(Button.Pressed, "#src-del")
    def _b4(self) -> None:
        self.action_src_delete()

    # ------------------------------------------------------------------ Telegram: общая обвязка
    async def ask(self, title: str, value: str = "", placeholder: str = "", password: bool = False) -> str | None:
        """Ввод строки из воркера."""
        return await self.push_screen_wait(PromptModal(title, value, placeholder, password))

    def _net_guard(self) -> bool:
        if self.busy:
            self.say("Идёт сборка/загрузка: сессия Telegram занята — дождитесь завершения", "warning")
            return False
        if self.net_busy:
            self.say("Предыдущий запрос к Telegram ещё выполняется", "warning")
            return False
        return True

    async def _net_job(self, label: str, status_id: str, make_coro, on_ok) -> None:
        """Запрос к Telegram: статус, журнал, понятная ошибка вместо молчания. Ничего не бросает наружу."""
        self.net_busy = True
        self.set_status(status_id, f"{label}…")
        self.log_line("dim", f"— {label}")
        try:
            result = await make_coro()
        except asyncio.CancelledError:
            self.log_line("warn", f"{label}: отменено")
            raise
        except BaseException as e:  # noqa: BLE001 — в т.ч. SystemExit из чужого кода: интерфейс не должен падать
            msg = str(e) or type(e).__name__
            if not isinstance(e, (RuntimeError, ValueError)):
                msg = f"{type(e).__name__}: {msg}"
                self.log_line("err", "".join(traceback.format_exception(type(e), e, e.__traceback__)))
            self.log_line("err", f"{label}: {msg}")
            self.set_status(status_id, f"Ошибка: {msg}   (подробности — вкладка «Журнал», F5)")
            self.say(f"{label}: {msg}", "error")
        else:
            on_ok(result)
        finally:
            self.net_busy = False

    # ------------------------------------------------------------------ Telegram: топики
    @on(Button.Pressed, "#top-fetch")
    def _fetch_topics(self) -> None:
        if self._net_guard():
            self.run_worker(self._net_job("Загрузка топиков", "#top-status",
                                          lambda: self.remote.topics(self.model, self._proc_line), self._topics_loaded),
                            exclusive=True, group="net", exit_on_error=False)

    def _topics_loaded(self, titles: dict[int, str]) -> None:
        self.titles = titles
        self.log_line("ok", f"✓ Топиков получено: {len(titles)}")
        self.set_status("#top-status", f"Топиков загружено: {len(titles)}. Топик логов: "
                                       f"{self.model.ab.get('log_topic') or 'не задан (нужен для полного бэкапа)'}")
        self.refresh_all()
        self.say(f"Топиков получено: {len(titles)}")

    def _topic_options(self) -> list[tuple[str, object]]:
        opts: list[tuple[str, object]] = [(f"{t}   ·   id {i}", i) for i, t in sorted(self.titles.items())]
        opts.append(("✎ ввести id вручную", "manual"))
        return opts

    def _assign_topic_to(self, name: str, picked: tuple | None) -> None:
        if picked is None:
            return
        value = picked[0]
        if value == "manual":
            def typed(text: str | None) -> None:
                if text is None:
                    return
                if not re.fullmatch(r"\d+", text.strip()):
                    self.say("id топика — целое число", "error")
                    return
                self._store_topic(name, int(text.strip()))
            self.push_screen(PromptModal(f"id топика для «{name}»", placeholder="например 12"), typed)
        else:
            self._store_topic(name, int(value))

    def _store_topic(self, name: str, tid: int) -> None:
        key = self.model.topic_key(name) or name
        self.model.topics[key] = tid
        self.touch()
        self.refresh_all()

    def action_top_assign(self) -> None:
        if self._active_tab() == "tab-top":
            self._assign_topic()

    @on(Button.Pressed, "#top-assign")
    def _assign_topic(self) -> None:
        idx = self._cursor_source("#top-table")
        if idx is None:
            self.say("Сначала добавьте директории на вкладке «Источники»", "warning")
            return
        name = self.model.sources[idx].effective_name()
        hint = "" if self.titles else "Список топиков не загружен — можно ввести id вручную"
        self.push_screen(PickModal(f"Топик для «{name}»", self._topic_options(), hint), partial(self._assign_topic_to, name))

    @on(Button.Pressed, "#top-reset")
    def _reset_topic(self) -> None:
        idx = self._cursor_source("#top-table")
        if idx is None:
            return
        key = self.model.topic_key(self.model.sources[idx].effective_name())
        if key is not None:
            del self.model.topics[key]
            self.touch()
            self.refresh_all()

    @on(Button.Pressed, "#top-log")
    def _log_topic(self) -> None:
        def picked(res: tuple | None) -> None:
            if res is None:
                return
            if res[0] == "manual":
                def typed(text: str | None) -> None:
                    if text and text.strip():
                        self.query_one("#p-logtopic", Input).value = text.strip()
                self.push_screen(PromptModal("Топик логов: id или название", placeholder="Logs"), typed)
            else:
                self.query_one("#p-logtopic", Input).value = str(res[0])

        self.push_screen(PickModal("Топик для меток autoback (логов)", self._topic_options()), picked)

    # ------------------------------------------------------------------ Telegram: вход, проверка, группа
    @on(Button.Pressed, "#tg-show-hash")
    def _toggle_hash(self) -> None:
        inp = self.query_one("#tg-api-hash", Input)
        inp.password = not inp.password

    @on(Button.Pressed, "#tg-login")
    def _login(self) -> None:
        """Вход выполняет tg_upload.py login в настоящем терминале: телефон, код и пароль 2FA вводятся там же,
        как в ./backup.sh tg login. TUI на это время приостанавливается и затем возвращается."""
        if not self._net_guard():
            return
        try:
            self.model.save_telegram()
            argv = self.remote.login_argv(self.model)
        except ValueError as e:
            self.say(str(e), "error")
            return
        self.log_line("dim", "$ " + shlex.join(argv))
        rc: int | None = None
        try:
            with self.suspend():
                print("\n=== Вход в Telegram (то же, что ./backup.sh tg login) ===\n")
                rc = subprocess.run(argv, cwd=self.model.root, check=False).returncode
                try:
                    input("\n[Enter — вернуться в TUI] ")
                except (EOFError, OSError):
                    pass
        except Exception as e:  # noqa: BLE001 — например, терминал не поддерживает приостановку
            self.log_line("err", f"Не удалось приостановить TUI для входа: {e!r}. Выполните в терминале: ./backup.sh tg login")
            self.set_status("#tg-status", "Вход: выполните в терминале ./backup.sh tg login")
            return
        if rc == 0:
            self.log_line("ok", "✓ Вход выполнен (tg_upload.py login завершился успешно)")
            self.set_status("#tg-status", "Вход выполнен. Нажмите «Проверить соединение».")
            self._check()
        else:
            self.log_line("err", f"Вход не удался: код возврата {rc}")
            self.set_status("#tg-status", f"Вход не удался (код {rc}); см. вывод выше или «Журнал»")

    @on(Button.Pressed, "#tg-check")
    def _check(self) -> None:
        if self._net_guard():
            self.show_tab("tab-log")  # шаги диагностики — сразу на виду
            self.run_worker(self._net_job("Проверка соединения", "#tg-status",
                                          lambda: self.remote.check(self.model, self._proc_line), self._check_ok),
                            exclusive=True, group="net", exit_on_error=False)

    def _check_ok(self, text: str) -> None:
        self.log_line("ok", "✓ " + text.replace("\n", " · "))
        self.set_status("#tg-status", text)

    @on(Button.Pressed, "#tg-pick-group")
    def _pick_group(self) -> None:
        if self._net_guard():
            self.run_worker(self._pick_group_job(), exclusive=True, group="net", exit_on_error=False)

    async def _pick_group_job(self) -> None:
        holder: list[list[tuple[int, str, str]]] = []
        await self._net_job("Список групп", "#tg-status", lambda: self.remote.groups(self.model, self._proc_line), holder.append)
        if not holder:
            return
        forums = [g for g in holder[0] if g[1] == "forum"]
        self.set_status("#tg-status", f"Групп найдено: {len(holder[0])}, из них форумов: {len(forums)}")
        if not forums:
            self.say("Нет групп с включёнными темами (форумов)", "warning")
            return
        res = await self.push_screen_wait(PickModal("Выберите группу-форум", [(f"{n}   ·   {i}", i) for i, _, n in forums]))
        if res is not None:
            self.query_one("#tg-group", Input).value = str(res[0])

    # ------------------------------------------------------------------ параметры
    @on(Button.Pressed, "#p-genkey")
    def _gen_key(self) -> None:
        try:
            pub = generate_age_key(self.model.root)
        except RuntimeError as e:
            self.say(str(e), "error")
            return
        self.query_one("#p-age", Input).value = pub
        self.notify("Ключ создан: .secrets/age.key. СОХРАНИТЕ его отдельно — без него зашифрованные архивы не открыть.",
                    timeout=12, markup=False)

    @on(Button.Pressed, "#excl-default")
    def _excl_default(self) -> None:
        p = self.model.excl_default
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        self.query_one("#excl-text", TextArea).load_text(text)

    # ------------------------------------------------------------------ запуск: сборка / загрузка
    @on(Button.Pressed, "#run-build")
    @on(Button.Pressed, "#run-upload")
    @on(Button.Pressed, "#run-all")
    def _run_pressed(self, event: Button.Pressed) -> None:
        mode = RUN_MODES[event.button.id or ""]
        self.run_worker(self._run_flow(mode), exclusive=True, group="run", exit_on_error=False)

    async def _ensure_sudo(self) -> bool:
        ok = await asyncio.to_thread(lambda: subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0)
        if ok:
            return True
        self.log_line("info", "Нужен sudo для системных директорий: подтвердите пароль в терминале…")
        try:
            with self.suspend():
                print("\nДля чтения системных директорий нужен sudo. Введите пароль:\n")
                rc = subprocess.run(["sudo", "-v"], check=False).returncode
        except Exception as e:  # noqa: BLE001
            self.log_line("err", f"Не удалось запросить sudo: {e!r}. Выполните в терминале sudo -v и повторите")
            return False
        return rc == 0

    async def _run_flow(self, mode: str) -> None:
        try:
            await self._run_flow_inner(mode)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            self.log_line("err", "Сбой запуска:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
            self.say(f"Сбой запуска: {e!r}", "error")
            self.busy = False
            self._set_running_ui(False)

    async def _run_flow_inner(self, mode: str) -> None:
        if self.busy:
            self.say("Процесс уже выполняется — дождитесь завершения или остановите его в «Журнале»", "warning")
            return
        if self.net_busy:
            self.say("Дождитесь завершения запроса к Telegram", "warning")
            return
        selected = list(self.query_one("#run-src", SelectionList).selected)
        if not selected:
            self.say("Выберите хотя бы одну директорию", "warning")
            return
        if self.model.dirty and not self.action_save():
            return
        chosen = [s for s in self.model.sources if s.effective_name() in selected]
        dry = self.query_one("#run-dry", Switch).value
        enc_all = self.query_one("#run-encrypt", Switch).value
        delete = self.query_one("#run-delete", Switch).value

        if mode in ("build", "all") and any(s.needs_sudo() for s in chosen):
            if not await self._ensure_sudo():
                self.log_line("err", "Запуск отменён: sudo не подтверждён")
                self.show_tab("tab-log")
                return

        yes = False
        if mode in ("upload", "all") and not dry:
            risky = [s.effective_name() for s in chosen
                     if SENSITIVE_RE.search(s.effective_name()) and not (s.encrypt or enc_all)]
            if risky:
                ans = await self.push_screen_wait(ConfirmModal(
                    "Незашифрованные «секретные» папки",
                    "Чаты Telegram не имеют сквозного шифрования. Эти папки уйдут БЕЗ шифрования:\n  "
                    + "\n  ".join(risky) + "\n\nВключить шифрование можно на вкладке «Источники» (e → Шифровать age).",
                    [("Всё равно загрузить", "yes", "error"), ("Отмена", "no", "primary")]))
                if ans != "yes":
                    self.log_line("warn", "Загрузка отменена пользователем (незашифрованные секретные папки)")
                    return
                yes = True
            if delete:
                ans = await self.push_screen_wait(ConfirmModal(
                    "Удалить локальные тома после выгрузки?",
                    "После успешной загрузки локальные тома будут УДАЛЕНЫ (останутся .sha256 и MANIFEST.txt).",
                    [("Да, удалять", "yes", "error"), ("Отмена", "no", "primary")]))
                if ans != "yes":
                    self.log_line("warn", "Запуск отменён пользователем (удаление локальных томов)")
                    return
                yes = True

        argv = self.run_command(mode, yes=yes)
        title = {"build": "Сборка", "upload": "Загрузка в Telegram", "all": "Сборка и загрузка"}[mode]
        await self._execute(title + (" (пробный запуск)" if dry else ""), argv)

    def _set_running_ui(self, running: bool) -> None:
        for bid in RUN_MODES:
            self.query_one(f"#{bid}", Button).disabled = running
        self.query_one("#log-stop", Button).disabled = not running
        self._update_title()

    async def _execute(self, title: str, argv: list[str]) -> None:
        self.show_tab("tab-log")        # сначала уводим фокус с кнопок запуска, затем блокируем их
        self.busy = True
        self._set_running_ui(True)
        self.set_status("#log-status", f"▶ {title}…")
        self.log_line("dim", f"════ {title} · {time.strftime('%F %T')} ════")
        self.log_line("dim", "$ " + shlex.join(argv))
        self.runner = ProcessRunner(self._proc_line, self._proc_progress)
        code: int | None = None
        try:
            code = await self.runner.run(argv, self.model.root)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            self.log_line("err", f"Не удалось запустить: {e!r}")
        finally:
            stopped = self.runner.stopped if self.runner else False
            self.busy = False
            self._set_running_ui(False)
            self._proc_progress("")
        if code == 0:
            self.log_line("ok", f"✓ {title}: завершено успешно")
            self.set_status("#log-status", f"✓ {title}: завершено успешно")
            self.say(f"{title}: завершено успешно")
        else:
            why = "остановлено пользователем" if stopped else f"код возврата {code}"
            self.log_line("err", f"✗ {title}: {why}")
            self.set_status("#log-status", f"✗ {title}: {why}")
            self.say(f"{title}: {why} — см. «Журнал»", "error")
        self.refresh_all()

    # ------------------------------------------------------------------ вкладка «Журнал»
    @on(Button.Pressed, "#log-stop")
    def _stop(self) -> None:
        if self.runner and self.busy:
            self.log_line("warn", f"Остановка… (Ctrl+C процессу; если не завершится — через {STOP_GRACE_INT:.0f} с SIGTERM, затем SIGKILL)")
            self.runner.stop()

    @on(Button.Pressed, "#log-clear")
    def _clear_log(self) -> None:
        self.query_one("#log-view", RichLog).clear()
        self.log_history.clear()

    @on(Button.Pressed, "#log-load")
    def _load_backup_log(self) -> None:
        p = self.model.root / "backup.log"
        if not p.exists():
            self.say("backup.log ещё не создан", "warning")
            return
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        self.log_line("dim", f"──── последние {len(lines)} строк backup.log ────")
        for ln in lines:
            self._proc_line(ln)
        self.log_line("dim", "──── конец backup.log ────")


def main() -> int:
    root = Path(__file__).resolve().parent
    app = BackupUtilApp(root)
    try:
        app.run()
    except BaseException:
        app._file_log("err", "СБОЙ TUI (верхний уровень):\n" + traceback.format_exc())
        print(f"\nTUI аварийно завершился. Подробности: {app.log_path}", file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
