import { Component } from 'react';
import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './styles.css';

/** A render error in one page must never leave a blank screen. */
class Boundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (!this.state.error) return this.props.children;
    return <div role="alert" style={{ minHeight: '100vh', display: 'grid', placeContent: 'center', gap: 12, padding: 24, textAlign: 'center', fontFamily: 'system-ui, sans-serif' }}>
      <h2 style={{ margin: 0 }}>Kybernaut hit an unexpected error</h2>
      <p style={{ margin: 0, color: '#555', maxWidth: 520 }}>{this.state.error.message}</p>
      <button onClick={() => window.location.assign('/console')} style={{ justifySelf: 'center', padding: '8px 18px', borderRadius: 8, border: '1px solid #0E8C8F', background: '#0E8C8F', color: '#fff', cursor: 'pointer' }}>Reload the console</button>
    </div>;
  }
}

createRoot(document.getElementById('root')!).render(<Boundary><BrowserRouter><App /></BrowserRouter></Boundary>);