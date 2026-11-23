import { FormEvent, Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, Divider, IconButton, TextField, Tooltip, Typography } from '@mui/material';
import { ArrowForward, CheckCircleOutline, DashboardOutlined, DescriptionOutlined, FactCheckOutlined, Logout, Menu, Refresh, RestaurantOutlined, SecurityOutlined, SettingsOutlined, ShieldOutlined } from '@mui/icons-material';
import { ApiError, Identity, request, SystemInfo } from './api';
import { PeopleOutline, ReceiptLongOutlined, StorageOutlined, FolderOutlined, AccountBalanceOutlined } from '@mui/icons-material';
const PhaseTwoPage = lazy(() => import('./PhaseTwo'));
const OverviewStats = lazy(() => import('./PhaseTwo').then(module => ({ default: module.OverviewStats })));
const PhaseThreePage = lazy(() => import('./PhaseThree'));
const ComplianceDashboard = lazy(() => import('./PhaseFive').then(module => ({ default: module.ComplianceDashboard })));
const MonitoringSettings = lazy(() => import('./PhaseFive').then(module => ({ default: module.MonitoringSettings })));

const navigation = [
  { label: 'Overview', icon: <DashboardOutlined /> },
  { label: 'Restaurants', icon: <RestaurantOutlined /> },
  { label: 'Employees', icon: <PeopleOutline /> },
  { label: 'Visits', icon: <ReceiptLongOutlined /> },
  { label: 'Resource Planning', icon: <AccountBalanceOutlined /> },
  { label: 'Loyalty', icon: <PeopleOutline /> },
  { label: 'Access', icon: <SecurityOutlined /> },
  { label: 'Systems', icon: <StorageOutlined /> },
  { label: 'Evidence', icon: <FolderOutlined /> },
  { label: 'Controls', icon: <FactCheckOutlined /> },
  { label: 'Assessments', icon: <FactCheckOutlined /> },
  { label: 'Findings', icon: <ShieldOutlined /> },
  { label: 'Audit', icon: <ReceiptLongOutlined /> },
  { label: 'Reports', icon: <DescriptionOutlined /> },
  { label: 'Settings', icon: <SettingsOutlined /> },
];

const emptyModules: Record<string, { title: string; description: string }> = {
  Restaurants: { title: 'No restaurant data', description: 'Restaurant inventory has not been loaded.' },
};

function Brand() {
  return <div className="brand"><span className="brand-mark">M</span><div><strong>MYL Inc.</strong><small>COMPLIANCE CONSOLE</small></div></div>;
}

function Login({ onLogin }: { onLogin: (identity: Identity) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const identity = await request<Identity>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      setPassword(''); onLogin(identity);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to sign in.'); }
    finally { setBusy(false); }
  }
  return <main className="login-layout">
    <section className="login-story"><Brand /><div className="login-message"><h2>Compliance monitoring</h2><p>Loyalty program and payment systems.</p><div className="framework-tags"><span>GDPR</span><span>PCI DSS</span></div></div><span className="story-footer">MYL Inc. · Restaurant operations</span></section>
    <section className="login-panel"><div className="login-form"><Typography variant="h4" component="h1" sx={{ mb: 1 }}>Sign in</Typography><Typography color="text.secondary" sx={{ mb: 4 }}>General Manager account</Typography>
      <form onSubmit={submit}><div className="form-fields">{error && <Alert severity="error" role="alert">{error}</Alert>}<TextField label="Username" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required fullWidth disabled={busy} autoFocus inputProps={{ maxLength: 80 }} /><TextField label="Password" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required fullWidth disabled={busy} inputProps={{ maxLength: 1024 }} /><Button type="submit" variant="contained" size="large" disabled={busy} endIcon={busy ? <CircularProgress size={18} color="inherit" /> : <ArrowForward />}>{busy ? 'Signing in…' : 'Sign in'}</Button></div></form>
      <div className="login-note"><SecurityOutlined fontSize="small" /><span>General Manager access only</span></div>
    </div><span className="demo-label">Local demonstration · Synthetic data</span></section>
  </main>;
}

