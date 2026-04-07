# Реализация MANGO webhook для уведомлений о пропущенных звонках

## Текущий статус

На текущий момент в репозитории уже есть локальная реализация этого контура:

- `mango_webhook_server.py`
- `test_mango_webhook_server.py`
- `deploy/mango-webhook.service`
- `deploy/mango_webhook.nginx.conf.example`
- блок `MANGO_*` в `.env.example`

Что уже реализовано локально:

- отдельный `ThreadingHTTPServer` для `POST /events/summary`
- проверка `sign` по формуле `sha256(api_key + json + api_salt)`
- allowlist IP через `MANGO_ALLOWED_IPS`
- запись пропущенных входящих звонков в таблицу `missed_calls`
- защита от дублей по `entry_id`
- отправка сообщения в Telegram
- сохранение `telegram_message_id`
- пометка failed state при ошибке Telegram
- background retry worker для повторной отправки
- unit/integration-like тесты и локальный HTTP smoke test

Что еще не сделано:

- production-выкладка на VPS
- включение отдельного `mango-webhook.service`
- nginx/HTTPS-публикация webhook endpoint на бою
- заполнение реальных `MANGO_API_KEY`, `MANGO_API_SALT` и allowlist IP
- проверка на реальном payload от MANGO в production

## Цель

Доработать проект так, чтобы он принимал webhook `POST /events/summary` от MANGO OFFICE и отправлял уведомление в Telegram-группу только по пропущенным входящим звонкам.

Условие отправки:

- `call_direction = 1`
- `entry_result = 0`

Одновременно нужно сохранять событие в PostgreSQL, не допускать дублей по `entry_id`, логировать ошибки и оставлять запись в БД даже при временной недоступности Telegram.

## Что уже есть в проекте

В текущем репозитории уже есть:

- рабочая отправка сообщений в Telegram;
- подключение к PostgreSQL и auto-create таблиц;
- `systemd`-запуск production daemon;
- общий `.env`-подход и логирование.

Чего сейчас еще нет именно в production:

- опубликованного HTTPS endpoint под MANGO;
- включенного `mango-webhook.service` на VPS;
- подтвержденного боевого allowlist IP от MANGO;
- верификации на реальном production payload.

## Степень готовности проекта относительно ТЗ

### Уже готово и может быть переиспользовано

- PostgreSQL уже используется в production.
- Telegram bot уже умеет отправлять сообщения в нужный чат.
- В проекте уже есть `.env`-конфигурация, логирование и `systemd`.
- В проекте уже есть стиль написания production-кода без внешнего framework.

### Нужно реализовать с нуля

- production deployment отдельного HTTP listener;
- публикацию endpoint через HTTPS;
- боевую настройку allowlist IP;
- финальную сверку парсера на реальном MANGO payload;
- rollout и post-deploy проверку.

### Вне кода, но обязательно для работоспособности

- включённый API-коннектор в кабинете MANGO;
- публичный HTTPS endpoint;
- whitelist IP MANGO на сервере;
- домен или поддомен под webhook;
- корректные `MANGO_API_KEY` и `MANGO_API_SALT`.

## Неподтверждённые предположения

Ниже в документе есть решения, которые подходят для реализации, но их желательно сверить на первом же реальном payload от MANGO:

- точный `Content-Type` запроса;
- точный формат поля `create_time` и `end_time`;
- форма поля `to`: объект или массив;
- возможные дополнительные обязательные поля в summary payload.

До получения реального sample payload считаем рабочей гипотезой:

- тело приходит как `application/x-www-form-urlencoded`;
- внутри формы есть `sign` и `json`;
- `json` содержит все нужные нам поля.

Как только появится реальный пример webhook от MANGO, его нужно сохранить в документации и обновить парсер при необходимости.

## Рекомендуемое архитектурное решение

### Базовое решение

Не встраивать MANGO webhook в текущий polling loop из `callbot_daemon.py`.

Рекомендуемая схема:

1. Оставить текущий `callbot_daemon.py` как есть для FTP/SFTP/Yandex Disk и OpenAI-анализа записей.
2. Добавить отдельный сервис `mango_webhook_server.py`.
3. Поднять его локально на `127.0.0.1:<порт>`.
4. Снаружи публиковать endpoint через Nginx по HTTPS.
5. На уровне Nginx и/или firewall ограничить доступ IP-адресами MANGO.

### Почему отдельный сервис лучше

- не ломаем текущий production-поток обработки аудио;
- проще деплой и rollback;
- webhook не зависит от OpenAI, FTP и длинного polling-цикла;
- легче тестировать дедупликацию, подпись и поведение при ошибках Telegram.

## Изменения по файлам

### Новые файлы

- `mango_webhook_server.py`
- `test_mango_webhook_server.py`
- `deploy/mango-webhook.service`
- `deploy/mango_webhook.nginx.conf.example`

### Ответственность новых файлов

#### `mango_webhook_server.py`

Отдельный процесс, который:

