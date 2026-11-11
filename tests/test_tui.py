"""Тесты TUI на Textual Pilot (без терминала и без сети: Telegram подменён, процессы — настоящие)."""
import asyncio
import contextlib
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

import tui  # noqa: E402
from textual.widgets import Button, DataTable, Input, RichLog, SelectionList, Static, Switch, TabbedContent, TextArea  # noqa: E402


class FakeRemote(tui.Remote):
    def __init__(self, root=None):
        super().__init__(root or Path("."))
        self.titles = {1: "General", 11: "Code", 12: "Config", 13: "Logs"}
        self.calls = []

    async def topics(self, model, log):
        self.calls.append("topics")
        log("fake: запрос топиков")
        return dict(self.titles)

    async def groups(self, model, log):
        return [(-100111, "forum", "Backups"), (-100222, "group", "Chat")]

    async def check(self, model, log):
        log("шаг 1: сеть")
        return "Вход выполнен: Тест (@test)\nГруппа: Backups — форум"

    def login_argv(self, model):
        return [sys.executable, "-c", "print('LOGIN-RAN')"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("code", ".config", "Documents", ".ssh"):
        (home / d).mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "excludes.default.txt").write_text("[*]\nnode_modules\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return root, home


def make_app(root, remote=None):
    app = tui.BackupUtilApp(root, remote or FakeRemote(root))
    app.suspend = lambda: contextlib.nullcontext()  # в headless-режиме терминал не приостанавливается
    return app


def run(coro):
    return asyncio.run(coro)


def write_script(root: Path, name: str, body: str) -> Path:
    p = root / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def log_text(app) -> str:
    return "\n".join(t for _, t in app.log_history)


async def add_path(app, pilot, path):
    await pilot.click("#src-add-path")
    await pilot.pause()
    app.screen.query_one("#prompt-input", Input).value = path
    await pilot.press("enter")
    await pilot.pause()


async def goto(app, pilot, tab):
    await pilot.click(f"#--content-tab-{tab}")
    await pilot.pause(0.2)


# =========================================================================== основа
def test_starts_and_lists_sources(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/.config | | whole\n", encoding="utf-8")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            t = app.query_one("#src-table", DataTable)
            assert t.row_count == 2
            assert [str(c) for c in t.get_row_at(1)][:3] == [".config", "~/.config", "whole"]
            assert app.sub_title == "сохранено"
    run(scenario())


def test_add_edit_delete_save(env):
    root, home = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await add_path(app, pilot, "~/code")
            await add_path(app, pilot, "~/Documents")
            await add_path(app, pilot, "~/code")  # дубликат не добавляется
            t = app.query_one("#src-table", DataTable)
            assert t.row_count == 2 and app.model.dirty
            t.move_cursor(row=1)
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#sw-encrypt", Switch).value = True
            app.screen.query_one("#sw-whole", Switch).value = True
            app.screen.query_one("#ed-name", Input).value = "docs-x"
            await pilot.click("#ok")
            await pilot.pause()
            s = app.model.sources[1]
            assert (s.encrypt, s.whole, s.name) == (True, True, "docs-x")
            t.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            await pilot.click("#btn-yes")
            await pilot.pause()
            assert [x.path for x in app.model.sources] == ["~/Documents"]
            await pilot.press("ctrl+s")                    # шифрование без ключа age — понятная ошибка
            await pilot.pause()
            assert app.model.dirty and (root / "backup.conf").exists() is False
            await pilot.click("#btn-ok")
            await pilot.pause()
            app.model.env["AGE_RECIPIENT"] = "age1xyz"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not app.model.dirty
        assert "~/Documents | docs-x | encrypt,whole" in (root / "backup.conf").read_text(encoding="utf-8")
    run(scenario())


def test_dir_picker_selects_into_input(env):
    root, home = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#dir-input", Input).value = "~/code"
            await pilot.click("#ok")
            await pilot.pause()
            assert [s.path for s in app.model.sources] == ["~/code"]
    run(scenario())


# =========================================================================== Telegram
def test_topics_fetch_assign_and_auto(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/.config\n~/Documents\n", encoding="utf-8")
    remote = FakeRemote(root)

    async def scenario():
        app = make_app(root, remote)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-top")
            await pilot.click("#top-fetch")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.titles[11] == "Code" and remote.calls == ["topics"]
            assert "fake: запрос топиков" in log_text(app)           # вывод tg_upload.py виден в журнале
            top = app.query_one("#top-table", DataTable)
            rows = {str(top.get_row_at(i)[0]): [str(c) for c in top.get_row_at(i)] for i in range(3)}
            assert rows["code"][1] == "11" and "авто" in rows["code"][2]
            assert rows[".config"][1] == "12"
            assert rows["Documents"][1] == "—"
            top.move_cursor(row=2)
            await pilot.click("#top-assign")
            await pilot.pause()
            app.screen.query_one("#pick-list").highlighted = 3
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.topics == {"Documents": 13} and app.model.dirty
            await pilot.click("#top-assign")
            await pilot.pause()
            app.screen.query_one("#pick-list").highlighted = len(app.titles)
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "42"
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.topics["Documents"] == 42
            await pilot.click("#top-reset")
            await pilot.pause()
            assert app.model.topics == {}
            await pilot.click("#top-log")
            await pilot.pause()
            app.screen.query_one("#pick-list").highlighted = 3
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.ab["log_topic"] == "13"
    run(scenario())


@pytest.mark.parametrize("exc", [RuntimeError("нет сети"), asyncio.TimeoutError(), SystemExit("выход из чужого кода"),
                                 ConnectionError("[Errno 111] Connect call failed")])
def test_telegram_errors_are_shown_and_never_crash_the_app(env, exc):
    root, _ = env

    class Broken(FakeRemote):
        async def topics(self, model, log):
            raise exc

        async def check(self, model, log):
            raise exc

    async def scenario():
        app = make_app(root, Broken(root))
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-top")
            await pilot.click("#top-fetch")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = str(app.query_one("#top-status", Static).render())
            assert status.startswith("Ошибка: ") and len(status) > len("Ошибка: ") + 5   # причина не пустая
            await goto(app, pilot, "tab-tg")
            await pilot.click("#tg-check")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running and not app.net_busy
            errs = [t for lv, t in app.log_history if lv == "err" and t.startswith("Проверка соединения:")]
            assert errs and len(errs[0]) > len("Проверка соединения: ") + 3          # в журнале — причина, а не пустота
    run(scenario())


def test_check_switches_to_journal_and_shows_steps(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-tg")
            await pilot.click("#tg-check")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert app._active_tab() == "tab-log"
            assert "шаг 1: сеть" in log_text(app) and "Вход выполнен: Тест" in log_text(app)
            assert "Вход выполнен: Тест (@test)" in app.status_texts["#tg-status"]
    run(scenario())


def test_login_runs_cli_in_terminal_then_checks(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        calls = []
        orig = tui.subprocess.run

        def spy(argv, **kw):
            calls.append(argv)
            return orig(argv, **kw)

        tui.subprocess.run = spy
        try:
            async with app.run_test(size=(150, 50)) as pilot:
                await goto(app, pilot, "tab-tg")
                app.query_one("#tg-api-id", Input).value = "123"
                app.query_one("#tg-api-hash", Input).value = "h" * 32
                await pilot.click("#tg-login")
                await app.workers.wait_for_complete()
                await pilot.pause(0.3)
        finally:
            tui.subprocess.run = orig
        assert calls and calls[0][1:3] == ["-c", "print('LOGIN-RAN')"]            # login_argv выполнен в «терминале»
        assert "Вход выполнен (tg_upload.py login завершился успешно)" in log_text(app)
        assert "Вход выполнен: Тест (@test)" in log_text(app)                     # затем автоматическая проверка
        assert (root / "telegram.toml").exists()                                  # креды сохранены для CLI
    run(scenario())


def test_group_pick(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-tg")
            await pilot.click("#tg-pick-group")
            await pilot.pause(0.5)
            await pilot.press("enter")                          # единственный форум — «Backups»
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.model.tg["group"] == "-100111"
            await pilot.click("#tg-show-hash")
            assert app.query_one("#tg-api-hash", Input).password is False
    run(scenario())


def test_net_blocked_while_process_runs(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n", encoding="utf-8")
    write_script(root, "backup.sh", "sleep 20\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            await pilot.click("#run-build")
            for _ in range(50):
                await pilot.pause(0.1)
                if app.busy:
                    break
            assert app.busy
            app._check()                                         # запрос к Telegram во время сборки — отказ
            await pilot.pause(0.2)
            assert app.remote.calls == [] and not app.net_busy
            app.runner.stop(hard=True)
            await app.workers.wait_for_complete()
    run(scenario())


# =========================================================================== параметры и прочее
def test_params_validation_and_preview(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-par")
            app.query_one("#p-volume", Input).value = "2500M"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.model.dirty
            await pilot.click("#btn-ok")
            await pilot.pause()
            app.query_one("#p-volume", Input).value = "1500M"
            app.query_one("#p-split", Switch).value = False
            app.query_one("#p-logmsg", Input).value = "{date} #{n}"
            await pilot.pause()
            assert "07.11.2026 #7" in str(app.query_one("#p-preview", Static).render())
            await pilot.press("ctrl+s")
            await pilot.pause()
        env_text = (root / "backup.env").read_text(encoding="utf-8")
        assert 'VOLUME_SIZE="1500M"' in env_text and "SPLIT=0" in env_text
    run(scenario())


def test_excludes_edit_saved_separately(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-exc")
            ta = app.query_one("#excl-text", TextArea)
            ta.load_text(ta.text + "target\n")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
    run(scenario())
    assert "target" in (root / "excludes.txt").read_text(encoding="utf-8")
    assert "target" not in (root / "excludes.default.txt").read_text(encoding="utf-8")


def test_quit_asks_when_dirty(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await add_path(app, pilot, "~/code")
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.screen.query("#btn-discard")
            await pilot.click("#btn-cancel")
            await pilot.pause()
            assert app.is_running
            await pilot.press("ctrl+q")
            await pilot.pause()
            await pilot.click("#btn-discard")
        assert not (root / "backup.conf").exists()
    run(scenario())


def test_build_command_matrix(tmp_path):
    f = tui.build_command
    names = ["a", "b"]
    sh = str(tmp_path / "backup.sh")
    assert f(tmp_path, "upload", "items", False, True, True, True, True, ["a", "b"], names) == [sh, "upload", "--delete-after-upload", "--fresh"]
    assert f(tmp_path, "all", "whole", False, False, True, True, False, ["b"], names) == [sh, "all", "--whole", "--single", "--encrypt", "--", "b"]
    assert f(tmp_path, "build", "config", True, True, False, False, True, names, names) == [sh, "build", "--dry-run"]
    assert f(tmp_path, "upload", "config", False, False, False, False, False, ["a"], names, yes=True) == [sh, "upload", "--yes", "--", "a"]


# =========================================================================== процессы и журнал
def test_process_runner_streams_lines_progress_and_exit_code(tmp_path):
    lines, progress = [], []

    async def scenario():
        r = tui.ProcessRunner(lines.append, progress.append)
        code = await r.run(["bash", "-c", r'echo one; printf "p 10%%\r"; printf "p 90%%\r"; echo; echo done; echo err >&2; exit 3'], tmp_path)
        return code

    assert run(scenario()) == 3
    assert lines == ["one", "done", "err"]                       # «\r»-прогресс и пустая строка после него не в журнале
    assert "p 10%" in progress and "p 90%" in progress


def test_process_runner_env_and_no_stdin(tmp_path):
    lines = []

    async def scenario():
        r = tui.ProcessRunner(lines.append, lambda t: None)
        return await r.run(["bash", "-c", 'echo "$BACKUPUTIL_NONINTERACTIVE|$BACKUPUTIL_PV_FORCE|$PYTHONUNBUFFERED"; read -r x && echo got || echo EOF'], tmp_path)

    assert run(scenario()) == 0
    assert lines == ["1|1|1", "EOF"]                              # stdin закрыт: подтверждения не повиснут


@pytest.fixture()
def fast_stop(monkeypatch):
    monkeypatch.setattr(tui, "STOP_GRACE_INT", 0.5)
    monkeypatch.setattr(tui, "STOP_GRACE_TERM", 0.5)


def test_process_runner_stop_kills_whole_group_quickly(tmp_path, fast_stop):
    """Фоновый потомок держит pipe открытым: остановка и «добивание» группы не должны зависать."""
    lines = []

    async def scenario():
        r = tui.ProcessRunner(lines.append, lambda t: None)
        task = asyncio.create_task(r.run(["bash", "-c", "trap 'exit 130' INT TERM; sleep 30 & wait"], tmp_path))
        await asyncio.sleep(0.5)
        r.stop()
        code = await asyncio.wait_for(task, 8)
        return r.stopped, code

    stopped, code = run(scenario())
    assert stopped and code != 0


def test_process_runner_hard_stop(tmp_path, fast_stop):
    async def scenario():
        r = tui.ProcessRunner(lambda t: None, lambda t: None)
        task = asyncio.create_task(r.run(["bash", "-c", "sleep 30"], tmp_path))
        await asyncio.sleep(0.4)
        r.stop(hard=True)
        return await asyncio.wait_for(task, 5)

    assert run(scenario()) != 0


def test_run_capture_collects_stdout_and_streams_stderr(tmp_path):
    err = []

    async def scenario():
        return await tui.run_capture([sys.executable, "-c", "import sys; print('{\"ok\": true}'); print('шаг', file=sys.stderr)"],
                                     tmp_path, err.append, 10)

    rc, out = run(scenario())
    assert rc == 0 and json.loads(out) == {"ok": True} and err == ["шаг"]


def test_run_capture_timeout_kills_process(tmp_path):
    async def scenario():
        await tui.run_capture([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, lambda t: None, 1)

    with pytest.raises(RuntimeError, match="не ответил"):
        run(scenario())


def test_remote_parses_json_and_reports_errors(tmp_path):
    """Настоящий Remote и настоящий дочерний процесс; вместо tg_upload.py — заглушка с тем же контрактом."""
    import bu_config as B
    (tmp_path / "tg_upload.py").write_text(
        "import sys, json\n"
        "args = sys.argv[3:]\n"
        "print('шаг диагностики', file=sys.stderr)\n"
        "if args[0] == 'topics': print(json.dumps({'ok': True, 'group': 'G', 'topics': {'11': 'Code', '12': 'Config'}}))\n"
        "elif args[0] == 'groups': print(json.dumps({'ok': True, 'groups': [{'id': -1, 'kind': 'forum', 'title': 'B'}]}))\n"
        "elif args[0] == 'check': print(json.dumps({'ok': False, 'error': 'Сессия не авторизована'})); sys.exit(1)\n",
        encoding="utf-8")
    m = B.Model(tmp_path)
    m.load()
    m.tg.update(api_id="1", api_hash="h" * 32, group="-100")
    logs = []
    r = tui.Remote(tmp_path)

    assert run(r.topics(m, logs.append)) == {11: "Code", 12: "Config"}
    assert run(r.groups(m, logs.append)) == [(-1, "forum", "B")]
    assert "шаг диагностики" in logs
    with pytest.raises(RuntimeError, match="Сессия не авторизована"):
        run(r.check(m, logs.append))
    assert (tmp_path / "telegram.toml").exists()                    # перед запросом TUI сохранил telegram.toml
    assert stat.S_IMODE((tmp_path / "telegram.toml").stat().st_mode) == 0o600

    (tmp_path / "tg_upload.py").write_text("print('не json')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Неожиданный ответ"):
        run(r.topics(m, logs.append))
    (tmp_path / "tg_upload.py").write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="не вернул ответа"):
        run(r.topics(m, logs.append))


def test_remote_rejects_missing_credentials(tmp_path):
    import bu_config as B
    m = B.Model(tmp_path)
    m.load()
    m.tg["api_id"] = "abc"
    with pytest.raises(ValueError, match="api_id"):
        run(tui.Remote(tmp_path).topics(m, lambda t: None))


# =========================================================================== запуск из TUI
def build_run_env(root, conf="~/code\n~/Documents\n", script='echo "ARGS: $*"\n'):
    (root / "backup.conf").write_text(conf, encoding="utf-8")
    write_script(root, "backup.sh", script)


async def click_run(app, pilot, button):
    await goto(app, pilot, "tab-run")
    await pilot.click(button)
    await app.workers.wait_for_complete()
    await pilot.pause(0.3)


def test_build_streams_output_into_journal_and_keeps_tui_open(env):
    root, _ = env
    build_run_env(root, script=(
        'echo "2026-11-08 10:00:00 [INFO] старт"\n'
        'echo "2026-11-08 10:00:01 [WARN] предупреждение"\n'
        'printf "12%%\\r"; printf "88%%\\r"; echo\n'
        'echo "2026-11-08 10:00:02 [INFO] code: готово — 1 файл"\n'
        'echo "ARGS: $*"\n'))

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-build")
            assert app.is_running and not app.busy
            assert app._active_tab() == "tab-log"                            # TUI сам показал журнал
            text = log_text(app)
            assert "[INFO] старт" in text and "[WARN] предупреждение" in text and "12%" not in text
            assert f"ARGS: build" in text
            levels = {t: lv for lv, t in app.log_history}
            assert levels["2026-11-08 10:00:01 [WARN] предупреждение"] == "warn"
            assert any(lv == "ok" for lv, _ in app.log_history)
            assert "завершено успешно" in app.status_texts["#log-status"]
            assert not app.query_one("#run-build", Button).disabled          # кнопки снова доступны
            assert app.query_one("#log-stop", Button).disabled
    run(scenario())
    log = root / ".state" / "tui.log"
    assert log.exists() and "[INFO] старт" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_failed_run_is_reported(env):
    root, _ = env
    build_run_env(root, script='echo "[ERR] плохо"\nexit 7\n')

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-all")
            assert "код возврата 7" in app.status_texts["#log-status"]
            assert any(lv == "err" and "код возврата 7" in t for lv, t in app.log_history)
    run(scenario())


def test_selected_names_and_flags_reach_backup_sh(env):
    root, _ = env
    build_run_env(root)

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            app.query_one("#run-src", SelectionList).deselect("Documents")
            app.query_one("#run-dry", Switch).value = True
            await pilot.pause()
            await pilot.click("#run-build")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "ARGS: build --dry-run -- code" in log_text(app)
    run(scenario())


def test_upload_button_runs_upload_mode(env):
    root, _ = env
    build_run_env(root)

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-upload")
            assert "ARGS: upload" in log_text(app)
    run(scenario())


def test_sensitive_unencrypted_upload_needs_confirmation(env):
    root, _ = env
    build_run_env(root, conf="~/.ssh\n~/code\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            await pilot.click("#run-upload")
            await pilot.pause(0.4)
            assert app.screen.query("#btn-yes")                                   # предупреждение показано
            await pilot.click("#btn-no")
            await app.workers.wait_for_complete()
            assert "ARGS" not in log_text(app) and "отменена" in log_text(app)    # без подтверждения — ничего не запущено
            await pilot.click("#run-upload")
            await pilot.pause(0.4)
            await pilot.click("#btn-yes")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "ARGS: upload --yes" in log_text(app)                          # подтвердили — передан --yes
    run(scenario())


def test_encrypted_secret_folder_does_not_ask(env):
    root, _ = env
    build_run_env(root, conf="~/.ssh | | encrypt\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-upload")
            assert "ARGS: upload" in log_text(app) and "--yes" not in log_text(app)
    run(scenario())


def test_dry_run_upload_skips_confirmation(env):
    root, _ = env
    build_run_env(root, conf="~/.ssh\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            app.query_one("#run-dry", Switch).value = True
            await pilot.pause()
            await pilot.click("#run-upload")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "ARGS: upload --dry-run" in log_text(app)
    run(scenario())


def test_delete_after_upload_needs_confirmation(env):
    root, _ = env
    build_run_env(root, conf="~/code\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            app.query_one("#run-delete", Switch).value = True
            await pilot.pause()
            await pilot.click("#run-upload")
            await pilot.pause(0.4)
            await pilot.click("#btn-no")
            await app.workers.wait_for_complete()
            assert "ARGS" not in log_text(app)
            await pilot.click("#run-upload")
            await pilot.pause(0.4)
            await pilot.click("#btn-yes")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "ARGS: upload --delete-after-upload --yes" in log_text(app)
    run(scenario())


def test_sudo_is_confirmed_before_system_dirs(env):
    root, _ = env
    build_run_env(root, conf="/etc\n")

    async def scenario():
        app = make_app(root)
        answers = []

        async def deny():
            answers.append("deny")
            return False

        app._ensure_sudo = deny
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-build")
            assert answers == ["deny"] and "ARGS" not in log_text(app)
            assert "sudo не подтверждён" in log_text(app)

            async def allow():
                return True

            app._ensure_sudo = allow
            await click_run(app, pilot, "#run-build")
            assert "ARGS: build" in log_text(app)
    run(scenario())


def test_stop_button_stops_process(env, fast_stop):
    root, _ = env
    build_run_env(root, script="trap 'exit 130' INT TERM\necho started\nsleep 30 &\nwait\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            await pilot.click("#run-build")
            for _ in range(50):
                await pilot.pause(0.1)
                if app.busy and "started" in log_text(app):
                    break
            assert app.busy and not app.query_one("#log-stop", Button).disabled
            await pilot.click("#log-stop")
            await asyncio.wait_for(app.workers.wait_for_complete(), 15)
            await pilot.pause(0.3)
            assert "остановлено пользователем" in app.status_texts["#log-status"]
            assert not app.busy
    run(scenario())


def test_quit_while_running_asks_and_stops(env, fast_stop):
    root, _ = env
    build_run_env(root, script="trap 'exit 130' TERM\nsleep 30 &\nwait\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            await pilot.click("#run-build")
            for _ in range(50):
                await pilot.pause(0.1)
                if app.busy:
                    break
            await pilot.press("ctrl+q")
            await pilot.pause(0.3)
            assert app.screen.query("#btn-stop")
            await pilot.click("#btn-stay")
            await pilot.pause(0.2)
            assert app.is_running and app.busy
            await pilot.press("ctrl+q")
            await pilot.pause(0.3)
            await pilot.click("#btn-stop")
            await pilot.pause(0.5)
    run(scenario())


def test_archive_status_labels_and_refresh(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/Documents\n", encoding="utf-8")
    d = root / "code"
    d.mkdir()
    vols = {"a.tar.zst.000": b"x" * 10, "a.tar.zst.001": b"y" * 10}
    shas = []
    for n, data in vols.items():
        (d / n).write_bytes(data)
        shas.append(f"{hashlib.sha256(data).hexdigest()}  {n}")
    (d / "code.sha256").write_text("\n".join(shas) + "\n")
    (d / "MANIFEST.txt").write_text("DATE=2026-11-07T10:00:00+05:00\nARCHIVE_BYTES=2147483648\n")
    state = root / ".state"
    state.mkdir()
    (state / "uploaded.jsonl").write_text(json.dumps(
        {"key": "-100|11|code/a.tar.zst.000", "sha256": hashlib.sha256(vols["a.tar.zst.000"]).hexdigest()}) + "\n")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            sl = app.query_one("#run-src", SelectionList)
            labels = [str(sl.get_option_at_index(i).prompt) for i in range(sl.option_count)]
            assert "собрано 07.11.2026 · 2.0 ГиБ · загружено 1/2" in labels[0]
            assert "не собрано" in labels[1]
    run(scenario())


def test_crash_in_background_task_is_logged_not_silent(env):
    root, _ = env

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            async def boom():
                raise ValueError("внезапный сбой")

            app.run_worker(boom(), exit_on_error=False)
            with contextlib.suppress(Exception):          # wait_for_complete сам бросает исключение упавшего воркера
                await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "внезапный сбой" in log_text(app)
    run(scenario())
    assert "внезапный сбой" in (root / ".state" / "tui.log").read_text(encoding="utf-8")


@pytest.mark.skipif(not __import__("shutil").which("zstd"), reason="нужен zstd")
def test_tui_runs_the_real_backup_sh_end_to_end(env):
    """Не заглушка: настоящий backup.sh собирает архивы, а TUI показывает его вывод в журнале."""
    import shutil
    root, home = env
    repo = Path(__file__).resolve().parent.parent
    for f in ("backup.sh", "restore.sh", "excludes.default.txt"):
        shutil.copy(repo / f, root / f)
    (root / "backup.sh").chmod(0o755)
    (root / "restore.sh").chmod(0o755)
    (home / "code" / "a.txt").write_text("привет\n", encoding="utf-8")
    (home / "code" / "big.bin").write_bytes(os.urandom(1_500_000))
    (root / "backup.conf").write_text("~/code\n", encoding="utf-8")
    (root / "backup.env").write_text('VOLUME_SIZE="1M"\nZSTD_LEVEL=3\nSPLIT=1\nONE_FS=0\n', encoding="utf-8")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await click_run(app, pilot, "#run-build")
            text = log_text(app)
            assert "готово — архивов 2" in text and "завершено успешно" in app.status_texts["#log-status"]
            assert app._active_tab() == "tab-log" and not app.busy
            levels = [lv for lv, t in app.log_history if "[INFO]" in t]
            assert levels and set(levels) <= {"info", "ok"}
            await goto(app, pilot, "tab-run")                                   # статус архивов обновился
            sl = app.query_one("#run-src", SelectionList)
            assert "собрано" in str(sl.get_option_at_index(0).prompt) and "загружено 0/" in str(sl.get_option_at_index(0).prompt)
    run(scenario())
    assert (root / "code" / "big.bin.tar.zst.000").exists() and (root / "code" / "code.sha256").exists()
    assert (root / "backup.log").exists()


def test_build_dry_run_through_tui_changes_nothing(env):
    import shutil
    if not shutil.which("zstd"):
        pytest.skip("нужен zstd")
    root, home = env
    repo = Path(__file__).resolve().parent.parent
    for f in ("backup.sh", "excludes.default.txt"):
        shutil.copy(repo / f, root / f)
    (root / "backup.sh").chmod(0o755)
    (root / "backup.conf").write_text("~/code\n", encoding="utf-8")

    async def scenario():
        app = make_app(root)
        async with app.run_test(size=(150, 50)) as pilot:
            await goto(app, pilot, "tab-run")
            app.query_one("#run-dry", Switch).value = True
            await pilot.pause()
            await pilot.click("#run-build")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert "dry-run завершён, ничего не записано" in log_text(app)
    run(scenario())
    assert not (root / "code").exists()
