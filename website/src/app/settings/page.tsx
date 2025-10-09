'use client';
import { useEffect, useState } from 'react';

export default function SettingsPage() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    (typeof window !== 'undefined' && (localStorage.getItem('theme') as 'light' | 'dark')) || 'light'
  );

  useEffect(() => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('theme', theme);
      document.documentElement.dataset.theme = theme;
    }
  }, [theme]);

  return (
    <main style={{ padding: 24 }}>
      <h1>Settings</h1>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <label>Theme</label>
        <select value={theme} onChange={(e) => setTheme(e.target.value as 'light' | 'dark')}>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </div>
    </main>
  );
}