- читает MANGO/Telegram/PostgreSQL настройки из `.env`;
- поднимает локальный HTTP server;
- принимает `POST /events/summary`;
- проверяет подпись и IP;
- сохраняет missed call;
- шлёт сообщение в Telegram;
- запускает retry worker.

#### `test_mango_webhook_server.py`

Набор unit/integration-like тестов для:

- подписи;
- разбора form body;
- дедупликации;
- записи в БД;
- отправки в Telegram;
- fallback-логики по полям `to`.

#### `deploy/mango-webhook.service`

Отдельный `systemd` unit под webhook-сервис.

#### `deploy/mango_webhook.nginx.conf.example`

Пример reverse proxy c:

- HTTPS;
- allowlist IP;
- `proxy_pass` на localhost;
- базовыми timeout-ами.

### Обновляемые файлы

- `.env.example`
- `README.md`
- `DEPLOYMENT.md`

### Что лучше не переиспользовать напрямую из текущего кода

#### Не использовать `Config.from_env()` из `callbot_daemon.py`

Причина:

- текущий `Config` заточен под FTP/Yandex/OpenAI pipeline;
- у него есть обязательные поля, которые для webhook-сервиса не нужны;
- прямое переиспользование даст лишнюю связность и риск падения сервиса из-за неотносящихся к MANGO env-переменных.

Решение:

- создать отдельный `MangoConfig`.

#### Не использовать `DatabaseStore` как есть

Причина:

- текущий store заточен под таблицы транскрибации и анализа;
- webhook-сервису нужна своя таблица и своя логика записи статусов Telegram.

Решение:

- создать отдельный `MangoStore`;
- при желании позже вынести общие postgres helper-функции в общий модуль.

## Новый runtime flow

### Поток webhook

1. Nginx принимает HTTPS-запрос от MANGO.
2. Nginx проверяет IP-источник по allowlist.
3. Nginx проксирует запрос на `127.0.0.1:MANGO_HTTP_PORT`.
4. `mango_webhook_server.py` принимает `POST /events/summary`.
5. Сервер извлекает `sign` и исходную строку `json`.
6. Сервер проверяет подпись:

```text
sha256(vpbx_api_key + json + vpbx_api_salt)
```

7. После проверки подписи парсится JSON payload.
8. Если звонок не подходит под фильтр missed inbound, сервер просто логирует skip и отвечает `200`.
9. Если подходит:
   сохраняет запись в `missed_calls` через `INSERT ... ON CONFLICT DO NOTHING`.
10. Если `entry_id` уже существует:
    ничего не шлём повторно, отвечаем `200`.
11. Если запись новая:
    пытаемся отправить сообщение в Telegram.
12. Если Telegram отправился:
    обновляем `telegram_message_id`, `telegram_sent`, `telegram_sent_at`.
13. Если Telegram не отправился:
    запись в БД остаётся, ставим статус pending retry.
14. Возвращаем `200 OK`.

### Подробный жизненный цикл одной записи

#### Сценарий 1. Новый missed inbound, Telegram успешен

1. Прилетает webhook.
2. Подпись валидна.
3. Событие проходит фильтр.
4. В `missed_calls` создаётся запись.
5. Выполняется отправка в Telegram.
6. В той же записи обновляются:
   - `telegram_message_id`
   - `telegram_sent = TRUE`
   - `telegram_sent_at`
   - `telegram_error = NULL`

#### Сценарий 2. Новый missed inbound, Telegram упал

1. Прилетает webhook.
2. Подпись валидна.
3. Событие проходит фильтр.
4. В `missed_calls` создаётся запись.
5. Отправка в Telegram падает.
6. В записи фиксируются:
   - `telegram_sent = FALSE`
   - `telegram_error`
   - `telegram_retry_count = 1`
   - `last_telegram_attempt_at`
7. Retry worker позже попробует отправить повторно.

#### Сценарий 3. Повторный webhook по тому же `entry_id`

1. Прилетает webhook.
2. Подпись валидна.
3. Событие проходит фильтр.
4. `INSERT ... ON CONFLICT DO NOTHING` не вставляет новую запись.
5. Telegram не отправляется.
6. Возвращается `200`.

#### Сценарий 4. Webhook не подходит под фильтр

1. Прилетает webhook.
2. Подпись валидна.
3. `call_direction != 1` или `entry_result != 0`.
4. Событие логируется как skipped.
5. В БД ничего не создаётся.
6. Возвращается `200`.

### Важная деталь по подписи

Подпись нужно считать по исходной строке `json` из запроса, а не по `json.dumps(parsed_payload)`.

Иначе подпись может не совпасть из-за:

- порядка ключей;
- пробелов;
- экранирования;
- отличий сериализации.

## Формат входящего payload

### Формат HTTP body

В первом релизе ориентируемся на формат `application/x-www-form-urlencoded`, где в теле приходят поля:

- `sign`
- `json`

То есть приложение должно:

1. прочитать raw body как bytes;
2. распарсить form-urlencoded;
3. достать `json` как исходную строку;
4. достать `sign`;
5. проверить подпись;
6. только потом вызвать `json.loads(json_str)`.

