import { useEffect, useState, type FormEvent } from "react";

import { api, clearToken, getToken, setToken } from "./api";
import Dashboard from "./Dashboard";
import { Button, Card, CardContent, CardHeader, Spinner } from "./components/ui";
import type { DeviceOut } from "./types";

export default function App() {
  const [token, setTokenState] = useState<string | null>(getToken());
  const [devices, setDevices] = useState<DeviceOut[] | null>(null);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    api
      .devices(token)
      .then((list) => {
        setDevices(list);
        if (list.length === 1) setDeviceId(list[0].id); // одно устройство — без выбора
      })
      .catch(() => {
        // протухший/чужой токен — честно на форму логина
        clearToken();
        setTokenState(null);
        setDevices(null);
      });
  }, [token]);

  const logout = () => {
    clearToken();
    setTokenState(null);
    setDevices(null);
    setDeviceId(null);
  };

  if (!token) {
    return (
      <LoginForm
        error={error}
        onSubmit={async (email, password) => {
          setError(null);
          try {
            const { access_token } = await api.login(email, password);
            setToken(access_token);
            setTokenState(access_token);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          }
        }}
      />
    );
  }

  if (devices === null) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Загружаю устройства…" />
      </div>
    );
  }

  if (deviceId === null) {
    return (
      <div className="mx-auto max-w-md px-4 py-16">
        <Card>
          <CardHeader title="Выберите устройство" />
          <CardContent className="space-y-2">
            {devices.length === 0 && (
              <p className="text-sm text-zinc-500">
                Устройств нет — сопрягите агента: thermal-agent pair
              </p>
            )}
            {devices.map((d) => (
              <Button
                key={d.id}
                variant="outline"
                className="w-full justify-between"
                onClick={() => setDeviceId(d.id)}
              >
                <span>{d.name}</span>
                <span className="text-xs text-zinc-500">{d.platform}</span>
              </Button>
            ))}
            <Button variant="ghost" className="w-full" onClick={logout}>Выйти</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const device = devices.find((d) => d.id === deviceId);
  return (
    <Dashboard
      deviceId={deviceId}
      deviceName={device?.name ?? deviceId}
      token={token}
      onLogout={logout}
    />
  );
}

function LoginForm({
  error,
  onSubmit,
}: {
  error: string | null;
  onSubmit: (email: string, password: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await onSubmit(email, password);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-sm px-4 py-24">
      <Card>
        <CardHeader title="ThermalOracle" subtitle="вход в дашборд" />
        <CardContent>
          <form onSubmit={(e) => void submit(e)} className="space-y-3">
            <input
              type="email"
              required
              placeholder="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600"
            />
            <input
              type="password"
              required
              placeholder="пароль"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600"
            />
            {error && <p className="text-xs text-red-400">{error}</p>}
            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Вхожу…" : "Войти"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
