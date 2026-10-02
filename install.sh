#!/usr/bin/env bash
# Установка EXIF-бота на сервер с Ubuntu 22.04+ или Debian 12+ одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/zfd430792-coder/CL-/HEAD/install.sh | sudo bash
#
# Скрипт ставит Python и ExifTool, спрашивает токен и запускает бота как systemd-службу,
# которая сама поднимается после перезагрузки. Повторный запуск обновляет бота.
# Без вопросов: ... | sudo BOT_TOKEN=123:ABC ALLOWED_USERS=111 bash

set -euo pipefail

REPO_URL="https://github.com/zfd430792-coder/CL-.git"
APP_DIR="/opt/exif-bot"
APP_USER="exifbot"
SERVICE="exif-bot"
API="https://api.telegram.org"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

# При запуске через «curl | bash» stdin занят самим скриптом, поэтому ввод читаем с терминала
has_tty() { (exec < /dev/tty) 2>/dev/null; }
ask() { local answer; read -r -p "$1" answer < /dev/tty; printf '%s' "$answer"; }

# Значение из уже существующего .env (при повторной установке)
env_value() { sed -n "s/^$1=//p" "$APP_DIR/.env" 2>/dev/null | tail -n 1 || true; }

# Вызов Telegram API: $1 — метод с параметрами, $2 — что достать из ответа r (выражение на Python)
tg() {
  curl -sS --max-time 30 "$API/bot$TOKEN/$1" \
    | python3 -c "import json, sys; r = json.load(sys.stdin); print($2)"
}

install_packages() {
  say "Ставлю Python, ExifTool и git..."
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv libimage-exiftool-perl git curl ca-certificates >/dev/null
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "нужен Python 3.10+, а здесь $(python3 -V). Подойдёт Ubuntu 22.04+ или Debian 12+"
}

install_app() {
  say "Скачиваю бота в $APP_DIR..."
  if [[ -d $APP_DIR/.git ]]; then
    git -C "$APP_DIR" fetch -q --depth 1 origin HEAD
    git -C "$APP_DIR" reset -q --hard FETCH_HEAD  # .env и .venv не в git, они сохранятся
  else
    git clone -q --depth 1 "$REPO_URL" "$APP_DIR"
  fi
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
  # Бот работает от отдельного пользователя без прав: он открывает файлы от кого угодно
  id "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
}

ask_token() {
  curl -sS --max-time 20 -o /dev/null "$API" || die "сервер не может достучаться до api.telegram.org"
  TOKEN=${BOT_TOKEN:-}
  local saved answer
  saved=$(env_value BOT_TOKEN)
  if [[ -z $TOKEN && -n $saved ]]; then
    TOKEN=$saved
    if has_tty; then
      answer=$(ask "Токен уже настроен. Enter — оставить, или вставь новый: ")
      TOKEN=${answer:-$saved}
    fi
  fi
  while true; do
    [[ -n $TOKEN ]] || TOKEN=$(ask "Вставь токен бота от @BotFather: ")
    TOKEN=${TOKEN//[[:space:]]/}
    if [[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && BOT_NAME=$(tg getMe 'r["result"]["username"]' 2>/dev/null); then
      say "Токен подходит, это бот @$BOT_NAME"
      return
    fi
    has_tty || die "Telegram не принял токен"
    warn "Telegram не принял токен. Скопируй его из @BotFather целиком и попробуй ещё раз."
    TOKEN=""
  done
}

ask_owner() {
  OWNER=${ALLOWED_USERS:-$(env_value ALLOWED_USERS)}
  if [[ -n $OWNER ]]; then
    say "Бот отвечает только этим ID: $OWNER"
    return
  fi
  has_tty || return 0
  # Работающая копия бота перехватывала бы сообщения, пока ищем владельца
  systemctl stop "$SERVICE" 2>/dev/null || true

  # Сообщения, пришедшие боту раньше, пропускаем: владелец — тот, кто напишет сейчас
  local last found
  if ! last=$(tg getUpdates 'max([u["update_id"] for u in r["result"]], default=-1)' 2>/dev/null); then
    warn "Не получилось прочитать сообщения бота, поэтому он будет отвечать всем"
    return
  fi
  echo
  say "Чтобы бот отвечал только тебе, отправь ему в Telegram любое сообщение."
  say "Или нажми Enter, чтобы пропустить: тогда бот будет отвечать всем."
  while true; do
    found=$(tg "getUpdates?offset=$((last + 1))&timeout=3" \
      'next((str(u["message"]["from"]["id"]) + " " + u["message"]["from"]["first_name"]
             for u in r["result"] if "from" in u.get("message", {})), "")' 2>/dev/null) || found=""
    if [[ -n $found ]]; then
      OWNER=${found%% *}
      say "Запомнил: бот будет отвечать только тебе (${found#* }, ID $OWNER)"
      return
    fi
    if read -r -t 0.1 _ < /dev/tty; then
      warn "Пропускаю: бот будет отвечать всем"
      return
    fi
  done
}

install_service() {
  (umask 077 && printf 'BOT_TOKEN=%s\nALLOWED_USERS=%s\n' "$TOKEN" "$OWNER" > "$APP_DIR/.env")
  chown "$APP_USER:" "$APP_DIR/.env"

  cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=EXIF Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5
# Бот ничего не пишет на диск, поэтому система для него только для чтения
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q "$SERVICE"
  systemctl restart "$SERVICE"

  sleep 3
  if ! systemctl is-active -q "$SERVICE"; then
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    die "бот не запустился, выше его последние логи"
  fi
}

main() {
  [[ $EUID -eq 0 ]] || die "запусти установку от root (через sudo)"
  command -v apt-get >/dev/null || die "скрипт рассчитан на Ubuntu или Debian"
  [[ -n ${BOT_TOKEN:-} || -n $(env_value BOT_TOKEN) ]] || has_tty \
    || die "нет терминала, чтобы спросить токен. Передай его так: ... | sudo BOT_TOKEN=123:ABC bash"

  install_packages
  install_app
  ask_token
  ask_owner
  install_service

  cat <<EOF

✅ Готово! Бот @$BOT_NAME работает и сам запустится после перезагрузки сервера.
Напиши ему /start и пришли фото файлом.

Полезные команды:
  journalctl -u $SERVICE -f      логи бота
  systemctl restart $SERVICE     перезапустить
  systemctl stop $SERVICE        остановить
  nano $APP_DIR/.env        токен и список ID (после правки — перезапустить)
Обновить бота: запусти команду установки ещё раз.
EOF
}

# Весь код в функциях: если загрузка через curl оборвётся, недокачанный скрипт не выполнится
main "$@"
