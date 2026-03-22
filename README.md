# Call Brief AI

Python-сервис для VPS, который:

1. Сканирует FTP или SFTP каждые 5 минут.
2. Забирает новые аудиофайлы разговоров.
3. Нормализует аудио через `ffmpeg`.
4. Если файл крупнее 4 МБ, режет его на MP3-части не больше 4 МБ.
5. Отправляет каждую часть в OpenAI на транскрибацию с диаризацией.
6. Объединяет части в единый JSON разговора.
7. Передает JSON разговора и `instructions.json` в OpenAI Responses API.
8. Получает готовое сообщение для Telegram и отправляет его в группу от имени бота.
9. Сохраняет `.json` рядом с исходным аудиофайлом на удаленном хранилище.
10. Может после успешной обработки перенести исходный файл в архив или удалить его.

## Файлы проекта

- `callbot_daemon.py` - основной daemon.
- `get_telegram_chat_id.py` - helper для получения `TELEGRAM_CHAT_ID`.
- `.env.example` - шаблон конфигурации.
- `instructions.json` - инструкция для шага анализа.
- `callbot.service.example` - шаблон systemd unit.

## Что изменилось по новому запросу

- Добавлена поддержка `SFTP` через `paramiko`.
- Добавлено разбиение MP3 по порогу 4 МБ.
- Добавлен порог перехода к анализу не только по словам, но и по длительности.
- В итоговый JSON добавлены метаданные из имени файла и объединенные сегменты с корректным сдвигом таймингов.
- Сервис может архивировать или удалять исходный файл после успешной обработки.
- Поддерживается новый формат `instructions.json`, который может быть как строкой, так и большим JSON-документом.

## Быстрый старт на VPS

### 1. Установите системные зависимости

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
```

`ffprobe` обычно ставится вместе с `ffmpeg` и тоже нужен сервису.

### 2. Подготовьте проект

```bash
cd /opt
sudo mkdir -p call_brief_ai
sudo chown "$USER":"$USER" call_brief_ai
cd /opt/call_brief_ai

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3. Заполните `.env`

```bash
cp .env.example .env
```

Минимально нужно заполнить:

- `OPENAI_API_KEY`
- `FTP_PROTOCOL`
- `FTP_HOST`
- `FTP_PORT`
- `FTP_USER` или `FTP_USERNAME`
- `FTP_PASSWORD`
- `FTP_REMOTE_ROOT`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Важные настройки:

- `OPENAI_BASE_URL` позволяет отправлять OpenAI-запросы в совместимый gateway вместо стандартного API.
- `OPENAI_PROXY` задает отдельный HTTP/HTTPS proxy только для OpenAI.
- `OPENAI_TIMEOUT_SEC=600` задает общий timeout OpenAI-запроса.
- `OPENAI_CONNECT_TIMEOUT_SEC=30` увеличивает timeout на подключение к proxy/OpenAI, что особенно полезно при `OPENAI_PROXY`.
- `OPENAI_REQUEST_ATTEMPTS=2`, `OPENAI_RETRY_DELAY_SEC=2` и `OPENAI_RETRY_BACKOFF=2` управляют повтором OpenAI-запросов при временных сетевых сбоях.
- `OPENAI_PROXY_FAILURE_COOLDOWN_SEC=300` временно останавливает попытки через умерший `OPENAI_PROXY`, чтобы сервис не тратил весь цикл на одинаковые таймауты.
- `OPENAI_PROXY_DIRECT_FALLBACK=1` разрешает временно уйти на прямой маршрут без proxy, но включайте это только если сам VPS может ходить в OpenAI напрямую и это допустимо по региону.
- `TELEGRAM_PROXY` задает отдельный HTTP/HTTPS proxy только для Telegram.
- `FTP_PROTOCOL=sftp` для SFTP, `ftp` для FTP/FTPS.
- `FTP_USE_TLS=1` актуален только для FTP/FTPS.
- `FTP_ENCODING` задает кодировку FTP-листинга; для старых серверов с кириллицей часто нужен `cp1251`.
- `FTP_TIMEOUT_SEC=60` задает timeout одного подключения.
- `FTP_CONNECT_ATTEMPTS=2` и `FTP_RETRY_DELAY_SEC=5` добавляют повтор подключения, если хранилище временно не отвечает.
- `SPLIT_THRESHOLD_BYTES=4194304` и `TARGET_PART_MAX_BYTES=4194304` задают разбиение по 4 МБ.
- `MIN_DURATION_MIN=0.5` не пускает в LLM-анализ слишком короткие звонки.
- `MIN_DIALOGUE_WORDS=30` отсеивает почти пустые диалоги.
- `FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1` переносит исходный файл в архив после успеха.
- `FTP_DELETE_AFTER_SUCCESS=1` удаляет исходный файл после успеха.

