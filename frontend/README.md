# ThermalOracle Dashboard

SPA-дашборд: Vite + React 19 + TypeScript + Tailwind v4 + Recharts.
Логин → выбор устройства → KPI-карты (Rth, T_ambient, Health Score),
скаттер истории Rth с линией базлайна эпохи, карточки CUSUM-предложений
с подтверждением обслуживания.

Формы ответов API зеркалят `backend/app/schemas/devices.py` (см. `src/types.ts`);
при изменении схем обновлять оба места.

## Запуск (dev)

Бэкенд должен быть поднят (`docker compose -f infra/docker-compose.yml up -d`).
CORS не нужен: dev-сервер проксирует `/api` на бэкенд (см. `vite.config.ts`).

С установленным Node 22+:

```powershell
cd frontend
npm install
npm run dev          # http://localhost:5173, прокси на 127.0.0.1:8000
```

Без Node — в докере, на одной сети с бэкендом (порт 5173 у Windows бывает
зарезервирован — поэтому 8090):

```powershell
docker run --rm -it --name thermal-fe-dev --network infra_default `
  -e THERMAL_API_TARGET=http://api:8000 `
  -v "$PWD\frontend:/app" -v thermal_fe_modules:/app/node_modules -w /app `
  -p 8090:8090 node:22-alpine sh -c "npm install && npx vite --port 8090"
```

`thermal_fe_modules` — именованный docker-volume: node_modules не попадает
в рабочую папку (OneDrive не должен синхронизировать npm-мусор).

## Сборка

```powershell
npm run build        # tsc --noEmit && vite build → dist/
```
