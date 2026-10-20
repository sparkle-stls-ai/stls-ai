import { useState, useCallback, useEffect } from 'react';

const STORAGE_KEY = 'sofia_settings_v1';

export type Theme = 'dark' | 'light' | 'system';

interface Settings {
  proxyUrl: string;
  noProxy: string;
  theme: Theme;
}

const DEFAULTS: Settings = { proxyUrl: '', noProxy: '', theme: 'dark' };

function load(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {}
  return DEFAULTS;
}

function resolveTheme(theme: Theme, systemPrefersLight: boolean): 'dark' | 'light' {
  return theme === 'system' ? (systemPrefersLight ? 'light' : 'dark') : theme;
}

// Applies the resolved theme to <html data-theme="…">. index.html has an
// inline script that does the same thing synchronously before first paint
// (to avoid a flash of the wrong theme); this keeps it in sync afterwards —
// when the user changes the setting, and live when the OS theme changes
// while the preference is "system".
function applyTheme(theme: Theme, systemPrefersLight: boolean): void {
  document.documentElement.setAttribute('data-theme', resolveTheme(theme, systemPrefersLight));
}

interface UseSettingsReturn {
  proxyUrl: string;
  noProxy: string;
  theme: Theme;
  update: (patch: Partial<Settings>) => void;
}

export function useSettings(): UseSettingsReturn {
  const [settings, setSettings] = useState<Settings>(load);

  const update = useCallback((patch: Partial<Settings>) => {
    setSettings(prev => {
      const next = { ...prev, ...patch };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  // Keep <html data-theme> in sync with the current preference, and — only
  // while the preference is "system" — with live OS theme changes too.
  useEffect(() => {
    const mql = window.matchMedia('(prefers-color-scheme: light)');
    applyTheme(settings.theme, mql.matches);

    if (settings.theme !== 'system') return;
    const onChange = (e: MediaQueryListEvent) => applyTheme('system', e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [settings.theme]);

  return { proxyUrl: settings.proxyUrl, noProxy: settings.noProxy, theme: settings.theme, update };
}
