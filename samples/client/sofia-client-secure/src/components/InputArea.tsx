import React, { useState, useRef, useCallback } from 'react';
import type { Part, FilePart, TextPart, DataPart } from '../types/a2a';

type TabId = 'text' | 'file' | 'data';

interface PendingPart {
  id: string;
  part: Part;
  label: string;
}

interface Props {
  onSend: (parts: Part[]) => void;
  isBusy: boolean;
  disabled: boolean;
}

function generateId(): string {
  return Math.random().toString(36).slice(2);
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      // strip data:...;base64, prefix
      const base64 = result.split(',')[1] ?? result;
      resolve(base64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function InputArea({ onSend, isBusy, disabled }: Props) {
  const [tab, setTab] = useState<TabId>('text');
  const [textValue, setTextValue] = useState('');
  const [dataValue, setDataValue] = useState('');
  const [dataError, setDataError] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [pendingParts, setPendingParts] = useState<PendingPart[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const addPendingPart = (part: Part, label: string) => {
    setPendingParts(prev => [...prev, { id: generateId(), part, label }]);
  };

  const removePendingPart = (id: string) => {
    setPendingParts(prev => prev.filter(p => p.id !== id));
  };

  const handleTextKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return;

    if (e.shiftKey) return; // let the textarea insert a newline as usual

    e.preventDefault();
    if (e.altKey) {
      // Alt+Enter: stage the text as a pending part without sending (old
      // plain-Enter behavior) — lets you build a multi-part message (text +
      // files + data) before sending it all together.
      handleAddText();
    } else {
      // Enter: send immediately (same as clicking "Send ➤").
      handleSend();
    }
  };

  const handleAddText = () => {
    const trimmed = textValue.trim();
    if (!trimmed) return;
    const part: TextPart = { kind: 'text', text: trimmed };
    addPendingPart(part, `✍️ "${trimmed.slice(0, 30)}${trimmed.length > 30 ? '…' : ''}"`);
    setTextValue('');
  };

  const handleDataChange = (val: string) => {
    setDataValue(val);
    if (!val.trim()) {
      setDataError(false);
      return;
    }
    try {
      JSON.parse(val);
      setDataError(false);
    } catch {
      setDataError(true);
    }
  };

  const handleAddData = () => {
    if (!dataValue.trim() || dataError) return;
    try {
      const parsed = JSON.parse(dataValue);
      const part: DataPart = { kind: 'data', data: parsed };
      addPendingPart(part, `{ } ${dataValue.slice(0, 20)}…`);
      setDataValue('');
      setDataError(false);
    } catch {
      setDataError(true);
    }
  };

  const processFiles = useCallback(async (files: FileList | File[]) => {
    const arr = Array.from(files);
    for (const file of arr) {
      try {
        const bytes = await fileToBase64(file);
        const part: FilePart = {
          kind: 'file',
          file: {
            name: file.name,
            mimeType: file.type || 'application/octet-stream',
            bytes,
          },
        };
        addPendingPart(part, `📎 ${file.name} (${formatSize(file.size)})`);
      } catch {
        console.error('Failed to encode file', file.name);
      }
    }
  }, []);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      processFiles(e.target.files);
      e.target.value = '';
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    if (e.dataTransfer.files) processFiles(e.dataTransfer.files);
  };

  const handleSend = () => {
    if (isBusy || disabled) return;

    const parts: Part[] = [];

    // Add any text currently in textarea (if not empty)
    const trimmedText = textValue.trim();
    if (trimmedText) {
      parts.push({ kind: 'text', text: trimmedText });
    }

    // Add pending parts
    parts.push(...pendingParts.map(p => p.part));

    if (parts.length === 0) return;

    onSend(parts);
    setTextValue('');
    setPendingParts([]);
    setDataValue('');
    setDataError(false);
  };

  const canSend = !isBusy && !disabled && (textValue.trim().length > 0 || pendingParts.length > 0);

  return (
    <div className="input-area">
      {pendingParts.length > 0 && (
        <div className="pending-parts">
          {pendingParts.map(pp => (
            <span key={pp.id} className="pending-chip">
              {pp.label}
              <button
                className="pending-chip-remove"
                onClick={() => removePendingPart(pp.id)}
                aria-label="Remove part"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="tab-switcher">
        <button
          className={`tab-btn ${tab === 'text' ? 'tab-btn--active' : ''}`}
          onClick={() => setTab('text')}
        >
          ✍️ Text
        </button>
        <button
          className={`tab-btn ${tab === 'file' ? 'tab-btn--active' : ''}`}
          onClick={() => setTab('file')}
        >
          📎 File
        </button>
        <button
          className={`tab-btn ${tab === 'data' ? 'tab-btn--active' : ''}`}
          onClick={() => setTab('data')}
        >
          {'{ }'} Data
        </button>
        <button
          className="btn btn--primary btn--send"
          onClick={handleSend}
          disabled={!canSend}
        >
          {isBusy ? <span className="send-spinner" /> : 'Send ➤'}
        </button>
      </div>

      <div className="tab-content">
        {tab === 'text' && (
          <div className="text-input-wrap">
            <textarea
              className="text-input"
              placeholder="Type a message… (Enter to send, Shift+Enter for new line, Alt+Enter to stage)"
              value={textValue}
              onChange={e => setTextValue(e.target.value)}
              onKeyDown={handleTextKeyDown}
              disabled={isBusy || disabled}
              rows={2}
            />
          </div>
        )}

        {tab === 'file' && (
          <div
            className={`drop-zone ${isDragOver ? 'drop-zone--over' : ''}`}
            onDragOver={e => { e.preventDefault(); setIsDragOver(true); }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              style={{ display: 'none' }}
              onChange={handleFileChange}
            />
            <div className="drop-zone-inner">
              <span className="drop-zone-icon">📁</span>
              <p className="drop-zone-text">
                {isDragOver ? 'Drop files here' : 'Drag files here or click to select'}
              </p>
              <p className="drop-zone-hint">Images, PDFs, documents…</p>
            </div>
          </div>
        )}

        {tab === 'data' && (
          <div className="data-input-wrap">
            <textarea
              className={`data-input ${dataError ? 'data-input--error' : ''}`}
              placeholder={'{ "key": "value" }'}
              value={dataValue}
              onChange={e => handleDataChange(e.target.value)}
              disabled={isBusy || disabled}
              rows={2}
              spellCheck={false}
            />
            {dataError && <span className="data-input-error-msg">Invalid JSON</span>}
            <button
              className="btn btn--secondary btn--add-data"
              onClick={handleAddData}
              disabled={!dataValue.trim() || dataError || isBusy || disabled}
            >
              + Add data
            </button>
          </div>
        )}
      </div>

    </div>
  );
}
