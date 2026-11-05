import { useState, useCallback } from 'react';
import { useApp, useDispatch } from '../../store/AppContext';
import { ChatArea } from '../chat/ChatArea';
import { InputArea } from '../chat/InputArea';
import { DmlBanner } from '../chat/DmlBanner';
import { findPendingDmlQuery } from '../chat/dmlActions';
import { TableSidebar } from '../sidebars/TableSidebar';
import { askAi, clearHistory, executeQuery, exportExcel } from '../../services/api';

let msgId = 0;
function newId() { return ++msgId; }

function messageContentForHistory(message) {
  const baseContent = message.content || '';
  const resultSnippets = (message.queries || [])
    .filter(query => query?.result?.columns?.length && Array.isArray(query.result.rows))
    .map(query => {
      const columns = query.result.columns;
      const rows = query.result.rows.slice(0, 3).map(row =>
        columns.map(column => `${column}=${row[column] ?? ''}`).join(', ')
      );
      return [
        `[${query.label || 'Result Table'}]`,
        `Columns: ${columns.join(', ')}`,
        'Rows:',
        ...rows,
      ].join('\n');
    });

  return [baseContent, ...resultSnippets].filter(Boolean).join('\n\n');
}

export function AiStudio() {
  const dispatch = useDispatch();
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { chatHistory } = useApp();

  const addMessage = useCallback((msg) => {
    dispatch({ type: 'ADD_MESSAGE', payload: { id: newId(), ...msg, timestamp: Date.now() } });
  }, [dispatch]);

  const handleSubmit = useCallback(async (text) => {
    // Prepare chat history with role/content only, limited to the last 10 messages.
    const history = chatHistory.slice(-10).map(m => ({
      role: m.role === 'ai' ? 'assistant' : m.role,
      content: messageContentForHistory(m)
    }));

    // Add user message
    addMessage({ role: 'user', content: text });
    // Add loading bubble
    const loadingId = newId();
    dispatch({ type: 'ADD_MESSAGE', payload: { id: loadingId, role: 'ai', content: '', loading: true, timestamp: Date.now() } });
    setLoading(true);

    try {
      const data = await askAi({ message: text, history });

      // Remove loading bubble by replacing dispatch
      // Since we can't remove by id easily, we'll just let it be replaced by real message
      // Better: add real message which will appear after loading
      dispatch({
        type: 'REPLACE_MESSAGE',
        payload: {
          id: loadingId,
          role: 'ai',
          content: data.aciklama || '',
          queries: data.queries || [],
          mapping_request: data.mapping_request || null,
          mapping_requests: data.mapping_requests || [],
          loading: false,
          timestamp: Date.now(),
        }
      });

      // Handle pending DML
      const dmlQuery = findPendingDmlQuery(data.queries);
      if (dmlQuery) {
        dispatch({ type: 'SET_PENDING_DML', payload: { sql: dmlQuery.sql, label: dmlQuery.label } });
      }

    } catch (e) {
      dispatch({
        type: 'REPLACE_MESSAGE',
        payload: {
          id: loadingId,
          role: 'ai',
          content: `Error: ${e.message}`,
          queries: [],
          loading: false,
          timestamp: Date.now(),
        }
      });
    } finally {
      setLoading(false);
    }
  }, [addMessage, chatHistory, dispatch]);

  const handleRun = useCallback(async (query) => {
    // Show executing message
    addMessage({ role: 'system', content: `Running: ${query.label}` });
    try {
      const data = await executeQuery({ sql: query.sql, label: query.label });
      addMessage({
        role: 'ai',
        content: data.result || 'Completed',
        queries: data.query ? [data.query] : [],
      });
    } catch (e) {
      addMessage({ role: 'ai', content: `Run error: ${e.message}`, queries: [] });
    }
  }, [addMessage]);

  const handleDmlResult = useCallback(({ success, error }) => {
    if (success) {
      addMessage({ role: 'ai', content: '✅ DML operation completed successfully.', queries: [] });
    } else {
      addMessage({ role: 'ai', content: `❌ DML error: ${error}`, queries: [] });
    }
  }, [addMessage]);

  const handleExport = useCallback(async () => {
    // Export to Excel = export the LAST data table returned in the chat
    // (not the conversation text). Finds the latest query.result.columns/rows.
    let headers = [];
    let rows = [];
    for (let i = chatHistory.length - 1; i >= 0 && headers.length === 0; i--) {
      const queries = chatHistory[i]?.queries || [];
      for (let j = queries.length - 1; j >= 0; j--) {
        const r = queries[j]?.result;
        if (r && Array.isArray(r.columns) && r.columns.length > 0) {
          headers = r.columns;
          rows = Array.isArray(r.rows) ? r.rows : [];
          break;
        }
      }
    }

    if (headers.length === 0) {
      dispatch({
        type: 'ADD_MESSAGE',
        payload: {
          id: newId(),
          role: 'system',
          content: 'No table to export. Excel export downloads the last data table from the chat; first run a query that returns a table.',
          timestamp: Date.now(),
        },
      });
      return;
    }

    try {
      const blob = await exportExcel({ headers, rows });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'dwh_export.xlsx';
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      dispatch({
        type: 'ADD_MESSAGE',
        payload: {
          id: newId(),
          role: 'system',
          content: err.message || 'Excel export failed.',
          timestamp: Date.now(),
        },
      });
    }
  }, [chatHistory, dispatch]);

  const handleClearHistory = useCallback(async () => {
    try {
      await clearHistory();
      dispatch({ type: 'CLEAR_CHAT' });
    } catch (err) {
      dispatch({
        type: 'ADD_MESSAGE',
        payload: {
          id: newId(),
          role: 'system',
          content: err.message || 'Chat history could not be cleared.',
          timestamp: Date.now(),
        },
      });
    }
  }, [dispatch]);

  const handleWizardOpen = useCallback((mappingRequest) => {
    dispatch({ type: 'OPEN_MODAL', payload: { type: 'wizard', props: { mappingRequest } } });
  }, [dispatch]);

  return (
    <div className="flex-1 flex min-w-0 flex-col overflow-hidden relative">
      <ChatArea
        onRun={handleRun}
        onWizardOpen={handleWizardOpen}
        onSuggestion={handleSubmit}
      />
      <DmlBanner onResult={handleDmlResult} />
      <InputArea
        onSubmit={handleSubmit}
        onToggleSidebar={() => setSidebarOpen(p => !p)}
        onExport={handleExport}
        onClearHistory={handleClearHistory}
        loading={loading}
      />
      <TableSidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
    </div>
  );
}
