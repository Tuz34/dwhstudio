import { getSchemaFindingsExportUrl } from '../../services/api';

const BUTTON =
  'inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50';

function SchemaFindingsPanel({ report, loading, owner }) {
  const labels = {
    column_type_size_mismatch: 'Type / Size',
    target_column_missing_in_odi: 'DB column missing in ODI',
    odi_column_missing_in_db: 'ODI column missing in DB',
  };
  const findings = Array.isArray(report?.findings) ? report.findings : [];

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500">
        Checking Schema Findings...
      </div>
    );
  }

  if (!findings.length) {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-800">
        No structural findings for this owner.
      </div>
    );
  }

  return (
    <div className="max-h-52 space-y-2 overflow-auto pr-1">
      <a
        className="mb-2 inline-flex items-center gap-2 rounded-lg border border-amber-200 bg-white px-3 py-2 text-xs font-black uppercase text-amber-700 hover:bg-amber-50"
        href={getSchemaFindingsExportUrl(owner)}
        download
      >
        <i className="fa-solid fa-file-csv" />
        Download CSV
      </a>
      {findings.map(item => (
        <div
          key={`${item.kind}.${item.owner}.${item.table}.${item.column}`}
          className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2"
        >
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="rounded bg-amber-100 px-2 py-0.5 text-[10px] font-black uppercase text-amber-800">
              {labels[item.kind] || item.kind}
            </span>
            {item.mapping_name && <span className="truncate text-[10px] font-semibold text-amber-700">{item.mapping_name}</span>}
          </div>
          <p className="break-words text-xs font-black text-amber-900">{item.summary}</p>
          <p className="mt-1 text-[11px] font-semibold leading-4 text-amber-700">{item.next_step}</p>
        </div>
      ))}
    </div>
  );
}

function SourceTargetFindingsPanel({ report, loading }) {
  const labels = {
    source_target_type_size_mismatch: 'Type / Size',
    source_target_nullability_mismatch: 'Nullability',
    source_column_missing_in_target: 'Missing in target',
    target_column_missing_in_source: 'Missing in source',
  };
  const findings = Array.isArray(report?.findings) ? report.findings : [];
  const mappingBadge = report?.mapping_link_aware ? 'mapping-link aware' : 'mapping-link pending';

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500">
        Checking source-to-target metadata...
      </div>
    );
  }

  if (!findings.length) {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-800">
        No source-to-target metadata findings.
      </div>
    );
  }

  return (
    <div className="max-h-52 space-y-2 overflow-auto pr-1">
      <div className="rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2 text-[11px] font-black uppercase text-cyan-800">
        {mappingBadge}
      </div>
      {findings.map(item => (
        <div
          key={`${item.kind}.${item.source_schema}.${item.source_table}.${item.target_owner}.${item.target_table}.${item.column}`}
          className="rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2"
        >
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="rounded bg-cyan-100 px-2 py-0.5 text-[10px] font-black uppercase text-cyan-800">
              {labels[item.kind] || item.kind}
            </span>
            <span className="truncate text-[10px] font-semibold text-cyan-700">
              {item.target_table || item.source_table || '-'}
            </span>
          </div>
          <p className="break-words text-xs font-black text-cyan-950">{item.summary}</p>
          <p className="mt-1 text-[11px] font-semibold leading-4 text-cyan-700">{item.next_step}</p>
        </div>
      ))}
    </div>
  );
}

