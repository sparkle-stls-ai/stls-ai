import { useState, useCallback } from 'react';
import { ConnectBar } from './components/ConnectBar';
import { AgentCard } from './components/AgentCard';
import { MessageList } from './components/MessageList';
import { InputArea } from './components/InputArea';
import { ErrorToast } from './components/ErrorToast';
import { SettingsBar } from './components/SettingsBar';
import { useAgent } from './hooks/useAgent';
import { useChat } from './hooks/useChat';
import { useSuggestions } from './hooks/useSuggestions';
import { useSettings } from './hooks/useSettings';
import type { Part } from './types/a2a';

export function AppDebug() {
  const [streaming, setStreaming] = useState(true);
  const [toastError, setToastError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showAgentCard, setShowAgentCard] = useState(false);

  const showError = useCallback((msg: string) => setToastError(msg), []);
  const { proxyUrl, noProxy, theme, update } = useSettings();
  const { card, agentRpcUrl, apiKey, sessionId, isConnecting, connect, disconnect } = useAgent({ onError: showError });
  const { messages, isBusy, sendParts, clearMessages } = useChat({
    agentRpcUrl, sessionId, streaming, apiKey, onError: showError,
  });

  // Debug mode: suggestions always on (needs the Express backend; silently no chips otherwise)
  const suggestions = useSuggestions({ messages, isBusy, enabled: !!card });

  const handleConnect = (url: string, key: string) => connect(url, key, proxyUrl || undefined, noProxy || undefined);
  const handleDisconnect = () => { disconnect(); setShowAgentCard(false); };
  const handleSend = async (parts: Part[]) => { await sendParts(parts); };

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-logo">
          <span className="app-logo-icon">✦</span>
          <span className="app-logo-text">Sofia Secure Chat</span>
          <span className="debug-badge">DEBUG</span>
        </div>
        <div className="app-header-actions">
          {card && (
            <button
              className={`header-agent-chip${showAgentCard ? ' header-agent-chip--active' : ''}`}
              onClick={() => setShowAgentCard(v => !v)}
              title="Agent details"
            >
              <span className="header-agent-dot" />
              <span className="header-agent-name">{card.name}</span>
              {card.version && <span className="header-agent-version">v{card.version}</span>}
              <span className="header-agent-chevron">{showAgentCard ? '▲' : '▼'}</span>
            </button>
          )}
          {card && <button className="btn--disconnect" onClick={handleDisconnect} title="Disconnect">✕</button>}
          <button
            className={`btn btn--ghost btn--icon${showSettings ? ' btn--active' : ''}`}
            onClick={() => setShowSettings(s => !s)}
            title="Settings"
          >
            ⚙️{proxyUrl && <span className="proxy-dot" title={`Proxy: ${proxyUrl}`} />}
          </button>
          {messages.length > 0 && (
            <button className="btn btn--ghost btn--clear" onClick={clearMessages}>🗑 Clear</button>
          )}
        </div>
      </header>

      <div className="app-body">
        {!card && (
          <ConnectBar
            isConnecting={isConnecting}
            isConnected={!!card}
            streaming={streaming}
            onConnect={handleConnect}
            onToggleStreaming={() => setStreaming(s => !s)}
          />
        )}
        {showSettings && (
          <SettingsBar
            proxyUrl={proxyUrl}
            noProxy={noProxy}
            onSave={(p, n) => update({ proxyUrl: p, noProxy: n })}
            theme={theme}
            onThemeChange={t => update({ theme: t })}
          />
        )}
        {card && showAgentCard && (
          <AgentCard card={card} onDisconnect={handleDisconnect} headerless />
        )}
        <MessageList
          messages={messages}
          suggestions={suggestions}
          onSuggestion={text => { void sendParts([{ kind: 'text', text }]); }}
        />
        <InputArea onSend={handleSend} isBusy={isBusy} disabled={!agentRpcUrl} />
      </div>

      <ErrorToast message={toastError} onDismiss={() => setToastError(null)} />
    </div>
  );
}
