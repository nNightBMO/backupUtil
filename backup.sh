#!/usr/bin/env bash
# =============================================================================
# backup.sh — архивация директорий в тома tar.zst (опционально age) + выгрузка в Telegram.
#
# Источники только ЧИТАЮТСЯ. Результат лежит рядом со скриптом.
# По умолчанию КАЖДЫЙ ЭЛЕМЕНТ директории (файл или подпапка) — отдельный архив:
#   ~/backupUtil/code/<элемент>.tar.zst.000, .001, …  + code.sha256 + MANIFEST.txt
# С флагом --whole (или опцией whole в backup.conf) вся директория — один архив:
#   ~/backupUtil/code/code.tar.zst.000, …
# Обратно: restore.sh (или вручную: cat <архив>.tar.zst.* | zstd -d | tar -x -C <куда>).
# =============================================================================
set -u -o pipefail
shopt -s nullglob
umask 077   # архивы содержат личные данные: файлы 600, каталоги 700

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/.state"
LOG="$ROOT/backup.log"
mkdir -p "$STATE" "$ROOT/.secrets" && chmod 700 "$ROOT/.secrets"

# ---- значения по умолчанию (можно переопределить в backup.env или флагами) ----
VOLUME_SIZE="1800M"          # размер тома; Telegram не принимает файлы >= 2 ГБ
ZSTD_LEVEL=3                 # 1..19
SPLIT=1                      # 1 — тома, 0 — один файл .tar.zst
ONE_FS=0                     # 1 — не заходить в другие ФС/subvolume (tar --one-file-system)
AGE_RECIPIENT=""             # публичный ключ age1…
AGE_RECIPIENTS_FILE=""       # или файл с публичными ключами
CONF="$ROOT/backup.conf"
EXCLUDES="$ROOT/excludes.txt"
# shellcheck disable=SC1091
[[ -f $ROOT/backup.env ]] && source "$ROOT/backup.env"

RESERVED=(".secrets" ".state" ".venv" "__pycache__" ".git" ".github" "docs" "tests" "examples")

if [[ -x $ROOT/.venv/bin/python ]]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi

# ------------------------------- утилиты --------------------------------------
log() {  # уровень сообщение…
    local lvl=$1; shift
    local msg; msg="$(date '+%F %T') [$lvl] $*"
    printf '%s\n' "$msg" >>"$LOG"
    if [[ $lvl == ERR ]]; then printf '%s\n' "$msg" >&2; else printf '%s\n' "$msg"; fi
}
die()   { log ERR "$*"; exit 1; }
human() { numfmt --to=iec-i --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0}B"; }
trim()  { local s=$1; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }
expand_path() {
    local p=$1
    p="${p/#\~/$HOME}"
    p="${p//\$HOME/$HOME}"
    realpath -m -- "$p"
}

SUDO_PID=""; MON_PID=""; CUR_TMP=""
declare -a XF=() ITF=()

stop_monitor() {
    if [[ -n $MON_PID ]]; then kill "$MON_PID" 2>/dev/null; wait "$MON_PID" 2>/dev/null; MON_PID=""; fi
}
cleanup() {
    stop_monitor
    [[ -n $SUDO_PID ]] && kill "$SUDO_PID" 2>/dev/null
    [[ -n $CUR_TMP && -d $CUR_TMP ]] && rm -rf -- "$CUR_TMP"
    local f; for f in "${XF[@]:-}" "${ITF[@]:-}"; do [[ -n $f ]] && rm -f -- "$f"; done
}
trap cleanup EXIT
trap 'log WARN "Прервано пользователем"; exit 130' INT TERM

