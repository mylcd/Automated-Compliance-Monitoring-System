import { FormEvent, ReactNode, useEffect, useId, useState } from 'react';
import { Alert, Button, Checkbox, Chip, CircularProgress, FormControl, FormControlLabel, FormGroup, FormLabel, InputLabel, MenuItem, Select, Switch, Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, TextField } from '@mui/material';
import { DownloadOutlined } from '@mui/icons-material';
import { ApiError, downloadFile, Identity, request } from './api';
import { EvidenceDetail, EvidenceTarget } from './PhaseTwo';

type Common = { onUnauthorized: () => void; refreshKey: number };
type Assessment = { id: string; status: string; trigger: string; created_at: string; finished_at: string | null; restaurant_ids: string[]; framework_ids: string[]; error: string | null };
type Counts = { pass: number; fail: number; unknown: number; error: number; not_applicable: number };
type Rollup = { counts: Counts; total_evaluations: number; applicable_evaluations: number; pass_rate: number | null };
type Finding = { id: string; control_id: string; title: string; target_id: string; status: string; severity: string; owner_employee_id: string | null; due_date: string | null; overdue: boolean | null; evaluation_status: string };
type Evidence = { id: string; dataset_type: string; captured_at: string; age_days_at_assessment: number | null; stale: boolean | null };
type Snapshot = {
  filters: { restaurant_id: string | null; framework_id: string | null; severity: string | null };
  assessment: { id: string; trigger: string; finished_at: string; scope: { restaurant_ids: string[]; framework_ids: string[] } };
  coverage: { total_restaurants: number; in_scope_restaurants: number; assessed_restaurants: number; unassessed_restaurants: number };
  summary: Rollup & { active_findings: number | null; resolved_findings: number | null; overdue_findings: number | null; severity_counts: Record<string, number>; workflow_snapshot_available: boolean };
  frameworks: (Rollup & { id: string })[];
  findings: Finding[];
  results: { id: string; restaurant_id: string; control_id: string; title: string; status: string; reason: string }[];
  evidence_references: Evidence[];
  evidence_health: { max_age_days: number; oldest_age_days: number | null; stale_references: number; missing: { restaurant_id: string; dataset_type: string }[] };
};
type Dashboard = { snapshot: Snapshot | null; last_successful_scan: Assessment | null; latest_attempt: Assessment | null; history: Assessment[] };
type Schedule = { enabled: boolean; interval_minutes: number; restaurant_ids: string[]; framework_ids: string[]; revision: number; next_run_at: string | null; last_run_at: string | null; last_error: string | null; last_assessment: Assessment | null; active_assessment: Assessment | null };
type Selection = { assessment_id: string; restaurant_id: string; framework_id: string; severity: string };
const emptySelection = { assessment_id: '', restaurant_id: '', framework_id: '', severity: '' };
const restaurantIds = ['R001', 'R002', 'R003', 'R004', 'R005'];
const frameworkIds = ['GDPR', 'PCI-DSS'];
const statuses = ['pass', 'fail', 'unknown', 'error', 'not_applicable'] as const;
const label = (s: string) => s.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const date = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';
const query = (selection: Selection) => new URLSearchParams(Object.entries(selection).filter(([, value]) => value)).toString();

function useData<T>(path: string, common: Common, poll = false) {
  const [state, setState] = useState<{ path: string; data?: T; loading: boolean; error: string }>({ path, loading: true, error: '' });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState({ path, loading: true, error: '' });
    async function load() {
      try {
        const data = await request<T>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState({ path, data, loading: false, error: '' });
        if (poll) timer = setTimeout(() => { void load(); }, 5000);
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && [401, 403].includes(cause.status)) common.onUnauthorized();
        else setState({ path, loading: false, error: cause instanceof Error ? cause.message : 'Unable to load data.' });
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, common.refreshKey, common.onUnauthorized, poll, revision]);
  return { ...(state.path === path ? state : { loading: true, error: '', data: undefined }), retry: () => setRevision(n => n + 1) };
}

function Status({ value }: { value: string }) {
  const color = ['pass', 'completed', 'resolved'].includes(value) ? 'success' : ['fail', 'error', 'failed'].includes(value) ? 'error' : ['unknown', 'open'].includes(value) ? 'warning' : 'default';
  return <Chip label={label(value)} color={color} variant="outlined" size="small" />;
}