Не включайте одновременно архивирование и удаление, если не уверены в сценарии. Обычно достаточно `FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1`.

### Прокси

Если нужно обойти региональные ограничения OpenAI, можно использовать отдельный proxy именно для OpenAI:

```dotenv
OPENAI_PROXY=http://login:password@PROXY_HOST:8888
```

Для Telegram можно задать свой proxy:

```dotenv
TELEGRAM_PROXY=http://login:password@PROXY_HOST:8888
```

Если вы хотите проксировать все HTTP(S)-запросы процесса целиком, вместо отдельных переменных можно использовать стандартные:

```dotenv
HTTP_PROXY=http://login:password@PROXY_HOST:8888
HTTPS_PROXY=http://login:password@PROXY_HOST:8888
NO_PROXY=127.0.0.1,localhost
```

Важно: proxy на том же VPS, где уже возникает `403 unsupported_country_region_territory`, проблему не решит. Выходной IP proxy должен быть в поддерживаемой OpenAI стране.

При `OPENAI_PROXY` сервис использует отдельный `httpx`-клиент без чтения глобальных `HTTP_PROXY` / `HTTPS_PROXY`, чтобы не смешивать маршруты. Если в диагностике видны `CONNECT tunnel failed` или `httpx.ConnectTimeout`, сначала проверьте сам proxy через `curl -x ... https://api.openai.com/v1/models ...`: это обычно означает проблему в proxy/firewall, а не в API-ключе OpenAI.

Начиная с текущей версии daemon сам делает управляемые повторы OpenAI-запросов и, если `OPENAI_PROXY` перестал отвечать, ставит proxy-маршрут на паузу на `OPENAI_PROXY_FAILURE_COOLDOWN_SEC`. Перед скачиванием новых аудио он также делает легкую проверку OpenAI-маршрута, чтобы не тратить цикл на загрузку и нормализацию файлов при заведомо мертвом proxy.

### 4. Подставьте актуальную инструкцию

Сервис по умолчанию ищет:

- `INSTRUCTIONS_JSON_PATH`
- если ее нет, то `INSTRUCTION_JSON_PATH`

Рекомендуемый путь:

```bash
/opt/call_brief_ai/instructions.json
```

### 5. Узнайте `TELEGRAM_CHAT_ID`

Добавьте бота в нужную группу, отправьте туда любое сообщение и выполните:

```bash
source .venv/bin/activate
python get_telegram_chat_id.py
```

Если у бота уже был webhook и `getUpdates` пустой:

```bash
TELEGRAM_DROP_WEBHOOK=1 python get_telegram_chat_id.py
```

### 6. Запуск

```bash
source .venv/bin/activate
python callbot_daemon.py
```

## Как работает разбиение

- Аудио сначала приводится к mono MP3.
- Если нормализованный файл меньше или равен 4 МБ, он уходит в OpenAI как есть.
- Если файл больше 4 МБ, сервис режет его на части через `ffmpeg`.
- Каждая часть должна быть не больше `TARGET_PART_MAX_BYTES`.
- Если часть все равно превышает лимит OpenAI, сервис завершает обработку ошибкой.

## Что попадает в JSON

Рядом с `call_001.mp3` создается `call_001.json`, где есть:

- `source` - путь, размер, время модификации и метаданные из имени файла.
- `transcription` - полный текст, диалог по спикерам, сегменты, длительность, usage и список частей.
- `analysis` - результат шага анализа или причина пропуска.
- `telegram` - информация об отправке в Telegram.

## Systemd

Шаблон лежит в `callbot.service.example`.

## Автодеплой

Для автодеплоя на VPS при каждом push в git теперь добавлены:

- `.github/workflows/deploy.yml`
- `deploy/remote_deploy.sh`
- `deploy/callbot.service`

Полная пошаговая инструкция лежит в `DEPLOYMENT.md`.

Обычный сценарий:

```bash
sudo cp callbot.service.example /etc/systemd/system/callbot.service
sudo systemctl daemon-reload
sudo systemctl enable callbot.service
sudo systemctl start callbot.service
sudo systemctl status callbot.service
```

## Замечания

- Для FTP сервис сначала пробует `MLSD`, а если сервер его не поддерживает, переключается на `NLST`.
- Для SFTP используется рекурсивный обход через `paramiko`.
- При временном timeout подключения сервис повторит соединение и, если хранилище недоступно, просто пропустит текущий цикл сканирования.
- Если на удаленном хранилище уже лежит одноименный `.json`, сервис считает запись обработанной и повторно ее не берет.
- Все секреты храните только в `.env`.
