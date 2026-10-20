import { useState } from 'react';
import type { AgentCard as AgentCardType, Skill } from '../types/a2a';

interface Props {
  card: AgentCardType;
  onDisconnect: () => void;
  /** When true, omits the compact header — body is always visible */
  headerless?: boolean;
}

const KNOWN_KEYS = new Set([
  'name','description','url','secureUrl','version','skills',
  'capabilities','defaultInputModes','defaultOutputModes',
  'securityScheme','supportedInterfaces','extensions',
]);

function JsonTree({ value }: { value: unknown }) {
  const str = JSON.stringify(value, null, 2);
  const highlighted = str.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      if (/^"/.test(match)) {
        if (/:$/.test(match)) return `<span class="jt-key">${match}</span>`;
        return `<span class="jt-str">${match}</span>`;
      }
      if (/true|false/.test(match)) return `<span class="jt-bool">${match}</span>`;
      if (/null/.test(match)) return `<span class="jt-null">${match}</span>`;
      return `<span class="jt-num">${match}</span>`;
    }
  );
  return <pre className="card-json" dangerouslySetInnerHTML={{ __html: highlighted }} />;
}

function SkillDetail({ skill }: { skill: Skill }) {
  return (
    <div className="skill-detail">
      <div className="skill-detail-header">
        <span className="skill-detail-name">{skill.name}</span>
        {skill.tags?.map(t => <span key={t} className="skill-tag">{t}</span>)}
      </div>
      {skill.description && <p className="skill-detail-desc">{skill.description}</p>}
      <div className="skill-detail-modes">
        {skill.inputModes && <span className="skill-mode">↓ {skill.inputModes.join(', ')}</span>}
        {skill.outputModes && <span className="skill-mode">↑ {skill.outputModes.join(', ')}</span>}
      </div>
    </div>
  );
}

export function AgentCard({ card, onDisconnect, headerless }: Props) {
  const [expanded, setExpanded] = useState(false);
  const caps = card.capabilities ?? {};
  const extraEntries = Object.entries(card).filter(([k]) => !KNOWN_KEYS.has(k));

  const body = (
    <div className="agent-card-body">
      {card.description && (
        <div className="card-section">
          <span className="card-section-label">Description</span>
          <p className="card-section-text">{card.description}</p>
        </div>
      )}

      <div className="card-section card-section--row">
        <div className="card-kv">
          <span className="card-k">RPC Endpoint</span>
          <span className="card-v card-v--mono">{card.url}</span>
        </div>
        {card.secureUrl && (
          <div className="card-kv">
            <span className="card-k">Secure URL</span>
            <span className="card-v card-v--mono">{card.secureUrl}</span>
          </div>
        )}
      </div>

      {(card.defaultInputModes || card.defaultOutputModes) && (
        <div className="card-section card-section--row">
          {card.defaultInputModes && (
            <div className="card-kv">
              <span className="card-k">Input modes</span>
              <span className="card-v">{card.defaultInputModes.join(', ')}</span>
            </div>
          )}
          {card.defaultOutputModes && (
            <div className="card-kv">
              <span className="card-k">Output modes</span>
              <span className="card-v">{card.defaultOutputModes.join(', ')}</span>
            </div>
          )}
        </div>
      )}

      {card.skills && card.skills.length > 0 && (
        <div className="card-section">
          <span className="card-section-label">Skills ({card.skills.length})</span>
          <div className="skill-details-list">
            {card.skills.map(skill => <SkillDetail key={skill.id} skill={skill} />)}
          </div>
        </div>
      )}

      {card.securityScheme && (
        <div className="card-section">
          <span className="card-section-label">Security Scheme</span>
          <JsonTree value={card.securityScheme} />
        </div>
      )}

      {card.supportedInterfaces && (
        <div className="card-section">
          <span className="card-section-label">Supported Interfaces</span>
          <JsonTree value={card.supportedInterfaces} />
        </div>
      )}

      {card.extensions && (card.extensions as unknown[]).length > 0 && (
        <div className="card-section">
          <span className="card-section-label">Extensions</span>
          <JsonTree value={card.extensions} />
        </div>
      )}

      {extraEntries.length > 0 && (
        <div className="card-section">
          <span className="card-section-label">Extra fields</span>
          <JsonTree value={Object.fromEntries(extraEntries)} />
        </div>
      )}

      <div className="card-section">
        <span className="card-section-label">Full JSON</span>
        <JsonTree value={card} />
      </div>
    </div>
  );

  if (headerless) {
    return (
      <div className="agent-card agent-card--expanded agent-card--headerless">
        {body}
      </div>
    );
  }

  return (
    <div className={`agent-card${expanded ? ' agent-card--expanded' : ''}`}>
      <div className="agent-card-header">
        <div className="agent-card-info">
          <h2 className="agent-card-name">{card.name}</h2>
          {card.version && <span className="agent-card-version">v{card.version}</span>}
          {caps.streaming && <span className="badge badge--streaming">⚡ Streaming</span>}
          {caps.pushNotifications && <span className="badge badge--push">🔔 Push</span>}
          {caps.stateTransitionHistory && <span className="badge badge--history">📋 History</span>}
        </div>
        <div className="agent-card-actions">
          <button
            className="btn-toggle-card"
            onClick={() => setExpanded(v => !v)}
            title={expanded ? 'Collapse' : 'Expand details'}
          >
            {expanded ? '▲' : '▼'}
          </button>
          <button className="btn-disconnect" onClick={onDisconnect} title="Disconnect">✕</button>
        </div>
      </div>
      {expanded && body}
    </div>
  );
}
