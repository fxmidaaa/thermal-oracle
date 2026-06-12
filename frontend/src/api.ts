import type {
  CurrentHealth,
  DeviceOut,
  DomainHealth,
  MaintenanceSuggestion,
  MaintenanceType,
  RthHistory,
} from "./types";

const TOKEN_KEY = "thermal_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* не-JSON ошибка — оставляем statusText */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; user_id: string }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  devices: (token: string) => request<DeviceOut[]>("/api/v1/devices", {}, token),

  currentHealth: (token: string, deviceId: string) =>
    request<CurrentHealth>(`/api/v1/devices/${deviceId}/current-health`, {}, token),

  health: (token: string, deviceId: string) =>
    request<DomainHealth[]>(`/api/v1/devices/${deviceId}/health`, {}, token),

  rthHistory: (token: string, deviceId: string, domain: string, days: number) =>
    request<RthHistory>(
      `/api/v1/devices/${deviceId}/rth-history?domain=${domain}&days=${days}`,
      {},
      token,
    ),

  suggestions: (token: string, deviceId: string) =>
    request<MaintenanceSuggestion[]>(
      `/api/v1/devices/${deviceId}/maintenance-suggestions`,
      {},
      token,
    ),

  confirmMaintenance: (
    token: string,
    deviceId: string,
    body: { maintenance_type: MaintenanceType; performed_at: string; notes?: string },
  ) =>
    request<unknown>(
      `/api/v1/devices/${deviceId}/maintenance`,
      { method: "POST", body: JSON.stringify(body) },
      token,
    ),
};
