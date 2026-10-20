interface Props {
  agentName: string;
  elapsed: number;    // ms elapsed since connection attempt started
  timeout: number;    // ms total timeout
  attempt: number;    // current attempt number (1-based)
  onCancel: () => void;
}

export function AgentStartupOverlay({ agentName, elapsed, timeout, attempt, onCancel }: Props) {
  const progress = Math.min((elapsed / timeout) * 100, 100);
  const remaining = Math.max(0, Math.ceil((timeout - elapsed) / 1000));

  return (
    <div className="agent-startup-wrap">
      <div className="agent-startup-card">
        <div className="agent-startup-spinner" />

        <p className="agent-startup-title">Connecting to agent…</p>

        <span className="agent-startup-name">{agentName}</span>

        <p className="agent-startup-hint">
          {attempt <= 1 ? 'Connecting…' : `Agent is starting up. Attempt ${attempt}…`}
        </p>

        <div className="agent-startup-progress-track">
          <div className="agent-startup-progress-fill" style={{ width: `${progress}%` }} />
        </div>

        <span className="agent-startup-countdown">{remaining}s</span>

        <button className="btn btn--ghost btn--sm" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
