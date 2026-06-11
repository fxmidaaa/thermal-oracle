# Live smoke-тест: от `compose up` до первой Rth-точки

Чек-лист первого прогона на реальном железе (Legion, i9-13900HX + RTX 4070).
Команды — PowerShell, из корня репозитория.

## 0. Предусловия

- [x] Docker Desktop запущен.
- [x] LibreHardwareMonitor запущен **от администратора**, Options → Remote Web
  Server → Run (порт 8085).
- [ ] На время теста отключить сон ноутбука (Параметры → Питание): уход в сон
  рвёт 15-минутный idle-эпизод, нужный для калибровки T_ambient.

## 1. Поднять серверный стек (одной командой)

```powershell
docker compose -f infra\docker-compose.yml up -d --build
```

Порядок автоматический: `timescaledb` (healthcheck) → `migrate` (one-shot,
7 миграций) → `api` + `worker`. Проверка:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz   # {"status":"ok","db":true}
docker logs infra-worker-1                         # worker.started, 5 джобов
```

## 2. Аккаунт и pairing-код

Swagger UI: **http://127.0.0.1:8000/docs**

1. `POST /api/v1/auth/register` → body `{"email": "...", "password": "..."}`
   (пароль ≥ 8 символов) → скопировать `access_token`.
2. Кнопка **Authorize** → вставить токен.
3. `POST /api/v1/devices/pairing-code` → получить код вида `67FA-HBPR`
   (живёт 10 минут, одноразовый).

## 3. Сопрячь и запустить агента

```powershell
cd agent; pip install -e .; cd ..
thermal-agent pair --code XXXX-XXXX --api-url http://127.0.0.1:8000
thermal-agent detect-sensors
thermal-agent run -v        # оставить работать; Ctrl+C — остановка
```

Ожидаемое для этого Legion в `detect-sensors`: cpu_temp = Core Max,
cpu_power = CPU Package, gpu_* = RTX 4070; **fan_rpm = n/a — это норма**:
LHM не читает EC-вентиляторы Lenovo. Аналитика это знает через capabilities;
диагностика «пыль vs паста» будет работать в ограниченном режиме (только по
росту Rth, без RPM-подтверждения).

## 4. Убедиться, что телеметрия течёт (1–2 минуты)

```powershell
thermal-agent status        # спул ~0 строк «в полёте» = батчи уходят
```

В Swagger: `GET /api/v1/devices` → `last_seen_at` обновляется каждые ~30 с.
График мощности за последние минуты: `GET /api/v1/devices/{id}/timeseries?bucket=1m`.

Прямо в БД:

```powershell
docker compose -f infra\docker-compose.yml exec timescaledb `
  psql -U postgres -d thermal -c "SELECT count(*), max(ts) FROM telemetry_raw"
```

## 5. Первая Rth-точка — сценарий измерения

Физика пайплайна: сначала калибровка среды (простой), потом нагрузка.

1. **15–20 минут не трогать ноутбук** (агент работает, экран можно не гасить).
   Это даст idle-эпизод для T_ambient: первые 10 минут отбрасываются
   (soak-back), по хвосту считается оценка.
2. Форсировать ambient-джоб (иначе ждать его часового тика):

   ```powershell
   docker compose -f infra\docker-compose.yml exec worker `
     python -m app.analytics.worker --once estimate_ambient
   docker compose -f infra\docker-compose.yml exec timescaledb `
     psql -U postgres -d thermal -c "SELECT * FROM ambient_estimates"
   ```

3. **Дать нагрузку ≥ 35 Вт на 2–5 минут**: Cinebench R23/R24, игра, стресс-тест.
   (Для i9-13900HX 35 Вт — это даже лёгкая нагрузка.)
