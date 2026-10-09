# Сервер test.savkin.xyz

| Что | Где |
|---|---|
| Сайт | https://test.savkin.xyz (вход: `krgen` / пароль из nginx) |
| Код | `/opt/krgen` — клон этого репозитория, пользователь `krgen` |
| Данные (база, кэш ответов модели, PDF, снимки) | `/var/lib/krgen` — вне репозитория, обновления кода их не трогают |
| Программа | служба `krgen` (`krgen.service`), слушает 127.0.0.1:8765 |
| Веб-сервер | nginx (`nginx-krgen.conf`) + сертификат Let's Encrypt (certbot, продлевается сам) |
| Токен Claude | `/etc/krgen/token.env`, задаётся командой `krgen-set-token` |

## Обновление из GitHub

Таймер `krgen-sync.timer` раз в минуту запускает `krgen-sync`. Если в `main` есть новые
коммиты, скрипт забирает их, при изменении `requirements.txt` ставит зависимости и
перезапускает программу. Пока идёт обработка, перезапуск откладывается, чтобы не оборвать
прогон. Доступ к GitHub — деплой-ключ только на чтение (`/home/krgen/.ssh/github_deploy`).

Журнал синхронизации:
```
journalctl -u krgen-sync -n 20
```

## Токен подписки Claude

Под root на сервере:
```
sudo -u krgen -i claude setup-token
krgen-set-token
```
Первая команда выдаёт ссылку: откройте её, войдите в Claude, вставьте код обратно. В ответ
придёт токен `sk-ant-oat…`; его вставляют во вторую команду.

## Полезное

```
systemctl status krgen
journalctl -u krgen -f
htpasswd /etc/nginx/krgen.htpasswd krgen
```
Последняя команда меняет пароль на сайт.