Для локальных тестов можно при желании добавить поддержку `application/json`, но это не обязательная часть первого релиза.

### Подробный алгоритм разбора HTTP body

Рекомендуемая последовательность:

1. Прочитать `Content-Length`.
2. Прочитать body ровно на это количество байт.
3. Декодировать в UTF-8.
4. Распарсить через `urllib.parse.parse_qs(..., keep_blank_values=True)`.
5. Забрать:
   - `sign = first(form["sign"])`
   - `json_str = first(form["json"])`
6. Проверить, что обе строки непустые.
7. Только после этого валидировать `sign` и парсить JSON.

Если request body пустой, битый или не содержит нужные поля:

- логируем `warning`;
- возвращаем `400`.

Ожидаем минимально такие поля:

- `entry_id`
- `call_direction`
- `entry_result`
- `create_time`
- `end_time`
- `disconnect_reason`
- `line_number`
- `from.number`
- `to.number`
- `to.extension`

### Точное маппирование полей payload -> БД -> Telegram

| Поле в MANGO | Поле в БД | Использование в Telegram | Fallback |
|---|---|---|---|
| `entry_id` | `entry_id` | нет | обязательно, иначе skip |
| `from.number` | `from_number` | `Входящий (пропущенный) номер` | `не указан` |
| `to.extension` | `to_extension` | часть `Вызываемый абонент` | см. приоритет |
| `to.number` | `to_number` | часть `Вызываемый абонент` | см. приоритет |
| `line_number` | `line_number` | fallback для `Вызываемый абонент` | `не указан` |
| `create_time` | `create_time` | `Дата и время` | `не указано` |
| `end_time` | `end_time` | нет | `NULL` |
| `call_direction` | `call_direction` | нет | если не приводится к int, skip |
| `entry_result` | `entry_result` | нет | если не приводится к int, skip |
| `disconnect_reason` | `disconnect_reason` | нет | `NULL` |
| весь parsed payload | `raw_payload` | нет | сохраняем как `JSONB` |

### Какие события лучше пропускать до вставки в БД

Лучше не создавать запись в `missed_calls`, если:

- нет `entry_id`;
- `call_direction` не читается как число;
- `entry_result` не читается как число;
- подпись невалидна;
- IP не разрешён.

Это снижает шум в таблице и не засоряет БД мусорными или подозрительными входящими событиями.

Нужно поддержать, что `to` может быть:

- объектом;
- пустым объектом;
- списком, если MANGO вернёт массив.

Рекомендуемая нормализация:

```python
def extract_to_block(payload: dict) -> dict:
    raw_to = payload.get("to")
    if isinstance(raw_to, dict):
        return raw_to
    if isinstance(raw_to, list):
        for item in raw_to:
            if isinstance(item, dict):
                return item
    return {}
```

## Логика отбора missed inbound

Событие считаем подходящим только если одновременно:

- `int(call_direction) == 1`
- `int(entry_result) == 0`

Все остальные события:

- успешные;
- исходящие;
- внутренние;
- любые без нужных полей

не должны приводить к отправке в Telegram.

## Правила заполнения полей

### from_number

Источник:

- `payload["from"]["number"]`

Если номера нет, сохраняем `NULL` и в сообщении показываем `не указан`.

### Вызываемый абонент

В БД сохраняем раздельно:

- `to_extension`
- `to_number`
- `line_number`

В Telegram отображаем по приоритету:

1. `to.extension + " / " + to.number`, если есть оба;
2. `to.number`, если есть только он;
3. `to.extension`, если есть только он;
4. `line_number`, если блок `to` пустой;
5. `не указан`, если нет вообще ничего.

### Время

В БД сохраняем:

- `create_time` как `TIMESTAMPTZ`
- `end_time` как `TIMESTAMPTZ`

В Telegram показываем локальное время в конфигурируемой timezone:

- `MANGO_DISPLAY_TIMEZONE=Asia/Novosibirsk` или нужная бизнес-таймзона.

Рекомендуемый формат строки:

```text
DD.MM.YYYY HH:MM:SS
```

Пример:

```text
06.04.2026 14:23:51
```

### Практика парсинга времени

Нужно писать функцию уровня:

```python
def parse_datetime_safe(value: Any) -> Optional[datetime]:
    ...
```

Она должна:

- принимать `None`, строку или число;
- пытаться распознать ISO-8601;
- опционально поддержать Unix timestamp, если MANGO так присылает;
- возвращать timezone-aware datetime;
- при неуспехе не падать, а отдавать `None`.

Для вывода локального времени использовать `zoneinfo.ZoneInfo`.

Если timezone в исходном времени отсутствует:

- это отдельный риск;
- до подтверждения формата лучше считать, что MANGO присылает timezone-aware время или UTC.

Если реальный payload покажет другое, этот участок нужно поправить первым.

## Формат сообщения в Telegram

```text
‼️‼️‼️ ПРОПУЩЕННЫЙ ЗВОНОК
Дата и время: {formatted_create_time}
Входящий (пропущенный) номер: {from_number}
Вызываемый абонент: {target_display}
```

