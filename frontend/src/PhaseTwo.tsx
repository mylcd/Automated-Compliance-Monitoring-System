import { ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Chip, CircularProgress, Dialog, DialogContent, DialogTitle,
  FormControl, IconButton, InputLabel, MenuItem, Select, Table,
  TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow,
  TextField, Typography,
} from '@mui/material';
import { Close, DownloadOutlined, OpenInNew } from '@mui/icons-material';
import { ApiError, request } from './api';
import { EvidenceJson, EvidenceAnnotations } from './EvidenceJson';

type Row = Record<string, any>;
type PageData = { items: Row[]; total: number };
type Column = { key: string; label: string; render?: (row: Row) => ReactNode };
type Common = { onUnauthorized: () => void; refreshKey: number };
export type EvidenceTarget = { id: string; day?: string; restaurant?: string; resultId?: string; assessmentId?: string; restaurantId?: string; frameworkId?: string; severity?: string };

const labels: Record<string, string> = {
  id: 'ID', restaurant_id: 'Restaurant', customer_id: 'Customer', employee_id: 'Employee',
  asset_id: 'System', owner_employee_id: 'Owner', job_role: 'Role', display_name: 'Name',
  platform_access: 'Platform access', restaurant_ids: 'Restaurants', payroll_month: 'Month',
  visited_at: 'Visit time', loyalty_member_at_visit: 'Member at visit', loyalty_member: 'Member',
  occurred_at: 'Event time', target_customer_id: 'Target customer', valid_from: 'Valid from',
  valid_until: 'Valid until', notice_version: 'Notice version', purpose_id: 'Purpose',
  loyalty_record_id: 'Loyalty record', collected_at: 'Collected', retention_due_at: 'Retention deadline',
  received_at: 'Received', due_at: 'Due', completed_at: 'Completed', request_type: 'Request',
  processing_purposes: 'Processing purposes', loyalty_records: 'Loyalty records',
  consent_events: 'Consent events', customer_activities: 'Customer activities',
  customer_requests: 'Customer requests', actual_access_grants: 'Observed permissions',
  erp_access_approvals: 'Approved permissions', employee_access_events: 'Action logs',
  payment_configurations: 'Payment configuration', restaurant_assignments: 'Restaurant assignments',
  employee_payroll: 'Employee payroll', payroll_allocations: 'Payroll allocations',
  sales_transactions: 'Sales transactions', transaction_at: 'Transaction time',
  transaction_kind: 'Type', amount_minor: 'Amount (minor units)', lawful_basis: 'Lawful basis',
  retention_days: 'Retention days', settings_json: 'Observed settings', policy_json: 'Policy',
  asset_type: 'Type', activity_type: 'Activity', event_type: 'Event', visit_id: 'Visit',
};
const label = (key: string) => labels[key] ?? key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const stamp = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : '—';
export const money = (value: number | null, currency = 'USD') => value === null ? 'Unavailable' : new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(value / 100);

function useData<T>(path: string, { onUnauthorized, refreshKey }: Common) {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setData(undefined);
    request<T>(path).then(value => { if (active) setData(value); }).catch(cause => {
      if (!active) return;
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) onUnauthorized();
      else setError(cause instanceof Error ? cause.message : 'Unable to load records.');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [path, refreshKey, retry, onUnauthorized]);
  return { data, loading, error, retry: () => setRetry(v => v + 1) };
}

function Loading({ state, children }: { state: { loading: boolean; error: string; retry: () => void }; children: ReactNode }) {
  if (state.loading) return <div className="data-loading" role="status"><CircularProgress size={24} /><span>Loading…</span></div>;
  if (state.error) return <Alert severity="error" action={<Button onClick={state.retry}>Retry</Button>}>{state.error}</Alert>;
  return <>{children}</>;
}

function cell(value: any): ReactNode {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value)) return stamp(value);
  return String(value);
}

