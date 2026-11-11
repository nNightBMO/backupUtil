"""Генерирует SVG-скриншоты TUI для документации: python tests/make_screenshots.py
Данные вымышленные, реальные конфиги и пути не читаются."""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bu_config as B  # noqa: E402
import tui  # noqa: E402
from textual.widgets import RichLog, Static  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"


class Demo(tui.Remote):
    async def topics(self, model, log):
        return {1: "General", 11: "Code", 12: "Config", 13: "Documents", 14: "Pictures", 15: "Logs", 16: "Ssh"}

    async def check(self, model, log):
        return "Вход выполнен: Иван (@ivan)\nГруппа: Backups — форум"


def fake_archive(root: Path, name: str, volumes: int, uploaded: int, size: int) -> None:
    d = root / name
    d.mkdir(parents=True)
    lines, recs = [], []
    for i in range(volumes):
        fn = f"a.tar.zst.{i:03d}"
        data = f"{name}{i}".encode()
        (d / fn).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        lines.append(f"{sha}  {fn}")
        if i < uploaded:
            recs.append(json.dumps({"key": f"-1001234567890|11|{name}/{fn}", "sha256": sha}))
    (d / f"{name}.sha256").write_text("\n".join(lines) + "\n")
    (d / "MANIFEST.txt").write_text(f"DATE=2026-11-07T21:40:00+05:00\nARCHIVE_BYTES={size}\n")
    st = root / ".state"
    st.mkdir(exist_ok=True)
    with open(st / "uploaded.jsonl", "a") as f:
        f.write("\n".join(recs) + ("\n" if recs else ""))


SAMPLE_LOG = [
    ("dim", "════ Сборка и загрузка · 2026-11-08 12:00:00 ════"),
    ("dim", "$ ./backup.sh all"),
    ("info", "2026-11-08 12:00:00 [INFO] === backup.sh all: 6 директор(ий) ==="),
    ("info", "2026-11-08 12:00:03 [INFO] [1/6] code ← ~/code  (режим: каждый элемент — архив; sudo=0 encrypt=0 split=1)"),
    ("info", "2026-11-08 12:00:03 [INFO]   (1/3) projA  (≈1.2GiB)"),
    ("info", "2026-11-08 12:01:41 [INFO]   (2/3) projB  (≈310MiB)"),
    ("warn", "2026-11-08 12:02:10 [WARN]     ./projB: часть файлов менялась во время чтения (tar код 1) — архив создан"),
    ("info", "2026-11-08 12:02:25 [INFO]   (3/3) notes  (≈4.0MiB)"),
    ("ok", "2026-11-08 12:02:27 [INFO] code: готово — архивов 3, всего 612.4MiB. Манифест: ~/backupUtil/code/MANIFEST.txt"),
    ("info", "── code → топик 11: архивов 3, файлов 5"),
    ("ok", "  ✓ notes.tar.zst.000 (1/1) → сообщение 101"),
    ("ok", "  ✓ projA.tar.zst.000 (1/1) → сообщение 102"),
    ("warn", "2026-11-08 12:04:52 [WARN] tg: code/projB.tar.zst.000: FloodWait/SlowMode — жду 14 с и продолжаю"),
]


async def switch(app, pilot, tab: str) -> None:
    await pilot.click(f"#--content-tab-{tab}")   # как у пользователя: клик по заголовку вкладки
    await pilot.pause(0.3)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    home, root = tmp / "home", tmp / "app"
    for d in ("code", "Documents", "Pictures", ".config", ".ssh"):
        (home / d).mkdir(parents=True)
    root.mkdir()
    os.environ["HOME"] = str(home)
    m = B.Model(root)
    m.sources = [B.Source("~/code"), B.Source("~/Documents"), B.Source("~/Pictures"),
                 B.Source("~/.config", whole=True), B.Source("~/.ssh", encrypt=True, whole=True),
                 B.Source("/etc", whole=True)]
    m.env["AGE_RECIPIENT"] = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
    m.tg.update(api_id="1234567", api_hash="0" * 32, group="-1001234567890")
    m.ab["log_topic"] = "Logs"
    m.excludes = (Path(__file__).resolve().parent.parent / "excludes.default.txt").read_text(encoding="utf-8")
    m.save()
    fake_archive(root, "code", volumes=5, uploaded=5, size=642_100_000)
    fake_archive(root, "Documents", volumes=2, uploaded=1, size=2_100_000_000)
    fake_archive(root, ".config", volumes=1, uploaded=0, size=88_000_000)

    app = tui.BackupUtilApp(root, Demo(root))
    async with app.run_test(size=(128, 36)) as pilot:
        await pilot.pause()
        app.save_screenshot("tui-sources.svg", str(OUT))
        await switch(app, pilot, "tab-top")
        await pilot.click("#top-fetch")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot("tui-topics.svg", str(OUT))
        await switch(app, pilot, "tab-par")
        app.save_screenshot("tui-params.svg", str(OUT))
        await switch(app, pilot, "tab-run")
        app.query_one("#run-dry").value = False
        await pilot.pause(0.3)
        app.save_screenshot("tui-run.svg", str(OUT))
        await switch(app, pilot, "tab-log")
        app.query_one("#log-view", RichLog).clear()
        app.log_history.clear()
        for level, text in SAMPLE_LOG:
            app.log_line(level, text)
        app.query_one("#log-progress", Static).update(
            "↑ code/projB.tar.zst.000: 42.7%    769.0/1800.0 МиБ    4.20 МиБ/с  осталось 0:04:06")
        app.set_status("#log-status", "▶ Сборка и загрузка…")
        app.query_one("#log-stop").disabled = False
        await pilot.pause(0.4)
        app.save_screenshot("tui-journal.svg", str(OUT))


if __name__ == "__main__":
    asyncio.run(main())
