"""Логика загрузчика на подставном клиенте Telegram: топики без учёта регистра, метки autoback,
продолжение прерванного запуска, пропуск уже загруженного, фолбэк получения топиков."""
import asyncio
import json
import types

import pytest

T = pytest.importorskip("tg_upload")


class Msg:
    def __init__(self, i, size=None):
        self.id = i
        self.file = types.SimpleNamespace(size=size)


class FakeClient:
    def __init__(self, sent):
        self.sent, self.n = sent, 100

    async def send_file(self, entity, path, caption=None, reply_to=None, **kw):
        import os
        self.n += 1
        self.sent.append(("file", reply_to, os.path.basename(path)))
        return Msg(self.n, os.path.getsize(path))

    async def send_message(self, entity, text, reply_to=None):
        self.n += 1
        self.sent.append(("text", reply_to, text))
        return Msg(self.n)

    async def disconnect(self):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path
    state = root / ".state"
    monkeypatch.setattr(T, "ROOT", root)
    monkeypatch.setattr(T, "STATE_DIR", state)
    monkeypatch.setattr(T, "JOURNAL", state / "uploaded.jsonl")
    monkeypatch.setattr(T, "AUTOBACK", state / "autoback.json")
    monkeypatch.setattr(T, "LOGFILE", root / "backup.log")
    for name, vols in (("code", ["a.tar.zst.000", "a.tar.zst.001", "b.tar.zst.000"]), (".config", ["dot_config.tar.zst.000"])):
        d = root / name
        d.mkdir()
        lines = []
        for v in vols:
            (d / v).write_bytes(v.encode() * 10)
            import hashlib
            lines.append(f"{hashlib.sha256((d / v).read_bytes()).hexdigest()}  {v}")
        (d / f"{name.lstrip('.')}.sha256").write_text("\n".join(lines) + "\n")
        (d / "MANIFEST.txt").write_text("DATE=2026-11-07T10:00:00+05:00\nENCRYPTED=1\n")
    sent = []
    titles = {1: "General", 11: "Code", 12: "Config", 13: "Logs"}

    async def connect(cfg):
        return FakeClient(sent)

    async def group(c, g):
        return types.SimpleNamespace(forum=True)

    async def topics(c, e):
        return titles

    monkeypatch.setattr(T, "connect_authorized", connect)
    monkeypatch.setattr(T, "resolve_group", group)
    monkeypatch.setattr(T, "fetch_topics", topics)
    (root / "telegram.toml").write_text('[telegram]\napi_id=1\napi_hash="x"\ngroup=-100\n[autoback]\nlog_topic="logs"\n')
    return root, sent, titles


def run(**kw):
    a = types.SimpleNamespace(names=kw.get("names", []), full=kw.get("full", False), fresh=kw.get("fresh", False),
                              dry_run=kw.get("dry", False), delete_after_upload=False, yes=True)
    return asyncio.run(T.cmd_upload(T.load_config(T.ROOT / "telegram.toml"), a))


def texts(sent):
    return [s[2] for s in sent if s[0] == "text"]


def test_full_run_markers_and_case_insensitive_topics(env):
    root, sent, _ = env
    assert run(full=True, names=["code", ".config"]) == 0
    assert sent[0] == ("text", 13, "07.11.2026 autoback#1")                      # старт — в топик логов
    code = [s for s in sent if s[1] == 11]
    assert [s[2] for s in code if s[0] == "file"] == ["a.tar.zst.000", "a.tar.zst.001", "b.tar.zst.000",
                                                      "code.sha256", "MANIFEST.txt"]
    assert code[-1] == ("text", 11, "autoback#1")                                 # метка — ПОСЛЕ архивов
    assert sent[-1] == ("text", 12, "autoback#1")                                 # .config ↔ «Config»
    assert json.loads(T.AUTOBACK.read_text()) == {"last": 1, "current": None}


def test_partial_run_writes_only_date_after_archives(env):
    root, sent, _ = env
    assert run(names=["code"]) == 0
    assert texts(sent) == ["07.11.2026"]
    assert all(s[1] == 11 for s in sent)                                          # больше нигде ничего
    assert not T.AUTOBACK.exists()


