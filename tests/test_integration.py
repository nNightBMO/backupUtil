"""Файлы, которые пишет TUI/bu_config, должен читать backup.sh (формат обязан совпадать)."""
import shutil
import subprocess
from pathlib import Path

import bu_config as B

REPO = Path(__file__).resolve().parent.parent


def test_backup_sh_reads_what_model_writes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "code").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    app = tmp_path / "app"
    app.mkdir()
    for f in ("backup.sh", "restore.sh", "excludes.default.txt"):
        shutil.copy(REPO / f, app / f)

    m = B.Model(app)
    m.load()
    m.sources = [B.Source("~/code"), B.Source("~/code", name="code2", whole=True, encrypt=True), B.Source("/etc", whole=True)]
    m.env["AGE_RECIPIENT"] = "age1abc"
    m.env["VOLUME_SIZE"] = "1500M"
    m.save()

    out = subprocess.run(["bash", str(app / "backup.sh"), "list"], capture_output=True, text=True, cwd=app,
                         env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    rows = {l.split()[0]: l.split() for l in out.stdout.splitlines()[1:]}
    assert rows["code"][1:4] == ["0", "0", "0"]            # без sudo / items / без шифрования
    assert rows["code2"][1:4] == ["0", "1", "1"]           # whole + encrypt
    assert rows["etc"][1:3] == ["1", "1"]                  # вне $HOME → sudo сам, whole


def test_backup_sh_rejects_reserved_name(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(REPO / "backup.sh", app / "backup.sh")
    (app / "backup.conf").write_text("~/docs\n", encoding="utf-8")
    out = subprocess.run(["bash", str(app / "backup.sh"), "list"], capture_output=True, text=True, cwd=app,
                         env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.returncode != 0 and "зарезервировано" in out.stderr


def test_env_written_by_model_is_valid_bash(tmp_path):
    m = B.Model(tmp_path)
    m.load()
    m.env["AGE_RECIPIENTS_FILE"] = "$HOME/keys.txt"
    m.save()
    r = subprocess.run(["bash", "-c", f'source "{tmp_path}/backup.env"; echo "$VOLUME_SIZE|$ZSTD_LEVEL|$SPLIT|$AGE_RECIPIENTS_FILE"'],
                       capture_output=True, text=True, env={"HOME": "/h", "PATH": "/usr/bin:/bin"})
    assert r.stdout.strip() == "1800M|3|1|/h/keys.txt"
