# Call Brief AI

`Call Brief AI` — это production-сервис для VPS, который забирает записи разговоров с FTP, SFTP и при необходимости с Яндекс Диска, делает транскрибацию через OpenAI, формирует итоговое сообщение и отправляет его в Telegram.

## Что делает сервис

1. Сканирует удаленное хранилище с интервалом `POLL_INTERVAL_SEC`.
2. Ждет `MIN_STABLE_POLLS` стабильных проходов, чтобы не брать файл, который еще догружается.
3. Скачивает аудио и нормализует его через `ffmpeg`.
4. При необходимости режет файл на части по `TARGET_PART_MAX_BYTES`.
5. Отправляет части в OpenAI на транскрибацию с diarization.
6. Собирает единый JSON разговора и сохраняет его рядом с аудио на том же источнике: FTP, SFTP или Яндекс Диск.
7. Отправляет JSON разговора в OpenAI Responses API и получает готовый текст для Telegram.
8. Публикует сообщение в Telegram от имени бота.
9. По настройке оставляет исходный файл, переносит его в архив или удаляет.

## Как устроен цикл обработки

Для файла `/recordings/call_001.mp3` сервис работает так:

1. Находит файл при очередном сканировании удаленного каталога.
2. Формирует сигнатуру файла из размера и времени модификации.
3. Если файл не менялся в течение нужного количества проходов, берет его в работу.
4. Сохраняет подробный результат в `/recordings/call_001.json`.
5. Сохраняет служебное состояние локально в `STATE_PATH`.
6. Если отправка в Telegram уже готова, но в прошлый раз упала, использует сохраненный `analysis.telegram_message` и повторяет только Telegram-шаг без новой транскрибации.

При настройках:

```dotenv
POLL_INTERVAL_SEC=60
MIN_STABLE_POLLS=2
```

новый файл обычно пойдет в обработку на втором стабильном сканировании, то есть примерно через один интервал после первого обнаружения.

Если нужна обработка сразу в том же цикле обнаружения, установите:

```dotenv
MIN_STABLE_POLLS=1
```

## Где хранится состояние

Сервис хранит данные в двух местах.

### 1. Локальное служебное состояние

Файл:

```text
/opt/call_brief_ai/shared/state.json
```

Там daemon хранит:

- `stage`
- `last_sig`
- `processed_sig`
- `last_started_at`
- `last_finished_at`
- `last_error`
- `skip_reason`

Это нужно для дедупликации, стабильных poll-проходов и повтора после ошибок.

### 2. JSON рядом с аудио на FTP/SFTP

Для файла:

```text
/recordings/call_001.mp3
```

создается:

```text
/recordings/call_001.json
```

В этом JSON лежит:

- блок `source`
- блок `transcription`
- блок `analysis`
- блок `telegram`

Если рядом с аудио уже лежит одноименный `*.json`, сервис считает запись уже обработанной и заново ее не берет.

## Как заново обработать один звонок

Чтобы принудительно перегнать конкретный файл, нужно удалить оба следа:

1. Запись о файле из `/opt/call_brief_ai/shared/state.json`
2. Одноименный `*.json` рядом с `*.mp3` на FTP/SFTP

Если удалить только `state.json`, а `*.json` рядом с аудио останется, сервис все равно пропустит файл.

## Файлы проекта

- `callbot_daemon.py` — основной daemon
- `get_telegram_chat_id.py` — helper для получения `TELEGRAM_CHAT_ID`
- `.env.example` — рабочий шаблон конфигурации
- `instruction.json` и `instructions.json` — примеры системной инструкции
- `callbot.service.example` — шаблон systemd unit для ручной установки
- `deploy/callbot.service` — unit для auto-deploy на VPS
- `deploy/remote_deploy.sh` — удаленный deploy-скрипт
- `.github/workflows/deploy.yml` — GitHub Actions workflow

## Быстрый старт на VPS

