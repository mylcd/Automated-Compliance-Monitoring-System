import { useEffect, useMemo, useRef } from 'react';

export type EvidenceHighlight = { path: string; reason: string; control_id: string; subject_id: string; restaurant_id: string };
export type EvidenceAnnotations = {
  assessment_id: string | null; evaluated_at?: string; result_ids: string[];
  issues: { result_id: string; control_id: string; restaurant_id: string; subject_id: string; reason: string; missing: boolean; explanation?: string; customer_id?: string; purpose_id?: string; activity_at?: string }[];
  highlights: EvidenceHighlight[];
};
type Line = { text: string; path: string };
const pointer = (key: string) => key.replaceAll('~', '~0').replaceAll('/', '~1');

function lines(value: unknown, path = '', depth = 0, prefix = '', comma = false): Line[] {
  const indent = '  '.repeat(depth);
  const end = comma ? ',' : '';
  if (value === null || typeof value !== 'object') return [{ path, text: indent + prefix + JSON.stringify(value) + end }];
  const array = Array.isArray(value);
  const entries = Object.entries(value);
  if (!entries.length) return [{ path, text: indent + prefix + (array ? '[]' : '{}') + end }];
  return [{ path, text: indent + prefix + (array ? '[' : '{') },
    ...entries.flatMap(([key, item], index) => lines(item, `${path}/${pointer(key)}`, depth + 1, array ? '' : `${JSON.stringify(key)}: `, index < entries.length - 1)),
    { path, text: indent + (array ? ']' : '}') + end }];
}

export function EvidenceJson({ value, highlights }: { value: unknown; highlights: EvidenceHighlight[] }) {
  const first = useRef<HTMLElement>(null);
  const rendered = useMemo(() => lines(value).map(line => ({ ...line,
    matches: highlights.filter(h => line.path === h.path || line.path.startsWith(h.path + '/')) })), [value, highlights]);
  const firstIndex = rendered.findIndex(line => line.matches.length > 0);
  useEffect(() => {
    if (firstIndex < 0) return;
    const frame = requestAnimationFrame(() => first.current?.scrollIntoView({ block: 'center', inline: 'nearest' }));
    return () => cancelAnimationFrame(frame);
  }, [rendered, firstIndex]);
  return <pre className="evidence-json annotated-evidence" aria-label="Evidence JSON">{rendered.map((line, index) => line.matches.length ?
    <mark key={index} ref={index === firstIndex ? first : undefined} className="failed-evidence-value" data-evidence-path={line.path} data-highlighted="failure" title={line.matches.map(h => `${h.control_id} · ${h.subject_id}: ${h.reason}`).join('\n')}>{line.text}</mark> :
    <span key={index} data-evidence-path={line.path}>{line.text}</span>)}</pre>;
}
