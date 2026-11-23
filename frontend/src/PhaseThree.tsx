import { FormEvent, ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  Alert, Button, Checkbox, Chip, CircularProgress, Dialog, DialogContent, DialogTitle,
  FormControl, FormControlLabel, FormGroup, FormLabel, IconButton, InputLabel,
  LinearProgress, MenuItem, Select, Table, TableBody, TableCell, TableContainer,
  TableHead, TablePagination, TableRow, TextField, Typography,
} from '@mui/material';
import { Close } from '@mui/icons-material';
import { ApiError, Identity, request } from './api';
import { EvidenceDetail, EvidenceTarget } from './PhaseTwo';

type Common = { onUnauthorized: () => void; refreshKey: number };
type EvaluationStatus = 'pass' | 'fail' | 'unknown' | 'not_applicable' | 'error';
type AssessmentStatus = 'queued' | 'running' | 'completed' | 'failed';
type Page<T> = { items: T[]; total: number; limit: number; offset: number };
type Subject = { subject_id: string; subject_type: string; status: EvaluationStatus; reason: string };
type Result = {
  id: string; assessment_id: string; restaurant_id: string; control_id: string; title: string;
  rule_version: string; status: EvaluationStatus; severity: string; reason: string; evaluated_at: string;
  remediation: string;
  framework_mappings: { framework_id: string; requirement_ref: string; mapping_note: string }[];
  details: Subject[];
  evidence: { id: string; dataset_type: string; sha256: string; captured_at: string; restaurant_ids: string[] }[];
};
type Assessment = {
  id: string; status: AssessmentStatus; restaurant_ids: string[]; framework_ids: string[];
  created_at: string; started_at: string | null; finished_at: string | null; evaluated_at: string | null;
  requested_by: string; completed_evaluations: number; total_evaluations: number; error: string | null;
  policy: { max_evidence_age_days: number };
  summary: {
    counts: Record<EvaluationStatus, number>; pass_rate: number | null;
    applicable_evaluations: number; total_evaluations: number;
    coverage: { total_restaurants: number; in_scope_restaurants: number; assessed_restaurants: number };
  };
};
type Finding = {
  id: string; restaurant_id: string | null; target_type: 'restaurant' | 'shared_system'; target_id: string; affected_restaurant_ids: string[]; control_id: string; title: string; severity: string; status: FindingStatus;
  first_seen_at: string; last_seen_at: string; occurrences: number; first_result_id: string;
  last_result_id: string; reason: string; remediation: string; latest_evaluation_status: EvaluationStatus;
  owner_employee_id: string | null; owner_name: string | null; due_date: string | null; notes: string;
  revision: number; resolved_at: string | null; resolved_result_id: string | null; reopen_count: number; updated_at: string;
};
type FindingStatus = 'open' | 'in_progress' | 'awaiting_verification' | 'resolved';
type Correction = {
  action: 'restore_consent_evidence' | 'enable_tls'; title: string; description: string; already_applied: boolean;
  changes: { dataset: string; subject_id: string; field: string; before: unknown; after: unknown }[];
};
type FindingDetail = Finding & { result: Result; history: Pick<Result, 'id' | 'assessment_id' | 'status' | 'evaluated_at' | 'reason'>[]; correction_actions: Correction[] };
type Employee = { id: string; name: string; job_role: string; active: boolean; restaurant_ids: string[] };
type AuditEvent = { id: string; finding_id: string | null; assessment_id: string | null; actor: string; event_type: string; occurred_at: string; before: Record<string, unknown>; after: Record<string, unknown>; details: Record<string, unknown> };
type Restaurant = { id: string; name?: string };
const statuses: EvaluationStatus[] = ['pass', 'fail', 'unknown', 'not_applicable', 'error'];
const frameworkIds = ['GDPR', 'PCI-DSS'];
const findingStatuses: FindingStatus[] = ['open', 'in_progress', 'awaiting_verification', 'resolved'];
const label = (s: string) => s.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const stamp = (s?: string | null) => s ? new Date(s).toLocaleString() : '—';
const activeAssessment = (a?: Assessment | null) => a?.status === 'queued' || a?.status === 'running';
const enc = encodeURIComponent;
const findingScope = (f: Finding) => f.restaurant_id ?? `Shared · ${f.target_id}`;
function parameters(values: Record<string, string>) {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value) query.set(key, value); });
  return query;
}
function message(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 409) return `Conflict: ${cause.message} Refresh the current record or wait for the active assessment, then try again.`;
  return cause instanceof Error ? cause.message : 'Unable to complete the request.';
}

