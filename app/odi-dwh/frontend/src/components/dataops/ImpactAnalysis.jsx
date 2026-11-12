import { useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import {
  getImpact,
  getOdiMappingDetail,
  getOdiMappings,
  getUsageDetails,
} from '../../services/api';
import {
  LAYER_OPTIONS,
  cleanName,
  displayRole,
  displayType,
  impactLabel,
  impactTitle,
  layerMatches,
  purposeText,
  sameTable,
  shortName,
  splitTables,
  tableMeta,
  tableRole,
  uniqueImpactRows,
} from './impactAnalysisModel';

function TableBadge({ value }) {
  const meta = tableMeta(value);
  return (
    <span className={clsx('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-black uppercase', meta.badge)}>
      {meta.system}
    </span>
  );
}

function LayerBadge({ layer }) {
  const value = cleanName(layer).toUpperCase();
  if (!value) return null;
  const tone =
    value === 'EDW' ? 'border-indigo-200 bg-indigo-50 text-indigo-700' :
    value === 'DM' ? 'border-amber-200 bg-amber-50 text-amber-700' :
    'border-cyan-200 bg-cyan-50 text-cyan-700';
  return (
    <span className={clsx('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-black uppercase', tone)}>
      {value}
    </span>
  );
}

function TableLabel({ value, className, showSchema = true }) {
  const meta = tableMeta(value);
  const display = showSchema && meta.schema ? `${meta.schema}.${meta.table}` : meta.table;
  return (
    <span className={clsx('flex min-w-0 items-center gap-2', className)}>
      <span className="truncate font-mono font-bold">{display}</span>
      <TableBadge value={value} />
    </span>
  );
}

export function ImpactAnalysis() {
  const [mappings, setMappings] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [impact, setImpact] = useState([]);
  const [usage, setUsage] = useState([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState(null);
  const [mode, setMode] = useState('mappings');
  const [search, setSearch] = useState('');
  const [selectedLayer, setSelectedLayer] = useState('');
  const [selectedTableMappingName, setSelectedTableMappingName] = useState(null);
  const [collapsedNodes, setCollapsedNodes] = useState({});

  function toggleNode(key) {
    setCollapsedNodes(prev => ({ ...prev, [key]: !prev[key] }));
  }

  useEffect(() => {
    const id = setTimeout(async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await getOdiMappings();
        const list = Array.isArray(data) ? data : [];
        setMappings(list);
        if (list.length > 0) setSelected({ type: 'mapping', value: list[0].MAPPING_NAME });
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }, 0);
    return () => clearTimeout(id);
  }, []);

  const tables = useMemo(() => {
    const grouped = new Map();
    mappings.forEach(mapping => {
      [...splitTables(mapping.SRC_TABLE), cleanName(mapping.TGT_TABLE)]
        .filter(t => t && t !== '?')
        .forEach(table => {
          const key = shortName(table).toUpperCase();
          if (!grouped.has(key)) grouped.set(key, { name: table, mappings: [] });
          grouped.get(key).mappings.push(mapping);
        });
    });
    return [...grouped.values()].sort((a, b) => shortName(a.name).localeCompare(shortName(b.name)));
  }, [mappings]);

  const visibleMappings = useMemo(() => {
    const q = search.toLowerCase();
    return mappings.filter(m => {
      const haystack = [
        m.MAPPING_NAME,
        m.SRC_TABLE,
        m.TGT_TABLE,
        m.DESC,
        m.LAYER,
      ].join(' ').toLowerCase();
      return layerMatches(m.LAYER, selectedLayer) && (!q || haystack.includes(q));
    });
  }, [mappings, search, selectedLayer]);

  const visibleTables = useMemo(() => {
    const q = search.toLowerCase();
    return tables.filter(t => {
      const haystack = [
        t.name,
        ...t.mappings.map(m => m.MAPPING_NAME),
      ].join(' ').toLowerCase();
      return !q || haystack.includes(q);
    });
  }, [tables, search]);

  const visibleTableGroups = useMemo(() => {
    const systems = new Map();
    visibleTables.forEach(item => {
      const meta = tableMeta(item.name);
      const systemKey = meta.system;
      const schemaKey = meta.schema || meta.system;
      if (!systems.has(systemKey)) {
        systems.set(systemKey, { system: systemKey, badge: meta.badge, count: 0, schemas: new Map() });
      }
      const system = systems.get(systemKey);
      system.count += 1;
      if (!system.schemas.has(schemaKey)) system.schemas.set(schemaKey, { schema: schemaKey, items: [] });
      system.schemas.get(schemaKey).items.push(item);
    });
    const order = ['ORACLE', 'SOURCE', 'FILE', 'DB'];
    return [...systems.values()]
      .map(system => ({
        ...system,
        schemas: [...system.schemas.values()].sort((a, b) => a.schema.localeCompare(b.schema)),
      }))
      .sort((a, b) => order.indexOf(a.system) - order.indexOf(b.system));
  }, [visibleTables]);

  const visibleMappingGroups = useMemo(() => {
    // Project -> Folder -> mappings tree. Folder/project come from the ODI
    // repository metadata (SNP_FOLDER / SNP_PROJECT). Until the mappings API
    // exposes them per row, fall back to the current single repository folder.
    const mappingProject = m => cleanName(m.PROJECT || m.project_name || m.PROJECT_NAME) || 'ODI Project';
    const mappingFolder = m => cleanName(m.FOLDER || m.folder_name || m.FOLDER_NAME) || 'FLD_CORE_MAPPINGS';
    const projects = new Map();
    visibleMappings.forEach(m => {
      const projectKey = mappingProject(m);
      const folderKey = mappingFolder(m);
      if (!projects.has(projectKey)) projects.set(projectKey, { project: projectKey, count: 0, folders: new Map() });
      const project = projects.get(projectKey);
      project.count += 1;
      if (!project.folders.has(folderKey)) project.folders.set(folderKey, { folder: folderKey, items: [] });
      project.folders.get(folderKey).items.push(m);
    });
    return [...projects.values()]
      .map(project => ({
        ...project,
        folders: [...project.folders.values()]
          .map(f => ({ ...f, items: f.items.sort((a, b) => cleanName(a.MAPPING_NAME).localeCompare(cleanName(b.MAPPING_NAME))) }))
          .sort((a, b) => a.folder.localeCompare(b.folder)),
      }))
      .sort((a, b) => a.project.localeCompare(b.project));
  }, [visibleMappings]);

  useEffect(() => {
    if (!selected) return undefined;
    const id = setTimeout(async () => {
      setDetail(null);
      setImpact([]);
      setUsage([]);
      setSelectedTableMappingName(null);
      setDetailLoading(true);
      setError(null);
      try {
        if (selected.type === 'mapping') {
          const data = await getOdiMappingDetail(selected.value);
          if (data?.error) {
            setError(data.error);
            setDetail({
              MAPPING_NAME: selected.value,
              SRC_TABLE: '?',
              TGT_TABLE: '?',
              columns: [],
              filters: [],
            });
          } else {
            setDetail(data);
          }
        } else {
          const relatedMappings = mappings.filter(m =>
            sameTable(m.TGT_TABLE, selected.value) ||
            splitTables(m.SRC_TABLE).some(t => sameTable(t, selected.value))
          );
          const [impactData, usageData] = await Promise.all([
            getImpact(shortName(selected.value), 'both').catch(e => ({ error: e.message })),
            Promise.all(
              relatedMappings.slice(0, 12).map(async m => {
                try {
                  const rows = await getUsageDetails(shortName(selected.value), m.MAPPING_NAME);
                  return { mapping: m.MAPPING_NAME, rows: Array.isArray(rows) ? rows : [] };
                } catch {
                  return { mapping: m.MAPPING_NAME, rows: [] };
                }
              })
            ),
          ]);
          if (impactData?.error) setError(impactData.error);
          else setImpact(Array.isArray(impactData) ? impactData : []);
          setUsage(usageData);
          setDetail({ table: selected.value, mappings: relatedMappings });
        }
      } catch (e) {
        setError(e.message);
      } finally {
        setDetailLoading(false);
      }
    }, 0);
    return () => clearTimeout(id);
  }, [selected, mappings]);

  function selectTable(tableName) {
    setMode('tables');
    setSelected({ type: 'table', value: tableName });
  }

  function renderLeftList() {
    const searching = search.trim().length > 0;

    if (mode === 'mappings') {
      return visibleMappingGroups.map(project => {
        const projectKey = `proj:${project.project}`;
        const projectCollapsed = !searching && collapsedNodes[projectKey];
        return (
        <div key={project.project} className="border-b border-slate-200">
          <button
            type="button"
            onClick={() => toggleNode(projectKey)}
            aria-expanded={!projectCollapsed}
            className="sticky top-0 z-20 flex w-full items-center justify-between border-b border-slate-200 bg-white px-3 py-2.5 text-left hover:bg-slate-50"
          >
            <div className="flex min-w-0 items-center gap-2">
              <i className={clsx(
                'fa-solid fa-chevron-right text-[9px] text-slate-400 transition-transform',
                !projectCollapsed && 'rotate-90'
              )}></i>
              <i className="fa-solid fa-diagram-project text-xs text-rose-500"></i>
              <div className="truncate text-xs font-black uppercase text-slate-700">{project.project}</div>
            </div>
            <span className="shrink-0 rounded border border-rose-100 bg-rose-50 px-1.5 py-0.5 text-[9px] font-black uppercase text-rose-700">
              {project.count} mappings
            </span>
          </button>
          {!projectCollapsed && project.folders.map(folderGroup => {
            const folderKey = `fld:${project.project}.${folderGroup.folder}`;
            const folderCollapsed = !searching && collapsedNodes[folderKey];
            return (
            <div key={`${project.project}-${folderGroup.folder}`}>
              <button
                type="button"
                onClick={() => toggleNode(folderKey)}
                aria-expanded={!folderCollapsed}
                className="flex w-full items-center justify-between border-b border-slate-100 bg-slate-50 px-6 py-2 text-left hover:bg-slate-100"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <i className={clsx(
                    'fa-solid fa-chevron-right text-[8px] text-slate-400 transition-transform',
                    !folderCollapsed && 'rotate-90'
                  )}></i>
                  <i className="fa-solid fa-folder text-[10px] text-amber-400"></i>
                  <span className="truncate font-mono text-[11px] font-black uppercase text-slate-600">{folderGroup.folder}</span>
                </div>
                <span className="shrink-0 font-mono text-[9px] text-slate-400">{folderGroup.items.length}</span>
              </button>
              {!folderCollapsed && folderGroup.items.map(mapping => {
                const active = selected?.type === 'mapping' && selected.value === mapping.MAPPING_NAME;
                return (
                  <button
                    key={mapping.MAPPING_NAME}
                    onClick={() => setSelected({ type: 'mapping', value: mapping.MAPPING_NAME })}
                    className={clsx(
                      'w-full text-left px-8 py-2.5 border-b border-slate-50 transition-colors',
                      active ? 'bg-rose-50 border-l-4 border-l-rose-500' : 'hover:bg-slate-50 border-l-4 border-l-transparent'
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <LayerBadge layer={mapping.LAYER} />
                      <span className="min-w-0 flex-1 truncate font-mono text-xs font-bold text-slate-700">{mapping.MAPPING_NAME}</span>
                    </div>
                  </button>
                );
              })}
            </div>
            );
          })}
        </div>
        );
      });
    }

    return visibleTableGroups.map(system => {
      const systemKey = `sys:${system.system}`;
      const systemCollapsed = !searching && collapsedNodes[systemKey];
      return (
      <div key={system.system} className="border-b border-slate-200">
        <button
          type="button"
          onClick={() => toggleNode(systemKey)}
          aria-expanded={!systemCollapsed}
          className="sticky top-0 z-20 flex w-full items-center justify-between border-b border-slate-200 bg-white px-3 py-2.5 text-left hover:bg-slate-50"
        >
          <div className="flex min-w-0 items-center gap-2">
            <i className={clsx(
              'fa-solid fa-chevron-right text-[9px] text-slate-400 transition-transform',
              !systemCollapsed && 'rotate-90'
            )}></i>
            <i className={clsx(
              'fa-solid text-xs',
              system.system === 'SOURCE' ? 'fa-server text-sky-500' : system.system === 'ORACLE' ? 'fa-database text-emerald-500' : 'fa-file-lines text-slate-500'
            )}></i>
            <div className="truncate text-xs font-black uppercase text-slate-700">{system.system}</div>
          </div>
          <span className={clsx('shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-black uppercase', system.badge)}>
            {system.count} tables
          </span>
        </button>
        {!systemCollapsed && system.schemas.map(schemaGroup => {
          const schemaKey = `sch:${system.system}.${schemaGroup.schema}`;
          const schemaCollapsed = !searching && collapsedNodes[schemaKey];
          return (
          <div key={`${system.system}-${schemaGroup.schema}`}>
            <button
              type="button"
              onClick={() => toggleNode(schemaKey)}
              aria-expanded={!schemaCollapsed}
              className="flex w-full items-center justify-between border-b border-slate-100 bg-slate-50 px-6 py-2 text-left hover:bg-slate-100"
            >
              <div className="flex min-w-0 items-center gap-2">
                <i className={clsx(
                  'fa-solid fa-chevron-right text-[8px] text-slate-400 transition-transform',
                  !schemaCollapsed && 'rotate-90'
                )}></i>
                <i className="fa-solid fa-folder text-[10px] text-amber-400"></i>
                <span className="truncate font-mono text-[11px] font-black uppercase text-slate-600">{schemaGroup.schema}</span>
              </div>
              <span className="shrink-0 font-mono text-[9px] text-slate-400">{schemaGroup.items.length}</span>
            </button>
            {!schemaCollapsed && schemaGroup.items.map(item => {
              const active = selected?.type === 'table' && sameTable(selected.value, item.name);
              const targetCount = item.mappings.filter(m => sameTable(m.TGT_TABLE, item.name)).length;
              const sourceCount = item.mappings.filter(m => splitTables(m.SRC_TABLE).some(t => sameTable(t, item.name))).length;
              return (
                <button
                  key={shortName(item.name)}
                  onClick={() => setSelected({ type: 'table', value: item.name })}
                  className={clsx(
                    'w-full text-left px-8 py-2.5 border-b border-slate-50 transition-colors',
                    active ? 'bg-rose-50 border-l-4 border-l-rose-500' : 'hover:bg-slate-50 border-l-4 border-l-transparent'
                  )}
                >
                  <div className="flex items-center gap-2">
                    <i className="fa-solid fa-table text-blue-400 text-xs"></i>
                    <span className="min-w-0 flex-1 truncate font-mono text-xs font-bold text-slate-700">{shortName(item.name)}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1.5 text-[10px] text-slate-400">
                    <span>{item.mappings.length} mapping</span>
                    {sourceCount > 0 && <span className="rounded bg-blue-50 px-1.5 py-0.5 font-bold text-blue-600">{sourceCount} source</span>}
                    {targetCount > 0 && <span className="rounded bg-emerald-50 px-1.5 py-0.5 font-bold text-emerald-600">{targetCount} target</span>}
                  </div>
                </button>
              );
            })}
          </div>
          );
        })}
      </div>
      );
    });
  }

  function renderMappingDetail() {
    if (!detail) return null;
    const sources = Array.isArray(detail.sources) && detail.sources.length > 0
      ? detail.sources
      : splitTables(detail.SRC_TABLE).map(src => ({ name: src, component: shortName(src), role: 'Source', usage_count: 0 }));
    return (
      <div className="space-y-5">
        <div>
          <div className="text-[10px] font-bold tracking-wider text-slate-400">Mapping</div>
          <h4 className="mt-1 font-mono text-lg font-black text-slate-800">{detail.MAPPING_NAME}</h4>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <div className="rounded-xl border border-blue-100 bg-blue-50/40 p-4">
            <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase text-blue-600">
              <i className="fa-solid fa-arrow-right-from-bracket"></i>
              Source Tables
            </div>
            <div className="space-y-2">
              {sources.length === 0 ? <span className="text-sm text-slate-400">No source found.</span> : sources.map(src => (
                <button
                  key={src.name}
                  onClick={() => selectTable(src.name)}
                  className="block w-full rounded-lg border border-blue-100 bg-white px-3 py-2 text-left hover:border-blue-300"
                >
                  <div className="flex flex-wrap items-start gap-2">
                    <TableLabel value={src.name} className="flex-1 text-xs text-blue-700" />
                    <span className={clsx(
                      'shrink-0 rounded px-1.5 py-0.5 text-[9px] font-black uppercase',
                      src.role?.includes('Lookup') ? 'bg-violet-50 text-violet-600' : 'bg-blue-50 text-blue-600'
                    )}>{displayRole(src.role)}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {src.usage_count > 0 ? `${src.usage_count} columns/expressions are fed by this source` : 'Used as a source component in the mapping'}
                  </div>
                </button>
              ))}
            </div>
          </div>
          <div className="rounded-xl border border-emerald-100 bg-emerald-50/40 p-4">
            <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase text-emerald-600">
              <i className="fa-solid fa-bullseye"></i>
              Target Table
            </div>
            <button onClick={() => selectTable(detail.TGT_TABLE)} className="rounded-lg border border-emerald-100 bg-white px-3 py-2 text-left text-xs text-emerald-700 hover:border-emerald-300">
              <TableLabel value={detail.TGT_TABLE} />
            </button>
          </div>
        </div>

        {detail.filters?.length > 0 && (
          <div className="rounded-xl border border-amber-100 bg-amber-50/40 p-4">
            <div className="mb-2 text-xs font-bold uppercase text-amber-600">Filters</div>
            <div className="space-y-2">
              {detail.filters.map((filter, i) => (
                <div key={i} className="rounded-lg bg-white p-2 font-mono text-xs text-amber-800 border border-amber-100 break-all">{filter}</div>
              ))}
            </div>
          </div>
        )}

        {detail.joins?.length > 0 && (
          <div className="rounded-xl border border-violet-100 bg-violet-50/40 p-4">
            <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase text-violet-600">
              <i className="fa-solid fa-code-branch"></i>
              Join / Lookup Conditions
            </div>
            <div className="space-y-2">
              {detail.joins.map((join, i) => (
                <div key={i} className="rounded-lg border border-violet-100 bg-white p-2 font-mono text-xs text-violet-800 break-all">{join}</div>
              ))}
            </div>
          </div>
        )}

        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden">
          <div className="border-b border-slate-100 bg-slate-50 px-4 py-3 text-xs font-bold uppercase text-slate-500">
            COLUMN MAPPINGS ({detail.columns?.length || 0})
          </div>
          <div className="max-h-[360px] overflow-y-auto">
            <table className="w-full table-fixed text-left text-xs">
              <thead className="sticky top-0 bg-white text-[10px] uppercase text-slate-400">
                <tr>
                  <th className="px-4 py-2 w-[28%]">Target Column</th>
                  <th className="px-4 py-2 w-[18%]">Type</th>
                  <th className="px-4 py-2">SOURCE / EXPRESSION</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {(detail.columns || []).map((col, i) => (
                  <tr key={i} className="hover:bg-slate-50">
                    <td className="px-4 py-2 font-mono font-bold text-slate-700 break-all">{col.tgt}</td>
                    <td className="px-4 py-2 font-mono text-slate-500">{displayType(col.type, 'N/A')}</td>
                    <td className="px-4 py-2 font-mono text-slate-600 break-all">
                      {col.is_exp && <span className="mr-2 rounded bg-amber-50 px-1.5 py-0.5 text-[9px] font-black text-amber-600">EXP</span>}
                      {col.src}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  }

  function renderTableDetail() {
    if (!detail) return null;
    const tableMappings = Array.isArray(detail.mappings) ? detail.mappings : [];
    const usageRows = (Array.isArray(usage) ? usage : []).flatMap(u => {
      const rows = Array.isArray(u?.rows) ? u.rows : [];
      return rows.map(row => ({ ...row, mapping: u.mapping }));
    });
    const impactRows = Array.isArray(impact) ? impact : [];
    const tableActsAsTarget = tableMappings.some(mapping => sameTable(mapping.TGT_TABLE, detail.table));
    const tableActsAsSource = tableMappings.some(mapping =>
      splitTables(mapping.SRC_TABLE).some(t => sameTable(t, detail.table))
    );
    const mappingUpstream = tableMappings.flatMap(mapping => {
      if (!sameTable(mapping.TGT_TABLE, detail.table)) return [];
      return splitTables(mapping.SRC_TABLE).map(table => ({
        related_table: table,
        mapping_name: mapping.MAPPING_NAME,
        direction: 'upstream',
        note: 'This mapping uses this source to produce the table.',
      }));
    });
    const mappingDownstream = tableMappings.flatMap(mapping => {
      if (!splitTables(mapping.SRC_TABLE).some(t => sameTable(t, detail.table))) return [];
      return cleanName(mapping.TGT_TABLE) ? [{
        related_table: mapping.TGT_TABLE,
        mapping_name: mapping.MAPPING_NAME,
        direction: 'downstream',
        note: 'This table is used as a source to produce the target table.',
      }] : [];
    });
    const mappingImpactKeys = new Set(
      [...mappingUpstream, ...mappingDownstream].map(row =>
        `${shortName(row.related_table).toUpperCase()}|${row.mapping_name}|${row.direction}`
      )
    );
    const lineageOnlyRows = impactRows.filter(row =>
      !mappingImpactKeys.has(`${shortName(row.related_table).toUpperCase()}|${row.mapping_name}|${row.direction}`) &&
      (row.direction !== 'upstream' || tableActsAsTarget) &&
      (row.direction !== 'downstream' || tableActsAsSource)
    );
    const upstream = uniqueImpactRows([...lineageOnlyRows.filter(item => item?.direction === 'upstream'), ...mappingUpstream]);
    const downstream = uniqueImpactRows([...lineageOnlyRows.filter(item => item?.direction === 'downstream'), ...mappingDownstream]);
    const selectedTableMapping = tableMappings.find(mapping => mapping.MAPPING_NAME === selectedTableMappingName);
    const selectedRole = selectedTableMapping ? tableRole(selectedTableMapping, detail.table) : null;
    const selectedSources = selectedTableMapping ? splitTables(selectedTableMapping.SRC_TABLE) : [];
    const selectedTarget = selectedTableMapping ? cleanName(selectedTableMapping.TGT_TABLE) : '';
    const selectedUsageRows = selectedTableMapping
      ? usageRows.filter(row =>
        row.mapping === selectedTableMapping.MAPPING_NAME &&
        (cleanName(row.expression) || cleanName(row.data_type) || row.comp_type !== 'DATASTORE')
      )
      : [];
    const sourceMappingCount = tableMappings.filter(m =>
      splitTables(m.SRC_TABLE).some(t => sameTable(t, detail.table))
    ).length;
    const targetMappingCount = tableMappings.filter(m => sameTable(m.TGT_TABLE, detail.table)).length;

    return (
      <div className="space-y-4">
        {/* Top compact info bar */}
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white px-4 py-2.5">
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Table</span>
          <TableLabel value={detail.table} className="text-sm text-slate-800" />
          <span className="ml-auto flex items-center gap-3 text-[11px] font-mono text-slate-500">
            <span><span className="font-black text-slate-700">{tableMappings.length}</span> mapping</span>
            <span className="text-slate-300">|</span>
            <span><span className="font-black text-blue-600">{sourceMappingCount}</span> source</span>
            <span className="text-slate-300">|</span>
            <span><span className="font-black text-emerald-600">{targetMappingCount}</span> target</span>
          </span>
        </div>

        {/* Mapping Usage table */}
        <div className="rounded-lg border border-slate-300 bg-white overflow-hidden">
          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-100 px-3 py-1.5">
            <span className="text-[11px] font-black tracking-wider text-slate-600">Mapping Usage</span>
            <span className="text-[10px] font-mono text-slate-500">{tableMappings.length} rows</span>
          </div>
          <div className="max-h-80 overflow-auto">
            <table className="w-full text-left text-[11px] border-collapse">
              <thead className="sticky top-0 z-10 bg-slate-50 text-[10px] uppercase text-slate-500">
                <tr>
                  <th className="border-b border-r border-slate-200 px-2 py-1.5 w-10 text-center">#</th>
                  <th className="border-b border-r border-slate-200 px-2 py-1.5">MAPPING</th>
                  <th className="border-b border-r border-slate-200 px-2 py-1.5 w-16 text-center">Role</th>
                  <th className="border-b border-r border-slate-200 px-2 py-1.5 w-20">Layer</th>
                  <th className="border-b border-r border-slate-200 px-2 py-1.5">Sources</th>
                  <th className="border-b border-slate-200 px-2 py-1.5">Target</th>
                </tr>
              </thead>
              <tbody>
                {tableMappings.length === 0 ? (
                  <tr><td colSpan={6} className="px-3 py-6 text-center text-slate-400">No mapping relationship was found for this table.</td></tr>
                ) : tableMappings.map((mapping, idx) => {
                  const role = tableRole(mapping, detail.table);
                  const active = selectedTableMappingName === mapping.MAPPING_NAME;
                  const sources = splitTables(mapping.SRC_TABLE);
                  return (
                    <tr
                      key={mapping.MAPPING_NAME}
                      onClick={() => setSelectedTableMappingName(mapping.MAPPING_NAME)}
                      className={clsx(
                        'cursor-pointer transition-colors',
                        active ? 'bg-rose-50' : 'hover:bg-slate-50'
                      )}
                    >
                      <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center font-mono text-slate-400">{idx + 1}</td>
                      <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono font-black text-slate-700">{mapping.MAPPING_NAME}</td>
                      <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center">
                        <span className={clsx(
                          'rounded px-1.5 py-0.5 text-[9px] font-black uppercase',
                          role === 'Target' ? 'bg-emerald-100 text-emerald-700' : role === 'Source' ? 'bg-blue-100 text-blue-700' : 'bg-slate-200 text-slate-600'
                        )}>{role}</span>
                      </td>
                      <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono text-slate-500">{mapping.LAYER || '—'}</td>
                      <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono text-slate-600 truncate max-w-xs" title={sources.join(', ')}>
                        {sources.map(shortName).join(', ') || '—'}
                      </td>
                      <td className="border-b border-slate-100 px-2 py-1.5 font-mono text-slate-600">{shortName(mapping.TGT_TABLE) || '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Selected mapping: two mini Excel-style tables, Sources | Target */}
        {selectedTableMapping ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between rounded-lg border border-slate-300 bg-slate-100 px-3 py-1.5">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-black tracking-wider text-slate-600">Mapping Detail</span>
                <span className="font-mono text-xs font-black text-slate-800">{selectedTableMapping.MAPPING_NAME}</span>
              </div>
              <button
                onClick={() => setSelected({ type: 'mapping', value: selectedTableMapping.MAPPING_NAME })}
                className="inline-flex items-center gap-1.5 rounded border border-slate-300 bg-white px-2 py-1 text-[10px] font-bold text-slate-600 hover:border-rose-300 hover:text-rose-600"
              >
                <i className="fa-solid fa-up-right-from-square text-[9px]"></i>
                Column Detail
              </button>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              {/* Sources: Excel-style table */}
              <div className="rounded-lg border border-slate-300 bg-white overflow-hidden">
                <div className="flex items-center justify-between border-b border-slate-200 bg-blue-50 px-3 py-1.5">
                  <span className="flex items-center gap-1.5 text-[11px] font-black uppercase tracking-wider text-blue-700">
                    <i className="fa-solid fa-arrow-right-from-bracket text-[10px]"></i>
                    Sources
                  </span>
                  <span className="text-[10px] font-mono text-blue-600">{selectedSources.length}</span>
                </div>
                <table className="w-full text-left text-[11px] border-collapse">
                  <thead className="bg-slate-50 text-[10px] uppercase text-slate-500">
                    <tr>
                      <th className="border-b border-r border-slate-200 px-2 py-1.5 w-10 text-center">#</th>
                      <th className="border-b border-r border-slate-200 px-2 py-1.5">Table</th>
                      <th className="border-b border-slate-200 px-2 py-1.5 w-16 text-center">System</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedSources.length === 0 ? (
                      <tr><td colSpan={3} className="px-3 py-4 text-center text-slate-400">No source found.</td></tr>
                    ) : selectedSources.map((src, i) => {
                      const meta = tableMeta(src);
                      const isSelected = sameTable(src, detail.table);
                      return (
                        <tr
                          key={src}
                          onClick={() => selectTable(src)}
                          className={clsx('cursor-pointer hover:bg-blue-50', isSelected && 'bg-rose-50')}
                        >
                          <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center font-mono text-slate-400">{i + 1}</td>
                          <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono font-bold text-slate-700">
                            {meta.schema ? `${meta.schema}.${meta.table}` : meta.table}
                          </td>
                          <td className="border-b border-slate-100 px-2 py-1.5 text-center">
                            <TableBadge value={src} />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Target: Excel-style table */}
              <div className="rounded-lg border border-slate-300 bg-white overflow-hidden">
                <div className="flex items-center justify-between border-b border-slate-200 bg-emerald-50 px-3 py-1.5">
                  <span className="flex items-center gap-1.5 text-[11px] font-black uppercase tracking-wider text-emerald-700">
                    <i className="fa-solid fa-bullseye text-[10px]"></i>
                    Target
                  </span>
                  <span className="text-[10px] font-mono text-emerald-600">1</span>
                </div>
                <table className="w-full text-left text-[11px] border-collapse">
                  <thead className="bg-slate-50 text-[10px] uppercase text-slate-500">
                    <tr>
                      <th className="border-b border-r border-slate-200 px-2 py-1.5 w-10 text-center">#</th>
                      <th className="border-b border-r border-slate-200 px-2 py-1.5">Table</th>
                      <th className="border-b border-slate-200 px-2 py-1.5 w-16 text-center">System</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(() => {
                      const meta = tableMeta(selectedTarget);
                      const isSelected = sameTable(selectedTarget, detail.table);
                      return (
                        <tr
                          onClick={() => selectTable(selectedTarget)}
                          className={clsx('cursor-pointer hover:bg-emerald-50', isSelected && 'bg-rose-50')}
                        >
                          <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center font-mono text-slate-400">1</td>
                          <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono font-bold text-slate-700">
                            {meta.schema ? `${meta.schema}.${meta.table}` : meta.table}
                          </td>
                          <td className="border-b border-slate-100 px-2 py-1.5 text-center">
                            <TableBadge value={selectedTarget} />
                          </td>
                        </tr>
                      );
                    })()}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-500">
              {purposeText(selectedRole, selectedTableMapping)}
            </div>
          </div>
        ) : tableMappings.length > 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white px-4 py-4 text-center text-sm text-slate-400">
            Select a row from the table above to view mapping details.
          </div>
        )}

        {/* Upstream + Downstream combined table */}
        {(upstream.length > 0 || downstream.length > 0) && (
          <div className="rounded-lg border border-slate-300 bg-white overflow-hidden">
            <div className="flex items-center justify-between border-b border-slate-200 bg-slate-100 px-3 py-1.5">
              <span className="text-[11px] font-black uppercase tracking-wider text-slate-600">Table Impacts</span>
              <span className="text-[10px] font-mono text-slate-500">{upstream.length + downstream.length} relationships</span>
            </div>
            <div className="max-h-72 overflow-auto">
              <table className="w-full text-left text-[11px] border-collapse">
                <thead className="sticky top-0 z-10 bg-slate-50 text-[10px] uppercase text-slate-500">
                  <tr>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5 w-28 text-center">Selected Table Role</th>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5">Related Table</th>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5 w-16 text-center">System</th>
                    <th className="border-b border-slate-200 px-2 py-1.5">MAPPING</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ...upstream.map(r => ({ ...r, dir: 'up' })),
                    ...downstream.map(r => ({ ...r, dir: 'down' })),
                  ].map((row, i) => {
                    const meta = tableMeta(row.related_table);
                    return (
                      <tr key={i} className="cursor-pointer hover:bg-slate-50" onClick={() => selectTable(row.related_table)}>
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center">
                          <span
                            title={impactTitle(row.dir)}
                            className={clsx(
                            'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-black uppercase',
                            row.dir === 'up' ? 'bg-blue-100 text-blue-700' : 'bg-amber-100 text-amber-700'
                          )}>
                            <i className={clsx('fa-solid text-[8px]', row.dir === 'up' ? 'fa-arrow-up' : 'fa-arrow-down')}></i>
                            {impactLabel(row.dir)}
                          </span>
                        </td>
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono font-bold text-slate-700">
                          {meta.schema ? `${meta.schema}.${meta.table}` : meta.table}
                        </td>
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center">
                          <TableBadge value={row.related_table} />
                        </td>
                        <td className="border-b border-slate-100 px-2 py-1.5 font-mono text-slate-500">{row.mapping_name}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Column Mappings: Excel-style table */}
        {selectedTableMapping && selectedUsageRows.length > 0 && (
          <div className="rounded-lg border border-slate-300 bg-white overflow-hidden">
            <div className="flex items-center justify-between border-b border-slate-200 bg-slate-100 px-3 py-1.5">
              <span className="text-[11px] font-black uppercase tracking-wider text-slate-600">COLUMN MAPPINGS</span>
              <span className="text-[10px] font-mono text-slate-500">{selectedUsageRows.length} rows</span>
            </div>
            <div className="max-h-[320px] overflow-auto">
              <table className="w-full text-left text-[11px] border-collapse">
                <thead className="sticky top-0 z-10 bg-slate-50 text-[10px] uppercase text-slate-500">
                  <tr>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5 w-10 text-center">#</th>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5 w-[26%]">Target Column</th>
                    <th className="border-b border-r border-slate-200 px-2 py-1.5 w-[18%]">Data Type</th>
                    <th className="border-b border-slate-200 px-2 py-1.5">SOURCE / EXPRESSION</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedUsageRows.map((row, i) => {
                    const fieldName = row.attr_name || row.comp_name || '—';
                    const kind = row.comp_type === 'FILTER' ? 'Filter' : row.comp_type === 'JOIN' ? 'Join' : 'Column';
                    const dataType = displayType(row.data_type, kind);
                    return (
                      <tr key={i} className="hover:bg-slate-50">
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 text-center font-mono text-slate-400">{i + 1}</td>
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono font-bold text-slate-700 break-all">{fieldName}</td>
                        <td className="border-b border-r border-slate-100 px-2 py-1.5 font-mono text-slate-500">
                          <span className="block font-bold text-slate-700">{dataType}</span>
                          {dataType !== kind && <span className="text-[9px] uppercase text-slate-400">{kind}</span>}
                        </td>
                        <td className="border-b border-slate-100 px-2 py-1.5 font-mono text-slate-600 break-all">{row.expression || '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-210px)] min-h-[560px] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <div className="shrink-0 border-b border-slate-200 bg-slate-50 px-5 py-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <i className="fa-solid fa-diagram-successor text-rose-500"></i>
            Impact Explorer
          </h3>
          <div className="flex items-center gap-2">
            <div className="relative">
              <i className="fa-solid fa-search absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-xs"></i>
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search mapping or table"
                className="w-72 rounded-lg border border-slate-200 bg-white py-2 pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-rose-300"
              />
            </div>
            <label className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-slate-400">
              <span>Layer filter</span>
              <select
                value={selectedLayer}
                onChange={e => setSelectedLayer(e.target.value)}
                className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-semibold normal-case tracking-normal text-slate-600 focus:outline-none focus:ring-2 focus:ring-rose-300"
                aria-label="Layer filter"
              >
                {LAYER_OPTIONS.map(layer => (
                  <option key={layer || 'all'} value={layer}>
                    {layer || 'All'}
                  </option>
                ))}
              </select>
            </label>
            <div className="rounded-lg border border-slate-200 bg-white p-1">
              {['mappings', 'tables'].map(id => (
                <button
                  key={id}
                  onClick={() => setMode(id)}
                  className={clsx(
                    'px-3 py-1.5 rounded-md text-xs font-bold transition-colors',
                    mode === id ? 'bg-rose-500 text-white' : 'text-slate-500 hover:bg-slate-50'
                  )}
                >
                  {id === 'mappings' ? 'Mappings' : 'Tables'}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="mx-5 mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-600">
          <i className="fa-solid fa-circle-exclamation mr-2"></i>{error}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-96 shrink-0 border-r border-slate-200 bg-white flex flex-col">
          <div className="border-b border-slate-100 px-4 py-3 text-xs font-bold uppercase tracking-wider text-slate-400">
            {mode === 'mappings' ? `${visibleMappings.length} map` : `${visibleTables.length} tables`}
          </div>
          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="p-6 text-center text-sm text-slate-400">
                <i className="fa-solid fa-spinner fa-spin mr-2"></i>Loading...
              </div>
            ) : renderLeftList()}
          </div>
        </aside>

        <main className="flex-1 overflow-y-auto bg-slate-50/50 p-5">
          {detailLoading ? (
            <div className="flex h-full items-center justify-center text-slate-400">
              <i className="fa-solid fa-spinner fa-spin mr-2"></i>Preparing details...
            </div>
          ) : selected?.type === 'mapping' ? renderMappingDetail() : renderTableDetail()}
        </main>
      </div>
    </div>
  );
}
