import { useState, useCallback } from 'react';
import type { AgentCard } from '../types/a2a';
import { fetchAgentCard, connectSecureSession, disconnectSecureSession } from '../api/client';

interface UseAgentOptions {
  onError?: (msg: string) => void;
}

interface UseAgentReturn {
  card: AgentCard | null;
  agentRpcUrl: string | null;
  apiKey: string;
  sessionId: string | null;
  isConnecting: boolean;
  connect: (baseUrl: string, apiKey: string, proxyUrl?: string, noProxy?: string) => Promise<void>;
  disconnect: () => void;
}

export function useAgent({ onError }: UseAgentOptions = {}): UseAgentReturn {
  const [card, setCard] = useState<AgentCard | null>(null);
  const [agentRpcUrl, setAgentRpcUrl] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isConnecting, setIsConnecting] = useState(false);

  const connect = useCallback(async (baseUrl: string, key: string, proxyUrl?: string, noProxy?: string) => {
    setIsConnecting(true);
    setCard(null);
    setAgentRpcUrl(null);
    setApiKey(key);
    setSessionId(null);
    try {
      const agentCard = await fetchAgentCard(baseUrl, key, proxyUrl || undefined, noProxy || undefined);
      const rpcUrl = agentCard.secureUrl ?? agentCard.url;
      const sid = await connectSecureSession(agentCard.name);
      setCard(agentCard);
      setAgentRpcUrl(rpcUrl);
      setSessionId(sid);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      onError?.(msg);
      setCard(null);
      setAgentRpcUrl(null);
      setApiKey('');
      setSessionId(null);
    } finally {
      setIsConnecting(false);
    }
  }, [onError]);

  const disconnect = useCallback(() => {
    if (sessionId) disconnectSecureSession(sessionId);
    setCard(null);
    setAgentRpcUrl(null);
    setApiKey('');
    setSessionId(null);
  }, [sessionId]);

  return { card, agentRpcUrl, apiKey, sessionId, isConnecting, connect, disconnect };
}
