#!/usr/bin/env bash
# Сквозной тест: сборка → проверка → восстановление → сравнение. Нужны bash, tar, zstd, coreutils.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v zstd >/dev/null || { echo "SKIP: нет zstd"; exit 0; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
export HOME="$work/home"; app="$work/app"
mkdir -p "$HOME/code/projA/node_modules" "$HOME/code/projB" "$HOME/code/my proj ё" \
         "$HOME/.config/app/Cache" "$HOME/.config/app2" "$app"
head -c 2500000 /dev/urandom > "$HOME/code/projA/big.bin"
echo x > "$HOME/code/projA/node_modules/skip.js"; echo a > "$HOME/code/projA/main.py"
echo b > "$HOME/code/projB/b.txt"; ln -s b.txt "$HOME/code/projB/lnk"
echo y > "$HOME/code/my proj ё/файл.txt"; echo hid > "$HOME/code/.hidden"; echo r > "$HOME/code/README.md"
echo junk > "$HOME/.config/app/Cache/junk"; echo keep > "$HOME/.config/app/c.conf"; echo k > "$HOME/.config/app2/k"

cp "$repo"/backup.sh "$repo"/restore.sh "$repo"/excludes.default.txt "$app/"
chmod +x "$app/backup.sh" "$app/restore.sh"
cd "$app"
# shellcheck disable=SC2088  # тильда в конфиге нужна буквально
printf '~/code\n~/.config |  | whole\n' > backup.conf
printf 'VOLUME_SIZE="1M"\nZSTD_LEVEL=3\nSPLIT=1\nONE_FS=0\n' > backup.env   # тот же формат пишет TUI

fail() { echo "FAIL: $*" >&2; exit 1; }
count() { ls "$@" 2>/dev/null | wc -l; }

echo "== dry-run"
out="$(./backup.sh -n)"; grep -q "items" <<<"$out" || fail "dry-run не показал режим items"
[[ ! -d code ]] || fail "dry-run создал файлы"

echo "== build (по элементам + whole)"
./backup.sh build >/dev/null
(($(count code/projA.tar.zst.*) >= 3)) || fail "projA не нарезан на тома"
[[ -f code/dot_hidden.tar.zst.000 && -f code/my_proj_ё.tar.zst.000 ]] || fail "имена элементов"
[[ -f .config/dot_config.tar.zst.000 && -f code/code.sha256 && -f code/MANIFEST.txt ]] || fail "служебные файлы"
grep -q '^MODE=items' code/MANIFEST.txt && grep -q '^MODE=whole' .config/MANIFEST.txt || fail "MODE в манифесте"
[[ "$(stat -c %a code/projA.tar.zst.000)" == 600 ]] || fail "права томов не 600"

echo "== restore: все архивы директории, сравнение с источником"
./restore.sh code --to "$work/r1" >/dev/null
diff -r --exclude=node_modules "$HOME/code" "$work/r1" || fail "восстановленное отличается"
[[ "$(readlink "$work/r1/projB/lnk")" == b.txt ]] || fail "симлинк потерян"
[[ ! -e "$work/r1/projA/node_modules" ]] || fail "node_modules не исключён"
./restore.sh .config --to "$work/r2" >/dev/null
[[ -f "$work/r2/app/c.conf" && ! -e "$work/r2/app/Cache" ]] || fail "исключения секции [.config]"

echo "== restore --only и обнаружение повреждения"
./restore.sh code --to "$work/r3" --only projB >/dev/null
[[ -f "$work/r3/projB/b.txt" && ! -e "$work/r3/projA" ]] || fail "--only"
cp code/projA.tar.zst.001 "$work/bak"; printf X | dd of=code/projA.tar.zst.001 bs=1 seek=50 conv=notrunc 2>/dev/null
if ./restore.sh code --verify-only >/dev/null 2>&1; then fail "повреждение не обнаружено"; fi
./restore.sh code --to "$work/r4" --only projB --verify-only >/dev/null || fail "проверка должна касаться только выбранного"
cp "$work/bak" code/projA.tar.zst.001

echo "== пересборка: исчезнувший элемент и смена режима"
rm -rf "$HOME/code/projB"
./backup.sh build code >/dev/null
[[ ! -e code/projB.tar.zst.000 ]] || fail "архив исчезнувшего элемента остался"
./backup.sh build code --whole >/dev/null
[[ -f code/code.tar.zst.000 && ! -e code/projA.tar.zst.000 ]] || fail "переход на whole"
./restore.sh code --verify-only >/dev/null || fail "sha256 после смены режима"
./backup.sh build code --items --single >/dev/null
[[ -f code/projA.tar.zst && ! -e code/projA.tar.zst.000 ]] || fail "--single"

echo "== конфиг: backup.sh list видит имена и режимы"
out="$(./backup.sh list)"; grep -q "^code" <<<"$out" || fail "list"

echo "OK: smoke"
