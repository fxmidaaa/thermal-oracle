import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  ReferenceLine,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ResponsiveContainer,
} from "recharts";

import { api } from "./api";
import { Alert, Badge, Button, Card, CardContent, CardHeader, Spinner, Toggle } from "./components/ui";
import type {
  CurrentHealth,
  Domain,
  DomainHealth,
  MaintenanceSuggestion,
  MaintenanceType,
  RthHistory,
} from "./types";
import { MAINTENANCE_LABELS } from "./types";

const DAYS_OPTIONS = [7, 30, 90] as const;

const fmtRth = (v: number) => `${v.toFixed(3)} K/W`;
const fmtTime = (ms: number) =>
  new Date(ms).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const fmtDate = (iso: string) => new Date(iso).toLocaleDateString("ru-RU");

interface Props {
  deviceId: string;
  deviceName: string;
  token: string;
  onLogout: () => void;
}

export default function Dashboard({ deviceId, deviceName, token, onLogout }: Props) {
  const [current, setCurrent] = useState<CurrentHealth | null>(null);
  const [healthRows, setHealthRows] = useState<DomainHealth[]>([]);
  const [history, setHistory] = useState<RthHistory | null>(null);
  const [suggestions, setSuggestions] = useState<MaintenanceSuggestion[]>([]);
  const [domain, setDomain] = useState<Domain>("cpu");
  const [days, setDays] = useState<number>(30);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const [cur, health, hist, sugg] = await Promise.all([
        api.currentHealth(token, deviceId),
        api.health(token, deviceId),
        api.rthHistory(token, deviceId, domain, days),
        api.suggestions(token, deviceId),
      ]);
      setCurrent(cur);
      setHealthRows(health);
      setHistory(hist);
      setSuggestions(sugg);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token, deviceId, domain, days]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const domainCurrent = current?.domains.find((d) => d.domain === domain) ?? null;
  const baseline = healthRows.find((h) => h.domain === domain)?.rth_baseline ?? null;

  const chartPoints = useMemo(
    () =>
      (history?.points ?? []).map((p) => ({
        ts: Date.parse(p.window_start),
        rth: p.rth,
        p_tail: p.p_tail,
        stratum: p.stratum,
        quality: p.quality,
      })),
    [history],
  );

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-4 py-6 text-zinc-100">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">ThermalOracle</h1>
          <p className="text-sm text-zinc-500">{deviceName}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void reload()}>Обновить</Button>
          <Button variant="ghost" onClick={onLogout}>Выйти</Button>
        </div>
      </header>

      {error && (
        <Alert title="Ошибка запроса к API">
          {error} — проверьте, что бэкенд запущен, и обновите страницу.
        </Alert>
      )}

      {suggestions.map((s) => (
        <SuggestionCard key={s.id} suggestion={s} token={token} deviceId={deviceId} onConfirmed={reload} />
      ))}

      {/* ------------------------------------------------ KPI-карты ------ */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader title={`Rth (${domain.toUpperCase()})`} subtitle="тепловое сопротивление" />
          <CardContent>
            {domainCurrent?.rth_latest != null ? (
              <>
                <div className="text-3xl font-semibold tabular-nums">{fmtRth(domainCurrent.rth_latest)}</div>
                <p className="mt-1 text-xs text-zinc-500">
                  последняя честная точка
                  {domainCurrent.rth_current != null && (
                    <> · уровень 7 дней: {fmtRth(domainCurrent.rth_current)}</>
                  )}
                </p>
              </>
            ) : (
              <div className="text-sm text-zinc-500">Нет валидных точек — дайте нагрузку ≥ 35 Вт</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader title="T_ambient" subtitle="опорный idle-режим устройства" />
          <CardContent>
            {current?.t_ambient != null ? (
              <>
                <div className="text-3xl font-semibold tabular-nums">{current.t_ambient.toFixed(1)} °C</div>
                <p className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
                  <ConfidenceBadge value={current.ambient_confidence} />
                  {current.ambient_day && <>оценка за {fmtDate(current.ambient_day)}</>}
                </p>
              </>
            ) : (
              <div className="text-sm text-zinc-500">
                Нет калибровки — оставьте машину в простое на 20 минут
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader title="Health Score" subtitle="состояние термоинтерфейса" />
          <CardContent>
            {domainCurrent?.health_score != null ? (
              <>
                <div className={`text-3xl font-semibold tabular-nums ${scoreColor(domainCurrent.health_score)}`}>
                  {domainCurrent.health_score}
                  <span className="text-base font-normal text-zinc-500"> / 100</span>
                </div>
                <p className="mt-1 text-xs text-zinc-500">
                  {domainCurrent.days_to_critical != null
                    ? `прогноз троттлинга через ~${domainCurrent.days_to_critical} дн.`
                    : "троттлинг не прогнозируется"}
                </p>
              </>
            ) : (
              <>
                <div className="flex items-center gap-2 text-zinc-300">
                  <Spinner label="Идёт калибровка данных…" />
                </div>
                <p className="mt-2 text-xs text-zinc-500">
                  Скор появится, когда наберётся базлайн эпохи (~14 дней наблюдений)
                </p>
                {domainCurrent?.data_quality && (
                  <p className="mt-1"><Badge variant="muted">{domainCurrent.data_quality}</Badge></p>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* ---------------------------------------------- график Rth ------- */}
      <Card>
        <div className="flex items-center justify-between px-5 pt-4">
          <CardHeaderInline
            title="История Rth"
            subtitle={
              history
                ? `${history.points.length} точек ≥ гейта q≥${history.quality_gate}`
                : undefined
            }
          />
          <div className="flex items-center gap-1">
            {(["cpu", "gpu"] as const).map((d) => (
              <Toggle key={d} active={domain === d} onClick={() => setDomain(d)}>
                {d.toUpperCase()}
              </Toggle>
            ))}
            <span className="mx-1 text-zinc-700">|</span>
            {DAYS_OPTIONS.map((d) => (
              <Toggle key={d} active={days === d} onClick={() => setDays(d)}>
                {d} дн
              </Toggle>
            ))}
          </div>
        </div>
        <CardContent className="pt-2">
          {chartPoints.length === 0 ? (
            <div className="flex h-64 items-center justify-center text-sm text-zinc-500">
              Нет точек за период — точки появляются после стабильной нагрузки ≥ 35 Вт
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={280}>
              <ScatterChart margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
                <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
                <XAxis
                  dataKey="ts"
                  type="number"
                  scale="time"
                  domain={["dataMin", "dataMax"]}
                  tickFormatter={fmtTime}
                  stroke="#52525b"
                  fontSize={11}
                />
                <YAxis
                  dataKey="rth"
                  type="number"
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) => v.toFixed(2)}
                  stroke="#52525b"
                  fontSize={11}
                  width={48}
                />
                <Tooltip content={<PointTooltip />} />
                {baseline != null && (
                  <ReferenceLine
                    y={baseline}
                    stroke="#f59e0b"
                    strokeDasharray="6 4"
                    label={{ value: "базлайн эпохи", fill: "#f59e0b", fontSize: 11, position: "insideTopRight" }}
                  />
                )}
                <Scatter data={chartPoints} fill="#38bdf8" fillOpacity={0.85} />
              </ScatterChart>
            </ResponsiveContainer>
          )}
          {baseline == null && chartPoints.length > 0 && (
            <p className="mt-1 text-xs text-zinc-600">
              Линия базлайна появится после ~14 дней наблюдений в эпохе
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------ детали --- */

function CardHeaderInline({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div>
      <div className="text-sm font-medium text-zinc-400">{title}</div>
      {subtitle && <div className="text-xs text-zinc-500">{subtitle}</div>}
    </div>
  );
}

function scoreColor(score: number): string {
  if (score >= 80) return "text-emerald-400"; // зоны architecture.md §5.5
  if (score >= 50) return "text-amber-400";
  return "text-red-400";
}

function ConfidenceBadge({ value }: { value: number | null }) {
  if (value == null) return null;
  const pct = `${Math.round(value * 100)}%`;
  if (value >= 0.6) return <Badge variant="ok">стабильная опора · {pct}</Badge>;
  if (value >= 0.3) return <Badge variant="warn">средняя уверенность · {pct}</Badge>;
  return <Badge variant="bad">низкая уверенность · {pct}</Badge>;
}

interface TooltipProps {
  active?: boolean;
  payload?: Array<{ payload: { ts: number; rth: number; p_tail: number; stratum: string; quality: number } }>;
}

function PointTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs shadow-lg">
      <div className="font-medium text-zinc-200">{fmtRth(p.rth)}</div>
      <div className="mt-1 space-y-0.5 text-zinc-400">
        <div>{fmtTime(p.ts)}</div>
        <div>P хвоста: {p.p_tail.toFixed(1)} Вт · {p.stratum}</div>
        <div>качество: {p.quality.toFixed(2)}</div>
      </div>
    </div>
  );
}

function SuggestionCard({
  suggestion,
  token,
  deviceId,
  onConfirmed,
}: {
  suggestion: MaintenanceSuggestion;
  token: string;
  deviceId: string;
  onConfirmed: () => Promise<void> | void;
}) {
  const [kind, setKind] = useState<MaintenanceType>("dust_cleaning");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      // performed_at = день ступеньки: попадает в ±3-дневное окно, которое
      // закрывает предложение на бэкенде
      await api.confirmMaintenance(token, deviceId, {
        maintenance_type: kind,
        performed_at: suggestion.suggested_at,
        notes: "Подтверждено из дашборда по CUSUM-предложению",
      });
      await onConfirmed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Alert title="Оракул рекомендует зафиксировать обслуживание">
      <p>
        {suggestion.note ?? "Обнаружена смена теплового режима."}{" "}
        <span className="text-amber-300/70">({fmtDate(suggestion.suggested_at)})</span>
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as MaintenanceType)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm text-zinc-200"
        >
          {(Object.keys(MAINTENANCE_LABELS) as MaintenanceType[]).map((k) => (
            <option key={k} value={k}>{MAINTENANCE_LABELS[k]}</option>
          ))}
        </select>
        <Button onClick={() => void confirm()} disabled={busy}>
          {busy ? "Сохраняю…" : "Подтвердить"}
        </Button>
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
    </Alert>
  );
}
