import type { Part, A2AMessage } from '../types/a2a';

export function uuid(): string {
  return crypto.randomUUID();
}

export function jsonRpcPayload(method: string, params: Record<string, unknown>) {
  return { jsonrpc: '2.0', id: uuid(), method, params };
}

export function buildMessage(parts: Part[], contextId?: string, history?: A2AMessage[]) {
  const msg: Record<string, unknown> = { messageId: uuid(), role: 'user', parts };
  if (contextId) msg['contextId'] = contextId;
  const params: Record<string, unknown> = { message: msg };
  if (history && history.length > 0) params['history'] = history;
  return params;
}

export function normalizePart(p: Record<string, unknown>): Part {
  const kind = (p['kind'] ?? p['type'] ?? 'text') as string;
  if (kind === 'text') return { kind: 'text', text: (p['text'] as string) ?? '', mimeType: p['mimeType'] as string | undefined };
  if (kind === 'file') return { kind: 'file', file: (p['file'] as Part extends { kind: 'file' } ? Part['file'] : never) ?? {} };
  if (kind === 'data') return { kind: 'data', data: (p['data'] as Record<string, unknown>) ?? {}, mimeType: p['mimeType'] as string | undefined };
  return { kind: 'text', text: JSON.stringify(p, null, 2) };
}

export function extractContextId(responseJson: Record<string, unknown>): string | undefined {
  const result = (responseJson['result'] ?? {}) as Record<string, unknown>;
  return (
    (result['contextId'] as string | undefined) ??
    (result['context_id'] as string | undefined) ??
    ((result['message'] as Record<string, unknown> | undefined)?.['contextId'] as string | undefined)
  );
}
