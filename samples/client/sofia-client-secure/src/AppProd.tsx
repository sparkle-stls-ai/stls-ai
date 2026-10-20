import { useState, useCallback, useRef, useEffect } from 'react';
import { MessageList } from './components/MessageList';
import { InputArea } from './components/InputArea';
import { AgentCard } from './components/AgentCard';
import { AgentSelector, type AgentOption } from './components/AgentSelector';
import { AgentStartupOverlay } from './components/AgentStartupOverlay';
import { LoginForm } from './components/LoginForm';
import { ErrorToast } from './components/ErrorToast';
import { SettingsBar } from './components/SettingsBar';
import { useAuth } from './hooks/useAuth';
import { useChatProd } from './hooks/useChatProd';
import { useSuggestions } from './hooks/useSuggestions';
import { useSettings } from './hooks/useSettings';
import { fetchAgentCardProd, connectAgentSession, disconnectAgentSession, heartbeatAgentSession } from './api/prodClient';
import type { AgentCard as AgentCardType } from './types/a2a';
import type { Part } from './types/a2a';

const CONNECT_TIMEOUT_MS = 60_000;
const DEFAULT_INTRO_MESSAGE = 'Please introduce yourself.';
const CONNECT_RETRY_MS   = 5_000;

export function AppProd() {
  const auth = useAuth();
  const { proxyUrl, noProxy, theme, update } = useSettings();

  const [streaming, setStreaming] = useState(true);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  // agentCard is optional extra info; the chip uses selectedAgent as primary key
  const [agentCard, setAgentCard] = useState<AgentCardType | null>(null);
  const [showAgentCard, setShowAgentCard] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [toastError, setToastError] = useState<string | null>(null);
  const [sessionExpiredMsg, setSessionExpiredMsg] = useState<string | null>(null);

  const [agentSessionId, setAgentSessionId] = useState<string | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Agent cold-start connection state
  const [isAgentConnecting, setIsAgentConnecting] = useState(false);
  const [connectElapsed, setConnectElapsed] = useState(0);
  const [connectAttempt, setConnectAttempt] = useState(0);
  const connectAbortRef = useRef<AbortController | null>(null);

  const showError = useCallback((msg: string) => setToastError(msg), []);

  const handleSessionExpired = useCallback((authMode: string) => {
    if (authMode === 'app') {
      setSessionExpiredMsg('Session expired');
    } else {
      auth.refresh();
    }
  }, [auth]);

  const { messages, isBusy, sendParts, clearMessages } = useChatProd({
    agentName: selectedAgent,
    sessionId: agentSessionId,
    streaming,
    onError: showError,
    onSessionExpired: handleSessionExpired,
  });

  // Tool-call visibility: initial value from sf_agents_config.show_tools, then user-toggleable
  const [showTools, setShowTools] = useState(true);
  // Suggested-action chips: same pattern (sf_agents_config.show_suggestions)
  const [showSuggestions, setShowSuggestions] = useState(true);

  // Intro message to auto-send once the agent is connected (set by handleSelectAgent)
  const [pendingIntro, setPendingIntro] = useState<string | null>(null);
  useEffect(() => {
    // agentSessionId is required: in the secure client sendParts is a no-op without a key session
    if (pendingIntro && selectedAgent && agentSessionId && !isAgentConnecting && !isBusy) {
      const text = pendingIntro;
      setPendingIntro(null);
      void sendParts([{ kind: 'text', text }], { silent: true });
    }
  }, [pendingIntro, selectedAgent, agentSessionId, isAgentConnecting, isBusy, sendParts]);

  const suggestions = useSuggestions({
    messages, isBusy, enabled: showSuggestions && !!selectedAgent && !isAgentConnecting, agentName: selectedAgent,
  });

  // Heartbeat: keep the key session alive while an agent is connected.
  useEffect(() => {
    if (!agentSessionId) return;
    const HEARTBEAT_MS = 30_000;
    heartbeatRef.current = setInterval(() => {
      heartbeatAgentSession(agentSessionId);
    }, HEARTBEAT_MS);
    return () => {
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    };
  }, [agentSessionId]);

  const handleSelectAgent = useCallback(async (name: string, agent?: AgentOption) => {
    setPendingIntro(null);
    setShowTools(agent?.show_tools ?? true);
    setShowSuggestions(agent?.show_suggestions ?? true);
    // Cancel any previous connection attempt
    connectAbortRef.current?.abort();
    const controller = new AbortController();
    connectAbortRef.current = controller;

    setSelectedAgent(name);
    setAgentCard(null);
    setShowAgentCard(false);
    setIsAgentConnecting(true);
    setConnectElapsed(0);
    setConnectAttempt(0);

    const startTime = Date.now();

    // Smooth progress bar ticker (updates every 250ms)
    const ticker = setInterval(() => {
      setConnectElapsed(Date.now() - startTime);
    }, 250);

    try {
      let attempt = 0;
      let connected = false;

      while (!controller.signal.aborted) {
        attempt++;
        setConnectAttempt(attempt);
        try {
          // null = agent reachable but no card endpoint — still counts as connected
          const card = await fetchAgentCardProd(name);
          if (!controller.signal.aborted) {
            // Initialise the Python key session for this agent connection.
            const sid = await connectAgentSession(name);
            setAgentSessionId(sid);
            if (card) setAgentCard(card);
            connected = true;
            if (agent?.intro_enabled) {
              setPendingIntro(agent.intro_message?.trim() || DEFAULT_INTRO_MESSAGE);
            }
          }
          break;
        } catch {
          const elapsed = Date.now() - startTime;
          if (elapsed >= CONNECT_TIMEOUT_MS || controller.signal.aborted) break;
          // Wait before next attempt, abort early if cancelled
          await new Promise<void>(resolve => {
            const t = setTimeout(resolve, CONNECT_RETRY_MS);
            controller.signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
          });
        }
      }

      // Timeout expired without a response — reset to selector and surface error
      if (!controller.signal.aborted && !connected) {
        setSelectedAgent(null);
        showError('Connection failed: agent did not respond within 60 seconds.');
      }
    } finally {
      clearInterval(ticker);
      if (!controller.signal.aborted) {
        setIsAgentConnecting(false);
        setConnectElapsed(0);
        setConnectAttempt(0);
      }
    }
  }, []);

  const handleDisconnectAgent = useCallback(() => {
    connectAbortRef.current?.abort();
    setIsAgentConnecting(false);
    setConnectElapsed(0);
    setConnectAttempt(0);
    // Stop heartbeat and tear down the Python key session (best-effort, non-blocking).
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    if (agentSessionId) disconnectAgentSession(agentSessionId);
    setAgentSessionId(null);
    setSelectedAgent(null);
    setAgentCard(null);
    setShowAgentCard(false);
    setPendingIntro(null);
  }, [selectedAgent]);

  const handleSend = async (parts: Part[]) => { await sendParts(parts); };

  // ── User auth mode: show login if not authenticated ──────────────────────
  if (auth.authMode === 'user' && !auth.loading && !auth.authenticated) {
    return <LoginForm onLogin={auth.login} />;
  }

  // ── Loading skeleton ──────────────────────────────────────────────────────
  if (auth.loading) {
    return (
      <div className="app" style={{ justifyContent: 'center', alignItems: 'center' }}>
        <div className="send-spinner" style={{ width: 24, height: 24 }} />
      </div>
    );
  }

  // ── Session expired overlay (app-to-app mode) ─────────────────────────────
  if (sessionExpiredMsg) {
    return (
      <div className="login-overlay">
        <div className="login-card" style={{ textAlign: 'center', gap: 16 }}>
          <span style={{ fontSize: 32 }}>⏱</span>
          <p style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>Session Expired</p>
          <p style={{ fontSize: 14, color: 'var(--muted)' }}>
            Your session has expired due to inactivity. Please reload the page.
          </p>
          <button className="btn btn--primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }

  // ── app_token → session exchange failed (app-to-app mode) ─────────────────
  if (auth.authMode === 'app' && !auth.authenticated && auth.exchangeError) {
    return (
      <div className="login-overlay">
        <div className="login-card" style={{ textAlign: 'center', gap: 16 }}>
          <span style={{ fontSize: 32 }}>⚠</span>
          <p style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>Unable to sign in</p>
          <p style={{ fontSize: 14, color: 'var(--muted)' }}>{auth.exchangeError}</p>
        </div>
      </div>
    );
  }

  // The chip label: prefer agentCard.name (pretty name from agent), fall back to selectedAgent key
  const chipLabel = agentCard?.name ?? selectedAgent;
  const chipVersion = agentCard?.version;

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-logo">
          <span className="app-logo-icon">✦</span>
          <span className="app-logo-text">Sofia Secure Chat</span>
        </div>

        <div className="app-header-actions">
          {/* Chip appears as soon as an agent is selected, with or without card */}
          {selectedAgent && (
            <button
              className={`header-agent-chip${showAgentCard ? ' header-agent-chip--active' : ''}`}
              onClick={() => setShowAgentCard(v => !v)}
              title={agentCard ? `${chipLabel}${chipVersion ? ` v${chipVersion}` : ''} — agent details` : selectedAgent}
            >
              <span className="header-agent-dot" />
              <span className="header-agent-name">{chipLabel}</span>
              {chipVersion && <span className="header-agent-version">v{chipVersion}</span>}
              {agentCard && <span className="header-agent-chevron">{showAgentCard ? '▲' : '▼'}</span>}
            </button>
          )}

          {selectedAgent && (
            <button className="btn--disconnect" onClick={handleDisconnectAgent} title="Disconnect agent">✕</button>
          )}

          {auth.authenticated && auth.userId && (
            <span
              className="header-user-badge"
              title={`${auth.appName ? `${auth.appName} / ` : ''}${auth.userId}
Profile: ${JSON.stringify(auth.profile ?? {})}`}
            >
              {auth.userId}
            </span>
          )}

          <button
            className={`btn btn--ghost btn--icon${showSettings ? ' btn--active' : ''}`}
            onClick={() => setShowSettings(s => !s)}
            title="Settings"
          >
            ⚙️{proxyUrl && <span className="proxy-dot" title={`Proxy: ${proxyUrl}`} />}
          </button>

          {messages.length > 0 && (
            <button className="btn btn--ghost btn--clear" onClick={clearMessages}>
              🗑 Clear
            </button>
          )}

          {auth.authenticated && auth.authMode === 'user' && (
            <button className="btn btn--ghost btn--clear" onClick={auth.logout} title="Sign out">
              Sign out
            </button>
          )}
        </div>
      </header>

      <div className="app-body">
        {/* Agent selector bar — shows when no agent is connected yet */}
        {!selectedAgent && (
          <div className="prod-agent-bar">
            <AgentSelector
              selectedAgent={selectedAgent}
              onSelect={handleSelectAgent}
              disabled={isBusy}
            />
            <button
              className={`btn btn--stream ${streaming ? 'btn--stream-on' : ''}`}
              onClick={() => setStreaming(s => !s)}
              title={streaming ? 'Streaming on' : 'Streaming off'}
            >
              ⚡ {streaming ? 'Stream ON' : 'Stream OFF'}
            </button>
          </div>
        )}

        {showSettings && (
          <SettingsBar
            proxyUrl={proxyUrl}
            noProxy={noProxy}
            onSave={(p, n) => update({ proxyUrl: p, noProxy: n })}
            theme={theme}
            onThemeChange={t => update({ theme: t })}
            showTools={showTools}
            onShowToolsChange={setShowTools}
            showSuggestions={showSuggestions}
            onShowSuggestionsChange={setShowSuggestions}
            appName={auth.appName}
          />
        )}

        {/* Agent detail panel — only when card is available and expanded */}
        {agentCard && showAgentCard && (
          <AgentCard card={agentCard} onDisconnect={handleDisconnectAgent} headerless />
        )}

        {isAgentConnecting ? (
          <AgentStartupOverlay
            agentName={selectedAgent!}
            elapsed={connectElapsed}
            timeout={CONNECT_TIMEOUT_MS}
            attempt={connectAttempt}
            onCancel={handleDisconnectAgent}
          />
        ) : (
          <>
            <MessageList
              messages={messages}
              showTools={showTools}
              suggestions={suggestions}
              onSuggestion={text => { void sendParts([{ kind: 'text', text }]); }}
            />
            <InputArea
              onSend={handleSend}
              isBusy={isBusy}
              disabled={!selectedAgent}
            />
          </>
        )}
      </div>

      <ErrorToast message={toastError} onDismiss={() => setToastError(null)} />
    </div>
  );
}
