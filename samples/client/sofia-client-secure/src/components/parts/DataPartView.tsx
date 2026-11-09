
import type { DataPart } from '../../types/a2a';

interface Props {
  part: DataPart;
}

function syntaxHighlight(json: string): string {
  return json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(
      /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
      (match) => {
        let cls = 'json-number';
        if (/^"/.test(match)) {
          if (/:$/.test(match)) {
            cls = 'json-key';
          } else {
            cls = 'json-string';
          }
        } else if (/true|false/.test(match)) {
          cls = 'json-boolean';
        } else if (/null/.test(match)) {
          cls = 'json-null';
        }
        return `<span class="${cls}">${match}</span>`;
      }
    );
}

export function DataPartView({ part }: Props) {
  const jsonStr = JSON.stringify(part.data, null, 2);
  const highlighted = syntaxHighlight(jsonStr);

  return (
    <div className="data-part">
      {part.mimeType && <span className="data-part-mime">{part.mimeType}</span>}
      <pre
        className="data-part-pre"
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
    </div>
  );
}
