#!/usr/bin/env python3
"""
tui.py — терминальный интерфейс backupUtil (Textual).

Запуск:  ./backup.sh tui      (или  .venv/bin/python tui.py)

Вкладки:
  Источники   какие директории бэкапить: дерево каталогов, имя, режим (элементы/целиком), шифрование, sudo
  Топики      в какой топик Telegram какая директория уходит (список топиков берётся из группы)
  Telegram    api_id / api_hash / группа / сессия, вход в аккаунт
  Параметры   размер тома, уровень zstd, нарезка, ключ age, формат меток autoback
  Исключения  редактор excludes.txt
  Запуск      собрать / загрузить / всё сразу — выбор директорий и флагов, предпросмотр команды

Ctrl+S — сохранить всё, Ctrl+Q — выйти, F1 — помощь.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from datetime import datetime
from functools import partial
from pathlib import Path

from bu_config import Model, Source, expand

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, DataTable, DirectoryTree, Footer, Header, Input, Label, OptionList,
                                 RadioButton, RadioSet, SelectionList, Static, Switch, TabbedContent, TabPane,
                                 TextArea)
    from textual.widgets.option_list import Option
except ImportError:  # pragma: no cover
    sys.exit("Не установлен textual. Выполните:\n"
             "  python -m venv .venv && .venv/bin/pip install -r requirements.txt")


# =========================================================================== Telegram (подменяется в тестах)
class Remote:
    """Все обращения к Telegram. Вынесено в класс, чтобы тесты и интерфейс не зависели от сети."""

    @staticmethod
    def _tg():
        try:
            import tg_upload
        except SystemExit as e:  # tg_upload завершает процесс с сообщением, если нет telethon
            raise RuntimeError(str(e)) from None
        return tg_upload

    async def _client(self, model: Model):
        tg = self._tg()
        cfg = model.telegram_cfg()
        client = tg.make_client(cfg)
        await client.connect()
        tg.lock_session_files(cfg)
        return tg, cfg, client

    async def _authorized(self, tg, client):
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована — нажмите «Войти» на вкладке Telegram")

    async def check(self, model: Model) -> str:
        tg, cfg, client = await self._client(model)
        try:
            await self._authorized(tg, client)
            me = await client.get_me()
            text = f"Вход выполнен: {me.first_name} (@{me.username})"
            if cfg["group"] is not None:
                try:
                    entity = await tg.resolve_group(client, cfg["group"])
                    kind = "форум" if getattr(entity, "forum", False) else "НЕ форум (топиков нет)"
                    text += f"\nГруппа: {getattr(entity, 'title', entity)} — {kind}"
                except SystemExit as e:
                    text += f"\nГруппа не найдена: {e}"
            return text
        finally:
            await client.disconnect()

    async def topics(self, model: Model) -> dict[int, str]:
        tg, cfg, client = await self._client(model)
        try:
            await self._authorized(tg, client)
            try:
                entity = await tg.resolve_group(client, cfg["group"])
            except SystemExit as e:
                raise RuntimeError(str(e)) from None
            if not getattr(entity, "forum", False):
                raise RuntimeError("Эта группа не форум — включите «Темы» в настройках группы")
            return await tg.fetch_topics(client, entity)
        finally:
            await client.disconnect()

    async def groups(self, model: Model) -> list[tuple[int, str, str]]:
        tg, cfg, client = await self._client(model)
        try:
            await self._authorized(tg, client)
            out = []
            async for d in client.iter_dialogs():
                if getattr(d.entity, "megagroup", False):
                    out.append((d.id, "forum" if getattr(d.entity, "forum", False) else "group", d.name))
            return out
        finally:
            await client.disconnect()

    async def login(self, app: "BackupUtilApp", model: Model) -> str:
        tg, cfg, client = await self._client(model)
        try:
            if await client.is_user_authorized():
                return "Уже авторизованы"
            phone = await app.ask("Телефон в международном формате", placeholder="+998901234567")
            if not phone:
                return "Отменено"
            await client.send_code_request(phone.strip())
            code = await app.ask("Код из Telegram (придёт в приложение или SMS)")
            if not code:
                return "Отменено"
            try:
                await client.sign_in(phone.strip(), code.strip())
            except tg.errors.SessionPasswordNeededError:
                pw = await app.ask("Пароль двухфакторной защиты (2FA)", password=True)
                if not pw:
                    return "Отменено"
                await client.sign_in(password=pw)
            tg.lock_session_files(cfg)
            me = await client.get_me()
            return f"Вход выполнен: {me.first_name} (@{me.username}). Сессия сохранена (права 600)"
        finally:
            await client.disconnect()


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
                  single: bool, fresh: bool, selected: list[str], all_names: list[str]) -> list[str]:
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
    if selected and set(selected) != set(all_names):
        cmd += ["--", *selected]
    return cmd


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
Ctrl+S  сохранить всё            Ctrl+Q  выйти
Вкладки: Источники · Топики · Telegram · Параметры · Исключения · Запуск

Источники:  a — добавить из дерева, p — по пути, e — изменить, d — удалить
Топики:     «Загрузить топики» подтягивает список из группы; клавиша t или кнопка «Назначить топик» — выбрать топик для строки.
            Если ничего не назначено, топик ищется по названию папки без учёта регистра.
Запуск:     перед стартом всё сохраняется; TUI на время уступает терминал backup.sh
            (там пароль sudo, прогресс и ввод), затем возвращается.

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
    #cols { height: 1fr; }
    #cols > Vertical { width: 1fr; height: 100%; padding-right: 1; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "Сохранить"),
        Binding("ctrl+q", "request_quit", "Выход"),
        Binding("f1", "help", "Помощь"),
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
        self.remote = remote or Remote()
        self.titles: dict[int, str] = {}
        self.sub_title = ""

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
                    yield Button("Назначить топик", id="top-assign")
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
                        yield Button("Выбрать группу", id="tg-pick-group")
                        yield Button("Проверить", id="tg-check")
                    yield Static(id="tg-status", classes="hint", markup=False)
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
                    yield Label("Режим", classes="dlg-title")
                    with RadioSet(id="run-mode"):
                        yield RadioButton("build — только собрать архивы", id="mode-build", value=True)
                        yield RadioButton("upload — только загрузить собранное", id="mode-upload")
                        yield RadioButton("all — собрать и загрузить", id="mode-all")
                    yield Label("Раскладка архивов", classes="dlg-title field")
                    with RadioSet(id="run-layout"):
                        yield RadioButton("как в backup.conf", id="lay-config", value=True)
                        yield RadioButton("--items: каждый элемент — свой архив", id="lay-items")
                        yield RadioButton("--whole: вся директория одним архивом", id="lay-whole")
                    for sid, text in (("run-dry", "Пробный запуск (--dry-run): ничего не писать"),
                                      ("run-single", "Без нарезки на тома (--single)"),
                                      ("run-encrypt", "Шифровать всё (--encrypt)"),
                                      ("run-delete", "Удалить локальные тома после выгрузки (--delete-after-upload)"),
                                      ("run-fresh", "Начать новый номер autoback#N (--fresh)")):
                        with Horizontal(classes="swrow"):
                            yield Switch(value=False, id=sid)
                            yield Label(" " + text, markup=False)
                    yield Label("Директории (все выбраны = полный бэкап с метками autoback#N)", classes="dlg-title field")
                    yield SelectionList[str](id="run-src")
                    yield Static(id="run-cmd", classes="hint", markup=False)
                    with Horizontal(classes="btnrow"):
                        yield Button("Сохранить и запустить", id="run-go", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#src-table", DataTable).add_columns("Имя", "Путь", "Режим", "Шифр.", "sudo", "Топик")
        self.query_one("#top-table", DataTable).add_columns("Имя", "Топик", "Как определён")
        self.query_one("#grp-table", DataTable).add_columns("ID", "Название")
        self.refresh_all()
        self._update_title()

    # ------------------------------------------------------------------ обновление данных
    def touch(self) -> None:
        self.model.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        self.sub_title = "● есть несохранённые изменения (Ctrl+S)" if self.model.dirty else "сохранено"

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
        self.query_one("#top-status", Static).update(
            f"Топиков загружено: {len(self.titles)}. Топик логов: {lt or 'не задан (нужен для полного бэкапа)'}")

    def refresh_run(self) -> None:
        sl = self.query_one("#run-src", SelectionList)
        keep = set(sl.selected) if sl.option_count else None
        sl.clear_options()
        names = [s.effective_name() for s in self.model.sources]
        sl.add_options([(Text(f"{n}  ←  {s.path}"), n, keep is None or n in keep)
                        for n, s in zip(names, self.model.sources)])
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

    def run_command(self) -> list[str]:
        sw = lambda i: self.query_one(i, Switch).value  # noqa: E731
        selected = list(self.query_one("#run-src", SelectionList).selected)
        names = [s.effective_name() for s in self.model.sources]
        return build_command(self.model.root, self._radio_value("#run-mode", "mode-") or "build",
                             self._radio_value("#run-layout", "lay-") or "config", sw("#run-dry"),
                             sw("#run-delete"), sw("#run-encrypt"), sw("#run-single"), sw("#run-fresh"),
                             selected, names)

    def update_run_preview(self) -> None:
        try:
            sel = self.query_one("#run-src", SelectionList).selected
        except Exception:  # noqa: BLE001 — интерфейс ещё не собран
            return
        if not sel:
            self.query_one("#run-cmd", Static).update("Выберите хотя бы одну директорию")
            return
        cmd = self.run_command()
        self.query_one("#run-cmd", Static).update("Команда: " + " ".join(shlex.quote(c) for c in cmd))

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
            self.notify(f"Ошибка записи: {e}", severity="error")
            return False
        self._update_title()
        self.notify("Сохранено: " + (", ".join(p.name for p in written) if written else "изменений нет"))
        return True

    def action_request_quit(self) -> None:
        if not self.model.dirty:
            self.exit()
            return

        def done(key: str | None) -> None:
            if key == "save":
                if self.action_save():
                    self.exit()
            elif key == "discard":
                self.exit()

        self.push_screen(ConfirmModal("Выход", "Есть несохранённые изменения.",
                                      [("Сохранить и выйти", "save", "success"), ("Выйти без сохранения", "discard", "error"),
                                       ("Отмена", "cancel", "default")]), done)

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
            self.notify("Эта директория уже в списке", severity="warning")
            return
        if not target.is_dir():
            self.notify(f"Внимание: {target} сейчас не существует (запись всё равно добавлена)", severity="warning")
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

    # ------------------------------------------------------------------ действия: топики
    async def ask(self, title: str, value: str = "", placeholder: str = "", password: bool = False) -> str | None:
        """Ввод строки из воркера (нужен для многошаговой авторизации)."""
        return await self.push_screen_wait(PromptModal(title, value, placeholder, password))

    def _set_status(self, widget_id: str, text: str) -> None:
        self.query_one(widget_id, Static).update(text)

    @on(Button.Pressed, "#top-fetch")
    def _fetch_topics(self) -> None:
        self.run_worker(self._fetch_topics_job(), exclusive=True, group="net")

    async def _fetch_topics_job(self) -> None:
        self._set_status("#top-status", "Подключаюсь к Telegram…")
        try:
            self.titles = await self.remote.topics(self.model)
        except Exception as e:  # noqa: BLE001 — любая сетевая/конфигурационная ошибка → в интерфейс
            self._set_status("#top-status", f"Ошибка: {e}")
            self.notify(f"Не удалось получить топики: {e}", severity="error")
            return
        self.refresh_all()
        self.notify(f"Топиков получено: {len(self.titles)}")

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
                    self.notify("id топика — целое число", severity="error")
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
            self.notify("Сначала добавьте директории на вкладке «Источники»", severity="warning")
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

    # ------------------------------------------------------------------ действия: Telegram
    @on(Button.Pressed, "#tg-show-hash")
    def _toggle_hash(self) -> None:
        inp = self.query_one("#tg-api-hash", Input)
        inp.password = not inp.password

    @on(Button.Pressed, "#tg-login")
    def _login(self) -> None:
        self.run_worker(self._login_job(), exclusive=True, group="net")

    async def _login_job(self) -> None:
        self._set_status("#tg-status", "Вход…")
        try:
            msg = await self.remote.login(self, self.model)
        except Exception as e:  # noqa: BLE001
            msg = f"Ошибка входа: {e}"
        self._set_status("#tg-status", msg)
        self.notify(msg)

    @on(Button.Pressed, "#tg-check")
    def _check(self) -> None:
        self.run_worker(self._check_job(), exclusive=True, group="net")

    async def _check_job(self) -> None:
        self._set_status("#tg-status", "Проверяю…")
        try:
            msg = await self.remote.check(self.model)
        except Exception as e:  # noqa: BLE001
            msg = f"Ошибка: {e}"
        self._set_status("#tg-status", msg)

    @on(Button.Pressed, "#tg-pick-group")
    def _pick_group(self) -> None:
        self.run_worker(self._pick_group_job(), exclusive=True, group="net")

    async def _pick_group_job(self) -> None:
        self._set_status("#tg-status", "Загружаю список групп…")
        try:
            groups = await self.remote.groups(self.model)
        except Exception as e:  # noqa: BLE001
            self._set_status("#tg-status", f"Ошибка: {e}")
            return
        forums = [g for g in groups if g[1] == "forum"]
        self._set_status("#tg-status", f"Групп найдено: {len(groups)}, из них форумов: {len(forums)}")
        if not forums:
            self.notify("Нет групп с включёнными темами (форумов)", severity="warning")
            return
        res = await self.push_screen_wait(PickModal("Выберите группу-форум", [(f"{n}   ·   {i}", i) for i, _, n in forums]))
        if res is not None:
            self.query_one("#tg-group", Input).value = str(res[0])

    # ------------------------------------------------------------------ действия: параметры
    @on(Button.Pressed, "#p-genkey")
    def _gen_key(self) -> None:
        try:
            pub = generate_age_key(self.model.root)
        except RuntimeError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.query_one("#p-age", Input).value = pub
        self.notify("Ключ создан: .secrets/age.key. СОХРАНИТЕ его отдельно — без него зашифрованные архивы не открыть.",
                    timeout=12)

    @on(Button.Pressed, "#excl-default")
    def _excl_default(self) -> None:
        p = self.model.excl_default
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        self.query_one("#excl-text", TextArea).load_text(text)

    # ------------------------------------------------------------------ запуск backup.sh
    @on(Button.Pressed, "#run-go")
    def _run_go(self) -> None:
        if not self.query_one("#run-src", SelectionList).selected:
            self.notify("Выберите хотя бы одну директорию", severity="warning")
            return
        if self.model.dirty and not self.action_save():
            return
        cmd = self.run_command()
        with self.suspend():
            print("\n$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
            try:
                subprocess.run(cmd, cwd=self.model.root, check=False)
            except KeyboardInterrupt:
                print("\nПрервано.")
            try:
                input("\n[Enter — вернуться в TUI] ")
            except EOFError:
                pass
        self.refresh_all()


def main() -> int:
    root = Path(__file__).resolve().parent
    BackupUtilApp(root).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