Если часть данных отсутствует, использовать безопасные заглушки:

- `не указан`

## Схема БД

### Обязательная таблица

```sql
CREATE TABLE IF NOT EXISTS missed_calls (
    id BIGSERIAL PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE,
    from_number TEXT,
    to_extension TEXT,
    to_number TEXT,
    line_number TEXT,
    create_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    call_direction INTEGER,
    entry_result INTEGER,
    disconnect_reason INTEGER,
    telegram_message_id BIGINT,
    telegram_sent BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_sent_at TIMESTAMPTZ,
    telegram_error TEXT,
    telegram_retry_count INTEGER NOT NULL DEFAULT 0,
    last_telegram_attempt_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Индексы

```sql
CREATE INDEX IF NOT EXISTS idx_missed_calls_create_time
ON missed_calls (create_time);

CREATE INDEX IF NOT EXISTS idx_missed_calls_pending_retry
ON missed_calls (telegram_sent, created_at)
WHERE telegram_sent = FALSE;
```

### Нормальный набор SQL-операций для `MangoStore`

#### Инициализация таблицы

При старте сервиса `MangoStore.initialize()` должен:

1. создать таблицу `missed_calls`;
2. создать индексы;
3. не создавать ничего лишнего из audio pipeline.

#### Вставка новой записи

Рекомендуемая операция:

```sql
INSERT INTO missed_calls (
    entry_id,
    from_number,
    to_extension,
    to_number,
    line_number,
    create_time,
    end_time,
    call_direction,
    entry_result,
    disconnect_reason,
    raw_payload
)
VALUES (
    %(entry_id)s,
    %(from_number)s,
    %(to_extension)s,
    %(to_number)s,
    %(line_number)s,
    %(create_time)s,
    %(end_time)s,
    %(call_direction)s,
    %(entry_result)s,
    %(disconnect_reason)s,
    %(raw_payload)s::jsonb
)
ON CONFLICT (entry_id) DO NOTHING
RETURNING id;
```

#### Обновление после успешной отправки Telegram

```sql
UPDATE missed_calls
SET
    telegram_message_id = %s,
    telegram_sent = TRUE,
    telegram_sent_at = NOW(),
    telegram_error = NULL,
    last_telegram_attempt_at = NOW()
WHERE id = %s;
```

#### Обновление после ошибки Telegram

```sql
UPDATE missed_calls
SET
    telegram_sent = FALSE,
    telegram_error = %s,
    telegram_retry_count = telegram_retry_count + 1,
    last_telegram_attempt_at = NOW()
WHERE id = %s;
```

#### Выборка для retry

```sql
SELECT
    id,
    entry_id,
    from_number,
    to_extension,
    to_number,
    line_number,
    create_time,
    raw_payload
FROM missed_calls
WHERE telegram_sent = FALSE
ORDER BY created_at ASC
LIMIT %s;
```

### Почему добавлены дополнительные поля сверх минимального ТЗ

Для корректного ретрая Telegram нужны:

- `telegram_sent`
- `telegram_sent_at`
- `telegram_error`
- `telegram_retry_count`
- `last_telegram_attempt_at`

Без них мы сохраним факт звонка, но не сможем нормально управлять повторными отправками.

## Поведение при дублях

Защита от дублей строится на `entry_id`.

Рекомендуемый алгоритм:

```sql
INSERT INTO missed_calls (...)
VALUES (...)
ON CONFLICT (entry_id) DO NOTHING
RETURNING id;
```

Если `RETURNING id` ничего не вернул:

- webhook считаем дублем;
- повторное сообщение в Telegram не отправляем;
- возвращаем `200 OK`.

## Поведение при ошибке Telegram

Если запись в БД создалась, а Telegram не отправился:

- не откатываем вставку события;
- ставим:
  - `telegram_sent = FALSE`
  - `telegram_error = <текст ошибки>`
  - `telegram_retry_count = telegram_retry_count + 1`
  - `last_telegram_attempt_at = NOW()`
- логируем ошибку.

Это полностью соответствует требованию:

> запись о звонке всё равно должна сохраниться в БД, а отправка должна быть помечена как неуспешная для последующего ретрая

## Ретрай Telegram

### Рекомендуемый первый этап

Реализовать простой retry loop внутри `mango_webhook_server.py` в отдельном background thread.

Параметры:

- `MANGO_RETRY_ENABLED=1`
- `MANGO_RETRY_INTERVAL_SEC=60`
- `MANGO_RETRY_BATCH_SIZE=20`

Алгоритм:

1. Раз в `MANGO_RETRY_INTERVAL_SEC` выбирать `telegram_sent = FALSE`.
2. Сортировать по `created_at ASC`.
3. Брать первые `MANGO_RETRY_BATCH_SIZE`.
4. Повторять отправку.
5. При успехе обновлять `telegram_message_id`, `telegram_sent`, `telegram_sent_at`, `telegram_error = NULL`.
6. При неуспехе обновлять `telegram_error`, `telegram_retry_count`, `last_telegram_attempt_at`.

### Альтернатива

Если не хочется фонового потока в первом релизе, можно:

- оставить только сохранение failed state;
- ретрай сделать отдельной CLI-командой или cron/systemd timer.

Но production-вариант с background retry удобнее.

### Как избежать гонок при retry

Если webhook и retry worker могут одновременно работать с одной и той же записью, важно не получить повторную отправку.

Для первого релиза есть два безопасных упрощения:

1. Retry worker брать только записи, где `telegram_sent = FALSE` и `last_telegram_attempt_at IS NULL OR last_telegram_attempt_at < NOW() - interval '30 seconds'`.
2. После неуспешной первой отправки не запускать немедленный retry в том же потоке webhook.

Если позже понадобится более строгая блокировка, можно добавить:

- `retry_locked_at`
- `retry_locked_by`

Но для текущего масштаба это, скорее всего, избыточно.

## Проверка подписи

Рекомендуемая функция:

```python
import hashlib
import hmac