// Poll serially, stop on unmount/terminal state, and discard obsolete responses.
function useResource<T>(path: string, common: Common, poll?: (value: T) => boolean, revisionToken?: string) {
  const [state, setState] = useState<{ path: string; data?: T; loading: boolean; error: string }>({ path, loading: true, error: '' });
  const [revision, setRevision] = useState(0);
  const pollRef = useRef(poll);
  pollRef.current = poll;
  const retry = useCallback(() => setRevision(n => n + 1), []);
  const replace = useCallback((data: T) => setState({ path, data, loading: false, error: '' }), [path]);
  useEffect(() => {
    if (!path) { setState({ path, loading: false, error: '' }); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState({ path, loading: true, error: '' });
    async function load() {
      try {
        const data = await request<T>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState({ path, data, loading: false, error: '' });
        if (pollRef.current?.(data)) timer = setTimeout(() => { void load(); }, 2500);
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) common.onUnauthorized();
        else setState(previous => ({ ...previous, path, loading: false, error: message(cause) }));
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, common.refreshKey, common.onUnauthorized, revision, revisionToken]);
  return { ...(state.path === path ? state : { loading: true, error: '', data: undefined }), retry, replace };
}

function Load({ state, children }: { state: { loading: boolean; error: string; retry: () => void }; children: ReactNode }) {
  if (state.loading) return <div className="data-loading" role="status"><CircularProgress size={24} />Loading…</div>;
  return <>{state.error && <Alert severity="error" sx={{ mb: 2 }} action={<Button onClick={state.retry}>Retry</Button>}>{state.error}</Alert>}{children}</>;
}
function Status({ value }: { value?: string | null }) {
  const color = value === 'pass' || value === 'completed' || value === 'resolved' ? 'success' : value === 'fail' || value === 'failed' || value === 'error' ? 'error' : value === 'unknown' || value === 'open' ? 'warning' : value === 'in_progress' || value === 'awaiting_verification' ? 'info' : 'default';
  return <Chip label={label(value ?? 'not_assessed')} color={color} variant="outlined" size="small" />;
}
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const titleId = useId();
  return <Dialog open fullWidth maxWidth="lg" onClose={onClose} aria-labelledby={titleId}><DialogTitle id={titleId} className="phase-three-dialog-title">{title}<IconButton aria-label={`Close ${title.toLowerCase()}`} onClick={onClose}><Close /></IconButton></DialogTitle><DialogContent dividers>{children}</DialogContent></Dialog>;
}
function Records<T extends { id: string }>({ rows, columns, empty, pagination }: {
  rows: T[]; columns: { title: string; render: (row: T) => ReactNode }[]; empty: string;
  pagination?: { page: number; size: number; total: number; change: (page: number) => void };
}) {
  return <div className="data-table phase-three-table"><TableContainer><Table size="small"><TableHead><TableRow>{columns.map(c => <TableCell key={c.title}>{c.title}</TableCell>)}</TableRow></TableHead><TableBody>
    {rows.map(row => <TableRow key={row.id}>{columns.map(c => <TableCell key={c.title}><div className="table-value" style={c.title === 'Reason' ? { minWidth: 220 } : undefined}>{c.render(row)}</div></TableCell>)}</TableRow>)}
    {!rows.length && <TableRow><TableCell colSpan={columns.length}><div className="table-empty">{empty}</div></TableCell></TableRow>}
  </TableBody></Table></TableContainer>{pagination && <TablePagination component="div" count={pagination.total} page={pagination.page} rowsPerPage={pagination.size} rowsPerPageOptions={[]} onPageChange={(_, n) => pagination.change(n)} />}</div>;
}
function Filter({ title, value, options, onChange }: { title: string; value: string; options: string[]; onChange: (value: string) => void }) {
  const id = useId();
  return <FormControl size="small" sx={{ minWidth: 170 }}><InputLabel id={id}>{title}</InputLabel><Select labelId={id} label={title} value={value} onChange={e => onChange(e.target.value)}><MenuItem value="">All</MenuItem>{options.map(o => <MenuItem key={o} value={o}>{label(o)}</MenuItem>)}</Select></FormControl>;
}
function RestaurantSelect(props: { value: string; onChange: (value: string) => void }) {
  return <Filter title="Restaurant" options={['R001', 'R002', 'R003', 'R004', 'R005']} {...props} />;
}
function Summary({ assessment: a }: { assessment: Assessment }) {
  const s = a.summary;
  return <>
    <div className="phase-three-rate"><span>Automated-check pass rate</span><strong>{a.status === 'completed' && s?.pass_rate !== null && s?.pass_rate !== undefined ? `${s.pass_rate}%` : 'N/A'}</strong></div>
    <div className="phase-three-counts">{statuses.map(status => <div key={status}><Status value={status} /><strong>{s?.counts?.[status] ?? 0}</strong></div>)}</div>
    <p className="data-caption">{s ? <>{s.counts.pass} passing / {s.applicable_evaluations} applicable evaluations · {s.total_evaluations} total evaluations<br />Coverage: {s.coverage.assessed_restaurants} assessed / {s.coverage.in_scope_restaurants} in scope / {s.coverage.total_restaurants} total restaurants</> : 'Summary pending.'}</p>
  </>;
}
function Progress({ assessment: a }: { assessment: Assessment }) {
  if (!activeAssessment(a)) return null;
  const total = a.total_evaluations;
  return <div className="phase-three-progress" role="status" aria-live="polite"><Typography variant="body2">{label(a.status)} · {a.completed_evaluations} / {total} evaluations</Typography><LinearProgress variant={total > 0 ? 'determinate' : 'indeterminate'} value={total > 0 ? Math.min(100, 100 * a.completed_evaluations / total) : undefined} /><small>Updates every few seconds.</small></div>;
}

export function AssessmentOverview({ onAssessments, onFindings, ...common }: Common & { onAssessments: () => void; onFindings: () => void }) {
  const state = useResource<{ assessed_restaurants: number; restaurants: number; open_findings: number; resolved_findings: number; latest_assessment: Assessment | null }>('/overview', common, data => activeAssessment(data.latest_assessment));
  const latest = state.data?.latest_assessment;
  return <section className="phase-three-panel"><div className="section-heading"><h2>Assessments</h2><Button onClick={onAssessments}>View history</Button></div><Load state={state}>{state.data && <>
    <div className="phase-three-overview"><span>Assessed restaurants <strong>{state.data.assessed_restaurants} / {state.data.restaurants}</strong></span><Button onClick={onFindings}>Open findings: {state.data.open_findings ?? 0}</Button><Button onClick={onFindings}>Resolved findings: {state.data.resolved_findings ?? 0}</Button></div>
    <p className="data-caption">Open findings include work in progress and findings awaiting verification.</p>
    {latest ? <><div className="data-toolbar"><Status value={latest.status} /><span className="phase-three-id">{latest.id}</span><span>{stamp(latest.created_at)}</span></div><p className="data-caption">Latest scope: {latest.restaurant_ids.join(', ')} · {latest.framework_ids.join(', ')}</p><Progress assessment={latest} /><Summary assessment={latest} /></> : <p className="data-caption">No assessments have run. Select restaurants and frameworks to start an assessment.</p>}
    <Button variant="contained" onClick={onAssessments}>Assess restaurants</Button>
  </>}</Load></section>;
}

function Assessments({ common, busy, run, openAssessment }: { common: Common; busy: boolean; run: (path: string, body?: object) => Promise<boolean>; openAssessment: (id: string) => void }) {
  const [restaurants, setRestaurants] = useState<string[]>([]);
  const [frameworks, setFrameworks] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [validation, setValidation] = useState('');
  const inventory = useResource<Page<Restaurant>>('/restaurants', common);
  const history = useResource<Page<Assessment>>(`/assessments?limit=25&offset=${page * 25}`, common, data => data.items.some(activeAssessment));
  function toggle(value: string, values: string[], setter: (values: string[]) => void) { setter(values.includes(value) ? values.filter(v => v !== value) : [...values, value]); }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!restaurants.length || !frameworks.length) { setValidation('Select at least one restaurant and one framework.'); return; }
    setValidation('');
    if (await run('/assessments', { restaurant_ids: restaurants, framework_ids: frameworks })) setPage(0);
  }
  return <><section className="phase-three-panel"><h2>New assessment</h2><form onSubmit={submit}>
    <p className="data-caption">Assess the current imported evidence snapshots. Evidence freshness is checked against the configured demo policy; the default maximum age is 30 days.</p>
    <Load state={inventory}>{inventory.data && <>
      {!inventory.data.items.length ? <Alert severity="info">No restaurants available.</Alert> : <FormControl component="fieldset" disabled={busy}><FormLabel component="legend">Restaurants</FormLabel><FormGroup row>{inventory.data.items.map(r => <FormControlLabel key={r.id} control={<Checkbox checked={restaurants.includes(r.id)} onChange={() => toggle(r.id, restaurants, setRestaurants)} />} label={r.name ? `${r.id} · ${r.name}` : r.id} />)}</FormGroup></FormControl>}
    </>}</Load>
    <FormControl component="fieldset" disabled={busy} sx={{ display: 'flex', my: 2 }}><FormLabel component="legend">Frameworks</FormLabel><FormGroup row>{frameworkIds.map(f => <FormControlLabel key={f} control={<Checkbox checked={frameworks.includes(f)} onChange={() => toggle(f, frameworks, setFrameworks)} />} label={f} />)}</FormGroup></FormControl>
    {validation && <Alert severity="warning" sx={{ mb: 2 }}>{validation}</Alert>}
    <Button type="submit" variant="contained" disabled={busy || inventory.loading || !!inventory.error || !inventory.data?.items.length} startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}>{busy ? 'Submitting…' : 'Start assessment'}</Button>
  </form></section><div className="section-heading"><h2>History</h2><Button onClick={history.retry}>Refresh history</Button></div><Load state={history}>{history.data && <Records rows={history.data.items} empty="No assessments" pagination={{ page, size: 25, total: history.data.total, change: setPage }} columns={[
    { title: 'Assessment', render: a => <Button size="small" onClick={() => openAssessment(a.id)}>{a.id}</Button> },
    { title: 'Status', render: a => <Status value={a.status} /> },
    { title: 'Scope', render: a => <>{a.restaurant_ids.join(', ')}<br />{a.framework_ids.join(', ')}</> },
    { title: 'Created', render: a => stamp(a.created_at) },
    { title: 'Progress', render: a => `${a.completed_evaluations} / ${a.total_evaluations}` },
  ]} />}</Load></>;
}