usage() {
cat <<EOF
Использование: backup.sh [РЕЖИМ] [ОПЦИИ] [ИМЯ|ПУТЬ ...]

Режимы:
  build     только собрать архивы (по умолчанию)
  upload    только загрузить уже собранные тома в Telegram
  all       собрать и загрузить (без имён = всё из конфига = «полный» бэкап с метками autoback#N)
  list      показать разобранный конфиг
  tui       интерфейс настройки: директории, топики, Telegram, параметры, запуск
  tg ...    передать аргументы в tg_upload.py (login | groups | topics | upload)

Опции:
  -n, --dry-run            ничего не писать: размеры, число томов, свободное место
      --whole              вся директория — ОДИН архив (по умолчанию: каждый элемент — свой архив)
      --items              принудительно «каждый элемент — свой архив» (игнорировать whole из конфига)
      --single             каждый архив одним файлом, без нарезки на тома
      --split              тома по \$VOLUME_SIZE (по умолчанию)
      --volume-size 1800M  размер тома
      --level N            уровень zstd 1..19 (сейчас $ZSTD_LEVEL)
      --encrypt            шифровать age ВСЕ архивы (иначе — только с опцией encrypt в конфиге)
      --config FILE        другой файл со списком директорий
      --excludes FILE      другой файл исключений
      --fresh              (upload/all, полный бэкап) начать НОВЫЙ номер autoback#N, не продолжать прерванный
      --no-space-check     не прерываться, если "в худшем случае" не хватает места
      --delete-after-upload  (upload/all) удалить локальные тома после успешной выгрузки
  -y, --yes                не задавать вопросов
  -h, --help

Без ИМЁН обрабатываются все директории из конфига. ИМЯ — имя папки архива
(или путь). Директорию вне конфига тоже можно указать — это разовый запуск.
EOF
}

# ------------------------------ разбор аргументов -----------------------------
if [[ ${1:-} == tui ]]; then shift; exec "$PY" "$ROOT/tui.py" "$@"; fi
if [[ ${1:-} == tg ]]; then shift; exec "$PY" "$ROOT/tg_upload.py" "$@"; fi

MODE=build; DRY=0; SPACE_CHECK=1; DELETE_AFTER=0; ASSUME_YES=0; FORCE_ENC=0; WHOLE_MODE=""; FRESH=0
TARGETS=()
case "${1:-}" in build|upload|all|list) MODE=$1; shift ;; esac
while (($#)); do
    case $1 in
        -n|--dry-run)  DRY=1 ;;
        --whole|--one-archive) WHOLE_MODE=1 ;;
        --items)       WHOLE_MODE=0 ;;
        --fresh)       FRESH=1 ;;
        --single)      SPLIT=0 ;;
        --split)       SPLIT=1 ;;
        --volume-size) VOLUME_SIZE=${2:?нужен размер}; shift ;;
        --level)       ZSTD_LEVEL=${2:?нужен уровень}; shift ;;
        --encrypt)     FORCE_ENC=1 ;;
        --config)      CONF=${2:?нужен файл}; shift ;;
        --excludes)    EXCLUDES=${2:?нужен файл}; shift ;;
        --no-space-check) SPACE_CHECK=0 ;;
        --delete-after-upload) DELETE_AFTER=1 ;;
        -y|--yes)      ASSUME_YES=1 ;;
        -h|--help)     usage; exit 0 ;;
        --)            shift; TARGETS+=("$@"); break ;;
        -*)            die "Неизвестная опция: $1 (см. --help)" ;;
        *)             TARGETS+=("$1") ;;
    esac
    shift
done
[[ $ZSTD_LEVEL =~ ^([1-9]|1[0-9])$ ]] || die "Уровень zstd должен быть 1..19"

