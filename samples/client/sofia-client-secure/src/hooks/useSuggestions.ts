import { useState, useEffect } from 'react';
import type { ChatMessage } from '../types/a2a';
import type { Suggestion } from '../utils/suggestions';
import { fetchSuggestions } from '../api/prodClient';

function textOf(m: ChatMessage): string {
  return m.parts.map(p => (p.kind === 'text' ? p.text : '')).join('').trim();
}

interface Options {
  messages: ChatMessage[];
  isBusy: boolean;
  enabled: boolean;
  agentName?: string | null;
}

/**
 * Generates follow-up action chips for the last agent message, from its text
 * only (agent/framework independent). Returns [] while loading, when disabled,
 * or when the last message isn't a finished agent response.
 */
export function useSuggestions({ messages, isBusy, enabled, agentName }: Options): Suggestion[] {
  const [state, setState] = useState<{ forId: string; items: Suggestion[] } | null>(null);
  const last = messages[messages.length - 1];

  useEffect(() => {
    if (!enabled || isBusy || !last || last.role !== 'agent' || last.streaming || last.error) return;
    const agentMessage = textOf(last);
    if (!agentMessage || agentMessage.startsWith('⚠') || agentMessage === '_Task completed._') return;
    const lastUser = [...messages].reverse().find(m => m.role === 'user');

    const controller = new AbortController();
    fetchSuggestions(agentName ?? undefined, lastUser ? textOf(lastUser) : '', agentMessage, controller.signal)
      .then(items => { if (!controller.signal.aborted) setState({ forId: last.id, items }); })
      .catch(() => { /* suggestions are best-effort: no chips on failure */ });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last?.id, last?.streaming, isBusy, enabled, agentName]);

  return enabled && !isBusy && last && state?.forId === last.id ? state.items : [];
}
