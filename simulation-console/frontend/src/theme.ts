export type Theme = 'light' | 'dark';
const preferenceKey = 'fleet.console-theme';

export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(preferenceKey);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* Use the system preference when storage is unavailable. */ }
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function applyTheme(theme: Theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  if (persist) {
    try { localStorage.setItem(preferenceKey, theme); }
    catch { /* The current session still supports theme switching. */ }
  }
}