def build_mango_sign(api_key: str, json_str: str, api_salt: str) -> str:
    raw = f"{api_key}{json_str}{api_salt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_mango_sign(api_key: str, json_str: str, api_salt: str, sign: str) -> bool:
    expected = build_mango_sign(api_key, json_str, api_salt)
    return hmac.compare_digest(expected.lower(), str(sign).strip().lower())
```

## HTTP server

### Рекомендуемый стек

Для первого релиза достаточно стандартной библиотеки Python:

- `ThreadingHTTPServer`
- `BaseHTTPRequestHandler`

Почему так:

- не добавляем лишний web framework;
- меньше зависимостей;
- проще встроить в текущий минималистичный проект.

### Слушать только локальный интерфейс

```text
MANGO_HTTP_HOST=127.0.0.1
MANGO_HTTP_PORT=8081
MANGO_WEBHOOK_PATH=/events/summary
```

Снаружи endpoint должен быть опубликован только через Nginx с TLS.

### Предлагаемая структура кода внутри `mango_webhook_server.py`

```text
imports
env helpers
datetime helpers
telegram helpers
MangoConfig dataclass
MangoStore class
MangoWebhookHTTPServer class
MangoWebhookHandler class
TelegramRetryWorker class
main()
```

### Состав `MangoConfig`

Рекомендуемые поля:

- `db_enabled: bool`
- `db_host: str`
- `db_port: int`
- `db_name: str`
- `db_user: str`
- `db_password: str`
- `db_sslmode: str`
- `db_connect_timeout_sec: int`
- `telegram_bot_token: str`
- `telegram_chat_id: str`
- `telegram_message_thread_id: Optional[int]`
- `telegram_proxy: str`
- `mango_enabled: bool`
- `mango_http_host: str`
- `mango_http_port: int`
- `mango_webhook_path: str`
- `mango_api_key: str`
- `mango_api_salt: str`
- `mango_allowed_ips: list[str]`
- `mango_display_timezone: str`
- `mango_retry_enabled: bool`
- `mango_retry_interval_sec: int`
- `mango_retry_batch_size: int`
- `mango_log_payloads: bool`

### Валидация `MangoConfig.from_env()`

Сервис должен падать на старте, если:

- `DB_ENABLED=1`, но не хватает DB-настроек;
- пустой `TELEGRAM_BOT_TOKEN`;
- пустой `TELEGRAM_CHAT_ID`;
- пустой `MANGO_API_KEY`;
- пустой `MANGO_API_SALT`;
- `MANGO_HTTP_PORT` некорректный;
- `MANGO_WEBHOOK_PATH` не начинается с `/`.

Это лучше, чем получить неочевидную ошибку уже после выкладки.

### Состав `MangoStore`

Рекомендуемые методы:

- `initialize()`
- `_connect()`
- `insert_missed_call_if_new(payload: dict) -> tuple[bool, int | None]`
- `mark_telegram_sent(row_id: int, message_id: int | None) -> None`
- `mark_telegram_failed(row_id: int, error_text: str) -> None`
- `fetch_pending_telegram(limit: int) -> list[dict]`
- `close() -> None`

### Состав `MangoWebhookHandler`

Рекомендуемые методы:

- `do_POST()`
- `_handle_summary()`
- `_read_raw_body()`
- `_parse_form_body()`
- `_send_json_response()`
- `log_message()` при желании переопределить, чтобы убрать шум стандартного `BaseHTTPRequestHandler`

### Что должно храниться в `server` object

У `ThreadingHTTPServer` удобно положить:

- `config`
- `store`
- `logger`

Тогда обработчик сможет брать зависимости через `self.server`.

## Nginx и HTTPS

### Идея

Python-сервис сам не поднимает TLS.

HTTPS завершается на Nginx:

- сертификат Let's Encrypt или существующий;
- `proxy_pass http://127.0.0.1:8081`;
- allowlist IP MANGO;
- при желании `access_log` и `error_log` в отдельные файлы.

### Пример конфига

