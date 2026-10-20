import { useState } from 'react';
import type { ChatMessage, DataPart, Part } from '../types/a2a';
import { TextPartView } from './parts/TextPartView';
import { FilePartView } from './parts/FilePartView';
import { DataPartView } from './parts/DataPartView';
import { DebugDrawer } from './DebugDrawer';
import type { Suggestion } from '../utils/suggestions';

interface Props {
  message: ChatMessage;
  showTools?: boolean;
  /** Follow-up actions to show as chips under the bubble (last agent message only) */
  suggestions?: Suggestion[];
  onSuggestion?: (message: string) => void;
}

// The request/response "{ } Debug" drawer is dev-only — hide it in
// production builds (VITE_MODE=production) even though message.debug is
// still populated there (useChatProd attaches it the same way useChat does).
const SHOW_DEBUG_DRAWER = (import.meta.env.VITE_MODE as string | undefined) !== 'production';

function formatTime(date: Date): string {
  // Include seconds: request/response usually land in the same minute, and
  // without seconds their displayed time looks identical even though the
  // underlying timestamps differ.
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** DataParts collapsible — hidden by default */
function DataPartsDrawer({ parts }: { parts: DataPart[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="data-drawer">
      <button
        className={`data-drawer-toggle ${open ? 'data-drawer-toggle--open' : ''}`}
        onClick={() => setOpen(v => !v)}
        title="Tool calls & intermediate data"
      >
        <span className="data-drawer-icon">⚙️</span>
        {parts.length} tool call{parts.length !== 1 ? 's' : ''}
        <span className="data-drawer-chevron">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="data-drawer-body">
          {parts.map((p, i) => <DataPartView key={i} part={p} />)}
        </div>
      )}
    </div>
  );
}

function renderVisiblePart(part: Part, index: number) {
  switch (part.kind) {
    case 'text': return <TextPartView key={index} part={part} />;
    case 'file': return <FilePartView key={index} part={part} />;
    default:     return null;
  }
}

export function MessageBubble({ message, showTools = true, suggestions, onSuggestion }: Props) {
  const isUser   = message.role === 'user';
  const isSystem = message.role === 'system';

  if (isSystem) {
    return (
      <div className="msg-system">
        <span className="msg-system-text">
          {message.parts.map(p => p.kind === 'text' ? p.text : '').join('')}
        </span>
      </div>
    );
  }

  const visibleParts = message.parts.filter(p => p.kind !== 'data');
  const dataParts    = message.parts.filter((p): p is DataPart => p.kind === 'data');

  const bubble = (
    <div className={`msg-bubble ${isUser ? 'msg-bubble--user' : 'msg-bubble--agent'}`}>
      <div className="msg-parts">
        {visibleParts.map((part, i) => renderVisiblePart(part, i))}

        {message.streaming && visibleParts.length === 0 && (
          <span className="msg-typing">
            <span className="dot" /><span className="dot" /><span className="dot" />
          </span>
        )}
        {message.streaming && visibleParts.length > 0 && (
          <span className="msg-streaming-cursor">▋</span>
        )}
      </div>

      {showTools && dataParts.length > 0 && <DataPartsDrawer parts={dataParts} />}

      {suggestions && suggestions.length > 0 && onSuggestion && (
        <div className="suggestions">
          {suggestions.map(s => (
            <button
              key={s.message}
              className="suggestion-chip"
              title={s.message}
              onClick={() => onSuggestion(s.message)}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      <span className="msg-time">{formatTime(message.timestamp)}</span>

      {!isUser && message.debug && SHOW_DEBUG_DRAWER && <DebugDrawer debug={message.debug} />}
    </div>
  );

  return (
    <div className={`msg-row ${isUser ? 'msg-row--user' : 'msg-row--agent'}`}>
      {!isUser && <div className="msg-avatar msg-avatar--agent">A</div>}

      {isUser ? bubble : <div className="msg-bubble-resizer">{bubble}</div>}

      {isUser && <div className="msg-avatar msg-avatar--user">U</div>}
    </div>
  );
}
