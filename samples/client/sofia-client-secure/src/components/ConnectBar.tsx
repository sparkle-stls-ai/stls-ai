import React, { useState } from 'react';

interface Props {
  isConnecting: boolean;
  isConnected: boolean;
  streaming: boolean;
  onConnect: (url: string, apiKey: string) => void;
  onToggleStreaming: () => void;
}

export function ConnectBar({ isConnecting, isConnected, streaming, onConnect, onToggleStreaming }: Props) {
  const [url, setUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = url.trim();
    if (trimmed) onConnect(trimmed, apiKey.trim());
  };

  return (
    <div className="connect-bar">
      <form className="connect-form" onSubmit={handleSubmit}>

        {/* Row 1: URL + buttons */}
        <div className="connect-row">
          <div className="connect-input-wrap">
            <span className="connect-prefix">🔗</span>
            <input
              className="connect-input"
              type="url"
              placeholder="https://agent.example.com"
              value={url}
              onChange={e => setUrl(e.target.value)}
              disabled={isConnecting}
            />
          </div>
          <button
            type="submit"
            className={`btn btn--primary ${isConnected ? 'btn--connected' : ''}`}
            disabled={isConnecting || !url.trim()}
          >
            {isConnecting ? 'Connecting…' : isConnected ? '✓ Connected' : 'Connect'}
          </button>
          <button
            type="button"
            className={`btn btn--stream ${streaming ? 'btn--stream-on' : ''}`}
            onClick={onToggleStreaming}
            title={streaming ? 'Streaming on — click to disable' : 'Streaming off — click to enable'}
          >
            ⚡ {streaming ? 'Stream ON' : 'Stream OFF'}
          </button>
        </div>

        {/* Row 2: API Key (always visible, optional) */}
        <div className="apikey-row">
          <div className="connect-input-wrap connect-input-wrap--apikey">
            <span className="connect-prefix">🔑</span>
            <input
              className="connect-input connect-input--apikey"
              type={showKey ? 'text' : 'password'}
              placeholder="API Key (optional)"
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              disabled={isConnecting}
              autoComplete="off"
            />
            {apiKey && (
              <button
                type="button"
                className="apikey-toggle"
                onClick={() => setShowKey(v => !v)}
                title={showKey ? 'Hide' : 'Show'}
              >
                {showKey ? '🙈' : '👁'}
              </button>
            )}
          </div>
          {apiKey && (
            <span className="apikey-badge">X-API-Key set</span>
          )}
        </div>

      </form>
    </div>
  );
}
