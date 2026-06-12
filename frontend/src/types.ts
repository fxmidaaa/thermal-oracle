/** Формы ответов — зеркало backend/app/schemas/devices.py (источник истины). */

export interface DeviceOut {
  id: string;
  name: string;
  platform: "windows" | "macos";
  device_class: "laptop" | "desktop";
  agent_version: string | null;
  last_seen_at: string | null;
  created_at: string;
}

export type Domain = "cpu" | "gpu";

/** 'ok' | 'sparse' | 'regime_change'; null — update_trends ещё не считал домен. */
export type DataQuality = string | null;

export interface DomainCurrent {
  domain: Domain;
  rth_latest: number | null; // последнее окно, прошедшее гейт качества
  rth_latest_at: string | null;
  rth_current: number | null; // 7-дневная медиана внутри эпохи
  health_score: number | null; // null, пока data_quality='sparse'
  data_quality: DataQuality;
  days_to_critical: number | null;
}

export interface CurrentHealth {
  t_ambient: number | null; // опорный idle-режим устройства, НЕ комната
  ambient_confidence: number | null;
  ambient_day: string | null;
  domains: DomainCurrent[];
}

export interface RthPoint {
  window_start: string;
  duration_s: number;
  rth: number;
  p_tail: number;
  stratum: string;
  quality: number;
  fan_rpm_avg: number | null;
}

export interface RthHistory {
  domain: string;
  days: number;
  quality_gate: number; // эффективный per-device гейт (float, напр. 0.5)
  points: RthPoint[];
}

/** GET /health — отсюда базлайн эпохи для линии-ориентира на графике. */
export interface DomainHealth {
  domain: Domain;
  computed_at: string;
  epoch_start: string | null;
  rth_baseline: number | null;
  rth_current: number | null;
  degradation_pct: number | null;
  slope_mkw_per_30d: number | null;
  slope_ci_low: number | null;
  slope_ci_high: number | null;
  forecast_throttle_date: string | null;
  days_to_critical: number | null;
  health_score: number | null;
  data_quality: string;
  diagnosis: string;
}

export interface MaintenanceSuggestion {
  id: number; // bigint журнала, не uuid
  suggested_at: string; // полночь дня ступеньки (локаль устройства)
  note: string | null; // «CUSUM: ступенька … подтвердите событие …»
}

export type MaintenanceType = "paste_replacement" | "dust_cleaning" | "repad";

export const MAINTENANCE_LABELS: Record<MaintenanceType, string> = {
  paste_replacement: "Замена термопасты",
  dust_cleaning: "Чистка от пыли",
  repad: "Замена термопрокладок",
};