function SelectField({ title, value, options, onChange, all = true }: { title: string; value: string; options: { value: string; text: string }[]; onChange: (value: string) => void; all?: boolean }) {
  const id = useId();
  return <FormControl size="small" className={title === 'Assessment' ? 'snapshot-select' : ''} sx={{ minWidth: 150 }}><InputLabel id={id}>{title}</InputLabel><Select labelId={id} label={title} value={value} onChange={event => onChange(event.target.value)}>{all && <MenuItem value="">All</MenuItem>}{options.map(option => <MenuItem key={option.value} value={option.value}>{option.text}</MenuItem>)}</Select></FormControl>;
}

function DataTable({ headings, rows, empty }: { headings: string[]; rows: ReactNode[][]; empty: string }) {
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, Math.max(0, Math.ceil(rows.length / 10) - 1));
  return <div className="data-table"><TableContainer><Table size="small"><TableHead><TableRow>{headings.map(h => <TableCell key={h}>{h}</TableCell>)}</TableRow></TableHead><TableBody>{rows.slice(safePage * 10, safePage * 10 + 10).map((cells, i) => <TableRow key={i}>{cells.map((cell, j) => <TableCell key={j}><div className="table-value">{cell}</div></TableCell>)}</TableRow>)}{!rows.length && <TableRow><TableCell colSpan={headings.length}><div className="table-empty">{empty}</div></TableCell></TableRow>}</TableBody></Table></TableContainer>{rows.length > 10 && <TablePagination component="div" count={rows.length} page={safePage} rowsPerPage={10} rowsPerPageOptions={[]} onPageChange={(_, next) => setPage(next)} />}</div>;
}

