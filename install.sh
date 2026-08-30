#!/usr/bin/env bash
#
# Установка markov-бота на VPS одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/cats63850-ctrl/lizrawai/main/install.sh | sudo bash
#
# Повторный запуск обновляет бота: код подтягивается заново, а .env
# и база сообщений остаются на месте.
#
# Удалить всё:  sudo bash install.sh --uninstall

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cats63850-ctrl/lizrawai.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/markovbot}"
APP_USER="${APP_USER:-markovbot}"
SERVICE="${SERVICE:-markovbot}"
SRC_SUBDIR="lizrawai"   # внутри репозитория код лежит в подпапке

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'

say()  { echo "${GREEN}==>${OFF} ${BOLD}$*${OFF}"; }
warn() { echo "${YELLOW}!${OFF} $*"; }
die()  { echo "${RED}Ошибка:${OFF} $*" >&2; exit 1; }

# /dev/tty существует всегда, но открыть его получается не везде (cron, CI,
# docker без -t). Проверяем именно возможность открыть.
have_tty() { { : >/dev/tty; } 2>/dev/null; }

# Вопросы задаём терминалу напрямую: при запуске через `curl | bash`
# обычный stdin занят самим скриптом, и read сработал бы вхолостую.
ask() {
    local prompt="$1" varname="$2" silent="${3:-}" answer=""
    have_tty || die "Нет терминала для ввода. Запусти: DISCORD_TOKEN=токен bash install.sh"
    if [ -n "$silent" ]; then
        read -rsp "$prompt" answer < /dev/tty; echo
    else
        read -rp "$prompt" answer < /dev/tty
    fi
    printf -v "$varname" '%s' "$answer"
}

# Выполнить команду от имени бота. На минимальных образах VPS sudo часто
# не установлен, поэтому основной вариант — runuser из util-linux.
as_app() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -u "$APP_USER" -H -- "$@"
    else
        su -s /bin/sh "$APP_USER" -c "$(printf '%q ' "$@")"
    fi
}

# ---------------------------------------------------------------- проверки

[ "$(id -u)" -eq 0 ] || die "Нужен root. Запусти с sudo."

command -v systemctl >/dev/null 2>&1 || die "systemd не найден — этот установщик рассчитан на него."

# ------------------------------------------------------------- удаление

if [ "${1:-}" = "--uninstall" ]; then
    say "Удаляю бота"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE}.service"
    systemctl daemon-reload
    warn "Папка $APP_DIR оставлена — там база и токен."
    warn "Снести совсем:  rm -rf $APP_DIR && userdel $APP_USER"
    exit 0
fi

# --------------------------------------------------------- зависимости

install_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        say "Ставлю зависимости (apt)"
        export DEBIAN_FRONTEND=noninteractive
        # Чужой сломанный репозиторий в sources.list не должен ронять установку:
        # нужные пакеты часто уже стоят или ставятся из основного зеркала.
        apt-get update -qq || warn "apt-get update отработал с ошибками, продолжаю."
        apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null \
            || warn "apt-get install ругнулся, проверю пакеты ниже."
    elif command -v dnf >/dev/null 2>&1; then
        say "Ставлю зависимости (dnf)"
        dnf install -y -q git python3 python3-pip >/dev/null
    elif command -v pacman >/dev/null 2>&1; then
        say "Ставлю зависимости (pacman)"
        pacman -Sy --noconfirm --needed git python python-pip >/dev/null
    elif command -v apk >/dev/null 2>&1; then
        say "Ставлю зависимости (apk)"
        apk add --quiet git python3 py3-pip
    else
        warn "Неизвестный пакетный менеджер — проверю, что git и python3 уже есть."
    fi

    command -v git >/dev/null 2>&1 || die "git не установился, поставь вручную."
    command -v python3 >/dev/null 2>&1 || die "python3 не установился, поставь вручную."
}

check_python_version() {
    local ver
    ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    local major="${ver%%.*}" minor="${ver##*.}"
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
        die "Нужен Python 3.10+, а на сервере $ver. Обнови систему или поставь новее."
    fi
    say "Python $ver — подходит"
}

