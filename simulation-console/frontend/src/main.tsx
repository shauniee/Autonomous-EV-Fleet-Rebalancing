import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

function App() {
  const [status, setStatus] = useState('Checking…');

  useEffect(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);

    async function checkHealth() {
      try {
        const response = await fetch('/health/live', { signal: controller.signal });
        const data = await response.json();
        setStatus(response.ok && data.status === 'ok' ? 'Available' : 'Unavailable');
      } catch {
        setStatus('Unavailable');
      } finally {
        clearTimeout(timeout);
      }
    }

    void checkHealth();
    return () => { controller.abort(); clearTimeout(timeout); };
  }, []);

  return (
    <main>
      <p className="eyebrow">EV FLEET</p>
      <h1>Simulation probe</h1>
      <p className="description">Configure and observe your EV simulation.</p>
      <p className="health" role="status">Console status: <strong>{status}</strong></p>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
