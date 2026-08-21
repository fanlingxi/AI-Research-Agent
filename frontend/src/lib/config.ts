const DEFAULT_OPERATIONS_URL = "http://127.0.0.1:8501";

export const appConfig = {
  operationsUrl: import.meta.env.VITE_OPERATIONS_URL || DEFAULT_OPERATIONS_URL,
} as const;