function DataTable({ rows, columns, remote, empty = 'No records' }: {
  rows: Row[]; columns: Column[]; empty?: string;
  remote?: { page: number; total: number; change: (page: number) => void };
}) {
  const [page, setPage] = useState(0);
  const visible = remote ? rows : rows.slice(page * 10, page * 10 + 10);
  useEffect(() => { setPage(0); }, [rows]);
  return <div className="data-table">
    <TableContainer><Table size="small"><TableHead><TableRow>{columns.map(c => <TableCell key={c.key}>{c.label}</TableCell>)}</TableRow></TableHead>
      <TableBody>{visible.map((row, i) => <TableRow key={row.id ?? `${row.employee_id}-${row.payroll_month}-${row.restaurant_id}-${i}`} hover>{columns.map(c => <TableCell key={c.key}><div className="table-value">{c.render ? c.render(row) : cell(row[c.key])}</div></TableCell>)}</TableRow>)}
        {!rows.length && <TableRow><TableCell colSpan={columns.length}><div className="table-empty">{empty}</div></TableCell></TableRow>}
      </TableBody></Table></TableContainer>
    {(remote ? remote.total > 25 : rows.length > 10) && <TablePagination component="div" count={remote?.total ?? rows.length} page={remote?.page ?? page} onPageChange={(_, next) => remote ? remote.change(next) : setPage(next)} rowsPerPage={remote ? 25 : 10} rowsPerPageOptions={[]} />}
  </div>;
}