function AssessmentDetail({ id, common, busy, run, onClose, openResult, mutationError }: { id: string; common: Common; busy: boolean; run: (path: string, body?: object) => Promise<boolean>; onClose: () => void; openResult: (id: string) => void; mutationError: ReactNode }) {
  const state = useResource<Assessment>(`/assessments/${enc(id)}`, common, activeAssessment);
  const [restaurant, setRestaurant] = useState('');
  const [status, setStatus] = useState('');
  const [control, setControl] = useState('');
  const [page, setPage] = useState(0);
  const query = parameters({ limit: '50', offset: String(page * 50), restaurant_id: restaurant, status, control_id: control.trim() });
  // Refresh results at progress transitions, including the terminal response.
  const results = useResource<Page<Result>>(`/assessments/${enc(id)}/results?${query}`, common, () => activeAssessment(state.data), `${state.data?.status}:${state.data?.completed_evaluations}`);
  function filter(setter: (s: string) => void, value: string) { setter(value); setPage(0); }
  const a = state.data;
  return <Modal title="Assessment" onClose={onClose}>{mutationError}<Load state={state}>{a && <>
    <div className="data-toolbar"><span className="phase-three-id">{a.id}</span><Status value={a.status} /></div>
    <div className="detail-fields"><div><small>Restaurants</small><strong>{a.restaurant_ids.join(', ')}</strong></div><div><small>Frameworks</small><strong>{a.framework_ids.join(', ')}</strong></div><div><small>Requested by</small><strong>{a.requested_by}</strong></div><div><small>Created</small><strong>{stamp(a.created_at)}</strong></div><div><small>Started</small><strong>{stamp(a.started_at)}</strong></div><div><small>Finished</small><strong>{stamp(a.finished_at)}</strong></div><div><small>Evaluation time</small><strong>{stamp(a.evaluated_at)}</strong></div><div><small>Maximum evidence age</small><strong>{a.policy?.max_evidence_age_days ?? '—'} days</strong></div></div>
    <Progress assessment={a} /><Summary assessment={a} />
    {a.error && <Alert severity="error" sx={{ mb: 2 }}>{a.error}</Alert>}
    {a.status === 'failed' && <div className="phase-three-retry"><Button variant="outlined" disabled={busy} onClick={() => { void run(`/assessments/${enc(a.id)}/retry`); }}>{busy ? 'Submitting…' : 'Retry failed scope'}</Button><p className="data-caption">Creates a new assessment. This history entry is preserved.</p></div>}
    <h3>Results</h3><div className="data-toolbar"><RestaurantSelect value={restaurant} onChange={v => filter(setRestaurant, v)} /><Filter title="Status" value={status} options={statuses} onChange={v => filter(setStatus, v)} /><TextField size="small" label="Control ID" value={control} onChange={e => filter(setControl, e.target.value)} /><Button onClick={() => { setRestaurant(''); setStatus(''); setControl(''); setPage(0); }}>Clear filters</Button></div>
    <Load state={results}>{results.data && <Records rows={results.data.items} empty={activeAssessment(a) ? 'Results are pending for this selection.' : 'No results match these filters.'} pagination={{ page, size: 50, total: results.data.total, change: setPage }} columns={[
      { title: 'Control', render: r => <Button size="small" onClick={() => openResult(r.id)}>{r.control_id}</Button> },
      { title: 'Title', render: r => r.title }, { title: 'Restaurant', render: r => r.restaurant_id },
      { title: 'Status', render: r => <Status value={r.status} /> }, { title: 'Severity', render: r => label(r.severity) },
      { title: 'Reason', render: r => r.reason },
    ]} />}</Load>
  </>}</Load></Modal>;
}

