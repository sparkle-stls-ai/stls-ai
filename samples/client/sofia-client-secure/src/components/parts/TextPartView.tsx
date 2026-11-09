import { useMemo, useEffect, useRef } from 'react';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import type { TextPart } from '../../types/a2a';

marked.use({ breaks: true, gfm: true });

interface Props {
  part: TextPart;
}

export function TextPartView({ part }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  const html = useMemo(() => {
    const withWrap = (marked.parse(part.text) as string)
      .replace(/<table/g, '<div class="table-wrap"><table')
      .replace(/<\/table>/g, '</table></div>');
    return DOMPurify.sanitize(withWrap, { USE_PROFILES: { html: true } });
  }, [part.text]);

  // Attach column-sort click handlers after each render.
  // data-col is set here on real DOM nodes (after DOMPurify) so it is never
  // stripped by the sanitizer. CSS selectors react to setAttribute immediately.
  // Cycle: unsorted → asc → desc → unsorted (restores original row order).
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const cleanups: (() => void)[] = [];

    container.querySelectorAll<HTMLTableElement>('table').forEach((table) => {
      const tbody = table.querySelector('tbody');
      if (!tbody) return;

      // Tag each header cell with its column index.
      const headers = Array.from(table.querySelectorAll<HTMLElement>('thead th'));
      headers.forEach((th, i) => th.setAttribute('data-col', String(i)));

      // Snapshot original row order so we can restore it on the third click.
      const originalRows = Array.from(tbody.querySelectorAll('tr'));

      let activeCol = -1;
      let activeDir: 'asc' | 'desc' = 'asc';

      headers.forEach((th, col) => {
        const onClick = () => {
          if (activeCol === col) {
            if (activeDir === 'asc') {
              activeDir = 'desc';
            } else {
              // Third click — restore original order and clear indicators.
              activeCol = -1;
              headers.forEach(h => h.removeAttribute('data-sort'));
              originalRows.forEach(r => tbody.appendChild(r));
              return;
            }
          } else {
            activeCol = col;
            activeDir = 'asc';
          }

          headers.forEach(h => h.removeAttribute('data-sort'));
          th.setAttribute('data-sort', activeDir);

          const rows = Array.from(tbody.querySelectorAll('tr'));
          rows.sort((a, b) => {
            const aText = a.querySelectorAll('td')[col]?.textContent?.trim() ?? '';
            const bText = b.querySelectorAll('td')[col]?.textContent?.trim() ?? '';
            // Numeric sort when both cells look like numbers (strips currency/units).
            const aNum = parseFloat(aText.replace(/[^0-9.-]/g, ''));
            const bNum = parseFloat(bText.replace(/[^0-9.-]/g, ''));
            const numeric = aText !== '' && bText !== '' && !isNaN(aNum) && !isNaN(bNum);
            const cmp = numeric
              ? aNum - bNum
              : aText.localeCompare(bText, undefined, { sensitivity: 'base', numeric: true });
            return activeDir === 'asc' ? cmp : -cmp;
          });
          rows.forEach(r => tbody.appendChild(r));
        };

        th.addEventListener('click', onClick);
        cleanups.push(() => th.removeEventListener('click', onClick));
      });
    });

    // Add native title tooltip to every td whose content is truncated by CSS ellipsis.
    container.querySelectorAll<HTMLTableCellElement>('td').forEach((td) => {
      if (td.scrollWidth > td.clientWidth) {
        td.title = td.textContent?.trim() ?? '';
      }
    });

    return () => cleanups.forEach(fn => fn());
  }, [html]);

  return (
    <div
      ref={containerRef}
      className="text-part markdown-body"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
