# Auto-Deploy And VPS Setup

Ниже описан полный рабочий сценарий:

1. Вы храните проект в GitHub.
2. Каждый push в `main` или `master` запускает GitHub Actions.
3. GitHub Actions по SSH подключается к VPS.
4. На сервер загружается архив свежего релиза.
5. Сервер раскладывает релиз в `/opt/call_brief_ai/releases/<commit_sha>`.
6. Обновляется symlink `/opt/call_brief_ai/current`.
7. Обновляется `systemd`-сервис и выполняется `restart`.

Это самый простой и надежный путь для вашего текущего проекта.

## 1. Что уже добавлено в репозиторий

- `.github/workflows/deploy.yml`
- `deploy/remote_deploy.sh`
- `deploy/callbot.service`

Эти файлы уже готовы к использованию.

## 2. Что нужно подготовить заранее

Вам понадобятся:

- GitHub-репозиторий
- VPS c Ubuntu или Debian
- домен не обязателен
- SSH-доступ к VPS
- токен OpenAI
- токен Telegram-бота
- данные FTP или SFTP

## 3. Как подготовить локальный проект и git

Сейчас папка проекта у вас еще не является git-репозиторием. Сделайте это в PowerShell:

```powershell
cd C:\Users\ilel\Desktop\call_brief_ai
git init -b main
git add .
git commit -m "Initial production setup"
```

Если `git` не установлен, поставьте его:

1. Скачайте Git for Windows: `https://git-scm.com/download/win`
2. Установите с настройками по умолчанию
3. Перезапустите PowerShell

Проверьте:

```powershell
git --version
```

## 4. Как создать репозиторий на GitHub

### Вариант через веб-интерфейс

1. Зайдите в GitHub.
2. Нажмите `New repository`.
3. Назовите репозиторий, например `call_brief_ai`.
4. Не ставьте `Initialize with README`, потому что README уже есть локально.
5. Создайте репозиторий.

После этого привяжите локальный проект:

```powershell
git remote add origin https://github.com/YOUR_GITHUB_LOGIN/call_brief_ai.git
git push -u origin main
```

### Если хотите работать по SSH с GitHub

Сначала создайте SSH-ключ для GitHub:

```powershell
ssh-keygen -t ed25519 -C "your_email@example.com"
```

Публичный ключ обычно лежит здесь:

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Скопируйте его в GitHub:

1. `GitHub -> Settings -> SSH and GPG keys`
2. `New SSH key`
3. Вставьте публичный ключ

Потом можно сменить remote:

```powershell
git remote remove origin
git remote add origin git@github.com:YOUR_GITHUB_LOGIN/call_brief_ai.git
git push -u origin main
```

## 5. Как подготовить VPS

Ниже команды для первого входа на сервер. Предполагается, что вы входите как `root`.

Подключение:

```bash
ssh root@YOUR_VPS_IP
```

Обновление системы:

```bash
apt-get update
apt-get upgrade -y
```

Установка пакетов:

```bash
apt-get install -y python3 python3-venv python3-pip ffmpeg git rsync curl nano
```

Создание системного пользователя, под которым будет работать сервис:

```bash
id -u callbot >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/call_brief_ai --shell /bin/bash callbot
```

Создание каталогов проекта:

```bash
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

## 6. Как заполнить `.env` на VPS

Откройте файл:

```bash
nano /opt/call_brief_ai/shared/.env
```

Вставьте и заполните:

```dotenv
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=
OPENAI_PROXY=
OPENAI_TIMEOUT_SEC=600
OPENAI_CONNECT_TIMEOUT_SEC=30
OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe-diarize
OPENAI_TRANSCRIBE_LANGUAGE=ru
OPENAI_CHUNKING_STRATEGY=auto
OPENAI_ANALYSIS_MODEL=gpt-5-mini
OPENAI_ANALYSIS_REASONING_EFFORT=
OPENAI_ANALYSIS_STORE=0
OPENAI_ANALYSIS_MAX_OUTPUT_TOKENS=1800

FTP_PROTOCOL=sftp
FTP_HOST=YOUR_FTP_OR_SFTP_HOST
FTP_PORT=22
FTP_USER=YOUR_USER
FTP_USERNAME=YOUR_USER
FTP_PASSWORD=YOUR_PASSWORD
FTP_REMOTE_ROOT=/recordings
FTP_ARCHIVE_DIR=/archive
FTP_DELETE_AFTER_SUCCESS=0
FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS=1
FTP_USE_TLS=0
FTP_ENCODING=utf-8
FTP_ENCODING_FALLBACKS=cp1251,cp866,latin-1
FTP_TIMEOUT_SEC=60
FTP_CONNECT_ATTEMPTS=2
FTP_RETRY_DELAY_SEC=5