function ResultBody({ result: r, openEvidence }: { result: Result; openEvidence: (id: string, resultId?: string) => void }) {
  const [page, setPage] = useState(0);
  return <><div className="data-toolbar"><Status value={r.status} /><Chip size="small" label={label(r.severity)} variant="outlined" /></div><Typography component="h3" variant="h6">{r.control_id} · {r.title}</Typography><p>{r.reason}</p>
    <h3>Remediation guidance</h3><p>{r.remediation}</p>
    <div className="detail-fields"><div><small>Restaurant</small><strong>{r.restaurant_id}</strong></div><div><small>Rule version</small><strong>{r.rule_version}</strong></div><div><small>Evaluated</small><strong>{stamp(r.evaluated_at)}</strong></div><div><small>Assessment</small><strong className="phase-three-id">{r.assessment_id}</strong></div></div>
    <h3>Framework references</h3>{r.framework_mappings.length ? <ul className="phase-three-mappings">{r.framework_mappings.map((m, i) => <li key={`${m.framework_id}-${m.requirement_ref}-${i}`}><strong>{m.framework_id} · {m.requirement_ref}</strong>{m.mapping_note && <p>{m.mapping_note}</p>}</li>)}</ul> : <p className="data-caption">No framework references.</p>}
    <h3>Subjects</h3><Records rows={r.details.slice(page * 25, (page + 1) * 25).map((s, i) => ({ ...s, id: `${page}-${i}` }))} empty="No subject details" pagination={{ page, size: 25, total: r.details.length, change: setPage }} columns={[
      { title: 'Subject', render: s => s.subject_id }, { title: 'Type', render: s => label(s.subject_type) },
      { title: 'Status', render: s => <Status value={s.status} /> }, { title: 'Reason', render: s => s.reason },
    ]} />
    <h3>Evidence</h3><Records rows={r.evidence} empty="No supporting evidence" columns={[
      { title: 'Snapshot', render: e => <Button size="small" aria-label={`View ${label(e.dataset_type)} evidence`} title={e.id} onClick={() => openEvidence(e.id, r.id)}>View snapshot</Button> },
      { title: 'Dataset', render: e => label(e.dataset_type) }, { title: 'Restaurants', render: e => e.restaurant_ids.join(', ') },
      { title: 'Captured', render: e => stamp(e.captured_at) }, { title: 'SHA-256', render: e => <span className="phase-three-hash">{e.sha256}</span> },
    ]} />
  </>;
}
function ResultDetail({ id, common, onClose, openEvidence, openAssessment }: { id: string; common: Common; onClose: () => void; openEvidence: (id: string, resultId?: string) => void; openAssessment: (id: string) => void }) {
  const state = useResource<Result>(`/results/${enc(id)}`, common);
  return <Modal title="Result" onClose={onClose}><Load state={state}>{state.data && <><Typography className="phase-three-id" variant="body2" sx={{ mb: 2 }}>{state.data.id}</Typography><ResultBody result={state.data} openEvidence={openEvidence} /><Button sx={{ mt: 2 }} onClick={() => openAssessment(state.data!.assessment_id)}>View assessment</Button></>}</Load></Modal>;
}

