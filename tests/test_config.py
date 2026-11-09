"""Тесты слоя конфигурации: чтение/запись всех файлов, валидация, совпадение с tg_upload."""
import os
import stat
import tomllib

import pytest

import bu_config as B


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "excludes.default.txt").write_text("[*]\nnode_modules\n", encoding="utf-8")
    return tmp_path


def full_model(root):
    m = B.Model(root)
    m.load()
    m.sources = [B.Source("~/code"), B.Source("~/.config", whole=True), B.Source("~/.ssh", encrypt=True, whole=True),
                 B.Source("/etc", name="etc-conf", whole=True)]
    m.env["AGE_RECIPIENT"] = "age1abc"
    m.tg.update(api_id="123", api_hash="a" * 32, group="-1001234", session="~/s/tg.session")
    m.topics = {"code": 11, ".config": 12}
    m.ab["log_topic"] = "Logs"
    return m


def test_roundtrip(root):
    m = full_model(root)
    m.save()
    m2 = B.Model(root)
    m2.load()
    assert (m2.sources, m2.env, m2.tg, m2.topics, m2.ab) == (m.sources, m.env, m.tg, m.topics, m.ab)


def test_secret_files_are_private(root):
    full_model(root).save()
    for name in ("telegram.toml", "backup.env"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600


def test_toml_is_valid_and_typed(root):
    full_model(root).save()
    data = tomllib.loads((root / "telegram.toml").read_text(encoding="utf-8"))
    assert data["telegram"]["api_id"] == 123 and data["telegram"]["group"] == -1001234
    assert data["topics"] == {".config": 12, "code": 11}
    assert data["autoback"]["log_topic"] == "Logs"


def test_conf_format_parsed_like_backup_sh(root):
    m = full_model(root)
    m.save()
    lines = [l for l in (root / "backup.conf").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    assert lines == ["~/code", "~/.config |  | whole", "~/.ssh |  | encrypt,whole", "/etc | etc-conf | whole"]


def test_env_update_keeps_foreign_lines(root):
    (root / "backup.env").write_text('# моё\nVOLUME_SIZE="1000M"   # комментарий\nFOO=bar\n', encoding="utf-8")
    m = B.Model(root)
    m.load()
    assert m.env["VOLUME_SIZE"] == "1000M"
    m.env["VOLUME_SIZE"] = "1500M"
    m.save()
    text = (root / "backup.env").read_text(encoding="utf-8")
    assert "# моё" in text and "FOO=bar" in text and 'VOLUME_SIZE="1500M"' in text


@pytest.mark.parametrize("bad", ['a"b', "a`b", "$(rm -rf /)", "a\\b", "a\nb"])
def test_env_injection_rejected(bad):
    with pytest.raises(ValueError):
        B.check_env_value("AGE_RECIPIENT", bad)


def test_validation(root):
    m = full_model(root)
    assert m.validate() == []
    m.env["VOLUME_SIZE"] = "2500M"
    assert any("Telegram" in p for p in m.validate())
    m.env["VOLUME_SIZE"] = "1800M"
    m.sources.append(B.Source("~/other/code"))
    assert any("повторяется" in p for p in m.validate())
    m.sources.pop()
    m.sources.append(B.Source("~/docs"))
    assert any("зарезервировано" in p for p in m.validate())
    m.sources.pop()
    m.env["AGE_RECIPIENT"] = ""
    assert any("age" in p for p in m.validate())
    with pytest.raises(ValueError):
        m.save()


def test_excludes_fall_back_to_default_and_override(root):
    m = B.Model(root)
    m.load()
    assert "node_modules" in m.excludes
    m.excludes += "target\n"
    m.save()
    assert (root / "excludes.txt").read_text(encoding="utf-8").endswith("target\n")
    assert (root / "excludes.default.txt").read_text(encoding="utf-8") == "[*]\nnode_modules\n"


def test_topic_resolution_case_insensitive(root):
    m = full_model(root)
    titles = {1: "General", 21: "Code", 22: "Local share", 23: "Config"}
    assert m.resolve_topic("code", titles) == (11, "вручную")
    assert m.resolve_topic("local-share", titles)[0] == 22
    assert m.resolve_topic("Config", {23: "Config"})[0] == 12  # ручной приоритетнее
    assert m.resolve_topic("nothing", titles)[0] is None
    assert m.resolve_topic("x", {1: "X", 2: "x"})[1] == "неоднозначно"


def test_norm_matches_uploader():
    tg = pytest.importorskip("tg_upload")
    for s in (".Config", "Local-share", "my proj", "Code", "_x_"):
        assert tg.norm(s) == B.norm(s)


def test_source_sudo_and_name():
    assert B.Source("/etc").needs_sudo() and not B.Source("~/code").needs_sudo()
    assert B.Source("~/.config").effective_name() == ".config"
    assert B.Source("~/x", name="y").effective_name() == "y"
