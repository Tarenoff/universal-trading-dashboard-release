# Установка Universal Trading Dashboard на Linux VPS

Это единственный поддерживаемый путь первой публичной версии: Linux VPS,
локальный bind и доступ из браузера через SSH tunnel. Выполняйте команды под
обычным пользователем VPS. Не запускайте Dashboard от `root`.

## 1. Подготовьте VPS

Нужны Python 3.12 или новее, модуль `venv`, `git` и `curl`. Для Debian/Ubuntu с
доступным пакетом Python 3.12:

```bash
sudo apt update
sudo apt install -y curl git nano python3.12 python3.12-venv
python3.12 --version
```

Последняя команда должна вывести версию не ниже `Python 3.12`. На другом
дистрибутиве установите эквивалентные пакеты его штатным package manager.

## 2. Склонируйте и установите Dashboard

Используйте отдельный каталог в домашней директории:

```bash
git clone https://github.com/Tarenoff/universal-trading-dashboard-release.git \
  "$HOME/universal-trading-dashboard"
cd "$HOME/universal-trading-dashboard"

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Команда устанавливает runtime dependencies, `universal-trading-dashboard` и
`universal-trading-dashboard-sync`. CSS, JavaScript и templates уже включены в
Python package.

## 3. Создайте данные первого запуска

Dashboard хранит импортированные события в собственном EventStore и отдельно
читает конфигурацию SQLite-источников:

```bash
cd "$HOME/universal-trading-dashboard"
mkdir -p data config
printf '%s\n' '{"signal_types": [], "sources": []}' > config/sources.json
```

Не добавляйте в этот JSON пароли, API keys или токены. Пустая конфигурация при
первом запуске — нормальное состояние.

## 4. Настройте пользовательские службы systemd

Web Dashboard и Auto Sync worker остаются отдельными процессами, но systemd
запускает и останавливает их как одну установку. Оба unit используют один
EventStore и один файл конфигурации.

Создайте каталог и основной unit:

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/universal-trading-dashboard.service
```

```ini
[Unit]
Description=Universal Trading Dashboard
Wants=network-online.target universal-trading-dashboard-sync.service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/universal-trading-dashboard
ExecStart=%h/universal-trading-dashboard/.venv/bin/universal-trading-dashboard --events-path %h/universal-trading-dashboard/data/events.sqlite3 --external-sources-config %h/universal-trading-dashboard/config/sources.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Создайте worker unit:

```bash
nano ~/.config/systemd/user/universal-trading-dashboard-sync.service
```

```ini
[Unit]
Description=Universal Trading Dashboard Auto Sync
After=network-online.target
PartOf=universal-trading-dashboard.service

[Service]
Type=simple
WorkingDirectory=%h/universal-trading-dashboard
ExecStart=%h/universal-trading-dashboard/.venv/bin/universal-trading-dashboard-sync --events-path %h/universal-trading-dashboard/data/events.sqlite3 --external-sources-config %h/universal-trading-dashboard/config/sources.json
Restart=on-failure
RestartSec=5
```

`%h` — домашняя директория пользователя systemd. Главный unit запускает worker
через `Wants=`; `PartOf=` связывает остановку и перезапуск worker с главным
unit. Не запускайте worker вручную и не включайте его отдельной командой.

Чтобы user-level службы продолжали работать после выхода из SSH-сессии и после
перезагрузки VPS, включите lingering один раз. Выполните от имени пользователя
Dashboard, если у него есть необходимые права:

```bash
loginctl enable-linger "$USER"
```

Если команда сообщает об отсутствии прав, администратор VPS должен один раз
выполнить для этого пользователя:

```bash
sudo loginctl enable-linger USER
```

Замените `USER` на имя пользователя, под которым установлены Dashboard и оба
unit.

## 5. Запустите Dashboard и worker

Загрузите unit и одной командой включите и запустите полную установку:

```bash
systemctl --user daemon-reload
systemctl --user enable --now universal-trading-dashboard.service
```

При первом старте web-служба создаёт пустой EventStore в
`data/events.sqlite3`. Worker запускается автоматически, но не открывает
внешние SQLite-базы для импорта, пока Auto Sync не включен хотя бы для одного
источника.

## 6. Проверьте работу

На VPS выполните:

```bash
systemctl --user status universal-trading-dashboard.service
curl -f http://127.0.0.1:8000/
```

Для управления установкой используйте только основной unit:

```bash
systemctl --user restart universal-trading-dashboard.service
systemctl --user stop universal-trading-dashboard.service
journalctl --user -u universal-trading-dashboard.service -u universal-trading-dashboard-sync.service -f
```

`restart` и `stop` главного unit применяются и к worker.

## 7. Откройте Dashboard через SSH tunnel

На своём компьютере, а не на VPS, выполните:

```bash
ssh -L 8000:127.0.0.1:8000 USER@VPS_HOST
```

Замените `USER` на пользователя VPS, а `VPS_HOST` — на его IP-адрес или DNS
имя. Затем откройте <http://127.0.0.1:8000> в локальном браузере.

Не передавайте `--host 0.0.0.0`, не открывайте порт `8000` в firewall и не
публикуйте Dashboard через публичный URL. В приложении нет встроенной
аутентификации для такого доступа.

## 8. Подключите SQLite-базу сигналов

1. Откройте `Settings` → `Signal Sources`.
2. Нажмите `Add Source`.
3. Укажите абсолютный путь к SQLite-файлу, доступному пользователю VPS.
4. В мастере выберите таблицу и сопоставьте первичный ключ, символ, время
   события и тип сигнала. Цена является опциональной.
5. Проверьте preview и сохраните source.
6. При необходимости нажмите `Import`, чтобы импортировать уже имеющуюся
   историю вручную.

Внешний SQLite-файл открывается read-only. Dashboard записывает только
собственный `events.sqlite3` и `sources.json`; доступ к ним должен быть только
у пользователя Dashboard.

## 9. Включите Auto Sync для нужных источников

Новые sources создаются с выключенным Auto Sync. В таблице `Signal Sources`
включите `Auto Sync` переключателем `On` только для источников, которые должны
импортироваться автоматически. Worker уже запущен systemd и начнёт читать такой
источник по его настроенному интервалу. Пока переключатель остаётся `Off`,
внешняя SQLite-база не опрашивается и не импортируется автоматически.

После импорта появятся symbols, фильтры и события. Для каждого symbol можно
выбрать `Bybit`, `Binance` или `Hyperliquid` в selector источника свечей.

## Источники свечей и ограничения

Dashboard получает свечи через публичные API Bybit, Binance и Hyperliquid.
Для этого VPS и браузеру требуется доступ в интернет. Доступность и глубина
истории определяются каждой биржей, поэтому Dashboard не обещает бесконечную
историю или постоянную доступность API. Hyperliquid не предоставляет интервал
`6h` в текущей реализации.

Пустой EventStore — штатное начальное состояние: до первого импорта в нём нет
symbols и графиков.
