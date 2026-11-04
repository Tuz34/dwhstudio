// Vite exposes these at build time. Changing VITE_* values after build requires rebuilding the SPA.
export const LOCAL_OPENAI_BASE_URL =
  import.meta.env.VITE_LOCAL_OPENAI_BASE_URL || 'http://localhost:1234/v1';

export const AIRFLOW_URL =
  import.meta.env.VITE_AIRFLOW_URL || 'http://localhost:8080';
