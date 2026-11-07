import type { Part } from '../types/a2a';
import { normalizePart, extractContextId, jsonRpcPayload, buildMessage } from './clientUtils';
import { apiBase } from './apiBase';
import { apiFetch } from './apiFetch';

// ---------------------------------------------------------------------------
// Production API client — routes all A2A traffic through the Express backend.
// The backend resolves agent URLs, injects credentials and user metadata.
// ---------------------------------------------------------------------------

export async function sendChatProd(
  agentName: string,
  sessionId: string,
  parts: Part[],
  contextId?: string,
): Promise<{ parts: Part[]; context_id: string; debug: { request: Record<string, unknown>; response: Record<string, unknown> } }> {
  const payload = jsonRpcPayload('message/send', buildMessage(parts, contextId));
  const res = await apiFetch(`${apiBase}/api/proxy/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agentName, sessionId, payload }),
  });

  if (res.status === 401) {
    const data = await res.json().catch(() => ({}));
    throw Object.assign(new Error('Session expired'), { sessionExpired: true, authMode: data.authMode });
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

  const data: Record<string, unknown> = await res.json();
  return {
    parts: extractPartsFromA2A(data),
    context_id: extractContextId(data) ?? contextId ?? '',
    debug: { request: payload, response: data },
  };
}

export async function* streamChatProd(
  agentName: string,
  sessionId: string,
  parts: Part[],
  contextId?: string,
  signal?: AbortSignal,
): AsyncGenerator<{ parts: Part[]; context_id: string; done: boolean; rawEvent?: Record<string, unknown>; append?: boolean }> {
  const payload = jsonRpcPayload('message/stream', buildMessage(parts, contextId));
  const res = await apiFetch(`${apiBase}/api/proxy/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ agentName, sessionId, payload }),
    signal,
  });

  if (res.status === 401) {
    const data = await res.json().catch(() => ({}));
    throw Object.assign(new Error('Session expired'), { sessionExpired: true, authMode: data.authMode });
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let currentContextId = contextId ?? '';

  const parseLine = (line: string) => {
    if (!line.startsWith('data:')) return null;
    const raw = line.slice(5).trim();
    if (!raw) return null;
    let event: Record<string, unknown>;
    try { event = JSON.parse(raw); } catch { return null; }

    // Top-level error from proxy relay (e.g. "Agent stream stalled", connection dropped)
    const topError = event['error'] as Record<string, unknown> | undefined;
    if (topError) {
      const msg = (topError['message'] as string | undefined) ?? 'Stream error';
      return { parts: [{ kind: 'text', text: `⚠ ${msg}` }], context_id: currentContextId, done: true, rawEvent: event };
    }

    const result = (event['result'] ?? {}) as Record<string, unknown>;
    const cid = (result['contextId'] ?? result['context_id']) as string | undefined;
    if (cid) currentContextId = cid;

    const isFinal =
      (result['final'] as boolean | undefined) === true ||
      (result['status'] as Record<string, unknown> | undefined)?.['state'] === 'completed' ||
      (result['status'] as Record<string, unknown> | undefined)?.['state'] === 'failed';

    const kind = result['kind'] as string | undefined;
    let outParts: Part[] = [];
    // True only for TaskArtifactUpdateEvent chunks explicitly marked as a
    // continuation (A2A "append" semantics) — text chunks streamed this way
    // must be concatenated onto the previous text, not rendered as a new
    // paragraph (see useChatProd/useChat's merge logic).
    let append = false;

    if (kind === 'artifact-update') {
      // TaskArtifactUpdateEvent — content in result.artifact.parts
      const artifact = (result['artifact'] ?? {}) as Record<string, unknown>;
      outParts = ((artifact['parts'] as Record<string, unknown>[] | undefined) ?? []).map(normalizePart);
      append = (artifact['append'] as boolean | undefined) ?? true;
    } else if (kind === 'status-update') {
      // TaskStatusUpdateEvent — content optionally in result.status.message.parts
      const msg = (status?.['message'] ?? {}) as Record<string, unknown>;
      outParts = ((msg['parts'] as Record<string, unknown>[] | undefined) ?? []).map(normalizePart);
      // If the task failed with no message, synthesize a visible error notice
      if (outParts.length === 0 && status?.['state'] === 'failed') {
        outParts = [{ kind: 'text', text: '⚠ Task failed' }];
      }
    } else {
      // MessageStreamEvent (kind === 'message' or absent) and any other kind:
      // try result.parts, result.artifacts, result.message.parts
      outParts = ((result['parts'] as Record<string, unknown>[] | undefined) ?? []).map(normalizePart);
      for (const artifact of (result['artifacts'] as Record<string, unknown>[] | undefined) ?? []) {
        outParts.push(...((artifact['parts'] as Record<string, unknown>[] | undefined) ?? []).map(normalizePart));
      }
      if (outParts.length === 0) {
        // Some agents embed the text inside result.message (same shape as non-streaming)
        const msg = (result['message'] as Record<string, unknown> | undefined);
        outParts = ((msg?.['parts'] as Record<string, unknown>[] | undefined) ?? []).map(normalizePart);
      }
    }

    if (outParts.length > 0 || isFinal) {
      return { parts: outParts, context_id: currentContextId, done: isFinal, rawEvent: event, append };
    }
    return null;
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() ?? '';
    for (const block of blocks) {
      for (const line of block.split('\n')) {
        const parsed = parseLine(line);
        if (parsed) { yield parsed; if (parsed.done) return; }
      }
    }
  }
  for (const line of buffer.split('\n')) {
    const parsed = parseLine(line);
    if (parsed) yield parsed;
  }
}

