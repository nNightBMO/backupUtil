#!/usr/bin/env bash
# =============================================================================
# restore.sh — проверить sha256, склеить тома и распаковать.
#
#   ./restore.sh ~/backupUtil/code --to ~/restored/code        # все архивы директории
#   ./restore.sh ~/backupUtil/code --to /tmp/x --only myproj   # только архив(ы), в имени которых есть "myproj"
#   ./restore.sh ~/backupUtil/etc  --original                  # в исходный путь из MANIFEST.txt (спросит подтверждение)
#   ./restore.sh ~/backupUtil/ssh  --to /tmp/x --age-key ~/backupUtil/.secrets/age.key
#   ./restore.sh ~/backupUtil/code --verify-only               # только sha256
#   ./restore.sh ~/backupUtil/code --test                      # sha256 + прогон через tar -t, без записи
#
# Цель (--to) — это ИСХОДНАЯ директория: и в режиме «элемент = архив», и в режиме whole
# содержимое раскладывается так же, как лежало в источнике.
# Папка может быть скачана из Telegram: достаточно томов и файла <имя>.sha256.
# =============================================================================
set -u -o pipefail
shopt -s nullglob

die() { echo "[ERR] $*" >&2; exit 1; }
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

DIR=""; TARGET=""; ORIG=0; KEY=""; USE_SUDO=auto; TEST=0; VERIFY_ONLY=0; YES=0; ONLY=""
while (($#)); do
    case $1 in
        --to)          TARGET=${2:?нужен путь}; shift ;;
        --original)    ORIG=1 ;;
        --only)        ONLY=${2:?нужна подстрока имени}; shift ;;
        --age-key)     KEY=${2:?нужен файл ключа}; shift ;;
        --sudo)        USE_SUDO=1 ;;
        --no-sudo)     USE_SUDO=0 ;;
        --test)        TEST=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        -y|--yes)      YES=1 ;;
        -h|--help)     usage 0 ;;
        -*)            die "Неизвестная опция: $1" ;;
        *)             [[ -z $DIR ]] && DIR=$1 || die "Лишний аргумент: $1" ;;
    esac
    shift
done
[[ -n $DIR && -d $DIR ]] || usage 1
DIR="$(cd "$DIR" && pwd)"
cd "$DIR" || exit 1

shas=(./*.sha256)
((${#shas[@]} == 1)) || die "В $DIR должен быть ровно один *.sha256 (найдено: ${#shas[@]})"
SHA="${shas[0]#./}"

# --- список архивов (наборов томов) из sha256-файла ---
declare -A SETVOLS=()
declare -a SETS=()
while read -r _ name; do
    name="${name#\*}"
    set="${name%.[0-9][0-9][0-9]}"
    [[ $ONLY == "" || $set == *"$ONLY"* ]] || continue
    [[ -z ${SETVOLS[$set]:-} ]] && SETS+=("$set")
    SETVOLS[$set]+="$name"$'\n'
done < <(sort -k2,2 "$SHA")
((${#SETS[@]})) || die "Нет архивов${ONLY:+, подходящих под «$ONLY»}"

# --- проверка sha256 только для выбранных томов ---
echo "[*] Проверяю sha256: архивов ${#SETS[@]} ($SHA)…"
tmpsum="$(mktemp)"; trap 'rm -f "$tmpsum"' EXIT
for set in "${SETS[@]}"; do
    while IFS= read -r v; do [[ -n $v ]] && grep -F -- "  $v" "$SHA" >>"$tmpsum"; done <<<"${SETVOLS[$set]}"
done
sha256sum -c --quiet -- "$tmpsum" || die "Контрольные суммы не совпали — том повреждён или скачан не полностью"
echo "[OK] Все выбранные тома целы."
((VERIFY_ONLY)) && exit 0

ANY_ENC=0; for set in "${SETS[@]}"; do [[ $set == *.tar.zst.age ]] && ANY_ENC=1; done
if ((ANY_ENC)); then
    [[ -n $KEY && -r $KEY ]] || die "Архив зашифрован age: укажите ключ через --age-key FILE"
    command -v age >/dev/null || die "Нужен age: sudo pacman -S age"
fi
command -v zstd >/dev/null || die "Нужен zstd"

SRC=""; [[ -f MANIFEST.txt ]] && SRC="$(sed -n 's/^SOURCE=//p' MANIFEST.txt | head -n1)"
if ((ORIG)); then [[ -n $SRC ]] || die "В MANIFEST.txt нет SOURCE"; TARGET=$SRC; fi
if ((!TEST)); then [[ -n $TARGET ]] || die "Укажите --to ПУТЬ или --original"; TARGET="$(realpath -m -- "$TARGET")"; fi

pre=()
if [[ $USE_SUDO == auto ]]; then
    if ((EUID != 0)) && [[ -n $TARGET && $TARGET != "$HOME" && $TARGET != "$HOME"/* ]]; then USE_SUDO=1; else USE_SUDO=0; fi
fi
((USE_SUDO)) && pre=(sudo)

if ((TEST)); then
    echo "[*] Режим --test: распаковки не будет"
else
    echo "[*] Распаковка в: $TARGET (существующие файлы будут перезаписаны); sudo: $USE_SUDO"
    if ((ORIG && !YES)); then
        read -r -p "Распаковать поверх исходного пути $TARGET? [y/N] " a
        [[ $a == y || $a == Y || $a == д || $a == Д ]] || die "Отменено"
    fi
    "${pre[@]}" mkdir -p -- "$TARGET" || die "Не удалось создать $TARGET"
fi

stage_tar() {
    if ((TEST)); then tar --list --file=- >/dev/null
    else "${pre[@]}" tar --extract --file=- --directory="$TARGET" --acls --xattrs --numeric-owner --preserve-permissions
    fi
}

n=0
for set in "${SETS[@]}"; do
    n=$((n + 1))
    mapfile -t V < <(printf '%s' "${SETVOLS[$set]}" | sort -V)
    enc=0; [[ $set == *.tar.zst.age ]] && enc=1
    total="$(stat -c %s -- "${V[@]}" | awk '{s+=$1} END{print s+0}')"
    echo "[*] ($n/${#SETS[@]}) $set: томов ${#V[@]}, $total байт"
    {
        if command -v pv >/dev/null 2>&1; then cat -- "${V[@]}" | pv -pterb -s "$total"; else cat -- "${V[@]}"; fi
    } | { if ((enc)); then age -d -i "$KEY"; else cat; fi; } | zstd -d -T0 -q -c | stage_tar
    st=("${PIPESTATUS[@]}")
    for s in "${st[@]}"; do ((s == 0)) || die "$set: ошибка распаковки (коды: ${st[*]})"; done
done
echo "[OK] Готово: распаковано архивов — $n."