### 1. Установите системные пакеты

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git curl nano
```

### 2. Подготовьте рабочие каталоги

```bash
sudo mkdir -p /opt/call_brief_ai/shared/work
sudo mkdir -p /opt/call_brief_ai/shared/logs
sudo touch /opt/call_brief_ai/shared/.env
sudo touch /opt/call_brief_ai/shared/instructions.json
printf '{\n  "files": {}\n}\n' | sudo tee /opt/call_brief_ai/shared/state.json > /dev/null
```

### 3. Разверните код

Если без GitHub Actions:

```bash
cd /opt/call_brief_ai
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Если через auto-deploy, workflow сам создаст release и virtualenv.

### 4. Заполните `.env`

Откройте:

```bash
sudo nano /opt/call_brief_ai/shared/.env
```

И используйте шаблон из `.env.example`.

## Пример рабочей конфигурации

Ниже production-шаблон для FTPS, кодировки `cp1251`, OpenAI proxy и сканирования раз в 60 секунд.

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

# Optional: Yandex Disk source. Can work together with FTP/SFTP.
YANDEX_DISK_OAUTH_TOKEN=
YANDEX_DISK_REMOTE_ROOT=
# YANDEX_DISK_REMOTE_ROOTS=disk:/recordings/sales,disk:/recordings/support
YANDEX_DISK_ARCHIVE_DIR=
YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
YANDEX_DISK_DELETE_AFTER_SUCCESS=0
YANDEX_DISK_TIMEOUT_SEC=120

INSTRUCTIONS_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
INSTRUCTION_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
STATE_PATH=/opt/call_brief_ai/shared/state.json
WORK_ROOT=/opt/call_brief_ai/shared/work

TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_MESSAGE_THREAD_ID=
TELEGRAM_PROXY=
TELEGRAM_DROP_WEBHOOK=0

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

Для одной папки используйте `FTP_REMOTE_ROOT`. Если нужно несколько корней, задайте `FTP_REMOTE_ROOTS` через запятую, например `FTP_REMOTE_ROOTS=/recordings/sales,/recordings/support`.

Для Яндекс Диска задайте `YANDEX_DISK_OAUTH_TOKEN` и один корень через `YANDEX_DISK_REMOTE_ROOT` или несколько корней через `YANDEX_DISK_REMOTE_ROOTS`. Пути можно указывать как `/recordings`, так и в формате `disk:/recordings`. Источник Яндекс Диска можно включать одновременно с FTP/SFTP.

Если `FTP_ARCHIVE_DIR` задан, все успешно обработанные файлы будут переноситься в этот общий архив. Если `FTP_ARCHIVE_DIR` не задан и включен `FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1`, daemon будет использовать `archive` внутри каждого корня, то есть `/recordings/sales/archive`, `/recordings/support/archive` и так далее.

Для Яндекс Диска действует та же схема: если `YANDEX_DISK_ARCHIVE_DIR` задан, обработанные записи будут переноситься туда; если переменная пустая и включен `YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1`, daemon будет использовать `archive` внутри каждого заданного корня.

## Настройка источников

Сервис умеет работать в трех режимах:

1. Только FTP/FTPS/SFTP
2. Только Яндекс Диск
3. Сразу оба источника одновременно

Если используете только FTP/SFTP, оставьте `YANDEX_DISK_*` пустыми.

Если используете только Яндекс Диск, очистите `FTP_HOST`, `FTP_USER`/`FTP_USERNAME` и `FTP_PASSWORD`, а также задайте `YANDEX_DISK_*`.

Если используете оба источника, заполните оба блока. Сервис будет сканировать оба источника в каждом цикле и обрабатывать все найденные аудиозаписи.

## Что Откуда Брать

### Для FTP/SFTP

Нужно заполнить:

- `FTP_PROTOCOL` — какой протокол использует ваш сервер: `ftp` или `sftp`
- `FTP_HOST` — адрес FTP/SFTP сервера
- `FTP_PORT` — порт сервера
- `FTP_USER` или `FTP_USERNAME` — логин
- `FTP_PASSWORD` — пароль
- `FTP_REMOTE_ROOT` или `FTP_REMOTE_ROOTS` — папка или список папок, где лежат записи разговоров

