# Сервер test.savkin.xyz

| Что | Где |
|---|---|
| Сайт | https://test.savkin.xyz, вход по паролю nginx (basic auth), логин `root` |
| Код | `/opt/krgen` — клон этого репозитория, пользователь `krgen`, окружение `/opt/krgen/.venv` |
| Данные | `/var/lib/krgen` (база, кэш ответов модели, PDF, снимки); обновления кода их не трогают |
| Программа | служба `krgen` (`krgen.service`), слушает 127.0.0.1:8765 |
| Веб-сервер | nginx (`nginx-krgen.conf`), сертификат Let's Encrypt через certbot, продлевается сам |
| Токен Claude | `/etc/krgen/token.env`, задаётся командой `krgen-set-token` |

Файлы в этой папке:

| Файл | Куда ставится |
|---|---|
| `krgen.service` | служба программы |
| `krgen-sync.sh`, `krgen-sync.service`, `krgen-sync.timer` | обновление из GitHub (`/usr/local/bin/krgen-sync`) |
| `krgen-set-token.sh` | сохранение токена подписки (`krgen-set-token`) |
| `nginx-krgen.conf` | сайт в nginx: пароль, прокси на 8765, загрузка файлов до 500 МБ |

## Обновление из GitHub

Таймер `krgen-sync.timer` раз в минуту запускает `krgen-sync`. Если в `main` появились новые
коммиты, скрипт забирает их, при изменении `requirements.txt` ставит зависимости и перезапускает
службу. Пока идёт обработка, перезапуск пропускается, чтобы не оборвать прогон, и скрипт пробует
снова через минуту. Доступ к GitHub — деплой-ключ только на чтение
(`/home/krgen/.ssh/github_deploy`).

Журнал синхронизации:

```bash
journalctl -u krgen-sync -n 20
```

## Токен подписки Claude

Под root на сервере:

```bash
sudo -u krgen -i claude setup-token
krgen-set-token
```

Первая команда выдаёт ссылку: открыть её, войти в Claude и вставить код обратно. В ответ придёт
токен `sk-ant-oat…`, его нужно вставить во вторую команду. Она сохранит токен и перезапустит службу.

## Полезные команды

```bash
systemctl status krgen
journalctl -u krgen -f
htpasswd /etc/nginx/krgen.htpasswd root
```

Последняя команда меняет пароль на сайт.
