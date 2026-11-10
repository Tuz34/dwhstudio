import { useEffect, useState } from 'react';
import { useApp, useDispatch } from './store/AppContext';
import { Header }         from './components/layout/Header';
import { Toast }          from './components/ui/Toast';
import { AiStudio }       from './components/tabs/AiStudio';
import { DataOperations } from './components/tabs/DataOperations';
import { KnowledgeHub }   from './components/tabs/KnowledgeHub';
import { ControlTower }   from './components/tabs/ControlTower';
import { MappingWizard }  from './components/modals/MappingWizard';
import { Scd2Wizard }     from './components/modals/Scd2Wizard';
import { SurgeonEdit }    from './components/modals/SurgeonEdit';
import { SnapshotCompare } from './components/modals/SnapshotCompare';
import { getAuthStatus, loginAuth, logoutAuth } from './services/api';

function TabContent() {
  const { currentTab } = useApp();
  switch (currentTab) {
    case 'ai-studio':  return <AiStudio />;
    case 'data-ops':   return <DataOperations />;
    case 'knowledge':  return <KnowledgeHub />;
    case 'control':    return <ControlTower />;
    default:           return <AiStudio />;
  }
}

function ModalRouter() {
  const { modal } = useApp();
  const dispatch  = useDispatch();
  const close     = () => dispatch({ type: 'CLOSE_MODAL' });

  if (!modal) return null;

  switch (modal.type) {
    case 'wizard':
      return <MappingWizard open onClose={close} initialData={modal.props?.mappingRequest || {}} />;
    case 'scd2':
      return <Scd2Wizard open onClose={close} initialData={modal.props?.mappingRequest || {}} />;
    case 'surgeon':
      return (
        <SurgeonEdit
          open
          onClose={close}
          mapping={modal.props?.mapping || {}}
          initialOperation={modal.props?.initialOperation}
          initialTruncateTargetTable={modal.props?.initialTruncateTargetTable}
        />
      );
    case 'compare':
      return <SnapshotCompare open onClose={close} mappingName={modal.props?.mappingName || ''} />;
    default:
      return null;
  }
}

function AuthGate({ children }) {
  const [auth, setAuth] = useState({ loading: true, enabled: false, authenticated: false, user: null });
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  async function refreshAuth() {
    try {
      const status = await getAuthStatus();
      setAuth({ loading: false, ...status });
    } catch (err) {
      setError(err.message || 'Authentication status could not be loaded.');
      setAuth(prev => ({ ...prev, loading: false }));
    }
  }

  useEffect(() => {
    let active = true;
    getAuthStatus()
      .then(status => {
        if (active) setAuth({ loading: false, ...status });
      })
      .catch(err => {
        if (!active) return;
        setError(err.message || 'Authentication status could not be loaded.');
        setAuth(prev => ({ ...prev, loading: false }));
      });
    return () => { active = false; };
  }, []);

  async function handleLogin(event) {
    event.preventDefault();
    setError('');
    try {
      const result = await loginAuth({ username, password });
      setAuth(prev => ({ ...prev, ...result, loading: false }));
      setPassword('');
    } catch (err) {
      setError(err.message || 'Sign in failed.');
      setPassword('');
    }
  }

  async function handleLogout() {
    setError('');
    try {
      await logoutAuth();
      await refreshAuth();
    } catch (err) {
      setError(err.message || 'Sign out failed.');
    }
  }

  if (auth.loading) {
    return <div className="flex h-full items-center justify-center text-sm text-slate-500">Loading</div>;
  }

  if (auth.enabled && !auth.authenticated) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-100 px-4">
        <form onSubmit={handleLogin} className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <h1 className="mb-4 text-lg font-semibold text-slate-900">Sign in</h1>
          <label className="mb-3 block text-sm font-medium text-slate-700">
            Username
            <input
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              value={username}
              onChange={event => setUsername(event.target.value)}
              autoComplete="username"
            />
          </label>
          <label className="mb-4 block text-sm font-medium text-slate-700">
            Password
            <input
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              type="password"
              value={password}
              onChange={event => setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          {error && <div className="mb-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
          <button className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white" type="submit">
            Sign in
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {auth.enabled && (
        <div className="flex items-center justify-end gap-2 border-b border-slate-200 bg-white px-3 py-1 text-xs text-slate-600">
          <span>{auth.user?.username} · {auth.user?.role}</span>
          <button className="rounded border border-slate-300 px-2 py-1 text-xs" type="button" onClick={handleLogout}>
            Sign out
          </button>
        </div>
      )}
      {error && <div className="bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}

export default function App() {
  return (
    <AuthGate>
      <div className="flex flex-col h-full">
        <Header />
        <main className="flex-1 flex min-w-0 overflow-hidden">
          <TabContent />
        </main>
        <ModalRouter />
        <Toast />
      </div>
    </AuthGate>
  );
}
