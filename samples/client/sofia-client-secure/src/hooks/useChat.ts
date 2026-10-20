import { useState, useCallback, useRef, useEffect } from 'react';
import type { ChatMessage, DebugInfo, Part } from '../types/a2a';
import { sendSecureChat, streamSecureChat, heartbeatSecureSession } from '../api/client';

const HEARTBEAT_MS = 30_000;

interface UseChatOptions {
  agentRpcUrl: string | null;
  sessionId: string | null;
  streaming: boolean;
  apiKey?: string;
  onError?: (msg: string) => void;
}

interface UseChatReturn {
  messages: ChatMessage[];
  contextId: string | null;
  isBusy: boolean;
  sendParts: (parts: Part[]) => Promise<void>;
  clearMessages: () => void;
}

function generateId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function useChat({ agentRpcUrl, sessionId, streaming, apiKey, onError }: UseChatOptions): UseChatReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [contextId, setContextId] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const contextIdRef = useRef<string | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const fallbackContextId = useRef<string>(crypto.randomUUID());
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const addMessage = useCallback((msg: ChatMessage) => {
    setMessages(prev => {
      const next = [...prev, msg];
      messagesRef.current = next;
      return next;
    });
  }, []);

  const updateMessage = useCallback((id: string, updater: (m: ChatMessage) => ChatMessage) => {
    setMessages(prev => {
      const next = prev.map(m => m.id === id ? updater(m) : m);
      messagesRef.current = next;
      return next;
    });
  }, []);

  // Reset context when agent/session changes.
  useEffect(() => {
    contextIdRef.current = null;
    setContextId(null);
    messagesRef.current = [];
    fallbackContextId.current = crypto.randomUUID();
  }, [agentRpcUrl, sessionId]);

  // Heartbeat: keep the key session alive while connected.
  useEffect(() => {
    if (!sessionId) return;
    heartbeatRef.current = setInterval(() => {
      heartbeatSecureSession(sessionId);
    }, HEARTBEAT_MS);
    return () => {
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    };
  }, [sessionId]);

  const sendParts = useCallback(async (parts: Part[]) => {
    if (!agentRpcUrl || !sessionId || isBusy) return;

    const effectiveContextId = contextIdRef.current ?? fallbackContextId.current;
    const userMsg: ChatMessage = { id: generateId(), role: 'user', parts, timestamp: new Date() };
    addMessage(userMsg);
    setIsBusy(true);

    try {
      if (streaming) {
        const agentMsgId = generateId();
        addMessage({ id: agentMsgId, role: 'agent', parts: [], streaming: true, timestamp: new Date() });

        const requestPayload = { method: 'message/stream', agentUrl: agentRpcUrl, contextId: effectiveContextId, parts };
        const gen = streamSecureChat(agentRpcUrl, sessionId, parts, effectiveContextId, apiKey);
        const accumulated: Part[] = [];
        const rawEvents: Record<string, unknown>[] = [];
        let reachedFinal = false;

        for await (const chunk of gen) {
          if (chunk.context_id) {
            contextIdRef.current = chunk.context_id;
            setContextId(chunk.context_id);
          }
          if (chunk.rawEvent) rawEvents.push(chunk.rawEvent);
          if (chunk.parts && chunk.parts.length > 0) {
            accumulated.push(...chunk.parts);
            updateMessage(agentMsgId, m => ({ ...m, parts: [...accumulated] }));
          }
          if (chunk.done) {
            reachedFinal = true;
            const debug: DebugInfo = { request: requestPayload, response: { events: rawEvents } };
            // Re-stamp the timestamp here instead of leaving the one set when the
            // streaming placeholder was created (right after the request, before
            // any response arrived) — otherwise request and response show the
            // same displayed time even though they aren't.
            updateMessage(agentMsgId, m => ({ ...m, streaming: false, debug, timestamp: new Date() }));
            break;
          }
        }

        if (!reachedFinal) {
          // The SSE body ended (clean EOF, no thrown error) without ever sending a
          // final status — the connection was cut somewhere between us and the
          // secure transport / agent while the task was still running. Silently
          // marking the bubble "not streaming" would leave the user staring at a
          // message that just stopped with no explanation, so surface it instead.
          const errMsg = 'Connessione interrotta: l\'agente non ha inviato una risposta finale.';
          onError?.(errMsg);
          updateMessage(agentMsgId, m => ({
            ...m,
            streaming: false,
            parts: m.parts.length > 0 ? m.parts : [{ kind: 'text' as const, text: `⚠ ${errMsg}` }],
            error: errMsg,
            timestamp: new Date(),
          }));
        }
      } else {
        const result = await sendSecureChat(agentRpcUrl, sessionId, parts, effectiveContextId, apiKey);
        if (result.context_id) {
          contextIdRef.current = result.context_id;
          setContextId(result.context_id);
        }
        addMessage({ id: generateId(), role: 'agent', parts: result.parts, debug: result.debug, timestamp: new Date() });
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      onError?.(errMsg);
      addMessage({
        id: generateId(),
        role: 'system',
        parts: [{ kind: 'text' as const, text: `⚠ ${errMsg}` }],
        error: errMsg,
        timestamp: new Date(),
      });
    } finally {
      setIsBusy(false);
    }
  }, [agentRpcUrl, sessionId, streaming, apiKey, isBusy, addMessage, updateMessage]);

  const clearMessages = useCallback(() => {
    setMessages([]);
    messagesRef.current = [];
    setContextId(null);
    contextIdRef.current = null;
    fallbackContextId.current = crypto.randomUUID();
  }, []);

  return { messages, contextId, isBusy, sendParts, clearMessages };
}