export function ComplianceDashboard({ onAssessments, onFindings, ...common }: Common & { onAssessments: () => void; onFindings: () => void }) {
  const [selection, setSelection] = useState<Selection>(emptySelection);
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null);
  const [download, setDownload] = useState({ busy: false, error: '', complete: '' });
  const state = useData<Dashboard>(`/dashboard?${query(selection)}`, common, true);
  const schedule = useData<Schedule>('/schedule', common, true);
  const snapshot = state.data?.snapshot;
  // Pin the first completed assessment; newer scans cannot switch an open report.
  useEffect(() => {
    if (!selection.assessment_id && snapshot) setSelection(previous => ({ ...previous, assessment_id: snapshot.assessment.id }));
  }, [selection.assessment_id, snapshot]);
  function filter(key: keyof Selection, value: string) {
    setDownload({ busy: false, error: '', complete: '' });
    setSelection(previous => key === 'assessment_id' ? { ...emptySelection, assessment_id: value } : { ...previous, [key]: value });
  }
  async function exportYaml() {
    if (!snapshot) return;
    setDownload({ busy: true, error: '', complete: '' });
    try {
      await downloadFile(`/reports/${encodeURIComponent(snapshot.assessment.id)}.yaml?${query({ ...selection, assessment_id: '' })}`);
      setDownload({ busy: false, error: '', complete: 'YAML downloaded.' });
    } catch (cause) {
      if (cause instanceof ApiError && [401, 403].includes(cause.status)) common.onUnauthorized();
      else setDownload({ busy: false, complete: '', error: cause instanceof Error ? cause.message : 'Download failed.' });
    }
  }
  return <div className="monitoring-page">
    <section className="phase-three-panel">
      <div className="section-heading"><h2>Assessment snapshot</h2><Button onClick={onAssessments}>Scan history</Button></div>
      <p className="data-caption">Last successful scan: {date(state.data?.last_successful_scan?.finished_at)} · Schedule: {schedule.loading ? 'Loading…' : schedule.error ? 'Unavailable' : schedule.data?.enabled ? `Every ${schedule.data.interval_minutes} minutes` : 'Disabled'}</p>
      {schedule.error && <Alert severity="warning" action={<Button onClick={schedule.retry}>Retry</Button>}>Schedule status is unavailable.</Alert>}
      {snapshot && state.data?.last_successful_scan && snapshot.assessment.id !== state.data.last_successful_scan.id && <Alert severity="info" sx={{ mb: 2 }} action={<Button onClick={() => filter('assessment_id', state.data!.last_successful_scan!.id)}>Latest completed</Button>}>A newer completed scan is available. This snapshot remains pinned.</Alert>}
      {schedule.data?.enabled && <p className="data-caption">Next scheduled run: {date(schedule.data.next_run_at)}{schedule.data.active_assessment ? ' · Waiting for the active scan to finish' : ''}</p>}
      {schedule.data?.last_error && <Alert severity="error" sx={{ mb: 2 }}>{schedule.data.last_error}</Alert>}
      {schedule.data?.last_assessment?.status === 'failed' && <Alert severity="error" sx={{ mb: 2 }}>The last scheduled assessment failed. Open scan history to retry.</Alert>}
      {state.data?.latest_attempt?.status === 'failed' && <Alert severity="error" sx={{ mb: 2 }}>Latest scan failed: {state.data.latest_attempt.error} The snapshot below is from a completed scan.</Alert>}
      {state.data?.latest_attempt && ['queued', 'running'].includes(state.data.latest_attempt.status) && <Alert severity="info" sx={{ mb: 2 }}>A scan is {state.data.latest_attempt.status}. This snapshot remains pinned.</Alert>}
      <div className="data-toolbar snapshot-filters">
        <SelectField title="Assessment" value={selection.assessment_id} all={false} options={(state.data?.history ?? []).map(a => ({ value: a.id, text: `${date(a.created_at)} · ${a.trigger} · ${a.status} · ${a.id.slice(0, 10)}` }))} onChange={value => filter('assessment_id', value)} />
        <SelectField title="Restaurant" value={selection.restaurant_id} options={(snapshot?.assessment.scope.restaurant_ids ?? restaurantIds).map(value => ({ value, text: value }))} onChange={value => filter('restaurant_id', value)} />
        <SelectField title="Framework" value={selection.framework_id} options={(snapshot?.assessment.scope.framework_ids ?? frameworkIds).map(value => ({ value, text: value.replace('-', ' ') }))} onChange={value => filter('framework_id', value)} />
        <SelectField title="Severity" value={selection.severity} options={['critical', 'high', 'medium', 'low'].map(value => ({ value, text: label(value) }))} onChange={value => filter('severity', value)} />
      </div>
      {state.error && <Alert severity="error" action={<Button onClick={() => { setSelection(emptySelection); state.retry(); }}>Latest completed</Button>}>{state.error}</Alert>}
      {state.loading && <div className="data-loading" role="status"><CircularProgress size={22} />Loading snapshot…</div>}
      {!state.loading && !state.error && !snapshot && <div className="table-empty">No completed assessments. <Button onClick={onAssessments}>Run an assessment</Button></div>}
      {snapshot && <>
        <div className="snapshot-meta"><code>{snapshot.assessment.id}</code><span>{date(snapshot.assessment.finished_at)} · {label(snapshot.assessment.trigger)}</span></div>
        <p className="data-caption">Original scope: {snapshot.assessment.scope.restaurant_ids.join(', ')} · {snapshot.assessment.scope.framework_ids.join(', ')}. Metrics below use the selected filters.</p>
        <div className="monitoring-metrics">
          <div><span>Automated-check pass rate</span><strong>{snapshot.summary.pass_rate === null ? 'N/A' : `${snapshot.summary.pass_rate}%`}</strong><small>{snapshot.summary.counts.pass} / {snapshot.summary.applicable_evaluations} applicable evaluations</small></div>
          <div><span>Restaurant coverage</span><strong>{snapshot.coverage.assessed_restaurants} / {snapshot.coverage.total_restaurants}</strong><small>{snapshot.coverage.in_scope_restaurants} in scope · {snapshot.coverage.unassessed_restaurants} unassessed</small></div>
          <div><span>Active findings</span><strong>{snapshot.summary.active_findings ?? 'N/A'}</strong><small>{snapshot.summary.resolved_findings ?? 'N/A'} resolved at completion</small></div>
          <div><span>Overdue findings</span><strong>{snapshot.summary.overdue_findings ?? 'N/A'}</strong><small>As of {snapshot.assessment.finished_at.slice(0, 10)} UTC</small></div>
        </div>
        <div className="phase-three-counts">{statuses.map(status => <div key={status}><Status value={status} /><strong>{snapshot.summary.counts[status]}</strong></div>)}</div>
        <p className="data-caption">Unknown and error results remain in the pass-rate denominator. Not applicable results are excluded.</p>
        <div className="framework-summary">{snapshot.frameworks.map(f => <div key={f.id}><h3>{f.id.replace('-', ' ')}</h3><strong>{f.pass_rate === null ? 'N/A' : `${f.pass_rate}%`}</strong><span>{f.counts.pass} pass · {f.counts.fail} fail · {f.counts.unknown} unknown · {f.counts.error} error · {f.counts.not_applicable} not applicable</span></div>)}</div>
        <div className="severity-summary"><strong>{snapshot.summary.workflow_snapshot_available ? 'Active findings by severity' : 'Failure observations by severity'}</strong>{Object.entries(snapshot.summary.severity_counts).map(([severity, count]) => <Chip key={severity} label={`${label(severity)}: ${count}`} size="small" variant="outlined" />)}</div>
        {!snapshot.summary.workflow_snapshot_available && <Alert severity="info" sx={{ my: 2 }}>Historical workflow details were not captured for this scan. Finding observations remain available; active, resolved and overdue totals are unavailable.</Alert>}
        <div className="data-toolbar"><Button variant="contained" startIcon={download.busy ? <CircularProgress size={16} color="inherit" /> : <DownloadOutlined />} disabled={download.busy} onClick={exportYaml}>Download YAML</Button><Button onClick={onFindings}>Manage findings</Button>{download.complete && <span role="status">{download.complete}</span>}</div>
        {download.error && <Alert severity="error">{download.error}</Alert>}
        <p className="data-caption">Schema 1.0 · Export includes this assessment and these filters. Automated checks support review and do not certify compliance.</p>
      </>}
    </section>
    {snapshot && <>
      <section className="phase-three-panel"><div className="section-heading"><h2>Snapshot findings</h2><span>{snapshot.findings.length} records</span></div>
        <DataTable headings={['Target / control', 'Severity', 'Workflow', 'Evaluation', 'Owner / due date']} rows={snapshot.findings.map(f => [<><strong>{f.target_id} · {f.control_id}</strong><span className="data-caption">{f.title}</span></>, label(f.severity), <Status value={f.status} />, <Status value={f.evaluation_status} />, <>{f.owner_employee_id ?? 'Unassigned'}<span className="data-caption">{f.due_date ?? 'No due date'}{f.overdue ? ' · Overdue' : ''}</span></>])} empty="No findings match this assessment and its filters." />
      </section>
      <section className="phase-three-panel"><h2>Results</h2><DataTable headings={['Restaurant', 'Control', 'Status', 'Reason']} rows={snapshot.results.map(r => [r.restaurant_id, <><strong>{r.control_id}</strong><span className="data-caption">{r.title}</span></>, <Status value={r.status} />, <div className="snapshot-reason">{r.reason}</div>])} empty="No evaluations match the selected filters." /></section>
      <section className="phase-three-panel"><h2>Evidence</h2>
        <p className="data-caption">Oldest evidence at scan: {snapshot.evidence_health.oldest_age_days === null ? 'Unavailable' : `${snapshot.evidence_health.oldest_age_days.toFixed(1)} days`} · Maximum age: {snapshot.evidence_health.max_age_days} days · Stale references: {snapshot.evidence_health.stale_references}</p>
        {snapshot.evidence_health.missing.length > 0 && <Alert severity="warning" sx={{ mb: 2 }}>Missing pinned evidence: {snapshot.evidence_health.missing.map(e => `${e.restaurant_id} / ${e.dataset_type}`).join(', ')}. Review unknown/error results.</Alert>}
        <DataTable headings={['Dataset', 'Captured', 'Age at scan', 'Snapshot']} rows={snapshot.evidence_references.map(e => [label(e.dataset_type), date(e.captured_at), <>{e.age_days_at_assessment === null ? 'Unknown' : `${e.age_days_at_assessment.toFixed(1)} days`}{e.stale && <Chip size="small" color="warning" label="Stale" />}</>, <Button onClick={() => setEvidence({ id: e.id, assessmentId: snapshot.assessment.id, restaurantId: snapshot.filters.restaurant_id ?? undefined, frameworkId: snapshot.filters.framework_id ?? undefined, severity: snapshot.filters.severity ?? undefined })} aria-label={`View ${e.dataset_type} snapshot`}>View evidence</Button>])} empty="No evidence references were available for these evaluations." />
      </section>
    </>}
    {evidence && <EvidenceDetail {...evidence} onClose={() => setEvidence(null)} common={common} />}
  </div>;
}

