import { useState, useCallback, useRef, useEffect } from 'react';
import type { ChatMessage, DebugInfo, Part } from '../types/a2a';
import { sendChatProd, streamChatProd } from '../api/prodClient';

// Client-side timeout: if no SSE chunk arrives within this window, abort.
const CLIENT_STREAM_TIMEOUT_MS = 180_000; // 180 s — aligned with server proxy and Python test

interface UseChatProdOptions {
  agentName: string | null;
  sessionId: string | null;
  streaming: boolean;
  onError?: (msg: string) => void;
  onSessionExpired?: (authMode: string) => void;
}

interface UseChatProdReturn {
  messages: ChatMessage[];
  isBusy: boolean;
  // silent: don't show the outgoing message as a user bubble (used for the automatic intro)
  sendParts: (parts: Part[], opts?: { silent?: boolean }) => Promise<void>;
  clearMessages: () => void;
}

function generateId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function useChatProd({ agentName, sessionId, streaming, onError, onSessionExpired }: UseChatProdOptions): UseChatProdReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isBusy, setIsBusy] = useState(false);
  const contextIdRef = useRef<string | null>(null);
  // Fallback contextId: stable per (agentName, sessionId) pair so that the first
  // message already carries a context grouping even before the agent echoes one back.
  const fallbackContextId = useRef<string>(crypto.randomUUID());
  const abortRef = useRef<AbortController | null>(null);

  // Cancel any in-flight stream when agent changes or component unmounts
  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);

  useEffect(() => {
    abortRef.current?.abort();
    contextIdRef.current = null;
    fallbackContextId.current = crypto.randomUUID();
    setMessages([]);
    setIsBusy(false);
  }, [agentName, sessionId]);

  const addMessage = useCallback((msg: ChatMessage) => {
    setMessages(prev => [...prev, msg]);
  }, []);

  const updateMessage = useCallback((id: string, updater: (m: ChatMessage) => ChatMessage) => {
    setMessages(prev => prev.map(m => m.id === id ? updater(m) : m));
  }, []);

  const handleError = useCallback((err: unknown) => {
    const e = err as { sessionExpired?: boolean; authMode?: string; message?: string; name?: string };
    if (e.sessionExpired) {
      onSessionExpired?.(e.authMode ?? 'user');
    } else if (e.name === 'AbortError') {
      onError?.('Request cancelled: agent did not respond in time.');
    } else {
      onError?.(e.message ?? String(err));
    }
  }, [onError, onSessionExpired]);

  const sendParts = useCallback(async (parts: Part[], opts?: { silent?: boolean }) => {
    if (!agentName || !sessionId || isBusy) return;

    // Cancel any previous in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const effectiveContextId = contextIdRef.current ?? fallbackContextId.current;
    const userMsg: ChatMessage = { id: generateId(), role: 'user', parts, timestamp: new Date() };
    if (!opts?.silent) addMessage(userMsg);
    setIsBusy(true);

    try {
      if (streaming) {
        const agentMsgId = generateId();
        addMessage({ id: agentMsgId, role: 'agent', parts: [], streaming: true, timestamp: new Date() });

        const accumulated: Part[] = [];
        const rawEvents: Record<string, unknown>[] = [];
        const requestPayload = { method: 'message/stream', agentName, contextId: effectiveContextId, parts };

        // Client-side no-data watchdog: if no chunk arrives within CLIENT_STREAM_TIMEOUT_MS, abort.
        let watchdog = setTimeout(() => controller.abort(), CLIENT_STREAM_TIMEOUT_MS);
        const resetWatchdog = () => {
          clearTimeout(watchdog);
          watchdog = setTimeout(() => controller.abort(), CLIENT_STREAM_TIMEOUT_MS);
        };

        let reachedFinal = false;
        try {
          const gen = streamChatProd(agentName, sessionId, parts, effectiveContextId, controller.signal);
          for await (const chunk of gen) {
            resetWatchdog();
            if (chunk.context_id) contextIdRef.current = chunk.context_id;
            if (chunk.rawEvent) rawEvents.push(chunk.rawEvent);
            if (chunk.parts.length > 0) {
              for (const part of chunk.parts) {
                const last = accumulated[accumulated.length - 1];
                // Token-by-token text streaming (e.g. LangChain) sends many
                // small TextParts marked "append" — merge them into one
                // running TextPart instead of rendering each as its own
                // markdown paragraph.
                if (chunk.append && part.kind === 'text' && last?.kind === 'text') {
                  accumulated[accumulated.length - 1] = { ...last, text: last.text + part.text };
                } else {
                  accumulated.push(part);
                }
              }
              updateMessage(agentMsgId, m => ({ ...m, parts: [...accumulated] }));
            }
            if (chunk.done) {
              reachedFinal = true;
              const debug: DebugInfo = { request: requestPayload, response: { events: rawEvents } };
              updateMessage(agentMsgId, m => ({ ...m, streaming: false, debug }));
              break;
            }
          }
        } finally {
          clearTimeout(watchdog);
          // The SSE body ended (clean EOF or our own watchdog abort) without ever
          // sending a final status — the connection was cut somewhere between us
          // and the agent while the task was still running. Distinguish that from
          // a genuinely empty-but-complete response ("_Task completed._") so the
          // user isn't left staring at a bubble that just stopped with no
          // explanation (the previous behavior for both cases).
          //
          // Both branches re-stamp the timestamp to when the response actually
          // finished, instead of leaving the one set when the streaming
          // placeholder was created (right after the request, before any
          // response arrived) — otherwise request and response show the same
          // displayed time even though they aren't.
          if (!reachedFinal) {
            const errMsg = 'Connessione interrotta: l\'agente non ha inviato una risposta finale.';
            onError?.(errMsg);
            updateMessage(agentMsgId, m => ({
              ...m,
              streaming: false,
              parts: m.parts.length > 0 ? m.parts : [{ kind: 'text' as const, text: `⚠ ${errMsg}` }],
              error: errMsg,
              timestamp: new Date(),
            }));
          } else {
            updateMessage(agentMsgId, m => ({
              ...m,
              streaming: false,
              parts: m.parts.length > 0
                ? m.parts
                : [{ kind: 'text' as const, text: '_Task completed._' }],
              timestamp: new Date(),
            }));
          }
        }

      } else {
        const result = await sendChatProd(agentName, sessionId, parts, effectiveContextId);
        if (result.context_id) contextIdRef.current = result.context_id;
        addMessage({ id: generateId(), role: 'agent', parts: result.parts, debug: result.debug, timestamp: new Date() });
      }
    } catch (err) {
      handleError(err);
      if (!(err instanceof Error && err.name === 'AbortError')) {
        addMessage({
          id: generateId(),
          role: 'system',
          parts: [{ kind: 'text' as const, text: `⚠ ${(err as Error).message}` }],
          error: String(err),
          timestamp: new Date(),
        });
      }
    } finally {
      setIsBusy(false);
    }
  }, [agentName, sessionId, streaming, isBusy, addMessage, updateMessage, handleError]);

  const clearMessages = useCallback(() => {
    setMessages([]);
    contextIdRef.current = null;
    fallbackContextId.current = crypto.randomUUID();
  }, []);

  return { messages, isBusy, sendParts, clearMessages };
}
