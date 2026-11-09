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