function ScheduleForm({ schedule, identity, onSaved, ...common }: Common & { schedule: Schedule; identity: Identity; onSaved: (saved?: boolean) => void }) {
  const [enabled, setEnabled] = useState(schedule.enabled);
  const [interval, setInterval] = useState(String(schedule.interval_minutes));
  const [restaurants, setRestaurants] = useState(schedule.restaurant_ids);
  const [frameworks, setFrameworks] = useState(schedule.framework_ids);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState({ error: '', success: '' });
  const toggle = (values: string[], value: string) => values.includes(value) ? values.filter(v => v !== value) : [...values, value].sort();
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setFeedback({ error: '', success: '' });
    try {
      await request('/schedule', { method: 'PUT', headers: { 'X-CSRF-Token': identity.csrf_token }, body: JSON.stringify({ enabled, interval_minutes: Number(interval), restaurant_ids: restaurants, framework_ids: frameworks, expected_revision: schedule.revision }) });
      setFeedback({ error: '', success: 'Schedule saved.' }); onSaved(true);
    } catch (cause) {
      if (cause instanceof ApiError && [401, 403].includes(cause.status)) common.onUnauthorized();
      else setFeedback({ error: cause instanceof Error ? cause.message : 'Unable to save schedule.', success: '' });
    } finally { setBusy(false); }
  }
  return <form onSubmit={save} className="schedule-form">
    <FormControlLabel control={<Switch checked={enabled} disabled={busy} onChange={e => setEnabled(e.target.checked)} />} label="Scheduled scans" />
    <TextField label="Interval (minutes)" type="number" value={interval} onChange={e => setInterval(e.target.value)} disabled={busy} required inputProps={{ min: 1, max: 10080, step: 1 }} helperText="1 minute to 7 days. Enabling or changing the schedule starts a new interval." sx={{ maxWidth: 430 }} />
    <FormControl component="fieldset"><FormLabel component="legend">Restaurants</FormLabel><FormGroup row>{restaurantIds.map(id => <FormControlLabel key={id} label={id} control={<Checkbox checked={restaurants.includes(id)} disabled={busy} onChange={() => setRestaurants(toggle(restaurants, id))} />} />)}</FormGroup></FormControl>
    <FormControl component="fieldset"><FormLabel component="legend">Frameworks</FormLabel><FormGroup row>{frameworkIds.map(id => <FormControlLabel key={id} label={id.replace('-', ' ')} control={<Checkbox checked={frameworks.includes(id)} disabled={busy} onChange={() => setFrameworks(toggle(frameworks, id))} />} />)}</FormGroup></FormControl>
    <p className="data-caption">Scheduled scans collect local mock-source updates, then evaluate the selected scope. Manual and scheduled scans share one execution slot. A due scan waits if another scan is active.</p>
    <div className="data-toolbar"><Button type="submit" variant="contained" disabled={busy || !restaurants.length || !frameworks.length || !Number.isInteger(Number(interval)) || Number(interval) < 1 || Number(interval) > 10080}>{busy ? 'Saving…' : 'Save schedule'}</Button><Button disabled={busy} onClick={() => onSaved(false)}>Reload saved settings</Button></div>
    {feedback.error && <Alert severity="error">{feedback.error}</Alert>}{feedback.success && <Alert severity="success" role="status">{feedback.success}</Alert>}
  </form>;
}