install_packages
check_python_version

# ------------------------------------------------------------------ юзер

if id "$APP_USER" >/dev/null 2>&1; then
    say "Пользователь $APP_USER уже есть"
else
    say "Создаю системного пользователя $APP_USER"
    useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$APP_USER" 2>/dev/null \
        || useradd -r -m -d "$APP_DIR" -s /sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR"

IS_UPDATE=0
[ -f "$APP_DIR/bot.py" ] && IS_UPDATE=1

# --------------------------------------------------------------- код

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Качаю код из $REPO_URL"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP/repo" >/dev/null 2>&1 \
    || die "Не удалось склонировать репозиторий. Проверь ссылку и что он публичный."

SRC="$TMP/repo/$SRC_SUBDIR"
[ -d "$SRC" ] || SRC="$TMP/repo"
[ -f "$SRC/bot.py" ] || die "В репозитории нет bot.py — структура изменилась?"

# .env и база не трогаются: rsync без --delete, а cp просто перезапишет код.
say "Раскладываю файлы в $APP_DIR"
cp -r "$SRC"/. "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------- venv

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    say "Создаю виртуальное окружение"
    as_app python3 -m venv "$APP_DIR/.venv" \
        || die "Не создалось venv. На Debian/Ubuntu поставь python3-venv."
fi

say "Ставлю discord.py"
as_app "$APP_DIR/.venv/bin/pip" install -q --upgrade pip >/dev/null
as_app "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
    || die "Не установились зависимости."

# ----------------------------------------------------------------- .env

ENV_FILE="$APP_DIR/.env"

if [ -f "$ENV_FILE" ] && grep -q '^DISCORD_TOKEN=.\+' "$ENV_FILE"; then
    say "Токен уже прописан, оставляю как есть"
else
    TOKEN="${DISCORD_TOKEN:-}"
    if [ -z "$TOKEN" ]; then
        echo
        echo "Токен бота: Discord Developer Portal → твоё приложение → Bot → Reset Token."
        echo "Там же включи MESSAGE CONTENT INTENT, иначе бот не увидит текст сообщений."
        echo
        ask "Вставь токен (ввод скрыт): " TOKEN silent
    fi
    [ -n "$TOKEN" ] || die "Пустой токен."

    GUILD=""
    if [ -z "${DEV_GUILD_ID:-}" ] && have_tty; then
        ask "ID сервера для мгновенных слэш-команд (Enter — пропустить): " GUILD
    else
        GUILD="${DEV_GUILD_ID:-}"
    fi

    umask 077
    {
        echo "DISCORD_TOKEN=$TOKEN"
        echo "DEFAULT_PREFIX=g."
        echo "DATABASE_PATH=$APP_DIR/markovbot.db"
        [ -n "$GUILD" ] && echo "DEV_GUILD_ID=$GUILD"
    } > "$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    say "Токен записан в $ENV_FILE (доступ только у $APP_USER)"
fi

# -------------------------------------------------------------- systemd

say "Настраиваю автозапуск"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=Markov Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
EnvironmentFile=$APP_DIR/.env

Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

# ---------------------------------------------------------------- итог

sleep 4
echo
if systemctl is-active --quiet "$SERVICE"; then
    if [ "$IS_UPDATE" = "1" ]; then
        say "Бот обновлён и работает"
    else
        say "Бот установлен и работает"
    fi
    echo
    echo "  Логи:      journalctl -u $SERVICE -f"
    echo "  Рестарт:   systemctl restart $SERVICE"
    echo "  Стоп:      systemctl stop $SERVICE"
    echo "  Обновить:  запусти этот же скрипт ещё раз"
    echo
    echo "На сервере в Discord: ${BOLD}g.wizard${OFF} → ${BOLD}g.import 5000${OFF} → ${BOLD}g.generate${OFF}"
else
    warn "Служба не поднялась. Смотри, что пишет:"
    echo
    journalctl -u "$SERVICE" -n 25 --no-pager
    echo
    warn "Чаще всего это неверный токен или выключенный MESSAGE CONTENT INTENT."
fi