Это все берется у администратора FTP/SFTP, на VPS или в настройках той системы, которая выгружает записи.

### Для Яндекс Диска

Нужно заполнить:

- `YANDEX_DISK_OAUTH_TOKEN` — OAuth токен Яндекса
- `YANDEX_DISK_REMOTE_ROOT` или `YANDEX_DISK_REMOTE_ROOTS` — папка или список папок на Яндекс Диске, куда Mango складывает записи
- `YANDEX_DISK_ARCHIVE_DIR` — необязательная папка архива

Что откуда брать:

- `YANDEX_DISK_OAUTH_TOKEN` берется в Яндекс OAuth
- `YANDEX_DISK_REMOTE_ROOT` — это точный путь к папке на Яндекс Диске, куда Mango выгружает записи
- `YANDEX_DISK_ARCHIVE_DIR` — это папка на Яндекс Диске, куда сервис будет переносить уже обработанные записи, если вы включите архивирование

Пути Яндекс Диска можно писать в двух вариантах:

- `/recordings/mango`
- `disk:/recordings/mango`

Оба варианта поддерживаются. Внутри сервиса путь будет нормализован.

## Как Настроить Яндекс Диск

### Шаг 1. Подготовьте папку на Яндекс Диске

Создайте папку, куда Mango будет выгружать записи, например:

```text
disk:/mango/records
```

Если хотите отдельный архив обработанных файлов, заранее создайте, например:

```text
disk:/mango/archive
```

### Шаг 2. Получите OAuth токен Яндекса

Что сделать:

1. Войдите в тот Яндекс-аккаунт, на чьем Диске лежит нужная папка.
2. Зарегистрируйте приложение в Yandex OAuth.
3. Получите OAuth token для этого приложения.
4. Скопируйте токен в `YANDEX_DISK_OAUTH_TOKEN`.

Официальные страницы Яндекса:

- Yandex Disk REST API: `https://yandex.com/dev/disk/rest/`
- Регистрация приложения: `https://yandex.com/dev/id/doc/en/register-client`
- Получение OAuth token: `https://yandex.com/dev/id/doc/access`

Важно:

- токен должен принадлежать аккаунту, у которого есть доступ к нужной папке на Яндекс Диске
- токен храните только в приватном `.env`, не в репозитории

### Шаг 3. Укажите корень, который сканировать

Если Mango выгружает в одну папку:

```dotenv
YANDEX_DISK_OAUTH_TOKEN=your_token_here
YANDEX_DISK_REMOTE_ROOT=disk:/mango/records
```

Если Mango выгружает в несколько папок:

```dotenv
YANDEX_DISK_OAUTH_TOKEN=your_token_here
YANDEX_DISK_REMOTE_ROOTS=disk:/mango/sales,disk:/mango/support
```

### Шаг 4. При необходимости включите архивирование

Если хотите переносить обработанные записи в архив:

```dotenv
YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1
YANDEX_DISK_ARCHIVE_DIR=disk:/mango/archive
```

Если архив не нужен:

```dotenv
YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
YANDEX_DISK_DELETE_AFTER_SUCCESS=0
```

Если хотите удалять исходные файлы после обработки:

```dotenv
YANDEX_DISK_DELETE_AFTER_SUCCESS=1
YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
```

Не включайте одновременно и перенос в архив, и удаление без понимания результата.

## Что Указывать Со Стороны Mango

Со стороны Mango нужно настроить выгрузку записей в ту папку, которую вы потом укажете у нас в `YANDEX_DISK_REMOTE_ROOT` или `YANDEX_DISK_REMOTE_ROOTS`.

Пример:

- в Mango выбрали папку выгрузки `disk:/mango/records`
- в `.env` указали `YANDEX_DISK_REMOTE_ROOT=disk:/mango/records`

