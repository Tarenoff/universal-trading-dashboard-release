# Portable Auto Sync

Portable Dashboard и Auto Sync остаются отдельными процессами. В установленной
версии используйте `universal-trading-dashboard` и
`universal-trading-dashboard-sync`, передавая им одинаковые `--events-path` и
`--external-sources-config`.

Для Linux VPS не запускайте worker вручную и не используйте `PYTHONPATH=src`:
создайте два user-level systemd unit из
[INSTALLATION_RU.md](INSTALLATION_RU.md)
и запускайте основной unit:

```bash
systemctl --user enable --now universal-trading-dashboard.service
```

Auto Sync читает только источники, для которых пользователь явно включил
`Auto Sync` в Settings; ручной Import остаётся отдельной операцией. При
выключенном Auto Sync у всех sources worker не открывает внешние SQLite-базы
для импорта. Он постоянно перечитывает конфигурацию, изолирует ошибку одного
источника от остальных и использует общие с ручным Import блокировки. Защита от
второго worker для той же пары EventStore/configuration сохраняется.
