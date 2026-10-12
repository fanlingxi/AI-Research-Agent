import { createContext, useContext, useEffect, useLayoutEffect, useState, type ReactNode } from "react";

export type ThemePreference = "system" | "light" | "dark";
export const THEME_STORAGE_KEY = "ara-theme";
function readPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    if (value === "light" || value === "dark") return value;
  } catch { /* Storage may be disabled. */ }
  return "system";
}
const ThemeContext = createContext({ preference: "system" as ThemePreference, setPreference: (_value: ThemePreference) => {} });
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, updatePreference] = useState(readPreference);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!media) return;
    const update = () => setSystemDark(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = preference === "system" ? (systemDark ? "dark" : "light") : preference;
  }, [preference, systemDark]);
  const setPreference = (value: ThemePreference) => {
    updatePreference(value);
    try { localStorage.setItem(THEME_STORAGE_KEY, value); } catch { /* Keep the in-memory preference. */ }
  };
  return <ThemeContext.Provider value={{ preference, setPreference }}>{children}</ThemeContext.Provider>;
}
export const useTheme = () => useContext(ThemeContext);