function RestaurantFilter({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <FormControl size="small" sx={{ minWidth: 160 }}><InputLabel>Restaurant</InputLabel><Select label="Restaurant" value={value} onChange={e => onChange(e.target.value)}><MenuItem value="">All restaurants</MenuItem>{[1, 2, 3, 4, 5].map(n => <MenuItem key={n} value={`R00${n}`}>{`R00${n}`}</MenuItem>)}</Select></FormControl>;
}

function Detail({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return <Dialog open onClose={onClose} fullWidth maxWidth="lg"><DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>{title}<IconButton onClick={onClose} aria-label="Close details"><Close /></IconButton></DialogTitle><DialogContent dividers>{children}</DialogContent></Dialog>;
}

export function EvidenceDetail({ id, day, restaurant, resultId, assessmentId, restaurantId, frameworkId, severity, onClose, common }: EvidenceTarget & { onClose: () => void; common: Common }) {
  const state = useData<Row>(`/evidence/${id}`, common);
  const content = useData<Row>(`/evidence/${id}/content`, common);
  const parameters = new URLSearchParams();
  Object.entries({ result_id: resultId, assessment_id: assessmentId, restaurant_id: restaurantId, framework_id: frameworkId, severity }).forEach(([key, value]) => { if (value) parameters.set(key, value); });
  const annotations = useData<EvidenceAnnotations>(`/evidence/${id}/annotations?${parameters}`, common);
  const highlighted = useRef<HTMLTableRowElement>(null);
  const rows: Row[] = useMemo(() => content.data?.dataset_type === 'visits' ? [...content.data.records].sort((a: Row, b: Row) => a.visited_at.localeCompare(b.visited_at) || a.id.localeCompare(b.id)) : [], [content.data]);
  const isSelected = (row: Row) => !!day && row.visited_at.slice(0, 10) === day && (!restaurant || row.restaurant_id === restaurant);
  const firstSelected = rows.findIndex(isSelected);
  const selectedCount = rows.filter(isSelected).length;
  useEffect(() => {
    if (firstSelected < 0) return;
    const frame = requestAnimationFrame(() => highlighted.current?.scrollIntoView({ block: 'center', inline: 'start' }));
    return () => cancelAnimationFrame(frame);
  }, [rows, firstSelected, day, restaurant]);
  return <Detail title={day ? `Evidence · ${restaurant} · ${day}` : 'Evidence'} onClose={onClose}><Loading state={state}>{state.data && <>
    <div className="detail-fields"><div><small>Dataset</small><strong>{label(state.data.dataset_type)}</strong></div><div><small>Source</small><strong>{state.data.collection.source_type.toUpperCase()}</strong></div><div><small>Captured</small><strong>{stamp(state.data.captured_at)}</strong></div><div><small>Records</small><strong>{state.data.record_count}</strong></div></div>
    <Typography variant="body2" sx={{ mb: 2, overflowWrap: 'anywhere' }}>SHA-256: {state.data.sha256}</Typography>
    <Typography variant="body2" sx={{ mb: 2 }}>Control references: {state.data.control_ids.join(', ') || 'None'}. Supporting evidence does not indicate a passing assessment.</Typography>
    <Button component="a" href={`/api/evidence/${id}/content?download=true`} startIcon={<DownloadOutlined />} variant="outlined" size="small">Download JSON</Button>
    {annotations.error && <Alert severity="warning" sx={{ mt: 2 }} action={<Button onClick={annotations.retry}>Retry</Button>}>Failure highlights are unavailable: {annotations.error}</Alert>}
    {annotations.data?.assessment_id && <div className="evidence-assessment-context"><strong>Assessment context</strong><code>{annotations.data.assessment_id}</code><span>Evaluated {stamp(annotations.data.evaluated_at)}</span>{!resultId && !assessmentId && <span>Latest completed assessment using this snapshot.</span>}</div>}
    {!!annotations.data?.issues.length && <Alert severity="error" sx={{ my: 2 }}><strong>Failed check details</strong><ul className="evidence-failure-reasons">{annotations.data.issues.slice(0, 10).map((issue, index) => <li key={`${issue.result_id}-${issue.subject_id}-${index}`}><strong>{issue.restaurant_id} · {issue.control_id} · {issue.subject_id}</strong><p>{issue.explanation || issue.reason}</p>{issue.customer_id && <p>Customer: {issue.customer_id} · Purpose: {issue.purpose_id} · Activity: {stamp(issue.activity_at)}</p>}</li>)}</ul>{annotations.data.issues.length > 10 && <p>{annotations.data.issues.length} failed subjects in this context. Red marks identify all matching locations below.</p>}{!annotations.data.highlights.length && <p>No incorrect existing value is identified in this supporting snapshot. Missing records are described above; unrelated records are not marked red.</p>}</Alert>}
    {!!annotations.data?.highlights.length && <Typography variant="body2" sx={{ mt: 2 }}>Red highlights identify failed values or affected records in this assessment. The downloaded JSON remains the original snapshot.</Typography>}
    <Loading state={content}>{content.data?.dataset_type === 'visits' ? <>
      <div className="data-toolbar" style={{ marginTop: 18, marginBottom: 12 }}><Chip label={`${content.data.restaurant_ids.join(', ')} · ${content.data.month}`} variant="outlined" size="small" />{day && <><Chip label={`${selectedCount} visits highlighted · ${day}`} size="small" color="warning" /><Button size="small" onClick={() => highlighted.current?.scrollIntoView({ block: 'center', inline: 'start', behavior: 'smooth' })}>Jump to day</Button></>}</div>
      {day && !selectedCount && <Alert severity="warning">The selected day is not present in this snapshot.</Alert>}
      <Typography variant="body2" sx={{ my: 1 }}>Payments include 10% tax.</Typography>
      <TableContainer className="visit-detail-table"><Table stickyHeader size="small" aria-label="Monthly visit evidence"><TableHead><TableRow><TableCell>Visit</TableCell><TableCell>Time (UTC)</TableCell><TableCell>Customer</TableCell><TableCell>Member</TableCell><TableCell>Payment received</TableCell></TableRow></TableHead><TableBody>{rows.map((row, index) => <TableRow key={row.id} ref={index === firstSelected ? highlighted : undefined} className={isSelected(row) ? 'highlighted-visit' : ''} data-visit-date={row.visited_at.slice(0, 10)} data-highlighted={isSelected(row) ? 'true' : 'false'}><TableCell>{row.id}</TableCell><TableCell>{row.visited_at.replace('T', ' ').replace('Z', '')}</TableCell><TableCell>{row.customer_id ?? 'Anonymous'}</TableCell><TableCell>{row.loyalty_member_at_visit ? 'Yes' : 'No'}</TableCell><TableCell>{money(row.payment_received_minor, row.currency)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>
      <details className="raw-evidence"><summary>Raw JSON</summary><pre className="evidence-json">{JSON.stringify(content.data, null, 2)}</pre></details>
    </> : content.data ? <EvidenceJson value={content.data} highlights={annotations.data?.highlights ?? []} /> : null}</Loading>
  </>}</Loading></Detail>;
}

function EvidenceButton({ id, onClick }: { id?: string; onClick: (id: string) => void }) {
  return id ? <Button size="small" onClick={() => onClick(id)}>Evidence</Button> : <>—</>;
}

export function OverviewStats(common: Common) {
  const state = useData<Row>('/overview', common);
  return <Loading state={state}>{state.data && <>
    <div className="metric-grid">{[['Restaurants', state.data.restaurants], ['Employees', state.data.employees], ['Visits', state.data.visits], ['Evidence snapshots', state.data.evidence]].map(([title, value]) => <article className="metric-card" key={title}><span>{title}</span><strong>{value}</strong></article>)}</div>
    <div className="data-caption">Last import: {stamp(state.data.latest_import)}</div>
  </>}</Loading>;
}

function Restaurants({ common }: { common: Common }) {
  const state = useData<PageData>('/restaurants', common);
  const [selected, setSelected] = useState<string>();
  const detail = useData<Row>(selected ? `/restaurants/${selected}` : '/overview', common);
  return <><Loading state={state}><DataTable rows={state.data?.items ?? []} columns={[
    { key: 'id', label: 'Restaurant' }, { key: 'employee_count', label: 'Assigned employees' },
    { key: 'visit_count', label: 'Visits' }, { key: 'asset_count', label: 'Systems' },
    { key: 'assessment_status', label: 'Assessment', render: r => <Chip label={r.assessment_status === 'assessed' ? 'Assessed' : 'Not assessed'} size="small" variant="outlined" /> },
    { key: 'detail', label: '', render: r => <Button size="small" onClick={() => setSelected(r.id)}>View</Button> },
  ]} /><p className="data-caption">The General Manager is included in each restaurant’s assigned employees and counted once in the company employee total.</p></Loading>
    {selected && <Detail title={selected} onClose={() => setSelected(undefined)}><Loading state={detail}>{detail.data && <><Typography variant="body2" sx={{ mb: 2 }}>{detail.data.visits} visits · {detail.data.employee_ids?.length} assigned employees · {(detail.data.assessment_status ?? state.data?.items.find(r => r.id === selected)?.assessment_status) === 'assessed' ? 'Assessed' : 'Not assessed'}</Typography><DataTable rows={detail.data.assets ?? []} columns={[{ key: 'id', label: 'System' }, { key: 'asset_type', label: 'Type' }, { key: 'owner_employee_id', label: 'Owner' }]} /><Typography variant="body2" sx={{ mt: 2, overflowWrap: 'anywhere' }}>Assigned employees: {detail.data.employee_ids?.join(', ')}</Typography></>}</Loading></Detail>}
  </>;
}

function Employees({ common }: { common: Common }) {
  const [restaurant, setRestaurant] = useState('');
  const [role, setRole] = useState('');
  const [selected, setSelected] = useState<string>();
  const state = useData<PageData>(`/employees?limit=200&restaurant_id=${restaurant}${role ? `&role=${encodeURIComponent(role)}` : ''}`, common);
  const detail = useData<Row>(selected ? `/employees/${selected}` : '/overview', common);
  return <><div className="data-toolbar"><RestaurantFilter value={restaurant} onChange={setRestaurant} /><FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>Role</InputLabel><Select label="Role" value={role} onChange={e => setRole(e.target.value)}><MenuItem value="">All roles</MenuItem>{['General Manager', 'Manager', 'Staff'].map(r => <MenuItem key={r} value={r}>{r}</MenuItem>)}</Select></FormControl><Chip size="small" label="Read-only ERP data" variant="outlined" /></div>
    <Loading state={state}><DataTable rows={state.data?.items ?? []} columns={[
      { key: 'id', label: 'Employee' }, { key: 'name', label: 'Name' }, { key: 'job_role', label: 'Role' },
      { key: 'restaurant_ids', label: 'Restaurants' }, { key: 'platform_access', label: 'Platform access' },
      { key: 'detail', label: '', render: r => <Button size="small" onClick={() => setSelected(r.id)}>Payroll</Button> },
    ]} /></Loading>
    {selected && <Detail title={`Employee ${selected}`} onClose={() => setSelected(undefined)}><Loading state={detail}>{detail.data && <>
      <Typography variant="h6">{detail.data.name}</Typography><Typography color="text.secondary" sx={{ mb: 3 }}>{detail.data.job_role} · Platform access: {detail.data.platform_access ? 'Yes' : 'No'}</Typography>
      <Typography component="h3" variant="subtitle1" sx={{ mb: 1 }}>Monthly payroll</Typography><DataTable rows={detail.data.payroll} columns={[
        { key: 'payroll_month', label: 'Month' }, { key: 'currency', label: 'Currency' }, { key: 'salary', label: 'Salary', render: r => money(r.monthly_salary_minor, r.currency) },
        { key: 'allowance', label: 'Allowance', render: r => money(r.allowance_minor, r.currency) }, { key: 'total', label: 'Total', render: r => money(r.monthly_salary_minor + r.allowance_minor, r.currency) },
      ]} /><Typography component="h3" variant="subtitle1" sx={{ mt: 3, mb: 1 }}>Restaurant allocations</Typography><DataTable rows={detail.data.allocations} columns={[
        { key: 'payroll_month', label: 'Month' }, { key: 'restaurant_id', label: 'Restaurant' },
        { key: 'salary', label: 'Salary', render: r => money(r.salary_allocated_minor, detail.data?.payroll.find((p: Row) => p.payroll_month === r.payroll_month)?.currency) },
        { key: 'allowance', label: 'Allowance', render: r => money(r.allowance_allocated_minor, detail.data?.payroll.find((p: Row) => p.payroll_month === r.payroll_month)?.currency) },
      ]} /><p className="data-caption">Imported monthly amounts; not a payment confirmation.</p>
    </>}</Loading></Detail>}
  </>;
}

function Visits({ common, openEvidence }: { common: Common; openEvidence: (target: EvidenceTarget) => void }) {
  const [restaurant, setRestaurant] = useState('');
  const [month, setMonth] = useState('2026-10');
  const [member, setMember] = useState('');
  const [page, setPage] = useState(0);
  const state = useData<PageData & { visits_total: number }>(`/visits/days?limit=25&offset=${page * 25}${restaurant ? `&restaurant_id=${restaurant}` : ''}${month ? `&month=${month}` : ''}${member ? `&member=${member}` : ''}`, common);
  function change(setter: (s: string) => void, value: string) { setter(value); setPage(0); }
  return <><div className="data-toolbar"><RestaurantFilter value={restaurant} onChange={v => change(setRestaurant, v)} /><TextField size="small" type="month" label="Month" value={month} onChange={e => change(setMonth, e.target.value)} slotProps={{ inputLabel: { shrink: true } }} /><FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>Membership</InputLabel><Select label="Membership" value={member} onChange={e => change(setMember, e.target.value)}><MenuItem value="">All visits</MenuItem><MenuItem value="true">Loyalty member</MenuItem><MenuItem value="false">Non-member</MenuItem></Select></FormControl></div>
    <Loading state={state}><DataTable rows={state.data?.items ?? []} remote={{ page, total: state.data?.total ?? 0, change: setPage }} columns={[
      { key: 'id', label: 'Daily evidence' }, { key: 'restaurant_id', label: 'Restaurant' }, { key: 'visit_date', label: 'Date (UTC)' },
      { key: 'visit_count', label: 'Visits' }, { key: 'member_visits', label: 'Members' }, { key: 'non_member_visits', label: 'Non-members' },
      { key: 'payments', label: 'Payment received', render: r => Object.entries(r.payments).map(([currency, amount]) => money(amount as number, currency)).join(' / ') },
      { key: 'evidence', label: '', render: r => <Button size="small" onClick={() => openEvidence({ id: r.evidence_id, day: r.visit_date, restaurant: r.restaurant_id })}>Evidence</Button> },
    ]} /><p className="data-caption">{state.data?.visits_total ?? 0} visits across {state.data?.total ?? 0} restaurant days. Payments include 10% tax. Select Evidence to view and highlight that day. Counts and payments reflect the selected filters.</p></Loading>
  </>;
}

function ResourcePlanning({ common }: { common: Common }) {
  const [month, setMonth] = useState('2026-10');
  const state = useData<Row>(`/resource-planning/summary?month=${month || '2026-10'}`, common);
  return <><div className="data-toolbar"><TextField type="month" size="small" label="Month" value={month} onChange={e => setMonth(e.target.value)} slotProps={{ inputLabel: { shrink: true } }} /><Chip label="Read-only mock ERP data" size="small" variant="outlined" /></div>
    <Loading state={state}>{state.data && <>
      {!state.data.complete && <Alert severity="warning" sx={{ mb: 2 }}>Complete source data is unavailable for this month. Missing amounts are not treated as zero.</Alert>}
      {state.data.totals.map((total: Row) => <div className="metric-grid summary-metrics" key={total.currency}>
        <article className="metric-card"><span>Total sales (excl. tax) · {total.currency}</span><strong>{money(total.total_sales_minor, total.currency)}</strong></article>
        <article className="metric-card"><span>Total payroll amount · {total.currency}</span><strong>{money(total.total_payroll_minor, total.currency)}</strong></article>
        <article className="metric-card"><span>Payment received · {total.currency}</span><strong>{money(total.total_payments_minor, total.currency)}</strong></article>
        <article className="metric-card"><span>Tax ({state.data?.tax_rate_percent}%) · {total.currency}</span><strong>{money(total.total_tax_minor, total.currency)}</strong></article>
      </div>)}
      <DataTable rows={state.data.restaurants.flatMap((r: Row) => r.totals.map((t: Row) => ({ ...t, restaurant_id: r.restaurant_id, complete: r.complete })))} columns={[
        { key: 'restaurant_id', label: 'Restaurant' }, { key: 'currency', label: 'Currency' },
        { key: 'payments', label: 'Payment received', render: r => money(r.total_payments_minor, r.currency) },
        { key: 'tax', label: `Tax (${state.data.tax_rate_percent}%)`, render: r => money(r.total_tax_minor, r.currency) },
        { key: 'sales', label: 'Total sales (excl. tax)', render: r => money(r.total_sales_minor, r.currency) },
        { key: 'payroll', label: 'Total payroll amount', render: r => money(r.total_payroll_minor, r.currency) },
        { key: 'complete', label: 'Source data', render: r => r.complete ? 'Complete' : 'Incomplete' },
      ]} />
      <div className="data-caption"><p>Payments include 10% tax. Total sales = payment received ÷ 1.10, rounded to cents per transaction. Tax is the difference. Posted refunds reduce totals; voided transactions are excluded.</p><p>Payroll: allocated monthly salary plus allowance. Shared General Manager payroll is allocated once across the five restaurants. Amounts do not confirm payment.</p><p>Source data as of {stamp(state.data.as_of)}. October is the only seeded month.</p></div>
    </>}</Loading>
  </>;
}

const DATA_GROUPS: Record<string, string[]> = {
  Loyalty: ['customers', 'loyalty_records', 'consent_events', 'processing_purposes', 'customer_requests', 'customer_activities'],
  Access: ['employee_access_events', 'erp_access_approvals', 'actual_access_grants'],
  Systems: ['assets', 'payment_configurations', 'sales_transactions'],
};

function Datasets({ group, common, openEvidence }: { group: string; common: Common; openEvidence: (id: string) => void }) {
  const options = DATA_GROUPS[group];
  const [dataset, setDataset] = useState(options[0]);
  const [restaurant, setRestaurant] = useState('');
  const [page, setPage] = useState(0);
  const [jsonValue, setJsonValue] = useState<Row>();
  const state = useData<PageData>(`/data/${dataset}?limit=25&offset=${page * 25}${restaurant ? `&restaurant_id=${restaurant}` : ''}`, common);
  const columns: Column[] = Object.keys(state.data?.items[0] ?? {}).filter(k => k !== 'evidence_id').map(key => ({ key, label: label(key), render: row => {
    const value = row[key];
    return value && typeof value === 'object' ? <Button size="small" onClick={() => setJsonValue({ [label(key)]: value })}>View</Button> : cell(value);
  } }));
  columns.push({ key: 'evidence', label: '', render: r => <EvidenceButton id={r.evidence_id} onClick={openEvidence} /> });
  return <><div className="data-toolbar"><FormControl size="small" sx={{ minWidth: 220 }}><InputLabel>Dataset</InputLabel><Select label="Dataset" value={dataset} onChange={e => { setDataset(e.target.value); setPage(0); }}>{options.map(o => <MenuItem key={o} value={o}>{label(o)}</MenuItem>)}</Select></FormControl><RestaurantFilter value={restaurant} onChange={v => { setRestaurant(v); setPage(0); }} /></div>
    {group === 'Access' && <Alert severity="info" sx={{ mb: 2 }}>Source permissions and actions are imported evidence. Authorization has not been assessed.</Alert>}
    <Loading state={state}><DataTable rows={state.data?.items ?? []} columns={columns} remote={{ page, total: state.data?.total ?? 0, change: setPage }} /></Loading>
    {jsonValue && <Detail title="Configuration" onClose={() => setJsonValue(undefined)}><pre className="evidence-json">{JSON.stringify(jsonValue, null, 2)}</pre></Detail>}
  </>;
}

function EvidenceList({ common, openEvidence }: { common: Common; openEvidence: (target: EvidenceTarget) => void }) {
  const [restaurant, setRestaurant] = useState('');
  const [dataset, setDataset] = useState('visits');
  const state = useData<PageData>(`/evidence?limit=200&dataset=${dataset}${restaurant ? `&restaurant_id=${restaurant}` : ''}`, common);
  const options = [...new Set(['visits', 'restaurants', 'employees', 'employee_payroll', 'payroll_allocations', 'restaurant_assignments', ...Object.values(DATA_GROUPS).flat()])];
  return <><div className="data-toolbar"><FormControl size="small" sx={{ minWidth: 220 }}><InputLabel>Dataset</InputLabel><Select label="Dataset" value={dataset} onChange={e => setDataset(e.target.value)}>{options.map(name => <MenuItem key={name} value={name}>{label(name)}</MenuItem>)}</Select></FormControl><Chip label="Immutable source snapshots" variant="outlined" size="small" /></div>{dataset === 'visits' ? <Visits common={common} openEvidence={openEvidence} /> : <><div className="data-toolbar"><RestaurantFilter value={restaurant} onChange={setRestaurant} /></div><Loading state={state}><DataTable rows={state.data?.items ?? []} columns={[
    { key: 'dataset_type', label: 'Dataset', render: r => label(r.dataset_type) }, { key: 'record_count', label: 'Records' },
    { key: 'captured_at', label: 'Captured' }, { key: 'complete', label: 'Source complete' },
    { key: 'inspect', label: '', render: r => <Button size="small" endIcon={<OpenInNew sx={{ fontSize: 14 }} />} onClick={() => openEvidence({ id: r.id })}>Inspect</Button> },
  ]} /></Loading></>}</>;
}

function Controls({ common }: { common: Common }) {
  const state = useData<PageData>('/controls', common);
  return <><Alert severity="info" sx={{ mb: 3 }}>Demo control catalog. References describe supporting scope, not complete requirement coverage. Status reflects the latest result.</Alert><Loading state={state}><DataTable rows={state.data?.items ?? []} columns={[
    { key: 'id', label: 'Control' }, { key: 'title', label: 'Title' }, { key: 'severity', label: 'Severity' },
    { key: 'mappings', label: 'Reference', render: r => r.mappings.map((m: Row) => `${m.framework_id}: ${m.requirement_ref}`).join('; ') },
    { key: 'status', label: 'Assessment', render: r => <Chip label={r.status ? label(r.status) : 'Not assessed'} size="small" variant="outlined" /> },
  ]} /></Loading></>;
}

export default function PhaseTwoPage({ page, ...common }: Common & { page: string }) {
  const [evidenceTarget, setEvidenceTarget] = useState<EvidenceTarget>();
  const openEvidence = (id: string) => setEvidenceTarget({ id });
  let content: ReactNode;
  if (page === 'Restaurants') content = <Restaurants common={common} />;
  else if (page === 'Employees') content = <Employees common={common} />;
  else if (page === 'Visits') content = <Visits common={common} openEvidence={setEvidenceTarget} />;
  else if (page === 'Resource Planning') content = <ResourcePlanning common={common} />;
  else if (page === 'Evidence') content = <EvidenceList common={common} openEvidence={setEvidenceTarget} />;
  else if (page === 'Controls') content = <Controls common={common} />;
  else content = <Datasets key={page} group={page} common={common} openEvidence={openEvidence} />;
  return <>{content}{evidenceTarget && <EvidenceDetail {...evidenceTarget} common={common} onClose={() => setEvidenceTarget(undefined)} />}</>;
}
