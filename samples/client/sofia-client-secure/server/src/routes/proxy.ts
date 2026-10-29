import { Router, Request, Response } from 'express';
import { pool } from '../db';
import { requireAuth } from '../middleware/requireAuth';
import { config } from '../config';
import type { SfAgentConfig, SfAgentAccess } from '../types';

const router = Router();

// ---------------------------------------------------------------------------
// RPC URL cache
// sf_agents_config stores the agent BASE URL (e.g. http://localhost:8013).
// In secure mode the actual RPC endpoint is card.secureUrl (preferred) or
// card.url from /.well-known/agent.json.  Resolved once, cached 5 minutes.
// ---------------------------------------------------------------------------
interface RpcCacheEntry { rpcUrl: string; cachedAt: number }
const rpcUrlCache = new Map<string, RpcCacheEntry>();
const RPC_CACHE_TTL_MS = 5 * 60 * 1000;

async function resolveRpcUrl(baseUrl: string, apiKey: string | null): Promise<string> {
  const cached = rpcUrlCache.get(baseUrl);
  if (cached && Date.now() - cached.cachedAt < RPC_CACHE_TTL_MS) {
    return cached.rpcUrl;
  }

  const base = baseUrl.replace(/\/+$/, '');
  const headers: Record<string, string> = {};
  if (apiKey) headers['X-API-Key'] = apiKey;

  for (const path of ['/.well-known/agent.json', '/.well-known/agent-card.json']) {
    try {
      const r = await fetch(`${base}${path}`, { headers, signal: AbortSignal.timeout(5000) });
      if (r.ok) {
        const card = await r.json() as Record<string, unknown>;
        // Prefer secureUrl (secure transport endpoint) over the plain url.
        const rpcUrl = (card['secureUrl'] as string | undefined)
          ?? (card['url'] as string | undefined)
          ?? baseUrl;
        // Log-only: the card decides where requests (and the X-API-Key) go.
        try {
          if (new URL(rpcUrl).host !== new URL(baseUrl).host) {
            console.warn(`[proxy] agent card url host differs from configured url: configured=${new URL(baseUrl).host} card=${new URL(rpcUrl).host}`);
          }
        } catch { /* unparsable URL — nothing to compare */ }
        rpcUrlCache.set(baseUrl, { rpcUrl, cachedAt: Date.now() });
        return rpcUrl;
      }
    } catch {
      // agent card not available — fall through to next path or fallback
    }
  }

  // Agent card not found: use the base URL as-is but do NOT cache — the agent
  // may still be starting up and its card could become available on the next request.
  return baseUrl;
}

// ---------------------------------------------------------------------------
// Inject user/caller metadata into the A2A JSON-RPC payload.
// ---------------------------------------------------------------------------
function injectMetadata(
  payload: Record<string, unknown>,
  userId: string | undefined,
  profile: Record<string, unknown> | null | undefined,
  appName: string | null | undefined,
): Record<string, unknown> {
  if (!userId) return payload;

  const params = (payload.params ?? {}) as Record<string, unknown>;
  const message = (params.message ?? {}) as Record<string, unknown>;
  // Identity fields are server-owned: drop anything the client put there so it
  // can't spoof a profile/app when the session has none.
  const {
    user_id: _uid, app_name: _app, profile: _prof,
    ...existing
  } = (message.metadata ?? {}) as Record<string, unknown>;

  return {
    ...payload,
    params: {
      ...params,
      message: {
        ...message,
        metadata: {
          ...existing,
          user_id: userId,
          ...(appName  ? { app_name: appName }  : {}),
          ...(profile  ? { profile }             : {}),
        },
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Resolve agent config + access for the requesting session.
// ---------------------------------------------------------------------------
async function resolveAgent(agentName: string, req: Request): Promise<{
  url: string;
  apiKey: string | null;
  profile: Record<string, unknown> | null;
  appName: string | null;
} | null> {
  const configRow = await pool.query<SfAgentConfig>(
    'SELECT url, api_key FROM sf_agents_config WHERE agent_name = $1',
    [agentName]
  );
  if (!configRow.rows.length) return null;

  const { url, api_key } = configRow.rows[0];
  let profile: Record<string, unknown> | null = null;
  const appName: string | null = req.session.appName ?? null;

  if (config.AUTH_MODE === 'app') {
    const userId = req.session.userId;
    if (userId) {
      const accessRow = await pool.query(
        'SELECT 1 FROM sf_agents_access WHERE agent_name = $1 AND user_id = $2',
        [agentName, userId]
      );
      if (!accessRow.rows.length) return null;
    }
    profile = req.session.profile ?? null;

  } else if (config.AUTH_MODE === 'user') {
    const userId = req.session.userId;
    if (!userId) return null;
    const accessRow = await pool.query<SfAgentAccess>(
      'SELECT profile FROM sf_agents_access WHERE agent_name = $1 AND user_id = $2',
      [agentName, userId]
    );
    if (!accessRow.rows.length) return null;
    profile = accessRow.rows[0].profile;
  }

  return { url, apiKey: api_key, profile, appName };
}

// ---------------------------------------------------------------------------
// POST /api/proxy/direct/send  — debug mode: url passed directly, no DB lookup
// POST /api/proxy/direct/stream
// Used by AppDebug when VITE_MODE=debug; bypasses agent-name resolution so the
// user can connect to any agent URL without it being registered in the DB.
// ---------------------------------------------------------------------------
router.post('/direct/send', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { url, sessionId, payload, apiKey } = req.body as { url?: string; sessionId?: string; payload?: Record<string, unknown>; apiKey?: string };
  if (!url || !sessionId || !payload) {
    res.status(400).json({ error: 'url, sessionId and payload are required' });
    return;
  }
  try {
    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, url, payload, api_key: apiKey || null }),
      signal: AbortSignal.timeout(180_000),
    });
    const body = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      res.status(502).json({ error: body['detail'] ?? body['error'] ?? 'Secure transport error' });
      return;
    }
    res.json(body);
  } catch (err) {
    console.error('[proxy] direct/send error:', err);
    res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
  }
});

