# Auto-Deploy And VPS Setup

Этот документ описывает полный production-сценарий для `Call Brief AI`: подготовка VPS, настройка GitHub Actions, заполнение `.env`, первый deploy, обновления, rollback и структура каталогов.

## Архитектура деплоя

После каждого push в `main` или `master` workflow:

1. Проверяет Python-файлы.
2. Собирает архив релиза.
3. Загружает архив на VPS по SSH.
4. Распаковывает релиз в `/opt/call_brief_ai/releases/<commit_sha>`.
5. Создает или обновляет virtualenv внутри релиза.
6. Обновляет symlink `/opt/call_brief_ai/current`.
7. Перезапускает `callbot.service`.
8. Оставляет только последние `RELEASES_TO_KEEP` релизов.

## Структура каталогов на VPS

После первого deploy структура обычно выглядит так:

```text
/opt/call_brief_ai/
├── current -> /opt/call_brief_ai/releases/<commit_sha>
├── releases/
│   ├── <commit_sha_1>/
│   ├── <commit_sha_2>/
│   └── <commit_sha_3>/
└── shared/
    ├── .env
    ├── instructions.json
    ├── state.json
    ├── logs/
    └── work/
```

`shared` не привязан к конкретному релизу и переживает деплои.

## Что нужно заранее

- GitHub-репозиторий
- VPS с Ubuntu или Debian
- SSH-доступ на VPS
- OpenAI API key
- Telegram bot token
- FTP или SFTP доступ к хранилищу записей
- PostgreSQL доступ для сохранения транскрибаций и анализа

## 1. Подготовьте VPS

Под root:

```bash
apt-get update
apt-get upgrade -y
apt-get install -y python3 python3-venv python3-pip ffmpeg git rsync curl nano
```

Создайте системного пользователя и каталоги:

```bash
id -u callbot >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/call_brief_ai --shell /bin/bash callbot
mkdir -p /opt/call_brief_ai/releases
mkdir -p /opt/call_brief_ai/shared/work
mkdir -p /opt/call_brief_ai/shared/logs
printf '{\n  "files": {}\n}\n' > /opt/call_brief_ai/shared/state.json
touch /opt/call_brief_ai/shared/.env
touch /opt/call_brief_ai/shared/instructions.json
chown -R callbot:callbot /opt/call_brief_ai
chmod 600 /opt/call_brief_ai/shared/.env
chmod 644 /opt/call_brief_ai/shared/instructions.json
```

## 2. Заполните `.env` на VPS

Откройте:

```bash
nano /opt/call_brief_ai/shared/.env
```

Используйте рабочий шаблон:

```dotenv
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=
OPENAI_PROXY=http://user:password@proxy-host:8889
OPENAI_TIMEOUT_SEC=600
OPENAI_CONNECT_TIMEOUT_SEC=30
OPENAI_ROUTE_PROBE_TIMEOUT_SEC=15
OPENAI_ROUTE_PROBE_CONNECT_TIMEOUT_SEC=5
OPENAI_REQUEST_ATTEMPTS=2
OPENAI_RETRY_DELAY_SEC=2
OPENAI_RETRY_BACKOFF=2
OPENAI_PROXY_FAILURE_COOLDOWN_SEC=300
OPENAI_PROXY_DIRECT_FALLBACK=0
OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe-diarize
OPENAI_TRANSCRIBE_LANGUAGE=ru
OPENAI_CHUNKING_STRATEGY=auto
OPENAI_ANALYSIS_MODEL=gpt-5-mini
OPENAI_ANALYSIS_REASONING_EFFORT=low
OPENAI_ANALYSIS_STORE=0
OPENAI_ANALYSIS_MAX_OUTPUT_TOKENS=1800

FTP_PROTOCOL=ftp
FTP_HOST=ftp.example.com
FTP_PORT=21
FTP_USER=ftpuser
FTP_USERNAME=ftpuser
FTP_PASSWORD=change-me
FTP_REMOTE_ROOT=/recordings
# Если нужно сканировать несколько папок:
# FTP_REMOTE_ROOTS=/recordings/sales,/recordings/support
# Если нужен отдельный archive внутри каждой папки, оставьте FTP_ARCHIVE_DIR пустым
FTP_ARCHIVE_DIR=/recordings/archive
FTP_DELETE_AFTER_SUCCESS=0
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
FTP_USE_TLS=1
FTP_ENCODING=cp1251
FTP_ENCODING_FALLBACKS=cp1251,cp866,latin-1
FTP_TIMEOUT_SEC=120
FTP_CONNECT_ATTEMPTS=2
FTP_RETRY_DELAY_SEC=5

INSTRUCTIONS_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
INSTRUCTION_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
INSTRUCTION_PROMPT_MODE=raw
STATE_PATH=/opt/call_brief_ai/shared/state.json
WORK_ROOT=/opt/call_brief_ai/shared/work

POSTGRES_DATABASE_URL=postgresql+asyncpg://callbot_user:change-me@127.0.0.1:5432/call_brief_ai
DB_ENABLED=1
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=call_brief_ai
DB_ADMIN_DB=postgres
DB_USER=callbot_user
DB_PASSWORD=change-me
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT_SEC=10

TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_MESSAGE_THREAD_ID=
TELEGRAM_PROXY=
TELEGRAM_DROP_WEBHOOK=0

# Optional: separate MANGO webhook service for missed inbound calls
MANGO_ENABLED=0
MANGO_HTTP_HOST=127.0.0.1
MANGO_HTTP_PORT=8081
MANGO_WEBHOOK_PATH=/events/summary
MANGO_API_KEY=
MANGO_API_SALT=
MANGO_ALLOWED_IPS=
MANGO_DISPLAY_TIMEZONE=Asia/Novosibirsk
MANGO_RETRY_ENABLED=1
MANGO_RETRY_INTERVAL_SEC=60
MANGO_RETRY_BATCH_SIZE=20
MANGO_LOG_PAYLOADS=0

POLL_INTERVAL_SEC=60
MIN_STABLE_POLLS=2
MIN_AUDIO_BYTES=102400
MIN_DURATION_MIN=0.5
MIN_DIALOGUE_WORDS=30
SPLIT_THRESHOLD_BYTES=4194304
TARGET_PART_MAX_BYTES=4194304
PART_EXPORT_BITRATE=64k
PART_EXPORT_FRAME_RATE=16000
PART_EXPORT_CHANNELS=1
MAX_API_FILE_SIZE_BYTES=26214400
LOG_LEVEL=INFO
```

Для обычного сценария достаточно `FTP_REMOTE_ROOT`. Если нужно сканировать несколько удаленных каталогов, используйте `FTP_REMOTE_ROOTS` и перечислите пути через запятую.

Если `FTP_ARCHIVE_DIR` указан явно, все файлы будут переноситься в общий архив. Если параметр не задан и включен `FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1`, сервис будет использовать каталог `archive` внутри каждого корня.

### Если нужен архив исходных файлов

Создайте каталог архива на удаленном FTP/SFTP-хранилище, например:

```text
/recordings/archive
```

После этого включите:

```dotenv
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1
FTP_ARCHIVE_DIR=/recordings/archive
```

Если архив не нужен, оставляйте:

```dotenv
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
FTP_DELETE_AFTER_SUCCESS=0
```

Блок `MANGO_*` нужен только для отдельного webhook-сервиса пропущенных звонков. Его можно оставить выключенным, пока этот контур не выкатывается в production.

## 3. Подготовьте `instructions.json`

Откройте:

```bash
nano /opt/call_brief_ai/shared/instructions.json
```

Минимальный пример:

```json
{
  "instructions": "Сформируй готовое сообщение для Telegram на русском языке. Верни только текст сообщения без пояснений и без markdown-кода."
}
```

Если вы храните инструкцию как подробный JSON-профиль, а не как одно поле `instructions`, можно вручную переключить режим сборки prompt:

```dotenv
INSTRUCTION_PROMPT_MODE=raw
```

Режимы:

- `raw` — поведение по умолчанию. Если в JSON есть ключ `instructions` / `instruction` / `prompt` / `system_prompt` / `system` / `text`, сервис берет его. Если таких ключей нет, в модель отправляется весь JSON целиком как текст.
- `rendered` — сервис собирает prompt из структурированных секций JSON (`analyzer_role`, `main_goal`, `call_type_routing`, `evaluation_model`, `output_requirements` и др.) и отправляет уже готовый текст инструкции.

Переключение на VPS:

```bash
nano /opt/call_brief_ai/shared/.env
sudo systemctl restart callbot.service
sudo journalctl -u callbot.service -n 20 --no-pager
```

При старте сервис пишет в лог, какой режим инструкции активен.

## 4. Подготовьте GitHub-репозиторий

Локально:

```powershell
cd C:\Users\ilel\Desktop\call_brief_ai
git init -b main
git add .
git commit -m "Initial production setup"
git remote add origin https://github.com/YOUR_GITHUB_LOGIN/call_brief_ai.git
git push -u origin main
```

Если используете SSH для GitHub:

```powershell
ssh-keygen -t ed25519 -C "your_email@example.com"
```

## 5. Подготовьте SSH-ключ для GitHub Actions

На локальной машине:

```powershell
mkdir $env:USERPROFILE\.ssh -Force
ssh-keygen -t ed25519 -C "github-actions-vps-deploy" -f $env:USERPROFILE\.ssh\github_actions_vps
```

Содержимое публичного ключа:

```powershell
Get-Content $env:USERPROFILE\.ssh\github_actions_vps.pub
```

Добавьте его на VPS:

```bash
mkdir -p /root/.ssh
chmod 700 /root/.ssh
nano /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

Проверьте вход:

```powershell
ssh -i $env:USERPROFILE\.ssh\github_actions_vps root@YOUR_VPS_IP
```

## 6. Добавьте GitHub Secrets

В репозитории откройте:

`Settings -> Secrets and variables -> Actions`

Создайте:

- `VPS_HOST`
- `VPS_PORT`
- `VPS_USER`
- `VPS_SSH_KEY`

Для `VPS_SSH_KEY` возьмите полный приватный ключ:

```powershell
Get-Content $env:USERPROFILE\.ssh\github_actions_vps -Raw
```

## 7. Первый deploy

Сделайте любой commit и push:

```powershell
cd C:\Users\ilel\Desktop\call_brief_ai
git add .
git commit -m "Setup production deploy"
git push
```

После этого откройте GitHub Actions и дождитесь успешного workflow `Deploy To VPS`.

## 8. Что делает workflow

Workflow из `.github/workflows/deploy.yml`:

1. Устанавливает Python 3.12 на runner.
2. Ставит зависимости из `requirements.txt`.
3. Проверяет `callbot_daemon.py` и `get_telegram_chat_id.py` через `py_compile`.
4. Собирает release archive без `.venv`, `.github`, `.env`, `work`, `state.json`.
5. Копирует archive, `deploy/remote_deploy.sh` и `deploy/callbot.service` на VPS.
6. Выполняет deploy-скрипт на VPS.

Важно: production-сервис читает `.env` и `instructions.json` из каталога `shared`. Если `/opt/call_brief_ai/shared/instructions.json` уже существует и не пустой, deploy не заменяет его автоматически файлом из нового релиза. То есть push в `main` обновляет код и перезапускает сервис, но не обязательно обновляет боевой prompt.

Дополнительно:

- локальная реализация `mango_webhook_server.py` уже есть в репозитории
- `deploy/mango-webhook.service` и `deploy/mango_webhook.nginx.conf.example` тоже уже подготовлены
- текущий GitHub Actions workflow пока не устанавливает и не перезапускает `mango-webhook.service`
- при включении MANGO в production этот шаг нужно делать отдельно или расширить workflow

## 9. Сколько релизов хранится

По умолчанию workflow хранит 3 последних релиза.

Это настраивается в:

```text
.github/workflows/deploy.yml
```

Переменная:

```yaml
RELEASES_TO_KEEP: 3
```

Если хотите хранить, например, 5 релизов, поменяйте значение на `5`.

## 10. Проверка после deploy

Статус сервиса:

```bash
systemctl status callbot.service --no-pager
```

Последние логи:

```bash
journalctl -u callbot.service -n 200 --no-pager
```

Живые логи:

```bash
journalctl -u callbot.service -f
```

Текущий релиз:

```bash
ls -lah /opt/call_brief_ai/current
```

Список релизов:

```bash
ls -lah /opt/call_brief_ai/releases
```

Текущий `.env`:

```bash
sed -n '1,200p' /opt/call_brief_ai/shared/.env
```

Если дополнительно включаете MANGO webhook, проверьте еще:

```bash
systemctl status mango-webhook.service --no-pager
journalctl -u mango-webhook.service -n 200 --no-pager
curl -i http://127.0.0.1:8081/healthz
```

## 11. Обновление `.env`

Если меняете только конфигурацию:

```bash
nano /opt/call_brief_ai/shared/.env
systemctl restart callbot.service
```

## 12. Rollback

Посмотреть релизы:

```bash
ls -1 /opt/call_brief_ai/releases
```

Переключить `current` на нужный релиз:

```bash
ln -sfn /opt/call_brief_ai/releases/PUT_RELEASE_ID_HERE /opt/call_brief_ai/current
chown -h callbot:callbot /opt/call_brief_ai/current
systemctl restart callbot.service
```

Проверить:

```bash
systemctl status callbot.service --no-pager
```

## 13. Где хранится состояние обработки

Сервис хранит состояние в трех местах:

- `/opt/call_brief_ai/shared/state.json`
- `*.json` рядом с аудио на FTP/SFTP
- PostgreSQL: таблицы `ai_cell_trans`, `ai_cell_analisys` и `ai_cell_audio_blob`

Удаление только `/opt/call_brief_ai/shared/state.json` не запускает повторную обработку, если рядом с аудио уже лежит одноименный `*.json`.

Чтобы заново обработать один звонок, удалите оба следа:

1. запись о файле из `state.json`
2. одноименный `*.json` рядом с исходным `*.mp3`

PostgreSQL очищать необязательно: при повторной обработке daemon обновит существующие строки по `storage_backend + source_path_audio` и по `id_cell_trans`.

Важно: теперь daemon сохраняет и сам бинарный `*.mp3` в PostgreSQL, в отдельной таблице `ai_cell_audio_blob`. JSON-данные обработки и текст для бота по-прежнему лежат отдельно в `ai_cell_trans` и `ai_cell_analisys`.

## 14. Как получить `TELEGRAM_CHAT_ID`

Откройте:

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
/opt/call_brief_ai/current/.venv/bin/python get_telegram_chat_id.py
```

Если `getUpdates` возвращает `409 Conflict`, удалите webhook:

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
TELEGRAM_DROP_WEBHOOK=1 /opt/call_brief_ai/current/.venv/bin/python get_telegram_chat_id.py
```

## 15. Ручной запуск daemon

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
/opt/call_brief_ai/current/.venv/bin/python callbot_daemon.py
```

## 16. Ручное управление сервисом

Перезапуск:

```bash
systemctl restart callbot.service
```

Остановка:

```bash
systemctl stop callbot.service
```

Запуск:

```bash
systemctl start callbot.service
```

### Отдельный MANGO webhook-сервис

Если включаете webhook пропущенных звонков из MANGO, рядом с основным daemon нужен второй systemd unit:

```bash
cp /opt/call_brief_ai/current/deploy/mango-webhook.service /etc/systemd/system/mango-webhook.service
systemctl daemon-reload
systemctl enable mango-webhook.service
systemctl restart mango-webhook.service
systemctl status mango-webhook.service --no-pager
```

Для nginx используйте шаблон:

```text
/opt/call_brief_ai/current/deploy/mango_webhook.nginx.conf.example
```

Перед включением в production нужно обязательно:

- заполнить `MANGO_API_KEY` и `MANGO_API_SALT` в `/opt/call_brief_ai/shared/.env`
- настроить allowlist IP MANGO
- опубликовать HTTPS endpoint для `POST /events/summary`
- только после этого регистрировать webhook URL в кабинете MANGO