function PkParityPanel({ report, loading, onRun }) {
  const results = Array.isArray(report?.results) ? report.results : [];
  const failed = results.filter(item => item.status === 'failed');
  const blocked = results.filter(item => item.status === 'blocked');

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-bold leading-5 text-slate-600">
        count-only read-only PK check; pk_values_included={String(report?.pk_values_included === true)}
      </div>
      <button
        className={`${BUTTON} w-full border border-cyan-200 bg-cyan-50 text-cyan-700 hover:bg-cyan-100`}
        disabled={loading}
        onClick={onRun}
      >
        <i className={`fa-solid ${loading ? 'fa-spinner fa-spin' : 'fa-key'}`} />
        Run PK Parity
      </button>
      {!report && !loading && (
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500">
          Not checked yet.
        </div>
      )}
      {report && (
        <div className={`rounded-lg border px-3 py-2 text-xs font-semibold ${report.status === 'passed' ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-amber-200 bg-amber-50 text-amber-800'}`}>
          {report.status === 'passed' ? 'PK parity passed.' : `${failed.length} failed, ${blocked.length} blocked.`}
        </div>
      )}
      {!!results.length && (
        <div className="max-h-52 space-y-2 overflow-auto pr-1">
          {results.map(item => (
            <div
              key={`${item.source_table}.${item.target_table}.${item.mapping_name || ''}`}
              className="rounded-lg border border-slate-200 bg-white px-3 py-2"
            >
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="rounded bg-slate-100 px-2 py-0.5 text-[10px] font-black uppercase text-slate-700">
                  {item.status}
                </span>
                <span className="truncate text-[10px] font-semibold text-slate-500">{item.mapping_name || '-'}</span>
              </div>
              <p className="break-words text-xs font-black text-slate-800">{item.source_table} {'->'} {item.target_table}</p>
              {item.status === 'blocked' ? (
                <p className="mt-1 text-[11px] font-semibold text-amber-700">{item.blocker}</p>
              ) : (
                <p className="mt-1 text-[11px] font-semibold leading-4 text-slate-600">
                  rows {item.source_row_count ?? '-'} / {item.target_row_count ?? '-'} | missing_key_count {item.missing_key_count ?? '-'} | extra_key_count {item.extra_key_count ?? '-'}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MappingDetailPanel({ context, detailSummary, loading, error }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-black text-slate-800">Mapping Detail</h3>
        <i className="fa-solid fa-folder-tree text-slate-400" />
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-lg bg-slate-50 px-3 py-2">
          <p className="font-black uppercase text-slate-400">Project</p>
          <p className="mt-1 break-all font-semibold text-slate-700">{context.project}</p>
        </div>
        <div className="rounded-lg bg-slate-50 px-3 py-2">
          <p className="font-black uppercase text-slate-400">Folder</p>
          <p className="mt-1 break-all font-semibold text-slate-700">{context.folder}</p>
        </div>
      </div>
      {loading && <p className="mt-3 text-xs font-semibold text-slate-500">Loading mapping detail...</p>}
      {error && <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700">{error}</p>}
      {!loading && !error && (
        <div className="mt-3 space-y-2 text-xs">
          <div className="rounded-lg bg-slate-50 px-3 py-2">
            <p className="font-black uppercase text-slate-400">Source Components</p>
            <p className="mt-1 break-words font-semibold text-slate-700">
              {detailSummary.sources.length ? detailSummary.sources.join(', ') : '-'}
            </p>
          </div>
          <div className="rounded-lg bg-slate-50 px-3 py-2">
            <p className="font-black uppercase text-slate-400">Target Component</p>
            <p className="mt-1 break-words font-semibold text-slate-700">{detailSummary.target || context.targetTable}</p>
          </div>
          <div className="rounded-lg bg-slate-50 px-3 py-2">
            <p className="font-black uppercase text-slate-400">Selected Column Flow</p>
            {detailSummary.selectedColumns.length ? detailSummary.selectedColumns.map(row => (
              <p key={`${row.target}-${row.source}`} className="mt-1 break-words font-semibold text-slate-700">
                {row.source} {'->'} {row.target} ({row.type || '-'}){row.expression ? ' expression' : ''}
              </p>
            )) : <p className="mt-1 font-semibold text-slate-500">No matching target-column row in detail.</p>}
          </div>
        </div>
      )}
    </div>
  );
}

function ImpactDrilldown({ groups, selectedMappingName, onSelectMapping }) {
  if (!groups.length) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-3 text-xs font-semibold leading-5 text-slate-500">
        Run Impact to see affected mappings and source/target tables.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {groups.map(group => (
        <details key={group.mappingName} className={`rounded-lg border bg-white ${selectedMappingName === group.mappingName ? 'border-cyan-300' : 'border-slate-200'}`} open>
          <summary
            className="cursor-pointer px-3 py-2 text-xs font-black text-slate-800"
            onClick={() => onSelectMapping(group.mappingName)}
          >
            {group.mappingName}
          </summary>
          <div className="space-y-2 border-t border-slate-100 px-3 py-2 text-xs">
            <div>
              <p className="font-black uppercase text-slate-400">Source Tables</p>
              <p className="mt-1 break-words font-semibold text-slate-700">
                {group.upstreamTables.length ? group.upstreamTables.join(', ') : '-'}
              </p>
            </div>
            <div>
              <p className="font-black uppercase text-slate-400">Target Tables</p>
              <p className="mt-1 break-words font-semibold text-slate-700">
                {group.downstreamTables.length ? group.downstreamTables.join(', ') : '-'}
              </p>
            </div>
          </div>
        </details>
      ))}
    </div>
  );
}

function EvidencePreview({ preflightOutcome, driftSummary }) {
  const items = [
    ['DB write', 'Only Apply DB changes the physical target DB.'],
    ['ODI write', 'Only Sync ODI changes ODI datastore metadata.'],
    ['Scenario', 'Reconcile metadata only; it does not run a scenario.'],
    ['Preflight', preflightOutcome.message],
    ['Drift', driftSummary.message],
  ];
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-black text-slate-800">Plain Summary</h3>
        <i className="fa-solid fa-list-check text-slate-400" />
      </div>
      <div className="space-y-2">
        {items.map(([label, value]) => (
          <div key={label} className="rounded-lg bg-slate-50 px-3 py-2">
            <p className="text-[11px] font-black uppercase text-slate-400">{label}</p>
            <p className="mt-1 text-xs font-semibold leading-5 text-slate-700">{value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function WorkflowStep({ step, state, active, disabled, onRun }) {
  const palette = {
    idle: active ? 'border-cyan-300 bg-cyan-50' : 'border-slate-200 bg-white',
    running: 'border-cyan-300 bg-cyan-50',
    done: 'border-emerald-200 bg-emerald-50',
    error: 'border-rose-200 bg-rose-50',
  };
  const stateIcon = {
    idle: active ? 'fa-arrow-right' : 'fa-circle',
    running: 'fa-spinner fa-spin',
    done: 'fa-check',
    error: 'fa-triangle-exclamation',
  };
  const kindTone = step.kind === 'write' ? 'text-amber-700 bg-amber-100' : 'text-slate-600 bg-slate-100';
  return (
    <div className={`rounded-lg border p-4 ${palette[state] || palette.idle}`}>
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <i className={`fa-solid ${stateIcon[state] || stateIcon.idle} text-xs ${active ? 'text-cyan-700' : 'text-slate-400'}`} />
            <h3 className="text-sm font-black text-slate-800">{step.label}</h3>
            <span className={`rounded-md px-2 py-1 text-[10px] font-black uppercase ${kindTone}`}>{step.kind}</span>
          </div>
          <p className="mt-1 text-xs font-semibold leading-5 text-slate-500">{step.description}</p>
        </div>
        <button
          className={`${BUTTON} min-w-[160px] ${active ? 'bg-cyan-700 text-white hover:bg-cyan-800' : 'border border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}
          disabled={disabled}
          onClick={onRun}
        >
          <i className={`fa-solid ${step.icon}`} />
          {step.actionLabel}
        </button>
      </div>
    </div>
  );
}

export { SchemaFindingsPanel, SourceTargetFindingsPanel, PkParityPanel, MappingDetailPanel, ImpactDrilldown, EvidencePreview, WorkflowStep };