// ---------------------------------------------------------------------------
// Session lifecycle — called by AppProd on agent connect / disconnect
// ---------------------------------------------------------------------------

export async function connectAgentSession(agentName: string): Promise<string> {
  const res = await apiFetch(`${apiBase}/api/proxy/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agentName }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error ?? `Key session init failed (HTTP ${res.status})`);
  }
  const data = await res.json();
  return data.session_id as string;
}

export async function disconnectAgentSession(sessionId: string): Promise<void> {
  // Best-effort: log but never throw — disconnect should never block the UI.
  try {
    await apiFetch(`${apiBase}/api/proxy/disconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId }),
    });
  } catch (err) {
    console.warn('[prodClient] disconnectAgentSession error:', err);
  }
}

export async function heartbeatAgentSession(sessionId: string): Promise<boolean> {
  try {
    const res = await apiFetch(`${apiBase}/api/proxy/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// Best-effort: follow-up actions generated server-side from the agent's response text.
export async function fetchSuggestions(
  agentName: string | undefined,
  userMessage: string,
  agentMessage: string,
  signal?: AbortSignal,
): Promise<import('../utils/suggestions').Suggestion[]> {
  const res = await apiFetch(`${apiBase}/api/suggestions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agentName, userMessage, agentMessage }),
    signal,
  });
  if (!res.ok) return [];
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data.items) ? data.items : [];
}

// Returns the agent card, or null if the agent is reachable but exposes no card (404).
// Throws on network errors or 5xx — used by the startup overlay to detect cold-start.
export async function fetchAgentCardProd(agentName: string): Promise<import('../types/a2a').AgentCard | null> {
  const res = await apiFetch(`${apiBase}/api/proxy/card?agentName=${encodeURIComponent(agentName)}`);
  if (res.status === 404) return null; // agent is up but doesn't expose a card — that's fine
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Internal helpers (same logic as client.ts, extracted for reuse)
// ---------------------------------------------------------------------------
function extractPartsFromA2A(responseJson: Record<string, unknown>): Part[] {
  const error = responseJson['error'] as Record<string, unknown> | undefined;
  if (error) return [{ kind: 'text', text: `[A2A Error ${error['code'] ?? ''}]: ${error['message'] ?? ''}` }];

  const result = (responseJson['result'] ?? {}) as Record<string, unknown>;
  if (!result || Object.keys(result).length === 0) {
    return [{ kind: 'text', text: JSON.stringify(responseJson, null, 2) }];
  }

  const collected: Part[] = [];
  for (const p of (result['parts'] as Record<string, unknown>[] | undefined) ?? []) collected.push(normalizePart(p));
  const msg = result['message'] as Record<string, unknown> | undefined;
  for (const p of (msg?.['parts'] as Record<string, unknown>[] | undefined) ?? []) collected.push(normalizePart(p));
  for (const artifact of (result['artifacts'] as Record<string, unknown>[] | undefined) ?? []) {
    for (const p of (artifact['parts'] as Record<string, unknown>[] | undefined) ?? []) collected.push(normalizePart(p));
  }
  if (collected.length === 0) {
    const history = (result['history'] as Record<string, unknown>[] | undefined) ?? [];
    for (let i = history.length - 1; i >= 0; i--) {
      if (history[i]['role'] === 'agent') {
        for (const p of (history[i]['parts'] as Record<string, unknown>[] | undefined) ?? []) collected.push(normalizePart(p));
        break;
      }
    }
  }
  return collected.length ? collected : [{ kind: 'text', text: JSON.stringify(result, null, 2) }];
}