function auditValue(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return '—';
  if (typeof value === 'string') return value || '(empty)';
  return JSON.stringify(value, null, 2);
}

function AuditLog({ common, findingId, openFinding, openAssessment }: {
  common: Common; findingId?: string; openFinding?: (id: string) => void; openAssessment: (id: string) => void;
}) {
  const [finding, setFinding] = useState('');
  const [assessment, setAssessment] = useState('');
  const [eventType, setEventType] = useState('');
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<AuditEvent>();
  const query = parameters({ finding_id: findingId ?? finding.trim(), assessment_id: assessment.trim(), event_type: eventType.trim(), limit: '25', offset: String(page * 25) });
  const state = useResource<Page<AuditEvent>>(`/audit-events?${query}`, common);
  function filter(setter: (value: string) => void, value: string) { setter(value); setPage(0); }
  return <section className="finding-audit" aria-label={findingId ? 'Finding audit history' : 'Audit events'}>
    <div className="section-heading"><h2>{findingId ? 'Audit history' : 'Audit events'}</h2><Button onClick={state.retry}>Refresh audit</Button></div>
    {!findingId && <form className="data-toolbar" onSubmit={e => { e.preventDefault(); state.retry(); }}>
      <TextField size="small" label="Finding ID" value={finding} onChange={e => filter(setFinding, e.target.value)} />
      <TextField size="small" label="Assessment ID" value={assessment} onChange={e => filter(setAssessment, e.target.value)} />
      <TextField size="small" label="Event type" placeholder="Exact event type" value={eventType} onChange={e => filter(setEventType, e.target.value)} />
      <Button onClick={() => { setFinding(''); setAssessment(''); setEventType(''); setPage(0); }}>Clear filters</Button>
    </form>}
    <Load state={state}>{state.data && <Records rows={state.data.items} empty="No audit events match these filters." pagination={{ page, size: 25, total: state.data.total, change: setPage }} columns={[
      { title: 'Occurred', render: e => stamp(e.occurred_at) },
      { title: 'Event', render: e => <Button size="small" onClick={() => setSelected(e)}>{label(e.event_type)}</Button> },
      { title: 'Actor', render: e => e.actor },
      ...(!findingId ? [{ title: 'Finding', render: (e: AuditEvent) => e.finding_id && openFinding ? <Button size="small" onClick={() => openFinding(e.finding_id!)}>{e.finding_id}</Button> : e.finding_id ?? '—' }] : []),
      { title: 'Assessment', render: e => e.assessment_id ? <Button size="small" onClick={() => openAssessment(e.assessment_id!)}>{e.assessment_id}</Button> : '—' },
      { title: 'Details', render: e => <Button size="small" aria-label={`View ${label(e.event_type)} event details`} onClick={() => setSelected(e)}>View event</Button> },
    ]} />}</Load>
    {selected && <Modal title="Audit event" onClose={() => setSelected(undefined)}>
      <div className="detail-fields"><div><small>Event</small><strong>{label(selected.event_type)}</strong></div><div><small>Actor</small><strong>{selected.actor}</strong></div><div><small>Occurred</small><strong>{stamp(selected.occurred_at)}</strong></div><div><small>Event ID</small><strong>{selected.id}</strong></div></div>
      <div className="data-toolbar">{selected.finding_id && openFinding && <Button onClick={() => { openFinding(selected.finding_id!); setSelected(undefined); }}>View finding</Button>}{selected.assessment_id && <Button onClick={() => { openAssessment(selected.assessment_id!); setSelected(undefined); }}>View assessment</Button>}</div>
      <div className="audit-values">{(['before', 'after', 'details'] as const).map(field => <div key={field}><h3>{label(field)}</h3><pre>{auditValue(selected[field])}</pre></div>)}</div>
    </Modal>}
  </section>;
}