4. Окна детектятся каждые 5 минут автоматически; форсировать:

   ```powershell
   docker compose -f infra\docker-compose.yml exec worker `
     python -m app.analytics.worker --once detect_windows
   ```

5. **Смотреть первую точку:**

   ```powershell
   docker compose -f infra\docker-compose.yml exec timescaledb `
     psql -U postgres -d thermal -c `
     "SELECT window_start, duration_s, p_tail, t_tail, t_ambient, rth, stratum, quality FROM rth_windows ORDER BY window_start DESC LIMIT 5"
   ```

   или в Swagger: `GET /api/v1/devices/{id}/trend?domain=cpu`.

   Санити-чек значения: для ноутбука Rth ≈ 0.6–1.5 K/W. Пример: 95 °C при
   100 Вт и комнате 25 °C → (95−25)/100 = 0.70 K/W.

6. Снапшот здоровья (с одним днём данных будет честный `sparse`):

   ```powershell
   docker compose -f infra\docker-compose.yml exec worker `
     python -m app.analytics.worker --once update_trends
   ```

   `GET /api/v1/devices/{id}/health`.

## 6. Если ambient_estimates пуст после простоя

Первым делом — диагностика (печатает эффективные пороги, профиль мощности,
скан порогов с длиной ранов и найденные эпизоды):

```powershell
cd backend; .venv\Scripts\python.exe -m app.cli diagnose-ambient --hours 6
```

Вероятная причина на i9-13900HX: idle package power 13–22 Вт (24 ядра +
Docker/WSL2 фоном) — «ультрабучный» дефолт 5 Вт недостижим. Все пороги
настраиваются per-device. **Точные имена ключей** (неизвестные ключи
игнорируются с warning в логе воркера):

| ключ | дефолт | смысл |
|---|---|---|
| `idle_power_w` | 5.0 | порог скользящего среднего, Вт |
| `idle_power_max_w` | 8.0 | мгновенный потолок (сэмплы выше исключаются из оценки) |
| `idle_grace_s` | 60 | выброс среднего над порогом ≤ этого не рвёт эпизод |
| `idle_min_duration_s` | 900 | минимальная длительность эпизода, с |
| `idle_discard_head_s` | 600 | отброс головы эпизода (soak-back), с |
| `idle_min_tail_s` | 300 | минимальный хвост после отброса, с |
| `ambient_clamp_high` | 45 | потолок оценки, °C — для горячего idle-профиля ПОДНЯТЬ |

Проверенный профиль для этого Legion (idle ~14–22 Вт, температура простоя
45–53 °C):

```powershell
docker compose -f infra\docker-compose.yml exec timescaledb psql -U postgres -d thermal -c "UPDATE devices SET analysis_overrides = '{\"idle_power_w\": 22.0, \"idle_power_max_w\": 28.0, \"idle_min_duration_s\": 300, \"idle_discard_head_s\": 180, \"idle_min_tail_s\": 120, \"ambient_clamp_high\": 60.0}'"
docker compose -f infra\docker-compose.yml exec worker python -m app.analytics.worker --once estimate_ambient
```

Важно понимать: при пороге 22 Вт оценка — это температура кристалла в
**опорном idle-режиме** (~47 °C на этой машине), а не комнатная. Для тренда
деградации это равноценная опора: важна её консистентность, а не абсолют.
Чем длиннее и спокойнее простой (ночь), тем выше confidence оценки.

## 7. Критерии успеха smoke-теста

- [ ] `healthz` ok; `migrate` exit 0; worker запустил 5 джобов.
- [ ] Агент сопряжён по коду, `run` шлёт батчи (спул не растёт).
- [ ] `telemetry_raw` пополняется, `timeseries` рисуется.
- [ ] Появилась строка в `ambient_estimates` (T_ambient ≈ комнатная +2–4 °C).
- [ ] Появились строки в `rth_windows` с правдоподобным Rth.
- [ ] `health` отдаёт снапшот (data_quality=`sparse` — норма для первого дня).

Дальше — просто жить с включённым агентом: через 10–14 дней появится базлайн,
тренд и первый осмысленный health score.

## Полезное

```powershell
docker compose -f infra\docker-compose.yml logs -f worker   # джобы в реальном времени
docker compose -f infra\docker-compose.yml down             # остановить (данные в volume)
docker compose -f infra\docker-compose.yml down -v          # остановить и СТЕРЕТЬ данные
```
