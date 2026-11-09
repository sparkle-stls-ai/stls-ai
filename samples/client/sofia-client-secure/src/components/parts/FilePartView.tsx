
import type { FilePart } from '../../types/a2a';

interface Props {
  part: FilePart;
}

export function FilePartView({ part }: Props) {
  const { file } = part;
  const { name, mimeType, bytes, uri } = file;

  const src = bytes
    ? `data:${mimeType ?? 'application/octet-stream'};base64,${bytes}`
    : uri ?? '';

  const isImage = mimeType?.startsWith('image/');
  const isPdf = mimeType === 'application/pdf';

  if (isImage) {
    return (
      <div className="file-part">
        <img src={src} alt={name ?? 'image'} className="file-part-image" />
        {name && <span className="file-part-name">{name}</span>}
      </div>
    );
  }

  if (isPdf) {
    return (
      <div className="file-part file-part-generic">
        <span className="file-part-icon">📄</span>
        <span className="file-part-name">{name ?? 'document.pdf'}</span>
        {src && (
          <a href={src} download={name ?? 'document.pdf'} className="file-part-download">
            Download
          </a>
        )}
      </div>
    );
  }

  return (
    <div className="file-part file-part-generic">
      <span className="file-part-icon">📎</span>
      <span className="file-part-name">{name ?? 'file'}</span>
      {mimeType && <span className="file-part-mime">{mimeType}</span>}
      {src && (
        <a href={src} download={name ?? 'file'} className="file-part-download">
          Download
        </a>
      )}
    </div>
  );
}