type FindingPatch = { owner_employee_id?: string | null; due_date?: string | null; notes?: string; status?: Exclude<FindingStatus, 'resolved'>; expected_revision: number };
function FindingEditor({ finding: f, common, disabled, save }: { finding: FindingDetail; common: Common; disabled: boolean; save: (patch: FindingPatch) => Promise<boolean> }) {
  const employees = useResource<Page<Employee>>('/employees?limit=200', common);
  const [owner, setOwner] = useState(f.owner_employee_id ?? '');
  const [due, setDue] = useState(f.due_date ?? '');
  const [notes, setNotes] = useState(f.notes ?? '');
  const [status, setStatus] = useState(f.status);
  const patch: FindingPatch = { expected_revision: f.revision };
  if (owner !== (f.owner_employee_id ?? '')) patch.owner_employee_id = owner || null;
  if (due !== (f.due_date ?? '')) patch.due_date = due || null;
  if (notes !== (f.notes ?? '')) patch.notes = notes;
  if (status !== f.status && status !== 'resolved' && f.status !== 'resolved') patch.status = status;
  const dirty = Object.keys(patch).length > 1;
  // Keep edits when a request conflicts; a successful update remounts this editor at the new revision.
  const owners = employees.data?.items ?? [];
  return <form className="finding-editor" onSubmit={e => { e.preventDefault(); if (dirty && !disabled) void save(patch); }}>
    <h3>Assignment and tracking</h3>
    <Load state={employees}><div className="finding-editor-fields">
      <FormControl size="small" disabled={disabled || !employees.data || !!employees.error}>
        <InputLabel id="finding-owner-label">Owner</InputLabel><Select labelId="finding-owner-label" label="Owner" value={owner} onChange={e => setOwner(e.target.value)}>
          <MenuItem value="">Unassigned</MenuItem>
          {owner && !owners.some(e => e.id === owner) && <MenuItem value={owner} disabled>{f.owner_name ?? owner}</MenuItem>}
          {owners.map(e => <MenuItem key={e.id} value={e.id} disabled={!e.active}>{e.name} · {e.id} · {e.job_role} · {e.restaurant_ids.join(', ') || 'No restaurant'}{!e.active ? ' · Inactive' : ''}</MenuItem>)}
        </Select>
      </FormControl>
      <TextField size="small" type="date" label="Due date" value={due} onChange={e => setDue(e.target.value)} disabled={disabled} slotProps={{ inputLabel: { shrink: true } }} />
      <FormControl size="small" disabled={disabled || f.status === 'resolved'}><InputLabel id="finding-status-label">Status</InputLabel><Select labelId="finding-status-label" label="Status" value={status} onChange={e => setStatus(e.target.value as FindingStatus)}>
        {(f.status === 'resolved' ? ['resolved'] : findingStatuses.filter(s => s !== 'resolved')).map(s => <MenuItem key={s} value={s}>{label(s)}</MenuItem>)}
      </Select></FormControl>
    </div></Load>
    <TextField label="Notes" value={notes} onChange={e => setNotes(e.target.value)} multiline minRows={3} fullWidth disabled={disabled} />
    <p className="data-caption">Resolution is recorded after a later applicable assessment passes. Unknown or not applicable results preserve the current finding state.</p>
    <Button type="submit" variant="contained" disabled={disabled || !dirty}>Save changes</Button>
  </form>;
}

