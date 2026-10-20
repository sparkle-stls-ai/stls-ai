import { useState, useEffect } from 'react';
import type { Theme } from '../hooks/useSettings';

interface Props {
  proxyUrl: string;
  noProxy: string;
  onSave: (proxyUrl: string, noProxy: string) => void;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  /** Tool-call visibility toggle (production only): row hidden when not provided */
  showTools?: boolean;
  onShowToolsChange?: (show: boolean) => void;
  /** Suggested-action chips toggle (production only) */
  showSuggestions?: boolean;
  onShowSuggestionsChange?: (show: boolean) => void;
  /** Integrating app name (app auth mode only): row hidden when not provided */
  appName?: string | null;
}

const IS_DEV = import.meta.env.DEV;
const APP_VERSION = __APP_VERSION__;

const THEME_OPTIONS: { value: Theme; label: string }[] = [
  { value: 'dark', label: '🌙 Dark' },
  { value: 'light', label: '☀️ Light' },
  { value: 'system', label: '🖥️ System' },
];

export function SettingsBar({ proxyUrl, noProxy, onSave, theme, onThemeChange, showTools, onShowToolsChange, showSuggestions, onShowSuggestionsChange, appName }: Props) {
  const [draftProxy, setDraftProxy] = useState(proxyUrl);
  const [draftNoProxy, setDraftNoProxy] = useState(noProxy);

  // Sync from external (e.g. first load from localStorage)
  useEffect(() => setDraftProxy(proxyUrl), [proxyUrl]);
  useEffect(() => setDraftNoProxy(noProxy), [noProxy]);

  const isDirty = draftProxy.trim() !== proxyUrl || draftNoProxy.trim() !== noProxy;
  const isActive = !!proxyUrl;

  const save = () => onSave(draftProxy.trim(), draftNoProxy.trim());
  const clear = () => { setDraftProxy(''); setDraftNoProxy(''); onSave('', ''); };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
  };

  return (
    <div className="settings-bar">

      {/* Theme */}
      <div className="settings-row">
        <label className="settings-label">🎨 Theme</label>
        <div className="theme-switcher">
          {THEME_OPTIONS.map(opt => (
            <button
              key={opt.value}
              type="button"
              className={`theme-switcher-btn ${theme === opt.value ? 'theme-switcher-btn--active' : ''}`}
              onClick={() => onThemeChange(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tool calls visibility */}
      {onShowToolsChange && (
        <div className="settings-row">
          <label className="settings-label">🛠 Tools</label>
          <div className="theme-switcher">
            {([[true, 'Show'], [false, 'Hide']] as const).map(([value, label]) => (
              <button
                key={label}
                type="button"
                className={`theme-switcher-btn ${showTools === value ? 'theme-switcher-btn--active' : ''}`}
                onClick={() => onShowToolsChange(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Suggested actions */}
      {onShowSuggestionsChange && (
        <div className="settings-row">
          <label className="settings-label">💡 Suggestions</label>
          <div className="theme-switcher">
            {([[true, 'Show'], [false, 'Hide']] as const).map(([value, label]) => (
              <button
                key={label}
                type="button"
                className={`theme-switcher-btn ${showSuggestions === value ? 'theme-switcher-btn--active' : ''}`}
                onClick={() => onShowSuggestionsChange(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Proxy URL */}
      <div className="settings-row">
        <label className="settings-label">🌐 HTTP Proxy</label>
        <div className="settings-input-wrap">
          <input
            className="settings-proxy-input"
            type="url"
            placeholder="http://proxy.example.com:8080"
            value={draftProxy}
            onChange={e => setDraftProxy(e.target.value)}
            onKeyDown={handleKeyDown}
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        {isActive && !isDirty && (
          <span className="settings-active-badge">active</span>
        )}
      </div>

      {/* No Proxy */}
      <div className="settings-row">
        <label className="settings-label">🚫 No Proxy</label>
        <div className="settings-input-wrap">
          <input
            className="settings-proxy-input"
            type="text"
            placeholder="localhost, 127.0.0.1, .internal"
            value={draftNoProxy}
            onChange={e => setDraftNoProxy(e.target.value)}
            onKeyDown={handleKeyDown}
            spellCheck={false}
            autoComplete="off"
            disabled={!draftProxy}
          />
        </div>
      </div>

      {/* App version */}
      <div className="settings-row">
        <label className="settings-label">ℹ️ Version</label>
        <span className="settings-hint">v{APP_VERSION}</span>
      </div>

      {/* Integrating app (app auth mode) */}
      {appName && (
        <div className="settings-row">
          <label className="settings-label">🔌 App</label>
          <span className="settings-hint">{appName}</span>
        </div>
      )}

      {/* Actions + hint */}
      <div className="settings-footer">
        <p className="settings-hint">
          {IS_DEV
            ? 'Proxy settings are applied server-side via the Vite proxy. "No proxy" field: comma-separated hostnames, supports .suffix wildcards.'
            : '⚠ No effect in production builds — configure your system or agent-side proxy.'}
        </p>
        <div className="settings-actions">
          {isActive && !isDirty && (
            <button className="btn btn--ghost btn--sm" onClick={clear}>
              Clear all
            </button>
          )}
          {isDirty && (
            <button className="btn btn--primary btn--sm" onClick={save}>
              Save
            </button>
          )}
        </div>
      </div>

    </div>
  );
}
