#!/bin/bash
# Сохраняет токен подписки Claude для программы и перезапускает её.
# Токен получают командой:  sudo -u krgen -i claude setup-token
set -euo pipefail
read -rsp "Вставьте токен Claude (sk-ant-oat...): " TOKEN
echo
[ -n "$TOKEN" ] || { echo "Пустой токен — ничего не изменено"; exit 1; }
mkdir -p /etc/krgen
umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" > /etc/krgen/token.env
chmod 600 /etc/krgen/token.env
systemctl restart krgen
echo "Токен сохранён, программа перезапущена."