```nginx
server {
    listen 443 ssl http2;
    server_name mango-webhook.example.com;

    ssl_certificate /etc/letsencrypt/live/mango-webhook.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mango-webhook.example.com/privkey.pem;

    location = /events/summary {
        allow 1.2.3.4;
        allow 5.6.7.8;
        deny all;

        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }
}
```

### Дополнительно

Whitelist IP MANGO лучше держать сразу в двух местах:

- в Nginx/firewall;
- в приложении через `MANGO_ALLOWED_IPS`.

Проверка в приложении не заменяет сетевую защиту, но полезна как дополнительный барьер.

## Новые env-переменные

Добавить в `.env.example`:

```dotenv
# MANGO webhook
MANGO_ENABLED=1
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
```

Пояснения:

- `MANGO_API_KEY` и `MANGO_API_SALT` нужны для `sign`;
- `MANGO_ALLOWED_IPS` можно хранить списком через запятую;
- `MANGO_DISPLAY_TIMEZONE` использовать для форматирования времени в Telegram;
- `MANGO_LOG_PAYLOADS=0` по умолчанию, чтобы не светить персональные данные в логах.

### Полный пример блока `.env`

```dotenv
MANGO_ENABLED=1
MANGO_HTTP_HOST=127.0.0.1
MANGO_HTTP_PORT=8081
MANGO_WEBHOOK_PATH=/events/summary
MANGO_API_KEY=your_mango_api_key
MANGO_API_SALT=your_mango_api_salt
MANGO_ALLOWED_IPS=1.2.3.4,5.6.7.8
MANGO_DISPLAY_TIMEZONE=Asia/Novosibirsk
MANGO_RETRY_ENABLED=1
MANGO_RETRY_INTERVAL_SEC=60
MANGO_RETRY_BATCH_SIZE=20
MANGO_LOG_PAYLOADS=0
```

## Структура `mango_webhook_server.py`

### Рекомендуемые сущности

```text
MangoConfig
MangoStore
MangoWebhookHandler
MangoWebhookServer
TelegramRetryWorker
```

### Рекомендуемые функции

```text
from_env()
setup_logging()
verify_source_ip()
parse_request_payload()
verify_mango_sign()
extract_to_block()
is_missed_inbound_call()
parse_datetime_safe()
format_local_datetime()
build_target_display()
build_telegram_text()
insert_missed_call_if_new()
mark_telegram_sent()
mark_telegram_failed()
retry_pending_telegram()
serve_forever()
```

### Telegram helper

Чтобы не тащить в первый релиз лишний рискованный рефакторинг, есть два варианта:

1. Быстрый и безопасный для текущего production:
   локально продублировать в `mango_webhook_server.py` минимальные helper-функции отправки Telegram из `callbot_daemon.py`.
2. Более чистый, но более инвазивный:
   вынести Telegram helper в общий модуль и подключить его из обоих сервисов.

Для первого релиза рекомендуется вариант 1.

### Предлагаемый каркас функций

```python
def env_bool(name: str, default: bool = False) -> bool: ...
def env_optional_int(name: str) -> Optional[int]: ...
def env_csv(name: str, default: str = "") -> list[str]: ...
def now_utc() -> datetime: ...
def parse_datetime_safe(value: Any) -> Optional[datetime]: ...
def format_local_datetime(value: Optional[datetime], tz_name: str) -> str: ...
def extract_to_block(payload: dict) -> dict: ...
def build_target_display(payload: dict) -> str: ...
def build_telegram_text(payload: dict, tz_name: str) -> str: ...
def build_mango_sign(api_key: str, json_str: str, api_salt: str) -> str: ...
def verify_mango_sign(api_key: str, json_str: str, api_salt: str, sign: str) -> bool: ...
def is_missed_inbound_call(payload: dict) -> bool: ...
```

### Рекомендуемый принцип построения кода

Разделять:

- pure functions без I/O;
- DB layer;
- HTTP layer;
- retry/background layer.

Это сильно упрощает тесты и уменьшает риск, что business logic окажется размазана по `BaseHTTPRequestHandler`.

## Псевдокод обработчика webhook

```python
def handle_summary(request):
    if request.method != "POST":
        return 405

    if request.path != cfg.mango_webhook_path:
        return 404

    if not verify_source_ip(request, cfg.allowed_ips):
        log.warning("MANGO IP is not allowed")
        return 403

    sign, json_str = parse_request_payload(request)
    if not sign or not json_str:
        log.warning("MANGO payload is incomplete")
        return 400

    if not verify_mango_sign(cfg.api_key, json_str, cfg.api_salt, sign):
        log.warning("MANGO sign verification failed")
        return 403

    payload = json.loads(json_str)

    if not is_missed_inbound_call(payload):
        log.info("MANGO event skipped: not missed inbound")
        return 200

    inserted, row_id = store.insert_missed_call_if_new(payload)
    if not inserted:
        log.info("MANGO duplicate skipped: entry_id=%s", payload.get("entry_id"))
        return 200

    text = build_telegram_text(payload, cfg.display_timezone)
    try:
        result = send_telegram_message(cfg, text)
        first_message_id = extract_first_message_id(result)
        store.mark_telegram_sent(row_id, first_message_id)
    except Exception as exc:
        store.mark_telegram_failed(row_id, str(exc))
        log.exception("Telegram send failed for entry_id=%s", payload.get("entry_id"))

    return 200
```