INSTRUCTIONS_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
INSTRUCTION_JSON_PATH=/opt/call_brief_ai/shared/instructions.json
STATE_PATH=/opt/call_brief_ai/shared/state.json
WORK_ROOT=/opt/call_brief_ai/shared/work

TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_MESSAGE_THREAD_ID=
TELEGRAM_PROXY=

POLL_INTERVAL_SEC=300
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

### Если нужен proxy для OpenAI или Telegram

Для OpenAI можно задать отдельный proxy:

```dotenv
OPENAI_PROXY=http://login:password@PROXY_HOST:8888
```

Если proxy отвечает нестабильно, можно увеличить таймауты для OpenAI-маршрута:

```dotenv
OPENAI_TIMEOUT_SEC=600
OPENAI_CONNECT_TIMEOUT_SEC=30
OPENAI_ROUTE_PROBE_TIMEOUT_SEC=15
OPENAI_ROUTE_PROBE_CONNECT_TIMEOUT_SEC=5
OPENAI_REQUEST_ATTEMPTS=2
OPENAI_RETRY_DELAY_SEC=2
OPENAI_RETRY_BACKOFF=2
OPENAI_PROXY_FAILURE_COOLDOWN_SEC=300
```

Если proxy иногда падает целиком, daemon теперь может быстро прекратить безрезультатные попытки через этот маршрут на время `OPENAI_PROXY_FAILURE_COOLDOWN_SEC`, чтобы не тратить весь цикл на один и тот же `ConnectTimeout`.

Опционально можно разрешить прямой fallback без proxy:

```dotenv
OPENAI_PROXY_DIRECT_FALLBACK=1
```

Включайте это только если ваш основной VPS сам может обращаться к OpenAI напрямую и такой обход допустим по региону и сети.

Для Telegram можно задать отдельный proxy:

```dotenv
TELEGRAM_PROXY=http://login:password@PROXY_HOST:8888
```

Если хотите проксировать весь HTTP(S)-трафик процесса целиком, вместо отдельных переменных можно использовать стандартные:

```dotenv
HTTP_PROXY=http://login:password@PROXY_HOST:8888
HTTPS_PROXY=http://login:password@PROXY_HOST:8888
NO_PROXY=127.0.0.1,localhost
```

Если используются `OPENAI_PROXY` и `TELEGRAM_PROXY`, глобальные `HTTP_PROXY` и `HTTPS_PROXY` обычно лучше не задавать, чтобы не было путаницы.

Важно: proxy на том же VPS, где у вас уже возникает `403 unsupported_country_region_territory`, не поможет. Нужен отдельный VPS или gateway с выходным IP в поддерживаемой OpenAI стране.

Сохранение в `nano`:

1. `Ctrl+O`
2. `Enter`
3. `Ctrl+X`

### Как поднять простой proxy на отдельном VPS

Если OpenAI блокирует ваш основной сервер по региону, поднимайте proxy не на нем, а на отдельном VPS в поддерживаемой стране.

Ниже пример с `tinyproxy` на отдельном сервере `PROXY_VPS_IP`.

Установка:

```bash
apt-get update
apt-get install -y tinyproxy
cp /etc/tinyproxy/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf.bak
```

Откройте конфиг:

```bash
nano /etc/tinyproxy/tinyproxy.conf
```

Минимальный рабочий пример:

```conf
User tinyproxy
Group tinyproxy
Port 8888
Timeout 600
LogLevel Info
PidFile "/run/tinyproxy/tinyproxy.pid"
MaxClients 100
Allow 127.0.0.1
Allow YOUR_APP_VPS_IP
BasicAuth callbot StrongPassword123
ConnectPort 443
ConnectPort 80
```

Сохраните файл и перезапустите сервис:

```bash
systemctl enable tinyproxy
systemctl restart tinyproxy
systemctl status tinyproxy
```

Если включен firewall, откройте порт только для IP вашего основного VPS:

```bash
ufw allow from YOUR_APP_VPS_IP to any port 8888 proto tcp
```

Проверьте proxy с вашего основного VPS:

```bash
curl -x http://callbot:StrongPassword123@PROXY_VPS_IP:8888 \
  https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

Если здесь видите `CONNECT tunnel failed`, `400`, `403` от proxy или просто timeout, сначала чините proxy/firewall/allow-list на proxy-VPS. В таком состоянии сам `callbot` работать не сможет, потому что OpenAI-трафик до API не доходит.

Если `curl` проходит, добавьте на основном VPS в `/opt/call_brief_ai/shared/.env`:

```dotenv
OPENAI_PROXY=http://callbot:StrongPassword123@PROXY_VPS_IP:8888
```

Потом перезапустите сервис:

```bash
sudo systemctl restart callbot.service
journalctl -u callbot -f
```

Если в логине или пароле proxy есть специальные символы вроде `@`, `:`, `/`, их нужно URL-encode.

## 7. Как подготовить `instructions.json` на VPS

Если хотите использовать именно серверную версию инструкции, откройте:

```bash
nano /opt/call_brief_ai/shared/instructions.json
```

И вставьте туда ваш актуальный JSON.

Если оставить файл пустым, то при первом деплое туда будет скопирована версия из репозитория.

## 8. Как подготовить SSH-ключ для GitHub Actions

Нужно создать отдельный ключ, которым GitHub Actions будет входить на сервер.

На локальном компьютере в PowerShell:

```powershell
mkdir $env:USERPROFILE\.ssh -Force
ssh-keygen -t ed25519 -C "github-actions-vps-deploy" -f $env:USERPROFILE\.ssh\github_actions_vps
```

Будет два файла:

- приватный ключ: `%USERPROFILE%\.ssh\github_actions_vps`
- публичный ключ: `%USERPROFILE%\.ssh\github_actions_vps.pub`

### Добавьте публичный ключ на VPS

В PowerShell выведите публичный ключ:

```powershell
Get-Content $env:USERPROFILE\.ssh\github_actions_vps.pub
```

Скопируйте весь вывод.

На VPS:

```bash
mkdir -p /root/.ssh
chmod 700 /root/.ssh
nano /root/.ssh/authorized_keys
```

Вставьте туда публичный ключ и сохраните.

Потом:

```bash
chmod 600 /root/.ssh/authorized_keys
```

Проверьте, что ключ действительно работает:

```powershell
ssh -i $env:USERPROFILE\.ssh\github_actions_vps root@YOUR_VPS_IP
```

Если вход выполняется без пароля, все готово.

## 9. Как добавить GitHub Secrets

Откройте репозиторий на GitHub:

1. `Settings`
2. `Secrets and variables`
3. `Actions`
4. `New repository secret`

Создайте секреты:

### `VPS_HOST`

Пример:

```text
203.0.113.10
```

### `VPS_PORT`

Пример:

```text
22
```

### `VPS_USER`

Пример:

```text
root
```

### `VPS_SSH_KEY`

Это содержимое приватного ключа:

```powershell
Get-Content $env:USERPROFILE\.ssh\github_actions_vps -Raw
```

Скопируйте весь ключ целиком, включая:

- `-----BEGIN OPENSSH PRIVATE KEY-----`
- `-----END OPENSSH PRIVATE KEY-----`

И вставьте его в secret `VPS_SSH_KEY`.

## 10. Как сделать первый деплой

После того как:

- репозиторий создан
- код запушен
- VPS подготовлен
- secrets добавлены

сделайте любой новый commit:

```powershell
cd C:\Users\ilel\Desktop\call_brief_ai
git add .
git commit -m "Setup auto deploy"
git push
```

После этого:

1. зайдите в GitHub
2. откройте вкладку `Actions`
3. откройте workflow `Deploy To VPS`
4. дождитесь зеленого статуса

## 11. Что произойдет на сервере при деплое

Workflow сделает следующее:

1. Проверит синтаксис Python.
2. Соберет архив проекта.
3. По SSH отправит архив на VPS.
4. Отправит на VPS `deploy/remote_deploy.sh`.
5. Отправит на VPS `deploy/callbot.service`.
6. Создаст релиз в `/opt/call_brief_ai/releases/<commit_sha>`.
7. Создаст виртуальное окружение и поставит зависимости.
8. Обновит symlink `/opt/call_brief_ai/current`.
9. Перезагрузит `systemd`.
10. Перезапустит `callbot.service`.

## 12. Как проверить сервис после деплоя

Проверить статус:

```bash
systemctl status callbot.service --no-pager
```

Смотреть логи:

```bash
journalctl -u callbot.service -n 200 --no-pager
```

Следить за логами онлайн:

```bash
journalctl -u callbot.service -f
```

Проверить текущий релиз:

```bash
ls -lah /opt/call_brief_ai/current
```

Проверить releases:

```bash
ls -lah /opt/call_brief_ai/releases
```

Проверить `.env`:

```bash
sed -n '1,200p' /opt/call_brief_ai/shared/.env
```

## 13. Как делать обычные обновления дальше

Ваш повседневный цикл будет таким:

```powershell
cd C:\Users\ilel\Desktop\call_brief_ai
git add .
git commit -m "Describe changes"
git push
```

Больше ничего руками на VPS делать не нужно, если:

- не менялись системные пакеты
- не менялись секреты
- не менялись пути

## 14. Когда надо руками менять сервер

Ручные действия на VPS нужны, если:

- меняется `.env`
- меняется `instructions.json`, которую вы храните на сервере отдельно
- нужна новая системная зависимость
- вы меняете базовый путь проекта
- вы меняете имя systemd-сервиса

## 15. Как обновить `.env` после запуска

```bash
nano /opt/call_brief_ai/shared/.env
systemctl restart callbot.service
```

## 16. Как вручную перезапустить сервис

```bash
systemctl restart callbot.service
```

## 17. Как вручную остановить сервис

```bash
systemctl stop callbot.service
```

## 18. Как вручную запустить сервис

```bash
systemctl start callbot.service
```

## 19. Как откатиться на предыдущий релиз

Посмотреть релизы:

```bash
ls -1 /opt/call_brief_ai/releases
```

Переключить `current` на нужную папку:

```bash
ln -sfn /opt/call_brief_ai/releases/PUT_RELEASE_ID_HERE /opt/call_brief_ai/current
chown -h callbot:callbot /opt/call_brief_ai/current
systemctl restart callbot.service
```

Проверить:

```bash
systemctl status callbot.service --no-pager
```

## 20. Что проверить, если GitHub Actions не деплоит

### Ошибка SSH

Проверьте:

- правильный `VPS_HOST`
- правильный `VPS_PORT`
- правильный `VPS_USER`
- что публичный ключ есть в `/root/.ssh/authorized_keys`
- что в `VPS_SSH_KEY` вставлен именно приватный ключ

### Ошибка `systemctl`

Проверьте:

```bash
systemctl status callbot.service --no-pager
journalctl -u callbot.service -n 200 --no-pager
```

### Ошибка Python-зависимостей

Проверьте вручную:

```bash
/opt/call_brief_ai/current/.venv/bin/pip freeze
```

### Ошибка конфигурации

Проверьте:

```bash
cat /opt/call_brief_ai/shared/.env
```

### Ошибка OpenAI / Telegram / FTP / SFTP

Если ошибка OpenAI выглядит как `403 unsupported_country_region_territory`, проверьте `OPENAI_PROXY` или `OPENAI_BASE_URL`. Proxy должен находиться на отдельном VPS в поддерживаемой стране, а не на том же сервере, где уже возникает блокировка.

Если после включения `OPENAI_PROXY` ошибка меняется на `httpx.ConnectTimeout`, `openai.APITimeoutError` или `CONNECT tunnel failed`, значит маршрут до proxy или сам proxy настроен неверно. Сначала добейтесь, чтобы `curl -x ... https://api.openai.com/v1/models ...` стабильно проходил с основного VPS, и только потом перезапускайте `callbot`.

