import { useState, useEffect } from 'react';
import { apiBase } from '../api/apiBase';
import { apiFetch } from '../api/apiFetch';

export interface AgentOption {
  agent_name: string;
  url: string;
  intro_enabled?: boolean;
  intro_message?: string | null;
  show_tools?: boolean;
  show_suggestions?: boolean;
}

interface Props {
  selectedAgent: string | null;
  onSelect: (agentName: string, agent?: AgentOption) => void;
  disabled?: boolean;
}

export function AgentSelector({ selectedAgent, onSelect, disabled }: Props) {
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch(`${apiBase}/api/agents`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((data: AgentOption[]) => {
        setAgents(data);
        // Auto-select if only one agent
        if (data.length === 1 && !selectedAgent) onSelect(data[0].agent_name, data[0]);
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) return <div className="agent-selector-loading">Loading agents…</div>;
  if (error) return <div className="agent-selector-error">Failed to load agents: {error}</div>;
  if (!agents.length) return <div className="agent-selector-empty">No agents available for your account.</div>;

  return (
    <div className="agent-selector">
      <label className="agent-selector-label" htmlFor="agent-select">Agent</label>
      <select
        id="agent-select"
        className="agent-selector-select"
        value={selectedAgent ?? ''}
        onChange={e => onSelect(e.target.value, agents.find(a => a.agent_name === e.target.value))}
        disabled={disabled}
      >
        {!selectedAgent && <option value="" disabled>Select an agent…</option>}
        {agents.map(a => (
          <option key={a.agent_name} value={a.agent_name}>{a.agent_name}</option>
        ))}
      </select>
    </div>
  );
}