def test_second_full_run_skips_files_but_gets_new_number(env):
    root, sent, _ = env
    run(full=True, names=["code", ".config"])
    sent.clear()
    assert run(full=True, names=["code", ".config"]) == 0
    assert [s for s in sent if s[0] == "file"] == []                              # журнал: всё уже загружено
    assert texts(sent) == ["07.11.2026 autoback#2", "autoback#2", "autoback#2"]


def test_interrupted_full_run_resumes_same_number(env, monkeypatch):
    root, sent, _ = env
    orig = T.upload_dir
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        return False if calls["n"] == 2 else await orig(*a, **k)

    monkeypatch.setattr(T, "upload_dir", flaky)
    assert run(full=True, names=["code", ".config"]) == 1
    assert json.loads(T.AUTOBACK.read_text())["current"]["n"] == 1
    monkeypatch.setattr(T, "upload_dir", orig)
    sent.clear()
    assert run(full=True, names=["code", ".config"]) == 0
    assert texts(sent) == ["autoback#1"]                                          # старт не дублируется
    assert json.loads(T.AUTOBACK.read_text()) == {"last": 1, "current": None}
    sent.clear()
    assert run(full=True, fresh=True, names=["code", ".config"]) == 0
    assert texts(sent)[0] == "07.11.2026 autoback#2"


def test_missing_log_topic_aborts_before_sending(env):
    root, sent, titles = env
    titles.pop(13)
    assert run(full=True, names=["code", ".config"]) == 1
    assert sent == []


def test_unresolved_topic_partial_continues_with_others(env):
    root, sent, titles = env
    titles.pop(12)
    assert run(names=["code", ".config"]) == 1
    assert any(s[1] == 11 for s in sent) and not any(s[1] == 12 for s in sent)


def test_explicit_topic_mapping_overrides_title(env):
    root, sent, _ = env
    (root / "telegram.toml").write_text('[telegram]\napi_id=1\napi_hash="x"\ngroup=-100\n[topics]\n"CODE" = 99\n')
    assert run(names=["code"]) == 0
    assert all(s[1] == 99 for s in sent)


def test_dry_run_sends_nothing(env):
    root, sent, _ = env
    assert run(full=True, dry=True, names=["code", ".config"]) == 0
    assert sent == [] and not T.AUTOBACK.exists() and not T.JOURNAL.exists()


def test_topics_fallback_from_service_messages():
    class MessageActionTopicCreate:
        def __init__(self, title): self.title = title

    class MessageActionTopicEdit:
        def __init__(self, title): self.title = title

    class M:
        def __init__(self, i, action, top=None):
            self.id, self.action = i, action
            self.reply_to = types.SimpleNamespace(reply_to_top_id=top, reply_to_msg_id=top)

    class C:  # новые сообщения идут первыми
        async def iter_messages(self, entity):
            for m in (M(50, MessageActionTopicEdit("Renamed"), top=11), M(40, None), M(11, MessageActionTopicCreate("Code")),
                      M(12, MessageActionTopicCreate("Config"))):
                yield m

    got = asyncio.run(T.topics_from_service_messages(C(), None))
    assert got == {11: "Renamed", 12: "Config", 1: "General"}


# =========================================================================== JSON-контракт для TUI
import socket  # noqa: E402
import sys  # noqa: E402


class AuthClient:
    """Клиент для check/topics/groups."""
    def __init__(self, authorized=True, hang=False):
        self.authorized, self.hang = authorized, hang

    async def connect(self):
        if self.hang:
            await asyncio.sleep(60)

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return types.SimpleNamespace(first_name="Имя", username="user")

    async def disconnect(self):
        pass

    async def iter_dialogs(self):
        for i, (name, forum) in enumerate((("Backups", True), ("Chat", False)), 1):
            yield types.SimpleNamespace(id=-100 - i, name=name, entity=types.SimpleNamespace(megagroup=True, forum=forum))


@pytest.fixture()
def cli(tmp_path, monkeypatch, capsys):
    toml = tmp_path / "telegram.toml"
    toml.write_text('[telegram]\napi_id=1\napi_hash="x"\ngroup=-100\nsession="%s"\n' % (tmp_path / "s" / "tg.session"))
    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "LOGFILE", tmp_path / "backup.log")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("нет сети")))

    def call(*args, client=None, forum=True):
        client = client or AuthClient()

        async def connect(cfg, quick=False):
            if not client.authorized:
                sys.exit("Сессия не авторизована. Сначала выполните: ./backup.sh tg login")
            return client

        async def group(c, g):
            return types.SimpleNamespace(forum=forum, title="Backups")

        async def topics(c, e):
            return {1: "General", 11: "Code"}

        monkeypatch.setattr(T, "connect_authorized", connect)
        monkeypatch.setattr(T, "make_client", lambda cfg, quick=False: client)
        monkeypatch.setattr(T, "resolve_group", group)
        monkeypatch.setattr(T, "fetch_topics", topics)
        monkeypatch.setattr(sys, "argv", ["tg_upload.py", "--config", str(toml), *args])
        rc = T.main()
        out, err = capsys.readouterr()
        return rc, out, err

    call.toml = toml
    return call