Новая версия daemon не будет бесконечно тратить цикл на мертвый proxy: перед скачиванием новых аудио она проверяет OpenAI-маршрут, после серии неудачных попыток ставит маршрут через `OPENAI_PROXY` на паузу на `OPENAI_PROXY_FAILURE_COOLDOWN_SEC`, а при включенном `OPENAI_PROXY_DIRECT_FALLBACK=1` сервис попробует временно использовать прямой маршрут.

Запустите сервис вручную для диагностики:

```bash
cd /opt/call_brief_ai/current
set -a
source /opt/call_brief_ai/shared/.env
set +a
/opt/call_brief_ai/current/.venv/bin/python callbot_daemon.py
```

## 21. Минимальный чек-лист перед первым push

- Проект закоммичен в git
- Репозиторий создан на GitHub
- Workflow лежит в `.github/workflows/deploy.yml`
- На VPS созданы каталоги `/opt/call_brief_ai/releases` и `/opt/call_brief_ai/shared`
- На VPS заполнен `/opt/call_brief_ai/shared/.env`
- На VPS готов `/opt/call_brief_ai/shared/instructions.json`
- На GitHub добавлены `VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_SSH_KEY`
- SSH-ключ GitHub Actions проверен вручную
- В Telegram у вас уже есть правильный `TELEGRAM_CHAT_ID`

## 22. Самая короткая версия процесса

1. Подготовить VPS.
2. Подготовить `.env` и `instructions.json` на VPS.
3. Создать SSH-ключ для GitHub Actions.
4. Добавить secrets в GitHub.
5. Запушить код.
6. Проверить `Actions`.
7. Проверить `systemctl status callbot.service`.