function Findings({ common, openFinding }: { common: Common; openFinding: (id: string) => void }) {
  const [restaurant, setRestaurant] = useState('');
  const [severity, setSeverity] = useState('');
  const [status, setStatus] = useState('');
  const [control, setControl] = useState('');
  const [page, setPage] = useState(0);
  const query = parameters({ restaurant_id: restaurant, severity, status, control_id: control.trim(), limit: '25', offset: String(page * 25) });
  const state = useResource<Page<Finding>>(`/findings?${query}`, common);
  function filter(setter: (s: string) => void, value: string) { setter(value); setPage(0); }
  return <><div className="data-toolbar"><RestaurantSelect value={restaurant} onChange={v => filter(setRestaurant, v)} /><Filter title="Status" options={findingStatuses} value={status} onChange={v => filter(setStatus, v)} /><Filter title="Severity" options={['critical', 'high', 'medium', 'low']} value={severity} onChange={v => filter(setSeverity, v)} /><TextField size="small" label="Control ID" value={control} onChange={e => filter(setControl, e.target.value)} /><Button onClick={() => { setRestaurant(''); setSeverity(''); setStatus(''); setControl(''); setPage(0); }}>Clear filters</Button></div>
    <Load state={state}>{state.data && <Records rows={state.data.items} empty="No findings match these filters." pagination={{ page, size: 25, total: state.data.total, change: setPage }} columns={[
      { title: 'Finding', render: f => <Button size="small" onClick={() => openFinding(f.id)}>{f.control_id} · {f.title}</Button> },
      { title: 'Scope', render: f => findingScope(f) }, { title: 'Severity', render: f => label(f.severity) },
      { title: 'Status', render: f => <Status value={f.status} /> }, { title: 'Owner', render: f => f.owner_name ?? f.owner_employee_id ?? 'Unassigned' }, { title: 'Due date', render: f => f.due_date ?? '—' }, { title: 'Latest evaluation', render: f => <Status value={f.latest_evaluation_status} /> },
      { title: 'Occurrences', render: f => f.occurrences }, { title: 'Last seen', render: f => stamp(f.last_seen_at) },
    ]} />}</Load><p className="data-caption">Findings retain their failed results and evidence. Verified corrections resolve automatically; a recurrent failure reopens the finding.</p></>;
}
function FindingModal({ id, common, identity, busy, run, onChanged, onClose, openResult, openAssessment, openEvidence, assessmentFeedback }: {
  id: string; common: Common; identity: Identity; busy: boolean; run: (path: string, body?: object) => Promise<boolean>;
  onChanged: () => void; onClose: () => void; openResult: (id: string) => void; openAssessment: (id: string) => void; openEvidence: (id: string, resultId?: string) => void;
  assessmentFeedback: ReactNode;
}) {
  const state = useResource<FindingDetail>(`/findings/${enc(id)}`, common);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => { mutation.current?.abort(); }, []);
  async function mutate(patch?: FindingPatch, action?: Correction['action']) {
    if (mutation.current || busy || !state.data) return false;
    const controller = new AbortController();
    mutation.current = controller;
    setSaving(true); setError(''); setNotice('');
    try {
      const response = await request<FindingDetail | { finding: FindingDetail; changed: boolean; revision_id: string | null }>(`/findings/${enc(id)}${action ? '/corrections' : ''}`, {
        method: action ? 'POST' : 'PATCH', signal: controller.signal, headers: { 'X-CSRF-Token': identity.csrf_token },
        body: JSON.stringify(action ? { action, expected_revision: state.data.revision } : patch),
      });
      if (controller.signal.aborted) return false;
      const detail = 'finding' in response ? response.finding : response;
      state.replace(detail);
      setNotice('finding' in response ? response.changed ? 'Correction applied. Awaiting verification; re-scan this scope to verify.' : 'This correction is already applied.' : 'Finding updated.');
      onChanged();
      return true;
    } catch (cause) {
      if (controller.signal.aborted) return false;
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) common.onUnauthorized();
      else setError(cause instanceof ApiError && cause.status === 409 ? `${message(cause)} Your unsaved edits are still shown. Reloading the finding discards those edits.` : message(cause));
      return false;
    } finally { if (!controller.signal.aborted) { mutation.current = null; setSaving(false); } }
  }
  const [page, setPage] = useState(0);
  const f = state.data;
  const frameworks = f?.control_id.startsWith('LOY') ? ['GDPR'] : f?.control_id.startsWith('PAY') ? ['PCI-DSS'] : [...new Set(f?.result?.framework_mappings.map(m => m.framework_id) ?? [])];
  const restaurants = f?.restaurant_id ? [f.restaurant_id] : f?.affected_restaurant_ids ?? [];
  return <Modal title="Finding" onClose={onClose}>
    {assessmentFeedback}
    {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError('')}>{error}</Alert>}
    {notice && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice('')}>{notice}</Alert>}
    <div className="data-toolbar"><Button disabled={saving || busy} onClick={state.retry}>Reload finding</Button></div>
    <Load state={state}>{f && <>
    <Typography className="phase-three-id" variant="body2">{f.id}</Typography><Typography variant="h6" component="h3" sx={{ my: 2 }}>{f.control_id} · {f.title}</Typography><div className="data-toolbar"><Status value={f.status} /><Chip label={label(f.severity)} size="small" variant="outlined" /><span>Latest evaluation:</span><Status value={f.latest_evaluation_status} /></div><p>{f.reason}</p>
    <div className="detail-fields"><div><small>Scope</small><strong>{findingScope(f)}</strong></div><div><small>First seen</small><strong>{stamp(f.first_seen_at)}</strong></div><div><small>Last seen</small><strong>{stamp(f.last_seen_at)}</strong></div><div><small>Occurrences</small><strong>{f.occurrences}</strong></div></div>
    {f.target_type === 'shared_system' && <p>Affected restaurants: {f.affected_restaurant_ids.join(', ')}</p>}
    <div className="detail-fields"><div><small>Owner</small><strong>{f.owner_name ?? f.owner_employee_id ?? 'Unassigned'}</strong></div><div><small>Due date</small><strong>{f.due_date ?? '—'}</strong></div><div><small>Updated</small><strong>{stamp(f.updated_at)}</strong></div><div><small>Reopened</small><strong>{f.reopen_count ?? 0} times</strong></div></div>
    {f.status === 'resolved' && <Alert severity="success" sx={{ mb: 2 }}>Verified {stamp(f.resolved_at)}{f.resolved_result_id && <Button onClick={() => openResult(f.resolved_result_id!)}>View verification result</Button>}</Alert>}
    <FindingEditor key={`${f.id}:${f.revision}`} finding={f} common={common} disabled={saving || busy} save={patch => mutate(patch)} />
    <h3>Remediation guidance</h3><p>{f.remediation}</p>
    <h3>Source corrections</h3>
    {f.correction_actions?.length ? f.correction_actions.map(action => <section className="correction-card" key={action.action}>
      <div className="section-heading"><h2>{action.title}</h2>{action.already_applied && <Chip size="small" color="success" label="Already applied" />}</div><p>{action.description}</p>
      <TableContainer><Table size="small" aria-label={`${action.title} before and after preview`}><TableHead><TableRow>{['Dataset / subject', 'Field', 'Before', 'After'].map(title => <TableCell key={title}>{title}</TableCell>)}</TableRow></TableHead><TableBody>
        {action.changes.map((change, i) => <TableRow key={`${change.dataset}:${change.subject_id}:${change.field}:${i}`}><TableCell>{label(change.dataset)}<br />{change.subject_id}</TableCell><TableCell>{change.field}</TableCell><TableCell><pre>{auditValue(change.before)}</pre></TableCell><TableCell><pre>{auditValue(change.after)}</pre></TableCell></TableRow>)}
        {!action.changes.length && <TableRow><TableCell colSpan={4}>No source changes pending.</TableCell></TableRow>}
      </TableBody></Table></TableContainer>
      <Button sx={{ mt: 2 }} variant="contained" disabled={saving || busy || action.already_applied || f.status === 'resolved'} onClick={() => { void mutate(undefined, action.action); }}>Apply correction</Button>
    </section>) : <p className="data-caption">No automated source correction is available for this finding.</p>}
    <div className="data-toolbar"><Button variant="outlined" disabled={saving || busy || !restaurants.length || !frameworks.length} onClick={() => { setError(''); setNotice(''); void run('/assessments', { restaurant_ids: restaurants, framework_ids: frameworks }); }}>Re-scan</Button><span className="data-caption">{restaurants.join(', ')} · {frameworks.join(', ')}{busy ? ' · Assessment in progress' : ''}</span></div>
    <AuditLog common={common} findingId={f.id} openAssessment={openAssessment} />
    <div className="data-toolbar"><Button onClick={() => openResult(f.first_result_id)}>First failed result</Button><Button onClick={() => openResult(f.last_result_id)}>Last failed result</Button></div>
    <h3>Failure history</h3><Records rows={f.history.slice(page * 25, (page + 1) * 25)} empty="No failure history" pagination={{ page, size: 25, total: f.history.length, change: setPage }} columns={[
      { title: 'Result', render: h => <Button size="small" onClick={() => openResult(h.id)}>{h.id}</Button> },
      { title: 'Assessment', render: h => <Button size="small" onClick={() => openAssessment(h.assessment_id)}>{h.assessment_id}</Button> },
      { title: 'Evaluated', render: h => stamp(h.evaluated_at) }, { title: 'Reason', render: h => h.reason },
    ]} /><h3>Last failed evaluation</h3>{f.result ? <ResultBody result={f.result} openEvidence={openEvidence} /> : <p className="data-caption">Result unavailable.</p>}
  </>}</Load></Modal>;
}

