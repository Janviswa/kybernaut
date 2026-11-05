import { useEffect, useState } from 'react';
import { Badge } from './ui';
import { getLocalServiceSimulationStatus, runLocalServiceSimulationNow } from './api';
import { refresh, useStore } from './store';

export function useSimulatorStatus() {
  const [status, setStatus] = useState<{
    state: 'loading' | 'ready' | 'error';
    enabled: boolean;
    configured: boolean;
    missing_configuration: string[];
    error: string;
  }>({ state: 'loading', enabled: false, configured: false, missing_configuration: [], error: '' });
  useEffect(() => {
    let active = true;
    const load = () => getLocalServiceSimulationStatus().then(result => {
      if (active) setStatus({
        state: 'ready',
        enabled: result.enabled === true,
        configured: result.configured === true,
        missing_configuration: Array.isArray(result.missing_configuration)
          ? result.missing_configuration.filter((item: unknown): item is string => typeof item === 'string')
          : [],
        error: typeof result.last_error === 'string' ? result.last_error : '',
      });
    }).catch(reason => {
      if (active) setStatus({
        state: 'error',
        enabled: false,
        configured: false,
        missing_configuration: [],
        error: reason instanceof Error ? reason.message : 'Simulator status request failed.',
      });
    });
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return status;
}

/** Four-service simulator controls (lives in Settings & Integrations). */
export function SimulatorPanel() {
  const state = useStore();
  const simulator = useSimulatorStatus();
  const [starting, setStarting] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const allowed = state.permissions.includes('remediation');

  const start = async () => {
    if (starting) return;
    setStarting(true);
    setMessage('');
    setError('');
    try {
      const result = await runLocalServiceSimulationNow();
      const count = typeof result.incidents_created === 'number' ? result.incidents_created : 0;
      setMessage(`Created ${count} incidents (one critical and one high) on two different services.`);
      await refresh('incidents');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not start the simulation.');
    } finally {
      setStarting(false);
    }
  };

  return <div role={simulator.error ? 'status' : undefined}>
    <div className="row between gap wrap">
      <div className="row gap wrap">
        <Badge v={simulator.enabled ? 'healthy' : 'blocked'}>
          {simulator.state === 'loading' ? 'checking' : simulator.state === 'error'
            ? 'status unavailable' : simulator.enabled ? 'enabled' : 'disabled'}
        </Badge>
        {simulator.enabled && <button className="btn sm primary"
          disabled={starting || !allowed}
          title={!allowed ? 'Requires the remediation permission' : ''}
          onClick={() => void start()}>
          {starting ? 'Starting…' : 'Simulate incidents now'}
        </button>}
      </div>
    </div>
    {simulator.enabled
      ? <p className="muted">Every 10 minutes two new incidents are created (one critical and one high) on different services and failure types, replacing the previous pair. Use the button to create a pair immediately. Existing Kubernetes lab incidents remain in history with their original source.</p>
      : simulator.configured
        ? <p className="muted">Simulator configuration is ready but the API scheduler is not running. Restart the API. Existing Kubernetes lab incidents remain unchanged.</p>
        : <p className="muted">{simulator.state === 'error' ? simulator.error : `To enable rotating two-incident cycles, configure ${simulator.missing_configuration.length ? simulator.missing_configuration.join('; ') : 'the local-demo environment and shared simulation token'}, then restart the API and target-service containers. Existing Kubernetes lab incidents are retained as history.`}</p>}
    {simulator.error && simulator.state === 'ready' &&
      <p className="err" role="alert">Last simulator cycle failed: {simulator.error}</p>}
    {message && <p className="tiny muted" role="status">{message}</p>}
    {error && <p className="err" role="alert">{error}</p>}
  </div>;
}