function Workspace({ identity, onLogout }: { identity: Identity; onLogout: () => void }) {
  const [page, setPage] = useState('Overview');
  const [menuOpen, setMenuOpen] = useState(false);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(async () => {
    setBusy(true); setError('');
    try {
      const [info] = await Promise.all([request<SystemInfo>('/system'), request('/ready')]);
      setSystem(info); setReady(true);
    } catch (cause) {
      setReady(false);
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) { onLogout(); return; }
      setError('Unable to reach the workspace. Check the local services and try again.');
    } finally { setBusy(false); }
  }, [onLogout]);
  useEffect(() => { void refresh(); }, [refresh]);
  async function logout() {
    setBusy(true); setError('');
    try { await request('/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': identity.csrf_token } }); onLogout(); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 401) onLogout(); else setError('Sign out failed. Please try again.'); }
    finally { setBusy(false); }
  }
  return <div className="workspace">
    {menuOpen && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMenuOpen(false)} />}
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`}><Brand /><span className="nav-heading">WORKSPACE</span><nav aria-label="Main navigation">{navigation.map(item => <button key={item.label} className={`nav-item ${page === item.label ? 'active' : ''}`} aria-current={page === item.label ? 'page' : undefined} onClick={() => { setPage(item.label); setMenuOpen(false); }}>{item.icon}{item.label}{page === item.label && <span className="nav-dot" />}</button>)}</nav><div className="sidebar-bottom"><div className="local-indicator"><span />Local demonstration</div><p>Synthetic data environment</p><Divider sx={{ borderColor: '#ffffff20', my: 2 }} /><div className="admin-row"><span className="avatar">G</span><div><strong>{identity.username}</strong><small>General Manager</small></div><Tooltip title="Sign out"><IconButton aria-label="Sign out" onClick={logout} disabled={busy} sx={{ color: '#d4e1da', ml: 'auto' }}><Logout fontSize="small" /></IconButton></Tooltip></div></div></aside>
    <div className="main-column"><header className="topbar"><div className="breadcrumb"><IconButton className="mobile-menu" aria-label="Open navigation" onClick={() => setMenuOpen(true)}><Menu /></IconButton><span>Workspace</span><span>/</span><strong>{page}</strong></div><Chip size="small" variant="outlined" label="MYL Inc." icon={<RestaurantOutlined />} /></header>
    <main className="content"><div className="page-title"><Typography component="h1" variant="h4">{page}</Typography><Button variant="outlined" onClick={() => { void refresh(); setRefreshKey(v => v + 1); }} disabled={busy} startIcon={busy ? <CircularProgress size={16} /> : <Refresh />}>Refresh</Button></div>
      {error && <Alert severity="error" sx={{ mb: 3 }} action={<Button color="inherit" size="small" onClick={refresh}>Retry</Button>}>{error}</Alert>}
      {page === 'Overview' ? <>
        <section className="status-banner"><span className="status-symbol"><CheckCircleOutline /></span><div><strong>{ready ? 'Services ready' : busy ? 'Connecting…' : 'Connection unavailable'}</strong><p>{ready ? system?.inventory_seeded ? 'Mock source data imported. Assessment status is shown below.' : 'API and database connected. No restaurant evidence loaded.' : 'Check service status or retry the connection.'}</p></div><Chip label={ready ? 'Connected' : busy ? 'Checking' : 'Unavailable'} size="small" sx={{ bgcolor: ready ? '#dcecd5' : '#e8ebe5', color: '#285239' }} /></section>
        <OverviewStats onUnauthorized={onLogout} refreshKey={refreshKey} />
        <ComplianceDashboard onUnauthorized={onLogout} refreshKey={refreshKey} onAssessments={() => setPage('Assessments')} onFindings={() => setPage('Findings')} />
      </> : page === 'Reports' ? <ComplianceDashboard onUnauthorized={onLogout} refreshKey={refreshKey} onAssessments={() => setPage('Assessments')} onFindings={() => setPage('Findings')} /> : page === 'Settings' ? <MonitoringSettings identity={identity} onUnauthorized={onLogout} refreshKey={refreshKey} /> : ['Assessments', 'Findings', 'Audit'].includes(page) ? <PhaseThreePage key={page} page={page} identity={identity} onUnauthorized={onLogout} refreshKey={refreshKey} /> : ['Restaurants', 'Employees', 'Visits', 'Resource Planning', 'Loyalty', 'Access', 'Systems', 'Evidence', 'Controls'].includes(page) ? <PhaseTwoPage key={page} page={page} onUnauthorized={onLogout} refreshKey={refreshKey} /> : <section className="module-empty"><span className="empty-icon">{navigation.find(item => item.label === page)?.icon}</span><h2>{emptyModules[page]?.title}</h2><p>{emptyModules[page]?.description}</p><Button onClick={() => setPage('Overview')}>Back to overview</Button></section>}
      <footer className="workspace-footer"><span>MYL Inc. · Compliance monitoring</span><span>Local demonstration / synthetic data</span></footer>
    </main></div>
  </div>;
}

export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const clearIdentity = useCallback(() => setIdentity(null), []);
  const restoreSession = useCallback(async () => {
    setLoading(true); setError(false);
    try { setIdentity(await request<Identity>('/auth/me')); }
    catch (cause) { if (!(cause instanceof ApiError && cause.status === 401)) setError(true); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void restoreSession(); }, [restoreSession]);
  if (loading) return <Box className="loading-screen" role="status" aria-label="Loading"><CircularProgress /><Typography>Loading…</Typography></Box>;
  if (error) return <Box className="loading-screen"><Alert severity="error">The local API is unavailable.</Alert><Button variant="contained" onClick={restoreSession}>Retry connection</Button></Box>;
  return <Suspense fallback={<Box className="loading-screen" role="status"><CircularProgress /><Typography>Loading…</Typography></Box>}>{identity ? <Workspace identity={identity} onLogout={clearIdentity} /> : <Login onLogin={setIdentity} />}</Suspense>;
}
