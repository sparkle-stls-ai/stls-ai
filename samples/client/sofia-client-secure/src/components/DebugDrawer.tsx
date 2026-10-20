import { useState } from 'react';
import type { DebugInfo } from '../types/a2a';

interface Props {
  debug: DebugInfo;
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  const str = JSON.stringify(value, null, 2);
  const highlighted = str.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (m) => {
      if (/^"/.test(m)) return /:$/.test(m) ? `<span class="jt-key">${m}</span>` : `<span class="jt-str">${m}</span>`;
      if (/true|false/.test(m)) return `<span class="jt-bool">${m}</span>`;
      if (/null/.test(m)) return `<span class="jt-null">${m}</span>`;
      return `<span class="jt-num">${m}</span>`;
    }
  );
  return (
    <div className="debug-block">
      <div className="debug-block-label">{label}</div>
      <pre className="card-json debug-json" dangerouslySetInnerHTML={{ __html: highlighted }} />
    </div>
  );
}

export function DebugDrawer({ debug }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <div className="debug-drawer">
      <button
        className={`debug-toggle ${open ? 'debug-toggle--open' : ''}`}
        onClick={() => setOpen(v => !v)}
        title="Show/hide request/response details"
      >
        {'{ }'} {open ? 'Hide debug' : 'Debug'}
      </button>

      {open && (
        <div className="debug-body">
          <JsonBlock label="▶ Request" value={debug.request} />
          <JsonBlock label="◀ Response" value={debug.response} />
        </div>
      )}
    </div>
  );
}
