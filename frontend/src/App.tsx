import { FormEvent, useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, Divider, IconButton, TextField, Tooltip, Typography } from '@mui/material';
import { ArrowForward, AssessmentOutlined, CheckCircleOutline, DashboardOutlined, DescriptionOutlined, FactCheckOutlined, GridViewRounded, Logout, Menu, Refresh, RestaurantOutlined, SecurityOutlined, SettingsOutlined, ShieldOutlined } from '@mui/icons-material';
import { ApiError, Identity, request, SystemInfo } from './api';

const navigation = [
  { label: 'Overview', icon: <DashboardOutlined /> },
  { label: 'Restaurants', icon: <RestaurantOutlined /> },
  { label: 'Assessments', icon: <FactCheckOutlined /> },
  { label: 'Findings', icon: <ShieldOutlined /> },
  { label: 'Reports', icon: <DescriptionOutlined /> },
  { label: 'Settings', icon: <SettingsOutlined /> },
];

const emptyModules: Record<string, { title: string; description: string }> = {
  Restaurants: { title: 'No restaurant data', description: 'Restaurant inventory has not been loaded.' },
  Assessments: { title: 'No assessments', description: 'No compliance scans have been run.' },
  Findings: { title: 'No findings data', description: 'Findings are available after an assessment.' },
  Reports: { title: 'No reports', description: 'YAML reports are available after an assessment.' },
  Settings: { title: 'Administrator', description: 'One local administrator account is configured. Scan scheduling is not available yet.' },
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
    <section className="login-panel"><div className="login-form"><Typography variant="h4" component="h1" sx={{ mb: 1 }}>Sign in</Typography><Typography color="text.secondary" sx={{ mb: 4 }}>Administrator account</Typography>
      <form onSubmit={submit}><div className="form-fields">{error && <Alert severity="error" role="alert">{error}</Alert>}<TextField label="Username" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required fullWidth disabled={busy} autoFocus inputProps={{ maxLength: 80 }} /><TextField label="Password" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required fullWidth disabled={busy} inputProps={{ maxLength: 1024 }} /><Button type="submit" variant="contained" size="large" disabled={busy} endIcon={busy ? <CircularProgress size={18} color="inherit" /> : <ArrowForward />}>{busy ? 'Signing in…' : 'Sign in'}</Button></div></form>
      <div className="login-note"><SecurityOutlined fontSize="small" /><span>Local administrator access</span></div>
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
  const refresh = useCallback(async () => {
    setBusy(true); setError('');
    try {
      const [info] = await Promise.all([request<SystemInfo>('/system'), request('/ready')]);
      setSystem(info); setReady(true);
    } catch (cause) {
      setReady(false);
      if (cause instanceof ApiError && cause.status === 401) { onLogout(); return; }
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
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`}><Brand /><span className="nav-heading">WORKSPACE</span><nav aria-label="Main navigation">{navigation.map(item => <button key={item.label} className={`nav-item ${page === item.label ? 'active' : ''}`} aria-current={page === item.label ? 'page' : undefined} onClick={() => { setPage(item.label); setMenuOpen(false); }}>{item.icon}{item.label}{page === item.label && <span className="nav-dot" />}</button>)}</nav><div className="sidebar-bottom"><div className="local-indicator"><span />Local demonstration</div><p>Synthetic data environment</p><Divider sx={{ borderColor: '#ffffff20', my: 2 }} /><div className="admin-row"><span className="avatar">A</span><div><strong>{identity.username}</strong><small>Administrator</small></div><Tooltip title="Sign out"><IconButton aria-label="Sign out" onClick={logout} disabled={busy} sx={{ color: '#d4e1da', ml: 'auto' }}><Logout fontSize="small" /></IconButton></Tooltip></div></div></aside>
    <div className="main-column"><header className="topbar"><div className="breadcrumb"><IconButton className="mobile-menu" aria-label="Open navigation" onClick={() => setMenuOpen(true)}><Menu /></IconButton><span>Workspace</span><span>/</span><strong>{page}</strong></div><Chip size="small" variant="outlined" label="MYL Inc." icon={<RestaurantOutlined />} /></header>
    <main className="content"><div className="page-title"><Typography component="h1" variant="h4">{page}</Typography><Button variant="outlined" onClick={refresh} disabled={busy} startIcon={busy ? <CircularProgress size={16} /> : <Refresh />}>Refresh status</Button></div>
      {error && <Alert severity="error" sx={{ mb: 3 }} action={<Button color="inherit" size="small" onClick={refresh}>Retry</Button>}>{error}</Alert>}
      {page === 'Overview' ? <>
        <section className="status-banner"><span className="status-symbol"><CheckCircleOutline /></span><div><strong>{ready ? 'Services ready' : busy ? 'Connecting…' : 'Connection unavailable'}</strong><p>{ready ? 'API and database connected. No restaurant evidence loaded.' : 'Check service status or retry the connection.'}</p></div><Chip label={ready ? 'Connected' : busy ? 'Checking' : 'Unavailable'} size="small" sx={{ bgcolor: ready ? '#dcecd5' : '#e8ebe5', color: '#285239' }} /></section>
        <div className="section-heading"><h2>Frameworks</h2></div>
        <div className="framework-grid"><article className="framework-card"><div className="card-top"><span className="feature-icon"><SecurityOutlined /></span><Chip label="Awaiting evidence" size="small" variant="outlined" /></div><span className="eyebrow">CUSTOMER PRIVACY</span><h3>{system?.frameworks[0] ?? 'GDPR'}</h3><p>Consent, retention, customer requests, and access to loyalty-program data.</p><div className="card-footer"><span>Loyalty program</span><span>5 planned checks</span></div></article><article className="framework-card"><div className="card-top"><span className="feature-icon peach"><AssessmentOutlined /></span><Chip label="Awaiting evidence" size="small" variant="outlined" /></div><span className="eyebrow">PAYMENT SECURITY</span><h3>{system?.frameworks[1] ?? 'PCI DSS'}</h3><p>Secure transmission, authentication, logging, patching, and storage policies.</p><div className="card-footer"><span>Payment systems</span><span>5 planned checks</span></div></article></div>
        <section className="empty-state"><span className="empty-icon"><GridViewRounded /></span><div><h2>No assessments</h2><p>No restaurant evidence has been evaluated.</p></div><Chip label="Not assessed" variant="outlined" size="small" /></section>
      </> : <section className="module-empty"><span className="empty-icon">{navigation.find(item => item.label === page)?.icon}</span><h2>{emptyModules[page]?.title}</h2><p>{emptyModules[page]?.description}</p><Button onClick={() => setPage('Overview')}>Back to overview</Button></section>}
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
  return identity ? <Workspace identity={identity} onLogout={clearIdentity} /> : <Login onLogin={setIdentity} />;
}