# ------------------------------- конфиг ---------------------------------------
declare -a ALL_PATH=() ALL_NAME=() ALL_ENC=() ALL_SUDO=() ALL_WHOLE=()
auto_sudo() { [[ $1 == "$HOME" || $1 == "$HOME"/* ]] && echo 0 || echo 1; }

load_config() {
    [[ -f $CONF ]] || die "Нет конфига: $CONF — создайте его в TUI (./backup.sh tui) или скопируйте backup.conf.example"
    local line path name opts enc sud r whl
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        IFS='|' read -r path name opts <<<"$line"
        path="$(trim "${path:-}")"; name="$(trim "${name:-}")"; opts="$(trim "${opts:-}")"
        [[ -z $path ]] && continue
        path="$(expand_path "$path")"
        [[ -z $name ]] && name="$(basename "$path")"
        if [[ $name == / || $name == . || $name == .. || $name == */* ]]; then
            die "Недопустимое имя папки '$name' для $path — задайте его явно: путь | имя"
        fi
        for r in "${RESERVED[@]}"; do [[ $name == "$r" ]] && die "Имя '$name' зарезервировано утилитой (путь $path)"; done
        for r in "${ALL_NAME[@]:-}"; do [[ $r == "$name" ]] && die "Имя '$name' повторяется — задайте уникальное: путь | имя"; done
        enc=0; [[ ",$opts," == *,encrypt,* ]] && enc=1
        sud="$(auto_sudo "$path")"; [[ ",$opts," == *,sudo,* ]] && sud=1
        whl=0; [[ ",$opts," == *,whole,* ]] && whl=1
        ALL_PATH+=("$path"); ALL_NAME+=("$name"); ALL_ENC+=("$enc"); ALL_SUDO+=("$sud"); ALL_WHOLE+=("$whl")
    done <"$CONF"
}

