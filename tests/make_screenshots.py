"""Генерирует SVG-скриншоты TUI для документации: python tests/make_screenshots.py
Данные вымышленные, реальные конфиги не читаются."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bu_config as B  # noqa: E402
import tui  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"


class Demo(tui.Remote):
    async def topics(self, model):
        return {1: "General", 11: "Code", 12: "Config", 13: "Documents", 14: "Pictures", 15: "Logs", 16: "Ssh"}


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
    app = tui.BackupUtilApp(root, Demo())
    async with app.run_test(size=(128, 34)) as pilot:
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
        app.query_one("#run-dry").value = True
        await pilot.pause(0.3)
        app.save_screenshot("tui-run.svg", str(OUT))


if __name__ == "__main__":
    asyncio.run(main())
