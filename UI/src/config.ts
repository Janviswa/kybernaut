export const config = {
  apiBase: import.meta.env.DEV
    ? '/api'
    : import.meta.env.VITE_API_BASE_URL || '/api',
  timeoutMs: 60000,
};
