import { useApp, useDispatch } from '../../store/AppContext';
import { MappingList }    from '../dataops/MappingList';
import { Sessions }       from '../dataops/Sessions';
import { Kms }            from '../dataops/Kms';
import { DqDashboard }    from '../dataops/DqDashboard';
import { IncrementalLoad } from '../dataops/IncrementalLoad';
import { ImpactAnalysis } from '../dataops/ImpactAnalysis';
import { SchemaSync }     from '../dataops/SchemaSync';
import clsx from 'clsx';

const VIEWS = [
  { id: 'mappings',  label: 'ODI Mappings',      icon: 'fa-diagram-project',    color: 'violet' },
  { id: 'sessions',  label: 'Run Status',         icon: 'fa-list-ol',            color: 'blue'   },
  { id: 'kms',       label: 'Knowledge Modules', icon: 'fa-microchip',          color: 'indigo' },
  { id: 'dq',        label: 'DQ Checks',         icon: 'fa-chart-column',       color: 'emerald'},
  { id: 'incremental', label: 'Incremental Load', icon: 'fa-water',             color: 'orange' },
  { id: 'impact',    label: 'Impact / Lineage',  icon: 'fa-diagram-successor',  color: 'rose'   },
  { id: 'schema-sync', label: 'Schema Sync',      icon: 'fa-code-compare',       color: 'cyan'   },
];

const COLOR_MAP = {
  violet:  { active: 'bg-violet-100 text-violet-700 border-violet-200',  hover: 'hover:bg-violet-50 hover:text-violet-600' },
  blue:    { active: 'bg-blue-100 text-blue-700 border-blue-200',        hover: 'hover:bg-blue-50 hover:text-blue-600'   },
  indigo:  { active: 'bg-indigo-100 text-indigo-700 border-indigo-200',  hover: 'hover:bg-indigo-50 hover:text-indigo-600'},
  emerald: { active: 'bg-emerald-100 text-emerald-700 border-emerald-200',hover: 'hover:bg-emerald-50 hover:text-emerald-600'},
  rose:    { active: 'bg-rose-100 text-rose-700 border-rose-200',        hover: 'hover:bg-rose-50 hover:text-rose-600'   },
  cyan:    { active: 'bg-cyan-100 text-cyan-700 border-cyan-200',        hover: 'hover:bg-cyan-50 hover:text-cyan-600'   },
  orange:  { active: 'bg-orange-100 text-orange-700 border-orange-200',  hover: 'hover:bg-orange-50 hover:text-orange-600'},
};

export function DataOperations() {
  const { etlView } = useApp();
  const dispatch = useDispatch();

  const setView = (id) => dispatch({ type: 'SET_ETL_VIEW', payload: id });
  const current = VIEWS.find(v => v.id === etlView) || VIEWS[0];

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Sub-tab navbar */}
      <div className="shrink-0 px-6 pt-4 pb-0">
        <div className="flex gap-1.5 flex-wrap">
          {VIEWS.map(view => {
            const colors = COLOR_MAP[view.color];
            const isActive = etlView === view.id;
            return (
              <button
                key={view.id}
                onClick={() => setView(view.id)}
                className={clsx(
                  'flex items-center gap-2 px-4 py-2 rounded-t-lg text-sm font-medium border border-b-0 transition-all',
                  isActive
                    ? `${colors.active} shadow-sm`
                    : `bg-white text-slate-500 border-slate-200 ${colors.hover}`
                )}
              >
                <i className={`fa-solid ${view.icon} text-xs`}></i>
                {view.label}
              </button>
            );
          })}
        </div>
        {/* Bottom border line connecting tabs to content */}
        <div className={clsx(
          'h-px',
          current.color === 'violet'  ? 'bg-violet-200'  :
          current.color === 'blue'    ? 'bg-blue-200'    :
          current.color === 'indigo'  ? 'bg-indigo-200'  :
          current.color === 'emerald' ? 'bg-emerald-200' :
          current.color === 'rose'    ? 'bg-rose-200'    :
          current.color === 'cyan'    ? 'bg-cyan-200'    :
          'bg-orange-200'
        )}></div>
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto p-6">
        {etlView === 'mappings' && <MappingList />}
        {etlView === 'sessions' && <Sessions />}
        {etlView === 'kms'      && <Kms />}
        {etlView === 'dq'       && <DqDashboard />}
        {etlView === 'incremental' && <IncrementalLoad />}
        {etlView === 'impact'   && <ImpactAnalysis />}
        {etlView === 'schema-sync' && <SchemaSync />}
      </div>
    </div>
  );
}
