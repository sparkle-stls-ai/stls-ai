export interface TextPart {
  kind: 'text';
  text: string;
  mimeType?: string;
}

export interface FileContent {
  name?: string;
  mimeType?: string;
  bytes?: string; // base64
  uri?: string;
}

export interface FilePart {
  kind: 'file';
  file: FileContent;
}

export interface DataPart {
  kind: 'data';
  data: Record<string, unknown>;
  mimeType?: string;
}

export type Part = TextPart | FilePart | DataPart;

export interface A2AMessage {
  messageId: string;
  role: 'user' | 'agent';
  parts: Part[];
  contextId?: string;
  timestamp?: string;
}

export interface Skill {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  inputModes?: string[];
  outputModes?: string[];
}

export interface AgentCard {
  name: string;
  description?: string;
  url: string;
  secureUrl?: string;
  version?: string;
  skills?: Skill[];
  capabilities?: {
    streaming?: boolean;
    pushNotifications?: boolean;
    stateTransitionHistory?: boolean;
  };
  defaultInputModes?: string[];
  defaultOutputModes?: string[];
  securityScheme?: Record<string, unknown>;
  /** A2A 1.0 (spec §4.4.1): named map of security schemes, alongside the legacy singular securityScheme above. */
  securitySchemes?: Record<string, unknown>;
  /** A2A 1.0 (spec §4.4.1): array of {url, protocolBinding, protocolVersion}, not the legacy boolean-flag shape. */
  supportedInterfaces?: unknown[];
  extensions?: unknown[];
  // catch-all for any extra fields returned by the agent
  [key: string]: unknown;
}

export interface DebugInfo {
  request: Record<string, unknown>;
  response: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'agent' | 'system';
  parts: Part[];
  debug?: DebugInfo;
  streaming?: boolean;
  error?: string;
  timestamp: Date;
}
