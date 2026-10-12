import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";
import { cn } from "../lib/utils";

const options = [
  { value: "system", label: "跟随系统", Icon: Monitor },
  { value: "light", label: "浅色", Icon: Sun },
  { value: "dark", label: "深色", Icon: Moon },
] as const;
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  return (
    <div aria-label="外观主题" role="group" className="my-1 flex rounded-lg border bg-subtle p-0.5">
      {options.map(({ value, label, Icon }) => (
        <button key={value} type="button" aria-pressed={preference === value}
          onClick={() => setPreference(value)}
          className={cn("flex flex-1 items-center justify-center gap-1 rounded-md px-1 py-1.5 text-[11px] transition-colors", preference === value ? "bg-panel text-brand-strong shadow-sm" : "text-muted-ink hover:text-ink")}>
          <Icon size={12} aria-hidden="true" />{label}
        </button>
      ))}
    </div>
  );
}