export default function PhaseThree({ page, identity, ...common }: Common & { page: string; identity: Identity }) {
  const [assessment, setAssessment] = useState<string>();
  const [verificationId, setVerificationId] = useState<string>();
  const [finding, setFinding] = useState<string>();
  const [result, setResult] = useState<string>();
  const [evidence, setEvidence] = useState<EvidenceTarget>();
  const openEvidence = (id: string, resultId?: string) => setEvidence({ id, resultId });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => { mutation.current?.abort(); }, []);
  const changed = useCallback(() => setRevision(n => n + 1), []);
  const scoped = { ...common, refreshKey: common.refreshKey + revision };
  // Keep following a submitted assessment even when its dialog is closed.
  const verification = useResource<Assessment>(verificationId ? `/assessments/${enc(verificationId)}` : '', common, activeAssessment);
  useEffect(() => {
    if (verification.data && !activeAssessment(verification.data)) { changed(); setVerificationId(undefined); }
  }, [verification.data, changed]);
  async function run(path: string, body?: object) {
    if (mutation.current) return false;
    const controller = new AbortController();
    mutation.current = controller;
    setBusy(true); setError('');
    try {
      const created = await request<Assessment>(path, { method: 'POST', signal: controller.signal, headers: { 'X-CSRF-Token': identity.csrf_token }, ...(body ? { body: JSON.stringify(body) } : {}) });
      if (controller.signal.aborted) return false;
      setAssessment(created.id); setVerificationId(created.id); setRevision(n => n + 1);
      return true;
    } catch (cause) {
      if (controller.signal.aborted) return false;
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) common.onUnauthorized();
      else setError(message(cause));
      return false;
    } finally {
      if (!controller.signal.aborted) { mutation.current = null; setBusy(false); }
    }
  }
  const mutationError = error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError('')}>{error}</Alert>;
  const verificationError = verification.error && <Alert severity="error" sx={{ mb: 2 }} action={<Button onClick={verification.retry}>Retry tracking</Button>}>Assessment tracking stopped: {verification.error} The submitted assessment may still be running.</Alert>;
  return <div className="phase-three">{mutationError}{verificationError}
    {page === 'Assessments' ? <Assessments common={scoped} busy={busy} run={run} openAssessment={setAssessment} /> : page === 'Audit' ? <AuditLog common={scoped} openFinding={setFinding} openAssessment={setAssessment} /> : <Findings common={scoped} openFinding={setFinding} />}
    {finding && <FindingModal key={finding} id={finding} common={scoped} identity={identity} busy={busy || (!!verificationId && !verification.error)} run={run} onChanged={changed} onClose={() => setFinding(undefined)} openResult={setResult} openAssessment={setAssessment} openEvidence={openEvidence} assessmentFeedback={<>{mutationError}{verificationError}</>} />}
    {assessment && <AssessmentDetail key={assessment} id={assessment} common={scoped} busy={busy} run={run} onClose={() => setAssessment(undefined)} openResult={setResult} mutationError={mutationError} />}
    {result && <ResultDetail key={result} id={result} common={scoped} onClose={() => setResult(undefined)} openEvidence={openEvidence} openAssessment={id => { setResult(undefined); setAssessment(id); }} />}
    {evidence && <EvidenceDetail key={`${evidence.id}-${evidence.resultId}`} {...evidence} common={scoped} onClose={() => setEvidence(undefined)} />}
  </div>;
}
