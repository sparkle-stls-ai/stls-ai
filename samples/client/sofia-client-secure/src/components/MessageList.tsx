import { useRef, useEffect } from 'react';
import type { ChatMessage } from '../types/a2a';
import { MessageBubble } from './MessageBubble';
import type { Suggestion } from '../utils/suggestions';

interface Props {
  messages: ChatMessage[];
  /** Show tool calls / intermediate data (DataParts). Default true. */
  showTools?: boolean;
  /** Follow-up actions for the last message (see useSuggestions) */
  suggestions?: Suggestion[];
  onSuggestion?: (message: string) => void;
}

export function MessageList({ messages, showTools = true, suggestions, onSuggestion }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="message-list message-list--empty">
        <div className="empty-state">
          <div className="empty-state-icon">💬</div>
          <p>Connect an agent to start chatting</p>
        </div>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((msg, i) => {
        // Chips only on the last message, once the agent has finished it
        const isLast = i === messages.length - 1;
        return (
          <MessageBubble
            key={msg.id}
            message={msg}
            showTools={showTools}
            suggestions={isLast && msg.role === 'agent' && !msg.streaming ? suggestions : undefined}
            onSuggestion={onSuggestion}
          />
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