### Более подробный пошаговый алгоритм `_handle_summary`

1. Проверить метод.
2. Проверить path.
3. Получить реальный source IP:
   - брать `self.client_address[0]`;
   - не доверять `X-Forwarded-For`, если запрос пришёл не от локального reverse proxy.
4. Проверить allowlist IP, если он задан.
5. Прочитать raw body.
6. Распарсить form-urlencoded.
7. Извлечь `sign` и `json`.
8. Проверить подпись.
9. `json.loads(json_str)`.
10. Убедиться, что root payload — объект.
11. Проверить, что это missed inbound.
12. Подготовить нормализованные поля для БД.
13. Сделать `insert if new`.
14. Если duplicate, завершить обработку.
15. Собрать текст сообщения.
16. Вызвать Telegram API.
17. Зафиксировать success/failure.
18. Вернуть `200`.

## Тонкости, которые важно не пропустить

### 1. `to` может быть пустым

Нельзя падать, если:

- `to` нет;
- `to` пустой;
- `to.number` нет;
- `to.extension` нет.

### 2. `from.number` тоже может отсутствовать

Нужно безопасно обрабатывать и БД, и Telegram текст.

### 3. На дубль нельзя повторно слать Telegram

Даже если тот же webhook прилетел ещё раз через секунду.

### 4. Подпись считается по raw JSON string

Это критично.

### 5. Ответ `200` нужен не только для успешных missed calls

Для неподходящих событий тоже лучше отдавать `200`, чтобы MANGO не ретраил весь поток.

Рекомендуемые коды:

- `200` для валидно обработанного или осознанно пропущенного события;
- `400` для некорректного формата запроса;
- `403` для неверной подписи или запрещённого IP;
- `405` для неверного метода.

Если хотите максимально упростить взаимоотношения с провайдером webhook, можно всегда возвращать `200`, кроме совсем явных ошибок инфраструктуры. Но базовый вариант выше лучше для диагностики.

## Изменения в деплое

### Новый systemd unit

Добавить `deploy/mango-webhook.service`:

```ini
[Unit]
Description=Call Brief AI MANGO webhook
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=callbot
Group=callbot
WorkingDirectory=/opt/call_brief_ai/current
EnvironmentFile=/opt/call_brief_ai/shared/.env
ExecStart=/opt/call_brief_ai/current/.venv/bin/python /opt/call_brief_ai/current/mango_webhook_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Что не менять

Текущий `callbot.service` остаётся для аудио-потока и не должен зависеть от webhook.

### Рекомендуемый порядок выкладки на VPS

1. Залить новый код.
2. Обновить `/opt/call_brief_ai/shared/.env`.
3. Установить новый `mango-webhook.service`.
4. Установить и проверить Nginx-конфиг.
5. Убедиться, что DNS указывает на VPS.
6. Проверить TLS-сертификат.
7. Запустить сервис локально.
8. Проверить локальный health-like запрос на порт.
9. Проверить запрос через Nginx с тестовой подписью.
10. Только потом регистрировать webhook URL в MANGO.

### Что проверить после выкладки

- сервис слушает localhost-порт;
- Nginx отвечает по HTTPS;
- POST на неверный path даёт `404`;
- POST с неверной подписью даёт `403`;
- валидный skipped event не падает;
- валидный missed event пишет в БД;
- сообщение реально приходит в Telegram.

## Логи

Нужно явно логировать:

- ошибка проверки подписи;
- запрет по IP;
- ошибка парсинга payload;
- skip неподходящего события;
- duplicate по `entry_id`;
- ошибка записи в БД;
- ошибка отправки в Telegram;
- успешная отправка Telegram.

Не логировать полный payload по умолчанию.

Если нужен debug-режим, включать через `MANGO_LOG_PAYLOADS=1`.

### Хороший формат логов для production

Желательно логировать `entry_id` везде, где это возможно:

- `entry_id=<...> accepted`
- `entry_id=<...> skipped_not_missed`
- `entry_id=<...> duplicate`
- `entry_id=<...> telegram_sent`
- `entry_id=<...> telegram_failed`

Так потом намного проще разбирать инциденты по журналам.

## Тесты

Добавить `test_mango_webhook_server.py` с кейсами:

1. Валидная подпись принимается.
2. Невалидная подпись отклоняется.
3. Невалидный IP отклоняется.
4. Не-missed inbound событие возвращает `200`, но не пишет в Telegram.
5. Missed inbound событие создаёт запись в БД.
6. Missed inbound событие отправляет Telegram и сохраняет `telegram_message_id`.
7. Дубликат по `entry_id` не создаёт вторую запись и не шлёт повторно.
8. При ошибке Telegram запись остаётся в БД и помечается как pending retry.
9. Корректно работает приоритет вывода `to.extension` / `to.number` / `line_number`.
10. Корректно форматируется локальное время.

### Что именно мокать в тестах

#### Мокать

- `requests.post` для Telegram;
- методы `MangoStore`, если тестируется только HTTP layer;
- `self.client_address`, если тестируется allowlist.

#### Не мокать

- функции подписи;
- функции маппинга полей;
- функции сборки Telegram текста.

Они должны проверяться как pure logic.

### Минимальный набор unit-тестов на pure functions

- `verify_mango_sign()` с валидным и невалидным sign;
- `is_missed_inbound_call()` на разные payload;
- `extract_to_block()` для dict/list/empty;
- `build_target_display()` для всех fallback-сценариев;
- `format_local_datetime()` на валидном и пустом времени.

## Порядок реализации

### Этап 1

- создать `mango_webhook_server.py`;
- описать `MangoConfig`;
- реализовать HTTP endpoint;
- реализовать проверку `sign`;
- реализовать таблицу `missed_calls`;
- реализовать insert + dedupe;
- реализовать отправку в Telegram;
- реализовать сохранение `telegram_message_id` и failed state.

#### Конкретный план работ по Этапу 1

1. Скопировать минимальные env helper-функции.
2. Описать `MangoConfig`.
3. Подключить psycopg и реализовать `MangoStore`.
4. Добавить SQL create table и индексы.
5. Реализовать pure functions:
   - подпись
   - фильтр missed inbound
   - маппинг `to`
   - формат Telegram текста
6. Реализовать HTTP handler.
7. Подключить Telegram send.
8. Протянуть success/failure update в БД.
9. Добавить `main()` и `serve_forever()`.

### Этап 2

- добавить retry worker;
- добавить unit-файл;
- добавить Nginx example;
- обновить `.env.example`;
- обновить документацию.

#### Конкретный план работ по Этапу 2

1. Добавить поток `TelegramRetryWorker`.
2. Добавить SQL select pending.
3. Реализовать backoff-поведение через время последней попытки.
4. Подготовить `deploy/mango-webhook.service`.
5. Подготовить `deploy/mango_webhook.nginx.conf.example`.
6. Обновить документацию по деплою и `.env`.

### Этап 3

- написать unit tests;
- прогнать локальные smoke tests;
- прогнать staging/prod webhook test с тестовым вызовом.

#### Конкретный план работ по Этапу 3

1. Закрыть unit tests по pure functions.
2. Закрыть handler tests на валидные и невалидные запросы.
3. Проверить duplicate flow.
4. Проверить failed Telegram flow.
5. Проверить retry flow.
6. Проверить deployment на VPS.

## Готовые решения по спорным местам

### Вопрос: встраивать в `callbot_daemon.py` или делать отдельно?

Решение:

- делать отдельным сервисом `mango_webhook_server.py`.

### Вопрос: использовать polling статистики MANGO?

Решение:

- нет, только Realtime webhook как основной механизм.

### Вопрос: что делать, если Telegram упал?

Решение:

- запись сохраняем в БД;
- Telegram отмечаем как failed;
- потом retry.

### Вопрос: как избежать дублей?

Решение:

- уникальный `entry_id` в таблице;
- `ON CONFLICT DO NOTHING`;
- повторно Telegram не слать.

### Вопрос: где делать HTTPS?

Решение:

- на Nginx;
- Python слушает только localhost.

## Открытые вопросы перед кодированием

Их не обязательно закрывать до старта разработки, но желательно закрыть до выкладки:

1. Есть ли уже реальный sample webhook body от MANGO?
2. Подтверждён ли точный список IP MANGO для allowlist?
3. Есть ли готовый поддомен под webhook?
4. В какую timezone нужно показывать время бизнесу?
5. Нужен ли отдельный Telegram topic через `message_thread_id`, или пишем в общий поток группы?

## Что я считаю лучшей практической стратегией реализации

Если цель — быстро и надёжно запустить это в текущем репозитории, оптимальная стратегия такая:

1. Делать отдельный `mango_webhook_server.py`.
2. Не трогать текущий audio/OpenAI daemon.
3. Не тащить web framework.
4. Не пытаться сразу красиво рефакторить весь проект в общие модули.
5. Сначала добиться стабильного webhook -> DB -> Telegram.
6. Только потом выносить общие helper-слои и улучшать retry.

## Минимальный итоговый результат приемки

После реализации должны выполняться все условия:

1. После пропущенного входящего звонка MANGO в Telegram появляется ровно одно сообщение нужного формата.
2. В `missed_calls` создаётся ровно одна запись.
3. Повторный webhook с тем же `entry_id` не приводит к дублю.
4. Успешные, исходящие и внутренние звонки не публикуются.
5. При падении Telegram запись в БД всё равно создаётся и помечается для ретрая.

## Практическая рекомендация по первому коммиту

Первый рабочий коммит лучше сделать узким:

1. `mango_webhook_server.py`
2. SQL create table
3. отправка в Telegram
4. dedupe по `entry_id`

И только вторым коммитом добавлять:

1. retry worker
2. Nginx example
3. docs
4. тесты расширенного поведения

Так будет проще отлаживать и безопаснее выкатывать.