def last_json(out: str) -> dict:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"в stdout должна быть ровно одна строка JSON, а там: {out!r}"
    return json.loads(lines[0])


def test_check_ok_prints_steps_to_stderr_and_json_to_stdout(cli):
    rc, out, err = cli("check")
    data = last_json(out)
    assert rc == 0 and data["ok"] and "Вход выполнен: Имя (@user)" in data["text"] and "Backups — форум" in data["text"]
    assert "Python " in err and "Telethon" in err and "Сеть: Telegram DC2" in err and "НЕДОСТИЖИМ" in err


def test_check_unauthorized_is_clear_json_error(cli):
    rc, out, err = cli("check", client=AuthClient(authorized=False))
    assert rc == 1 and last_json(out) == {"ok": False, "error": "Сессия не авторизована. Сначала выполните: ./backup.sh tg login"} \
        or "не авторизована" in last_json(out)["error"]


def test_check_reports_unauthorized_after_connect(cli):
    class NoAuth(AuthClient):
        async def is_user_authorized(self):
            return False

    # check использует make_client напрямую (не connect_authorized)
    rc, out, err = cli("check", client=NoAuth())
    assert rc == 1 and "не авторизована" in last_json(out)["error"]


def test_hanging_connection_is_cut_by_timeout_with_clear_message(cli):
    rc, out, err = cli("--timeout", "1", "check", client=AuthClient(hang=True))
    assert rc == 1
    assert "Таймаут 1 с" in last_json(out)["error"] and "VPN" in last_json(out)["error"]


def test_quick_connect_message_mentions_handshake():
    with pytest.raises(ConnectionError, match="рукопожатие MTProto"):
        asyncio.run(T.quick_connect(AuthClient(hang=True), 0.2))


def test_topics_json(cli):
    rc, out, err = cli("topics", "--json")
    assert rc == 0 and last_json(out) == {"ok": True, "group": "Backups", "topics": {"1": "General", "11": "Code"}}


def test_topics_json_not_a_forum(cli):
    rc, out, err = cli("topics", "--json", forum=False)
    assert rc == 1 and "не форум" in last_json(out)["error"]


def test_groups_json(cli):
    rc, out, err = cli("groups", "--json")
    assert rc == 0 and last_json(out)["groups"] == [{"id": -101, "kind": "forum", "title": "Backups"},
                                                    {"id": -102, "kind": "group", "title": "Chat"}]


def test_groups_human_output_still_works(cli):
    rc, out, err = cli("groups")
    assert rc == 0 and "forum" in out and "Backups" in out


def test_json_error_when_config_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["tg_upload.py", "--config", str(tmp_path / "нет.toml"), "check"])
    rc = T.main()
    out, _ = capsys.readouterr()
    assert rc == 1 and "Нет файла" in last_json(out)["error"]


def test_stdout_stays_clean_when_permissions_get_fixed(cli):
    cli.toml.chmod(0o644)
    rc, out, err = cli("topics", "--json")
    assert rc == 0 and last_json(out)["ok"]            # уведомление о правах ушло в stderr
    assert "установлены 600" in err
    assert oct(cli.toml.stat().st_mode & 0o777) == "0o600"


def test_unexpected_exception_in_json_mode_becomes_error_not_traceback_on_stdout(cli):
    class Boom(AuthClient):
        async def get_me(self):
            raise ValueError("странный сбой")

    rc, out, err = cli("check", client=Boom())
    assert rc == 1 and "ValueError: странный сбой" in last_json(out)["error"] and "Traceback" in err


def test_log_goes_to_stderr_only(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "LOGFILE", tmp_path / "x.log")
    T.log("INFO", "привет")
    out, err = capsys.readouterr()
    assert out == "" and "привет" in err
