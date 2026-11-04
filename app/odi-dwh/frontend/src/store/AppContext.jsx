/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useReducer } from 'react';

const AppContext = createContext(null);
const AppDispatch = createContext(null);

const initialState = {
  // Tab
  currentTab: 'ai-studio',   // 'ai-studio' | 'data-ops' | 'knowledge' | 'control'

  // Status
  systemStatus: null,        // { ok, checks, count }
  statusLoading: false,

  // Alerts
  alerts: [],
  alertCount: 0,

  // Chat
  chatHistory: [],           // [{ id, role, content, timestamp, queries?, mapping_request? }]
  pendingDml: null,          // { sql, label } | null

  // Data Ops
  etlView: 'mappings',      // 'mappings'|'sessions'|'scenarios'|'kms'|'dq'|'langgraph'|'impact'|'schema-sync'
  currentMappingData: null,  // mapping detail from ODI
  mappingsList: [],          // ODI mappings list

  // Modals
  modal: null,               // { type: 'rag'|'compare'|'wizard'|'surgeon', props: {} }

  // Toast
  toast: null,               // { message, type: 'success'|'error' }
};

function reducer(state, action) {
  switch (action.type) {
    case 'SET_TAB':
      return { ...state, currentTab: action.payload };

    case 'SET_STATUS':
      return { ...state, systemStatus: action.payload, statusLoading: false };
    case 'SET_STATUS_LOADING':
      return { ...state, statusLoading: true };

    case 'SET_ALERTS':
      return { ...state, alerts: action.payload, alertCount: action.payload.length };

    case 'ADD_MESSAGE':
      return { ...state, chatHistory: [...state.chatHistory, action.payload] };
    case 'REPLACE_MESSAGE':
      return {
        ...state,
        chatHistory: state.chatHistory.map(m =>
          m.id === action.payload.id ? { ...m, ...action.payload } : m
        ),
      };
    case 'CLEAR_CHAT':
      return { ...state, chatHistory: [], pendingDml: null };
    case 'SET_PENDING_DML':
      return { ...state, pendingDml: action.payload };
    case 'CLEAR_PENDING_DML':
      return { ...state, pendingDml: null };

    case 'SET_ETL_VIEW':
      return { ...state, etlView: action.payload };
    case 'SET_CURRENT_MAPPING':
      return { ...state, currentMappingData: action.payload };
    case 'SET_MAPPINGS_LIST':
      return { ...state, mappingsList: action.payload };

    case 'OPEN_MODAL':
      return { ...state, modal: action.payload };
    case 'CLOSE_MODAL':
      return { ...state, modal: null };

    case 'SHOW_TOAST':
      return { ...state, toast: action.payload };
    case 'CLEAR_TOAST':
      return { ...state, toast: null };

    default:
      return state;
  }
}

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <AppContext.Provider value={state}>
      <AppDispatch.Provider value={dispatch}>
        {children}
      </AppDispatch.Provider>
    </AppContext.Provider>
  );
}

export const useApp = () => {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp must be inside AppProvider');
  return ctx;
};

export const useDispatch = () => {
  const ctx = useContext(AppDispatch);
  if (!ctx) throw new Error('useDispatch must be inside AppProvider');
  return ctx;
};