export function MonitoringSettings({ identity, ...common }: Common & { identity: Identity }) {
  const state = useData<Schedule>('/schedule', common, true);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState('');
  const data = state.data;
  return <section className="phase-three-panel monitoring-page"><h2>Scan schedule</h2>
    {state.error && <Alert severity="error" action={<Button onClick={state.retry}>Retry</Button>}>{state.error}</Alert>}
    {state.loading && <div className="data-loading" role="status"><CircularProgress size={24} />Loading schedule…</div>}
    {data && <>
      <div className="schedule-status"><Status value={data.enabled ? 'enabled' : 'disabled'} /><span>Next run: <strong>{date(data.next_run_at)}</strong></span><span>Last attempt: <strong>{date(data.last_run_at)}</strong></span></div>
      {data.last_assessment && <p className="data-caption">Last scheduled assessment: {data.last_assessment.id} · {data.last_assessment.status} · Finished {date(data.last_assessment.finished_at)}</p>}
      {data.active_assessment && <Alert severity="info" sx={{ my: 2 }}>Active scan: {data.active_assessment.id} ({data.active_assessment.trigger}). New scans wait for it to finish.</Alert>}
      {(data.last_error || data.last_assessment?.error) && <Alert severity="error" sx={{ my: 2 }}>{data.last_error || data.last_assessment?.error}</Alert>}
      {notice && <Alert severity="success" role="status" sx={{ my: 2 }}>{notice}</Alert>}
      <ScheduleForm key={`${data.revision}-${reload}`} schedule={data} identity={identity} {...common} onSaved={saved => { setNotice(saved ? 'Schedule saved.' : ''); setReload(v => v + 1); state.retry(); }} />
    </>}
  </section>;
}