router.post('/direct/stream', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { url, sessionId, payload, apiKey } = req.body as { url?: string; sessionId?: string; payload?: Record<string, unknown>; apiKey?: string };
  if (!url || !sessionId || !payload) {
    res.status(400).json({ error: 'url, sessionId and payload are required' });
    return;
  }
  try {
    const CONNECT_TIMEOUT_MS = 180_000;
    const NO_DATA_TIMEOUT_MS = 180_000;

    const controller = new AbortController();
    const connectTimer = setTimeout(
      () => controller.abort(new Error('Secure transport connection timeout')),
      CONNECT_TIMEOUT_MS,
    );

    let noDataTimer: ReturnType<typeof setTimeout> | null = null;
    const resetNoDataTimer = () => {
      if (noDataTimer) clearTimeout(noDataTimer);
      noDataTimer = setTimeout(
        () => controller.abort(new Error('Secure transport stream stalled')),
        NO_DATA_TIMEOUT_MS,
      );
    };

    const startedAt = Date.now();
    const elapsedS = () => ((Date.now() - startedAt) / 1000).toFixed(1);
    console.log(`[proxy] direct/stream start url=${url}`);

    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_id: sessionId, url, payload, api_key: apiKey || null }),
      signal: controller.signal,
    });
    clearTimeout(connectTimer);

    if (!upstream.ok || !upstream.body) {
      if (noDataTimer) clearTimeout(noDataTimer);
      console.error(`[proxy] direct/stream upstream error url=${url} status=${upstream.status} elapsed=${elapsedS()}s`);
      res.status(502).json({ error: `Secure transport returned ${upstream.status}` });
      return;
    }

    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();

    let bytesForwarded = 0;
    let chunkCount = 0;
    let endedNormally = false;

    // Heartbeat so a stuck/long-running stream is visible in logs while it's
    // still in flight, not just after the fact — makes it possible to tell an
    // app-level timeout (connect/no-data, logged separately below) apart from
    // an external cutoff (LB/Cloud Run/secure transport process) that kills
    // the connection with no error surfaced to this handler at all.
    const heartbeat = setInterval(() => {
      console.log(`[proxy] direct/stream heartbeat url=${url} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
    }, 30_000);

    const reader = (upstream.body as unknown as ReadableStream<Uint8Array>).getReader();
    // res (not req): since Node 16 the request's 'close' fires once the body has
    // been read, so a listener added here never saw the client disconnecting.
    res.on('close', () => {
      controller.abort();
      reader.cancel().catch(() => { /* already finished/errored */ });
      if (!endedNormally) {
        console.warn(`[proxy] direct/stream connection closed early url=${url} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
      }
    });

    try {
      resetNoDataTimer();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetNoDataTimer();
        chunkCount++;
        bytesForwarded += value.byteLength;
        res.write(value);
      }
      endedNormally = true;
      console.log(`[proxy] direct/stream complete url=${url} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
    } finally {
      clearInterval(heartbeat);
      if (noDataTimer) clearTimeout(noDataTimer);
      res.end();
    }
  } catch (err) {
    console.error('[proxy] direct/stream error:', err);
    if (!res.headersSent) {
      res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
    } else if (!res.writableEnded) {
      res.write(`data: ${JSON.stringify({ error: err instanceof Error ? err.message : 'Stream error' })}\n\n`);
      res.end();
    }
  }
});

// ---------------------------------------------------------------------------
// POST /api/proxy/connect    — create a key session for (user, agent)
// POST /api/proxy/disconnect — tear down a specific key session
// POST /api/proxy/heartbeat  — refresh TTL of a key session
// ---------------------------------------------------------------------------
router.post('/connect', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { agentName } = req.body as { agentName?: string };
  const sessionId = crypto.randomUUID();
  try {
    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, agent_name: agentName ?? null }),
      signal: AbortSignal.timeout(30_000),
    });
    const body = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      res.status(502).json({ error: body['detail'] ?? body['error'] ?? 'Secure transport error' });
      return;
    }
    res.json({ ...body, session_id: sessionId });
  } catch (err) {
    console.error('[proxy] connect error:', err);
    res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
  }
});

router.post('/disconnect', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { sessionId } = req.body as { sessionId?: string };
  if (!sessionId) {
    res.status(400).json({ error: 'sessionId is required' });
    return;
  }
  try {
    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/disconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
      signal: AbortSignal.timeout(10_000),
    });
    const body = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      res.status(502).json({ error: body['detail'] ?? body['error'] ?? 'Secure transport error' });
      return;
    }
    res.json(body);
  } catch (err) {
    console.error('[proxy] disconnect error:', err);
    res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
  }
});

router.post('/heartbeat', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { sessionId } = req.body as { sessionId?: string };
  if (!sessionId) {
    res.status(400).json({ error: 'sessionId is required' });
    return;
  }
  try {
    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
      signal: AbortSignal.timeout(5_000),
    });
    const body = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      res.status(upstream.status === 404 ? 404 : 502).json({ error: body['detail'] ?? body['error'] ?? 'Secure transport error' });
      return;
    }
    res.json(body);
  } catch (err) {
    console.error('[proxy] heartbeat error:', err);
    res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
  }
});

// ---------------------------------------------------------------------------
// POST /api/proxy/send  — non-streaming
// Forwards the enriched A2A payload to the Python secure transport service,
// which wraps it in SecureA2ATransportAdapter before calling the agent.
// ---------------------------------------------------------------------------
router.post('/send', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { agentName, sessionId, payload } = req.body as {
    agentName?: string;
    sessionId?: string;
    payload?: Record<string, unknown>;
  };

  if (!agentName || !sessionId || !payload) {
    res.status(400).json({ error: 'agentName, sessionId and payload are required' });
    return;
  }

  try {
    const agent = await resolveAgent(agentName, req);
    if (!agent) {
      res.status(config.AUTH_MODE !== 'none' ? 403 : 404).json({ error: 'Agent not found or access denied' });
      return;
    }

    const rpcUrl = await resolveRpcUrl(agent.url, agent.apiKey);
    const enriched = injectMetadata(payload, req.session.userId, agent.profile, agent.appName);

    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, url: rpcUrl, payload: enriched, api_key: agent.apiKey }),
      signal: AbortSignal.timeout(180_000),
    });

    const body = await upstream.json() as Record<string, unknown>;
    if (!upstream.ok) {
      res.status(502).json({ error: body['detail'] ?? body['error'] ?? 'Secure transport error' });
      return;
    }
    res.json(body);
  } catch (err) {
    console.error('[proxy] send error:', err);
    const isTimeout = err instanceof Error && err.name === 'TimeoutError';
    res.status(isTimeout ? 504 : 502).json({
      error: isTimeout ? 'Agent did not respond in time' : err instanceof Error ? err.message : 'Upstream error',
    });
  }
});

// ---------------------------------------------------------------------------
// POST /api/proxy/stream  — SSE streaming relay via Python secure transport
//
// The Python service calls the agent via SecureA2ATransportAdapter and emits
// the result as A2A-compatible SSE events, so the frontend receives the same
// event format it expects from a native streaming agent.
// ---------------------------------------------------------------------------
router.post('/stream', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { agentName, sessionId, payload } = req.body as {
    agentName?: string;
    sessionId?: string;
    payload?: Record<string, unknown>;
  };

  if (!agentName || !sessionId || !payload) {
    res.status(400).json({ error: 'agentName, sessionId and payload are required' });
    return;
  }

  try {
    const agent = await resolveAgent(agentName, req);
    if (!agent) {
      res.status(config.AUTH_MODE !== 'none' ? 403 : 404).json({ error: 'Agent not found or access denied' });
      return;
    }

    const rpcUrl = await resolveRpcUrl(agent.url, agent.apiKey);
    const enriched = injectMetadata(payload, req.session.userId, agent.profile, agent.appName);

    const CONNECT_TIMEOUT_MS = 180_000;
    const NO_DATA_TIMEOUT_MS = 180_000;

    const controller = new AbortController();
    const connectTimer = setTimeout(
      () => controller.abort(new Error('Secure transport connection timeout')),
      CONNECT_TIMEOUT_MS,
    );

    let noDataTimer: ReturnType<typeof setTimeout> | null = null;
    const resetNoDataTimer = () => {
      if (noDataTimer) clearTimeout(noDataTimer);
      noDataTimer = setTimeout(
        () => controller.abort(new Error('Secure transport stream stalled')),
        NO_DATA_TIMEOUT_MS,
      );
    };

    const startedAt = Date.now();
    const elapsedS = () => ((Date.now() - startedAt) / 1000).toFixed(1);
    console.log(`[proxy] stream start agent=${agentName}`);

    const upstream = await fetch(`${config.SECURE_TRANSPORT_URL}/secure/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_id: sessionId, url: rpcUrl, payload: enriched, api_key: agent.apiKey }),
      signal: controller.signal,
    });
    clearTimeout(connectTimer);

    if (!upstream.ok || !upstream.body) {
      if (noDataTimer) clearTimeout(noDataTimer);
      console.error(`[proxy] stream upstream error agent=${agentName} status=${upstream.status} elapsed=${elapsedS()}s`);
      res.status(502).json({ error: `Secure transport returned ${upstream.status}` });
      return;
    }

    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();

    let bytesForwarded = 0;
    let chunkCount = 0;
    let endedNormally = false;

    // Heartbeat so a stuck/long-running stream is visible in logs while it's
    // still in flight, not just after the fact — makes it possible to tell an
    // app-level timeout (connect/no-data, logged separately below) apart from
    // an external cutoff (LB/Cloud Run/secure transport process) that kills
    // the connection with no error surfaced to this handler at all.
    const heartbeat = setInterval(() => {
      console.log(`[proxy] stream heartbeat agent=${agentName} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
    }, 30_000);

    const reader = (upstream.body as unknown as ReadableStream<Uint8Array>).getReader();
    // res (not req): since Node 16 the request's 'close' fires once the body has
    // been read, so a listener added here never saw the client disconnecting.
    res.on('close', () => {
      controller.abort();
      reader.cancel().catch(() => { /* already finished/errored */ });
      if (!endedNormally) {
        console.warn(`[proxy] stream connection closed early agent=${agentName} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
      }
    });

    try {
      resetNoDataTimer();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetNoDataTimer();
        chunkCount++;
        bytesForwarded += value.byteLength;
        res.write(value);
      }
      endedNormally = true;
      console.log(`[proxy] stream complete agent=${agentName} elapsed=${elapsedS()}s chunks=${chunkCount} bytes=${bytesForwarded}`);
    } finally {
      clearInterval(heartbeat);
      if (noDataTimer) clearTimeout(noDataTimer);
      res.end();
    }
  } catch (err) {
    console.error('[proxy] stream error:', err);
    if (!res.headersSent) {
      const msg = err instanceof Error ? err.message : 'Upstream error';
      res.status(502).json({ error: msg });
    } else {
      if (!res.writableEnded) {
        res.write(`data: ${JSON.stringify({ error: err instanceof Error ? err.message : 'Stream error' })}\n\n`);
        res.end();
      }
    }
  }
});

// ---------------------------------------------------------------------------
// GET /api/proxy/card?agentName=<name>
// ---------------------------------------------------------------------------
router.get('/card', requireAuth, async (req: Request, res: Response): Promise<void> => {
  const { agentName } = req.query as { agentName?: string };
  if (!agentName) {
    res.status(400).json({ error: 'agentName required' });
    return;
  }

  try {
    const agent = await resolveAgent(agentName, req);
    if (!agent) {
      res.status(403).json({ error: 'Agent not found or access denied' });
      return;
    }

    const base = agent.url.replace(/\/+$/, '');
    const headers: Record<string, string> = {};
    if (agent.apiKey) headers['X-API-Key'] = agent.apiKey;

    for (const path of ['/.well-known/agent.json', '/.well-known/agent-card.json']) {
      const r = await fetch(`${base}${path}`, { headers });
      if (r.ok) {
        const card = await r.json();
        res.json(card);
        return;
      }
    }
    res.status(404).json({ error: 'Agent card not found' });
  } catch (err) {
    console.error('[proxy] card error:', err);
    res.status(502).json({ error: err instanceof Error ? err.message : 'Upstream error' });
  }
});

export default router;
