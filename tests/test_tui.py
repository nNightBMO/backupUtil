"""Тесты TUI на Textual Pilot (без терминала и без сети: Telegram подменён)."""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

import tui  # noqa: E402
from textual.widgets import DataTable, Input, Static, Switch, TabbedContent, TextArea  # noqa: E402


class FakeRemote(tui.Remote):
    def __init__(self):
        self.titles = {1: "General", 11: "Code", 12: "Config", 13: "Logs"}
        self.calls = []

    async def topics(self, model):
        self.calls.append("topics")
        return dict(self.titles)

    async def groups(self, model):
        return [(-100111, "forum", "Backups"), (-100222, "group", "Chat")]

    async def check(self, model):
        return "ok"

    async def login(self, app, model):
        phone = await app.ask("телефон")
        code = await app.ask("код")
        return f"вошли {phone}/{code}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("code", ".config", "Documents"):
        (home / d).mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "excludes.default.txt").write_text("[*]\nnode_modules\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return root, home


def run(coro):
    return asyncio.run(coro)


async def add_path(app, pilot, path):
    await pilot.click("#src-add-path")
    await pilot.pause()
    app.screen.query_one("#prompt-input", Input).value = path
    await pilot.press("enter")
    await pilot.pause()


def test_starts_and_lists_sources(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/.config | | whole\n", encoding="utf-8")

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            t = app.query_one("#src-table", DataTable)
            assert t.row_count == 2
            assert [str(c) for c in t.get_row_at(1)][:3] == [".config", "~/.config", "whole"]
            assert app.sub_title == "сохранено"
    run(scenario())


def test_add_edit_delete_save(env):
    root, home = env

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            await add_path(app, pilot, "~/code")
            await add_path(app, pilot, "~/Documents")
            await add_path(app, pilot, "~/code")  # дубликат не добавляется
            t = app.query_one("#src-table", DataTable)
            assert t.row_count == 2 and app.model.dirty
            # правка второй строки: шифрование + whole
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
            # удаление первой строки
            t.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            await pilot.click("#btn-yes")
            await pilot.pause()
            assert [x.path for x in app.model.sources] == ["~/Documents"]
            # без ключа age шифрование не сохраняется — понятная ошибка
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.model.dirty and (root / "backup.conf").exists() is False
            await pilot.click("#btn-ok")
            await pilot.pause()
            app.model.env["AGE_RECIPIENT"] = "age1xyz"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not app.model.dirty
        text = (root / "backup.conf").read_text(encoding="utf-8")
        assert "~/Documents | docs-x | encrypt,whole" in text
    run(scenario())


def test_dir_picker_selects_into_input(env):
    root, home = env

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            inp = app.screen.query_one("#dir-input", Input)
            inp.value = "~/code"
            await pilot.click("#ok")
            await pilot.pause()
            assert [s.path for s in app.model.sources] == ["~/code"]
    run(scenario())


def test_topics_fetch_assign_and_auto(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/.config\n~/Documents\n", encoding="utf-8")
    remote = FakeRemote()

    async def scenario():
        app = tui.BackupUtilApp(root, remote)
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-top"
            await pilot.pause()
            await pilot.click("#top-fetch")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.titles[11] == "Code" and remote.calls == ["topics"]
            top = app.query_one("#top-table", DataTable)
            rows = {str(top.get_row_at(i)[0]): [str(c) for c in top.get_row_at(i)] for i in range(3)}
            assert rows["code"][1] == "11" and "авто" in rows["code"][2]        # регистр не важен
            assert rows[".config"][1] == "12"                                      # ведущая точка не важна
            assert rows["Documents"][1] == "—"                                     # нет такого топика
            # ручное назначение через список
            top.move_cursor(row=2)
            await pilot.click("#top-assign")
            await pilot.pause()
            pick = app.screen.query_one("#pick-list")
            pick.highlighted = 3  # «Logs»
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.topics == {"Documents": 13} and app.model.dirty
            # ввод id вручную
            await pilot.click("#top-assign")
            await pilot.pause()
            app.screen.query_one("#pick-list").highlighted = len(app.titles)  # «ввести id вручную»
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "42"
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.topics["Documents"] == 42
            await pilot.click("#top-reset")
            await pilot.pause()
            assert app.model.topics == {}
            # топик логов
            await pilot.click("#top-log")
            await pilot.pause()
            app.screen.query_one("#pick-list").highlighted = 3
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.ab["log_topic"] == "13"
    run(scenario())


def test_fetch_error_is_reported_not_crash(env):
    root, _ = env

    class Broken(FakeRemote):
        async def topics(self, model):
            raise RuntimeError("нет сети")

    async def scenario():
        app = tui.BackupUtilApp(root, Broken())
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-top"
            await pilot.pause()
            await pilot.click("#top-fetch")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "нет сети" in str(app.query_one("#top-status", Static).render())
    run(scenario())


def test_telegram_login_flow_and_group_pick(env):
    root, _ = env

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-tg"
            await pilot.pause()
            app.query_one("#tg-api-id", Input).value = "123"
            app.query_one("#tg-api-hash", Input).value = "h" * 32
            await pilot.click("#tg-login")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "+998901234567"
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "12345"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "вошли +998901234567/12345" in str(app.query_one("#tg-status", Static).render())
            await pilot.click("#tg-pick-group")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("enter")  # единственный форум — «Backups»
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.model.tg["group"] == "-100111"
            await pilot.click("#tg-show-hash")
            assert app.query_one("#tg-api-hash", Input).password is False
    run(scenario())


def test_params_validation_and_preview(env):
    root, _ = env

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-par"
            await pilot.pause()
            app.query_one("#p-volume", Input).value = "2500M"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.model.dirty                       # не сохранено
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
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-exc"
            await pilot.pause()
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
        app = tui.BackupUtilApp(root, FakeRemote())
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


def test_run_tab_command_preview(env):
    root, _ = env
    (root / "backup.conf").write_text("~/code\n~/Documents\n", encoding="utf-8")

    async def scenario():
        app = tui.BackupUtilApp(root, FakeRemote())
        async with app.run_test(size=(150, 50)) as pilot:
            app.query_one(TabbedContent).active = "tab-run"
            await pilot.pause()
            assert app.run_command() == [str(root / "backup.sh"), "build"]  # всё выбрано → без имён
            app.query_one("#run-dry", Switch).value = True
            sel = app.query_one("#run-src")
            sel.deselect("Documents")
            await pilot.pause()
            assert app.run_command() == [str(root / "backup.sh"), "build", "--dry-run", "--", "code"]
    run(scenario())


def test_build_command_matrix(tmp_path):
    f = tui.build_command
    names = ["a", "b"]
    assert f(tmp_path, "upload", "items", False, True, True, True, True, ["a", "b"], names) == \
        [str(tmp_path / "backup.sh"), "upload", "--delete-after-upload", "--fresh"]
    assert f(tmp_path, "all", "whole", False, False, True, True, False, ["b"], names) == \
        [str(tmp_path / "backup.sh"), "all", "--whole", "--single", "--encrypt", "--", "b"]
    assert f(tmp_path, "build", "config", True, True, False, False, True, names, names) == \
        [str(tmp_path / "backup.sh"), "build", "--dry-run"]