То есть в нашем конфиге путь должен совпадать с фактической папкой выгрузки Mango.

Если в Mango записи попадают в разные папки для разных отделов, лучше сразу использовать `YANDEX_DISK_REMOTE_ROOTS`.

## Готовые Примеры

### Только FTP/SFTP

```dotenv
FTP_PROTOCOL=sftp
FTP_HOST=sftp.example.com
FTP_PORT=22
FTP_USER=myuser
FTP_PASSWORD=mypassword
FTP_REMOTE_ROOT=/recordings

YANDEX_DISK_OAUTH_TOKEN=
YANDEX_DISK_REMOTE_ROOT=
```

### Только Яндекс Диск

```dotenv
FTP_HOST=
FTP_USER=
FTP_PASSWORD=

YANDEX_DISK_OAUTH_TOKEN=your_token_here
YANDEX_DISK_REMOTE_ROOT=disk:/mango/records
YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1
YANDEX_DISK_ARCHIVE_DIR=disk:/mango/archive
```

### И FTP, И Яндекс Диск

```dotenv
FTP_PROTOCOL=sftp
FTP_HOST=sftp.example.com
FTP_PORT=22
FTP_USER=myuser
FTP_PASSWORD=mypassword
FTP_REMOTE_ROOT=/recordings

YANDEX_DISK_OAUTH_TOKEN=your_token_here
YANDEX_DISK_REMOTE_ROOT=disk:/mango/records
```

Если хотите переносить исходные записи в архив после успеха:

1. Создайте каталог архива на удаленном хранилище, например `/recordings/archive`
2. Включите:

```dotenv
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1
FTP_ARCHIVE_DIR=/recordings/archive
```

Если архив на FTP не нужен, оставляйте:

```dotenv
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=0
FTP_DELETE_AFTER_SUCCESS=0
```

## Как заполнить `instructions.json`

Сервис умеет читать инструкцию в двух форматах:

1. Простой текстовый файл
2. JSON-объект с одним из ключей:

- `instructions`
- `instruction`
- `prompt`
- `system_prompt`
- `system`
- `text`

Простой пример:

```json
{
  "instructions": "Сформируй короткое сообщение для Telegram на русском языке. Верни только готовый текст сообщения без префиксов и без markdown-кода."
}
```

Файл должен лежать по пути:

```text
/opt/call_brief_ai/shared/instructions.json
```

## Как получить `TELEGRAM_CHAT_ID`

1. Добавьте бота в нужную группу или канал.
2. Отправьте в этот чат любое сообщение.
3. Запустите helper:

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
/opt/call_brief_ai/current/.venv/bin/python get_telegram_chat_id.py
```

Если у бота ранее был webhook и `getUpdates` возвращает `409 Conflict`, используйте:

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
TELEGRAM_DROP_WEBHOOK=1 /opt/call_brief_ai/current/.venv/bin/python get_telegram_chat_id.py
```

## Запуск вручную

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
/opt/call_brief_ai/current/.venv/bin/python callbot_daemon.py
```

## Systemd

Для ручной установки можно использовать `callbot.service.example`.

Типовой запуск:

```bash
sudo cp callbot.service.example /etc/systemd/system/callbot.service
sudo systemctl daemon-reload
sudo systemctl enable callbot.service
sudo systemctl restart callbot.service
sudo systemctl status callbot.service --no-pager
```

Живые логи:

```bash
sudo journalctl -u callbot.service -f
```

## Auto-deploy

Проект уже настроен на deploy через GitHub Actions.

Workflow:

- собирает release archive
- загружает его на VPS
- раскладывает релиз в `/opt/call_brief_ai/releases/<commit_sha>`
- обновляет symlink `/opt/call_brief_ai/current`
- перезапускает `callbot.service`
- оставляет только последние `RELEASES_TO_KEEP` релизов

Подробный пошаговый гайд лежит в [DEPLOYMENT.md](DEPLOYMENT.md).
