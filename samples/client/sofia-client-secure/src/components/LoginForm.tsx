import React, { useState } from 'react';

interface Props {
  onLogin: (userId: string, password: string) => Promise<string | null>;
}

export function LoginForm({ onLogin }: Props) {
  const [userId, setUserId] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!userId.trim() || !password) return;
    setLoading(true);
    setError(null);
    const err = await onLogin(userId.trim(), password);
    if (err) setError(err);
    setLoading(false);
  };

  return (
    <div className="login-overlay">
      <div className="login-card">
        <div className="login-logo">
          <span className="app-logo-icon">✦</span>
          <span className="app-logo-text">Sofia Secure Chat</span>
        </div>
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-field">
            <label className="login-label" htmlFor="login-user">User ID</label>
            <input
              id="login-user"
              className="login-input"
              type="text"
              value={userId}
              onChange={e => setUserId(e.target.value)}
              placeholder="Enter your user ID"
              autoFocus
              disabled={loading}
            />
          </div>
          <div className="login-field">
            <label className="login-label" htmlFor="login-pass">Password</label>
            <input
              id="login-pass"
              className="login-input"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="Enter your password"
              disabled={loading}
            />
          </div>
          {error && <p className="login-error">{error}</p>}
          <button
            className="btn btn--primary login-btn"
            type="submit"
            disabled={loading || !userId.trim() || !password}
          >
            {loading ? <span className="send-spinner" /> : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
