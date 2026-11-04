import { FaBrain } from 'react-icons/fa6';
import { useApp, useDispatch } from '../../store/AppContext';
import { StatusIndicator } from './StatusIndicator';
import { clsx } from 'clsx';

const TABS = [
  { id: 'ai-studio',  label: 'AI Studio', title: 'AI Studio' },
  { id: 'data-ops',   label: 'Data Ops', title: 'Data Operations' },
  { id: 'knowledge',  label: 'Knowledge', title: 'Knowledge Hub' },
  { id: 'control',    label: 'Control', title: 'Control Tower' },
];

export function Header() {
  const { currentTab } = useApp();
  const dispatch = useDispatch();

  function setTab(id) {
    dispatch({ type: 'SET_TAB', payload: id });
  }

  return (
    <header className="flex items-center gap-2 px-3 sm:px-4 py-2 glass border-b border-white/60 z-40 flex-shrink-0 overflow-hidden">
      {/* Branding */}
      <div
        className="flex items-center gap-2.5 cursor-pointer select-none flex-shrink-0"
        onClick={() => window.location.reload()}
      >
        <div className="w-8 h-8 bg-oracle rounded-lg flex items-center justify-center shadow-oracle-glow">
          <FaBrain className="text-white text-sm" />
        </div>
        <h1 className="text-base font-bold text-slate-800 tracking-tight">DWH Studio</h1>
      </div>

      {/* Tab Navigation */}
      <nav className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto custom-scrollbar pb-1 sm:pb-0">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setTab(tab.id)}
            title={tab.title}
            className={clsx(
              'flex-shrink-0 whitespace-nowrap',
              currentTab === tab.id ? 'tab-btn-active' : 'tab-btn'
            )}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Right Controls */}
      <div className="ml-auto flex flex-shrink-0 items-center gap-2">
        <StatusIndicator />
      </div>
    </header>
  );
}