declare -a SEL_PATH=() SEL_NAME=() SEL_ENC=() SEL_SUDO=() SEL_WHOLE=()
select_sources() {
    local t i p found
    if ((${#TARGETS[@]} == 0)); then
        SEL_PATH=("${ALL_PATH[@]:-}"); SEL_NAME=("${ALL_NAME[@]:-}")
        SEL_ENC=("${ALL_ENC[@]:-}");   SEL_SUDO=("${ALL_SUDO[@]:-}"); SEL_WHOLE=("${ALL_WHOLE[@]:-}")
    else
        for t in "${TARGETS[@]}"; do
            found=0; p="$(expand_path "$t")"
            for i in "${!ALL_PATH[@]}"; do
                if [[ ${ALL_NAME[i]} == "$t" || ${ALL_PATH[i]} == "$p" ]]; then
                    SEL_PATH+=("${ALL_PATH[i]}"); SEL_NAME+=("${ALL_NAME[i]}")
                    SEL_ENC+=("${ALL_ENC[i]}");   SEL_SUDO+=("${ALL_SUDO[i]}"); SEL_WHOLE+=("${ALL_WHOLE[i]}")
                    found=1; break
                fi
            done
            if ((!found)); then
                [[ -d $p ]] || die "'$t' нет в конфиге и такой директории не существует"
                log WARN "'$t' нет в конфиге — разовая директория (имя папки: $(basename "$p"))"
                SEL_PATH+=("$p"); SEL_NAME+=("$(basename "$p")"); SEL_ENC+=(0); SEL_SUDO+=("$(auto_sudo "$p")"); SEL_WHOLE+=(0)
            fi
        done
    fi
    local k
    if ((FORCE_ENC)); then for k in "${!SEL_ENC[@]}"; do SEL_ENC[k]=1; done; fi
    if [[ -n $WHOLE_MODE ]]; then for k in "${!SEL_WHOLE[@]}"; do SEL_WHOLE[k]=$WHOLE_MODE; done; fi
}

# ------------------------------ sudo / проверки -------------------------------
sudo_init() {
    [[ -n $SUDO_PID ]] && return 0
    log INFO "Нужен sudo (для директорий вне \$HOME). Запрашиваю пароль один раз…"
    sudo -v || die "sudo недоступен"
    ( while true; do sudo -n true 2>/dev/null || exit; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) &
    SUDO_PID=$!
}

HAVE_PV=0; command -v pv >/dev/null 2>&1 && HAVE_PV=1
NEED_ENC=0
check_tools() {
    local t
    for t in tar zstd split sha256sum numfmt du df flock stat realpath; do
        command -v "$t" >/dev/null 2>&1 || die "Не найдена утилита: $t"
    done
    tar --version 2>/dev/null | head -n1 | grep -q 'GNU tar' || die "Нужен GNU tar"
    if ((NEED_ENC)); then command -v age >/dev/null 2>&1 || die "Для шифрования нужен age: sudo pacman -S age"; fi
    ((HAVE_PV)) || log WARN "pv не установлен — без процентов прогресса (sudo pacman -S pv)"
}

declare -a AGE_ARGS=()
setup_age() {
    if   [[ -n $AGE_RECIPIENTS_FILE ]]; then AGE_ARGS=(-R "${AGE_RECIPIENTS_FILE/#\~/$HOME}")
    elif [[ -n $AGE_RECIPIENT ]];       then AGE_ARGS=(-r "$AGE_RECIPIENT")
    else die "Шифрование запрошено, но в backup.env не задан AGE_RECIPIENT / AGE_RECIPIENTS_FILE"; fi
}

# ------------------------- исключения и оценка размера ------------------------
safe_name() {  # имя для файла: пробелы → _, ведущая точка → dot_
    local s=${1// /_}
    [[ $s == .* ]] && s="dot_${s#.}"
    printf '%s' "$s"
}

make_excludes() {  # src name out — секции [*] и [name] из excludes.txt
    local src=$1 name=$2 out=$3
    : >"$out"
    local ex=$EXCLUDES
    [[ -f $ex ]] || ex="$ROOT/excludes.default.txt"   # личного excludes.txt нет — берём список по умолчанию
    if [[ -f $ex ]]; then
        awk -v want="$name" '
            /^[[:space:]]*(#|$)/ { next }
            /^\[.*\][[:space:]]*$/ { s=$0; sub(/^\[/,"",s); sub(/\][[:space:]]*$/,"",s); cur=s; next }
            { if (cur=="" || cur=="*" || cur==want) print }
        ' "$ex" >"$out"
    fi
    # саму папку утилиты (и секреты) никогда не кладём в бэкап
    if [[ $ROOT == "$src"/* ]]; then printf './%s\n' "${ROOT#"$src"/}" >>"$out"; fi
    return 0
}

is_excluded() {  # элемент верхнего уровня подпадает под шаблон без «/» из файла исключений?
    local item=$1 pat
    while IFS= read -r pat; do
        [[ -z $pat || $pat == */* ]] && continue
        # shellcheck disable=SC2053
        [[ $item == $pat ]] && return 0
    done <"$2"
    return 1
}

du_bytes() {  # sudo xfile path → байты (оценка сверху)
    local -a pre=(); (($1)) && pre=(sudo)
    local out
    out="$("${pre[@]}" du -sb -X "$2" -- "$3" 2>/dev/null | tail -n1 | cut -f1)"
    echo "${out:-0}"
}

list_items() {  # src sudo xfile outfile → "размер\x1fимя\0" для каждого элемента верхнего уровня
    local src=$1 sud=$2 xf=$3 out=$4 item sz
    local -a pre=(); ((sud)) && pre=(sudo)
    while IFS= read -r -d '' item; do
        is_excluded "$item" "$xf" && continue
        [[ "$src/$item" == "$ROOT" ]] && continue
        sz="$(du_bytes "$sud" "$xf" "$src/$item")"
        printf '%s\x1f%s\0' "$sz" "$item"
    done < <("${pre[@]}" find "$src" -mindepth 1 -maxdepth 1 -printf '%f\0' | LC_ALL=C sort -z) >"$out"
}

vols_for() {  # байты → оценка числа томов (≥1)
    local n=1
    if ((SPLIT)); then n=$(( ($1 + vol_bytes - 1) / vol_bytes )); ((n < 1)) && n=1; fi
    echo "$n"
}

summarize_items() {  # file → "число_элементов сумма_байт оценка_томов"
    local size item c=0 r=0 v=0
    while IFS=$'\x1f' read -r -d '' size item; do
        c=$((c + 1)); r=$((r + size)); v=$((v + $(vols_for "$size")))
    done <"$1"
    echo "$c $r $v"
}

# ---------------------------- этапы конвейера ---------------------------------
stage_progress() { if (($2)) && (($1 > 0)); then pv -pterb -s "$1"; else cat; fi; }
stage_encrypt()  { if (($1)); then age "${AGE_ARGS[@]}"; else cat; fi; }
stage_write()    { if ((SPLIT)); then split -b "$VOLUME_SIZE" -d -a 3 - "$1"; else cat >"$1"; fi; }

start_monitor() {  # если нет pv — раз в 20 с пишем, сколько уже записано
    ( while sleep 20; do log INFO "    …записано $(human "$(du -sb "$1" 2>/dev/null | cut -f1)")"; done ) &
    MON_PID=$!
}

# Один архив: build_set src sudo enc xfile member base raw tmpd → 0/1; тома в $tmpd/$base[.NNN]
build_set() {
    local src=$1 sud=$2 enc=$3 xf=$4 member=$5 base=$6 raw=$7 tmpd=$8
    local -a pre=(); ((sud)) && pre=(sudo)
    local -a tar_cmd=(tar --create --file=- --directory="$src"
                      --acls --xattrs --numeric-owner --sparse
                      --warning=no-file-ignored --exclude-from="$xf")
    ((ONE_FS)) && tar_cmd+=(--one-file-system)
    tar_cmd+=(-- "$member")

    local wprefix; if ((SPLIT)); then wprefix="$tmpd/$base."; else wprefix="$tmpd/$base"; fi
    local big=0; ((raw >= 8388608)) && big=1
    local use_pv=0; ((HAVE_PV && big)) && use_pv=1
    ((!HAVE_PV && big)) && start_monitor "$tmpd"

    local -a st; local rc=0 i
    "${pre[@]}" "${tar_cmd[@]}" \
        | stage_progress "$raw" "$use_pv" \
        | zstd -T0 -q -c "-$ZSTD_LEVEL" \
        | stage_encrypt "$enc" \
        | stage_write "$wprefix"
    st=("${PIPESTATUS[@]}")
    stop_monitor

    if ((st[0] > 1)); then log ERR "    $member: tar завершился с кодом ${st[0]}"; rc=1; fi
    if ((st[0] == 1)); then log WARN "    $member: часть файлов менялась во время чтения (tar код 1) — архив создан"; fi
    for i in 1 2 3 4; do
        if ((st[i] != 0)); then log ERR "    $member: этап конвейера #$i завершился с кодом ${st[i]}"; rc=1; fi
    done
    if ((rc == 0)); then
        local -a got=("$tmpd/$base" "$tmpd/$base".[0-9][0-9][0-9])
        local f ok=0; for f in "${got[@]}"; do [[ -e $f ]] && ok=1; done
        ((ok)) || { log ERR "    $member: тома не созданы"; rc=1; }
    fi
    if ((rc)); then rm -f -- "$tmpd/$base" "$tmpd/$base".[0-9][0-9][0-9]; return 1; fi
    return 0
}

write_manifest() {  # outdir name src enc raw whole failed_csv dfn
    local outdir=$1 name=$2 src=$3 enc=$4 raw=$5 whole=$6 failed=$7 dfn=$8
    local f b set total=0 sz sets=0
    local -A nv=() by=()
    for f in "$outdir"/*.tar.zst*; do
        b=${f##*/}; set=${b%.[0-9][0-9][0-9]}
        sz="$(stat -c %s -- "$f")"
        nv[$set]=$(( ${nv[$set]:-0} + 1 )); by[$set]=$(( ${by[$set]:-0} + sz )); total=$((total + sz))
    done
    sets=${#nv[@]}
    {
        echo "# backupUtil manifest"
        echo "DATE=$(date --iso-8601=seconds)"
        echo "HOST=$(hostname)"
        echo "NAME=$name"
        echo "SOURCE=$src"
        echo "MODE=$( ((whole)) && echo whole || echo items )"
        echo "ENCRYPTED=$enc"
        echo "SPLIT=$SPLIT"
        echo "VOLUME_SIZE=$VOLUME_SIZE"
        echo "ZSTD_LEVEL=$ZSTD_LEVEL"
        echo "RAW_BYTES=$raw"
        echo "ARCHIVE_BYTES=$total"
        echo "SETS=$sets"
        echo "SHA256_FILE=$dfn.sha256"
        [[ -n $failed ]] && echo "FAILED=$failed"
        printf 'RESTORE=%q %q --to %q\n' "$ROOT/restore.sh" "$outdir" "$src"
        printf 'MANUAL=cd %q && sha256sum -c %q && cat ARCHIVE.[0-9][0-9][0-9] | %szstd -d | tar -x --acls --xattrs --numeric-owner -C %q\n' \
            "$outdir" "$dfn.sha256" "$( ((enc)) && echo 'age -d -i KEYFILE | ')" "$src"
        local k
        while IFS= read -r k; do echo "SET=$k|${nv[$k]}|${by[$k]}"; done < <(printf '%s\n' "${!nv[@]}" | LC_ALL=C sort)
    } >"$outdir/MANIFEST.txt"
}

# ------------------------------ сборка одной директории -----------------------
build_dir() {  # src name enc sudo whole raw xfile itemsfile idx total
    local src=$1 name=$2 enc=$3 sud=$4 whole=$5 raw=$6 xf=$7 itemsf=$8 idx=$9 total=${10}
    local outdir="$ROOT/$name" dfn tmpd fname size item k=0 n=0 nitems f b fn
    local ext=tar.zst; ((enc)) && ext=tar.zst.age
    dfn="$(safe_name "$name")"
    log INFO "[$idx/$total] $name ← $src  (режим: $( ((whole)) && echo 'один архив' || echo 'каждый элемент — архив'); sudo=$sud encrypt=$enc split=$SPLIT)"
    [[ -d $src ]] || { log ERR "$name: нет директории $src"; return 1; }
    mkdir -p "$outdir" || return 1
    tmpd="$outdir/.tmp.$$"; CUR_TMP=$tmpd
    mkdir -p "$tmpd" || return 1

    local -A ALLF=() BUILT_F=() USED=()
    local -a failed=()

    if ((whole)); then
        ALLF["$dfn"]=1
        log INFO "  архив $dfn (≈$(human "$raw"))"
        if build_set "$src" "$sud" "$enc" "$xf" . "$dfn.$ext" "$raw" "$tmpd"; then n=1; BUILT_F["$dfn"]=1; else failed+=("$name"); fi
    else
        nitems="$(tr -cd '\0' <"$itemsf" | wc -c)"
        while IFS=$'\x1f' read -r -d '' size item; do
            k=$((k + 1))
            fname="$(safe_name "$item")"
            [[ -n ${USED["$fname"]:-} ]] && fname="${fname}~$k"
            USED["$fname"]=1; ALLF["$fname"]=1
            log INFO "  ($k/$nitems) $item  (≈$(human "$size"))"
            if build_set "$src" "$sud" "$enc" "$xf" "./$item" "$fname.$ext" "$size" "$tmpd"; then
                n=$((n + 1)); BUILT_F["$fname"]=1
            else
                failed+=("$item")
            fi
        done <"$itemsf"
    fi

    if ((n == 0)); then
        rm -rf -- "$tmpd"; CUR_TMP=""
        if ((${#failed[@]})); then log ERR "$name: не собран ни один архив"; return 1; fi
        log WARN "$name: в директории нечего архивировать (пусто или всё исключено)"; return 0
    fi

    log INFO "$name: считаю sha256…"
    local -a newf=("$tmpd"/*.tar.zst*)
    local -a names=("${newf[@]##*/}")
    ( cd "$tmpd" && sha256sum -- "${names[@]}" >"$tmpd/new.sha" ) \
        || { log ERR "$name: sha256sum не удался"; rm -rf -- "$tmpd"; CUR_TMP=""; return 1; }

    # Новое готово — заменяем прошлое: удаляем тома пересобранных и исчезнувших из источника
    # элементов (тома не пересобравшихся из-за ошибки элементов остаются нетронутыми).
    for f in "$outdir"/*.tar.zst*; do
        b=${f##*/}; fn=${b%.tar.zst*}
        if [[ -z ${ALLF["$fn"]:-} || -n ${BUILT_F["$fn"]:-} ]]; then rm -f -- "$f"; fi
    done
    local oldsha="$outdir/$dfn.sha256" l fnm; local -a keep=()
    if [[ -f $oldsha ]]; then
        while IFS= read -r l; do
            fnm="${l#*  }"; fnm="${fnm#\*}"
            [[ -e $outdir/$fnm ]] && keep+=("$l")
        done <"$oldsha"
    fi
    { ((${#keep[@]})) && printf '%s\n' "${keep[@]}"; cat "$tmpd/new.sha"; } | LC_ALL=C sort -k2,2 >"$tmpd/final.sha"
    mv -- "${newf[@]}" "$outdir/" || { log ERR "$name: не удалось переместить результат"; return 1; }
    mv -- "$tmpd/final.sha" "$oldsha"
    rm -rf -- "$tmpd"; CUR_TMP=""

    local fcsv=""
    if ((${#failed[@]})); then printf -v fcsv '%s,' "${failed[@]}"; fcsv=${fcsv%,}; fi
    write_manifest "$outdir" "$name" "$src" "$enc" "$raw" "$whole" "$fcsv" "$dfn"
    local total_bytes; total_bytes="$(stat -c %s -- "$outdir"/*.tar.zst* | awk '{s+=$1} END{print s+0}')"
    log INFO "$name: готово — архивов $n, всего $(human "$total_bytes"). Манифест: $outdir/MANIFEST.txt"
    if ((${#failed[@]})); then log ERR "$name: НЕ собраны: ${failed[*]}"; return 1; fi
    return 0
}

run_upload() {  # full(0/1) имя…
    local full=$1; shift
    local -a args=(upload)
    ((full)) && args+=(--full)
    ((FRESH)) && args+=(--fresh)
    ((DRY)) && args+=(--dry-run)
    ((DELETE_AFTER)) && args+=(--delete-after-upload)
    ((ASSUME_YES)) && args+=(--yes)
    args+=("$@")
    "$PY" "$ROOT/tg_upload.py" "${args[@]}"
}

covers_all() {  # имена… — все ли директории из конфига среди переданных?
    local a s found
    for a in "${ALL_NAME[@]}"; do
        found=0; for s in "$@"; do [[ $s == "$a" ]] && { found=1; break; }; done
        ((found)) || return 1
    done
    return 0
}

# ---------------------------------- main --------------------------------------
if [[ $MODE == upload ]]; then
    load_config
    UP=(); full=0
    if ((${#TARGETS[@]} == 0)); then
        UP=("${ALL_NAME[@]}")
    else
        for t in "${TARGETS[@]}"; do
            nm="$t"; p="$(expand_path "$t")"
            for i in "${!ALL_PATH[@]}"; do
                if [[ ${ALL_NAME[i]} == "$t" || ${ALL_PATH[i]} == "$p" ]]; then nm="${ALL_NAME[i]}"; break; fi
            done
            UP+=("$nm")
        done
    fi
    covers_all "${UP[@]}" && full=1
    run_upload "$full" "${UP[@]}"; exit $?
fi

load_config
select_sources

if [[ $MODE == list ]]; then
    printf '%-22s %-6s %-5s %-8s %s\n' NAME SUDO WHOLE ENCRYPT SOURCE
    for i in "${!SEL_PATH[@]}"; do printf '%-22s %-6s %-5s %-8s %s\n' "${SEL_NAME[i]}" "${SEL_SUDO[i]}" "${SEL_WHOLE[i]}" "${SEL_ENC[i]}" "${SEL_PATH[i]}"; done
    exit 0
fi

exec 9>"$STATE/lock"; flock -n 9 || die "Уже запущен другой экземпляр backup.sh"

N=${#SEL_PATH[@]}
((N > 0)) || die "Список директорий пуст"
for e in "${SEL_ENC[@]}"; do ((e)) && NEED_ENC=1; done
check_tools
((NEED_ENC)) && setup_age
vol_bytes="$(numfmt --from=iec "$VOLUME_SIZE")" || die "Некорректный размер тома: $VOLUME_SIZE"

for s in "${SEL_SUDO[@]}"; do if ((s)); then sudo_init; break; fi; done

log INFO "=== backup.sh $MODE: $N директор(ий)$( ((DRY)) && echo ' [dry-run]') ==="
declare -a RAW=() NARC=() EVOL=(); total_raw=0
for ((i = 0; i < N; i++)); do
    XF[i]="$(mktemp "$STATE/excl.XXXXXX")"; ITF[i]="$(mktemp "$STATE/items.XXXXXX")"
    RAW[i]=0; NARC[i]=0; EVOL[i]=0
    [[ -d ${SEL_PATH[i]} ]] || continue
    make_excludes "${SEL_PATH[i]}" "${SEL_NAME[i]}" "${XF[i]}"
    if ((SEL_WHOLE[i])); then
        RAW[i]="$(du_bytes "${SEL_SUDO[i]}" "${XF[i]}" "${SEL_PATH[i]}")"; NARC[i]=1; EVOL[i]="$(vols_for "${RAW[i]}")"
    else
        list_items "${SEL_PATH[i]}" "${SEL_SUDO[i]}" "${XF[i]}" "${ITF[i]}"
        read -r "NARC[i]" "RAW[i]" "EVOL[i]" < <(summarize_items "${ITF[i]}")
    fi
    total_raw=$((total_raw + RAW[i]))
done

free="$(df --output=avail -B1 "$ROOT" | tail -n1 | tr -d ' ')"
if ((DRY)) || [[ $MODE == build || $MODE == all ]]; then
    printf '\n%-20s %11s %8s %8s  %-8s %s\n' NAME "RAW(≤)" АРХИВОВ "ТОМОВ(≤)" РЕЖИМ ИСТОЧНИК
    for ((i = 0; i < N; i++)); do
        miss=""; [[ -d ${SEL_PATH[i]} ]] || miss="  [НЕТ ДИРЕКТОРИИ]"
        printf '%-20s %11s %8s %8s  %-8s %s%s\n' "${SEL_NAME[i]}" "$(human "${RAW[i]}")" "${NARC[i]}" "${EVOL[i]}" \
            "$( ((SEL_WHOLE[i])) && echo whole || echo items )" "${SEL_PATH[i]}" "$miss"
    done
    printf '\nИтого до сжатия ≤ %s; свободно в %s: %s\n\n' "$(human "$total_raw")" "$ROOT" "$(human "$free")"
fi
if ((total_raw > free)); then
    msg="В худшем случае (без сжатия) нужно $(human "$total_raw"), свободно $(human "$free")."
    if ((DRY)); then log WARN "$msg"
    elif ((SPACE_CHECK)); then die "$msg Если данные хорошо сжимаются — запустите с --no-space-check, либо дополните excludes.txt."
    else log WARN "$msg Продолжаю (--no-space-check)."; fi
fi

FULL=0; covers_all "${SEL_NAME[@]}" && FULL=1

if ((DRY)); then
    log INFO "dry-run завершён, ничего не записано."
    [[ $MODE == all ]] && run_upload "$FULL" "${SEL_NAME[@]}"
    exit 0
fi

fail=0; BUILT=()
for ((i = 0; i < N; i++)); do
    if build_dir "${SEL_PATH[i]}" "${SEL_NAME[i]}" "${SEL_ENC[i]}" "${SEL_SUDO[i]}" "${SEL_WHOLE[i]}" "${RAW[i]}" "${XF[i]}" "${ITF[i]}" "$((i + 1))" "$N"; then
        BUILT+=("${SEL_NAME[i]}")
    else
        fail=$((fail + 1))
    fi
done

# Страховка: файлы создаёт конвейер от вашего пользователя (sudo только у tar), но если что-то
# оказалось не вашим — возвращаем владельца.
if find "$ROOT" -xdev ! -user "$(id -u)" -print -quit 2>/dev/null | grep -q .; then
    log WARN "Найдены файлы с чужим владельцем — возвращаю владельца на $(id -un)"
    sudo_init; sudo chown -R "$(id -u):$(id -g)" "$ROOT"
fi

log INFO "=== Сборка завершена: успешно ${#BUILT[@]}, с ошибками $fail ==="
rc=0; ((fail)) && rc=1

if [[ $MODE == all ]]; then
    if ((${#BUILT[@]})); then
        if ((FULL && fail)); then log WARN "Не всё собрано — выгрузка пойдёт как частичная (без меток autoback#N)"; FULL=0; fi
        run_upload "$FULL" "${BUILT[@]}" || rc=1
    else log WARN "Нечего загружать"; fi
fi
exit $rc
