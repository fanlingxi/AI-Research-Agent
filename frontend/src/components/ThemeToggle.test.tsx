import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ThemeProvider, THEME_STORAGE_KEY } from "../lib/theme";
import { ThemeToggle } from "./ThemeToggle";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});
function renderToggle() { return render(<ThemeProvider><ThemeToggle /></ThemeProvider>); }
it("persists the selected theme across remounts and follows system changes", async () => {
  let listener = () => {};
  const media = { matches: true, addEventListener: vi.fn((_event, callback) => { listener = callback; }), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn(() => media));
  const user = userEvent.setup();
  const first = renderToggle();
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  await user.click(screen.getByRole("button", { name: "浅色" }));
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  first.unmount();
  const second = renderToggle();
  expect(screen.getByRole("button", { name: "浅色" })).toHaveAttribute("aria-pressed", "true");
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  await user.click(screen.getByRole("button", { name: "深色" }));
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  await user.click(screen.getByRole("button", { name: "跟随系统" }));
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
  act(() => { media.matches = false; listener(); });
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  second.unmount();
  expect(media.removeEventListener).toHaveBeenCalled();
});
it("keeps switching when storage is unavailable", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  renderToggle();
  await userEvent.setup().click(screen.getByRole("button", { name: "深色" }));
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
});
