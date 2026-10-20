import { useEffect, useRef, useState } from 'react';

interface Props {
  message: string | null;
  onDismiss: () => void;
  autoDismissMs?: number;
}

export function ErrorToast({ message, onDismiss, autoDismissMs = 6000 }: Props) {
  const [visible, setVisible] = useState(false);
  // Keep the last non-null message so it stays readable during fade-out
  const lastMsgRef = useRef<string>('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (message) {
      lastMsgRef.current = message;
      setVisible(true);
      // clear any previous auto-dismiss
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        setVisible(false);
        timerRef.current = setTimeout(onDismiss, 300);
      }, autoDismissMs);
    } else {
      setVisible(false);
    }
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [message, autoDismissMs, onDismiss]);

  const dismiss = () => {
    setVisible(false);
    setTimeout(onDismiss, 300);
  };

  // Always rendered so the CSS transition works smoothly
  return (
    <div className={`error-toast${visible ? ' error-toast--visible' : ''}`} role="alert" aria-live="assertive">
      <span className="error-toast-icon">⚠</span>
      <span className="error-toast-msg">{lastMsgRef.current}</span>
      <button className="error-toast-close" onClick={dismiss} aria-label="Chiudi">✕</button>
    </div>
  );
}
