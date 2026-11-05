import { useCallback, useEffect, useState } from 'react';
import { ApiError, getIncident } from './api';

export type IncidentState = {
  status: 'idle' | 'loading' | 'ready' | 'notfound' | 'error';
  incident: Record<string, unknown> | null;
  error: string | null;
};

/**
 * Loads one persisted incident straight from the backend by its stable ID.
 * Read-only: it never reruns diagnosis and never depends on the shared
 * incident list, so direct links and browser refresh work for any record.
 */
export function useIncident(incidentId: string | null | undefined) {
  const [state, setState] = useState<IncidentState>({ status: 'idle', incident: null, error: null });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let active = true;
    if (!incidentId) {
      setState({ status: 'idle', incident: null, error: null });
      return () => { active = false; };
    }
    setState(previous => ({
      status: 'loading',
      incident: previous.incident?.incident_id === incidentId ? previous.incident : null,
      error: null,
    }));
    getIncident(incidentId).then(incident => {
      if (active) setState({ status: 'ready', incident, error: null });
    }).catch(reason => {
      if (!active) return;
      if (reason instanceof ApiError && reason.status === 404) {
        setState({ status: 'notfound', incident: null, error: null });
      } else {
        setState({
          status: 'error', incident: null,
          error: reason instanceof Error ? reason.message : 'The incident request failed.',
        });
      }
    });
    return () => { active = false; };
  }, [incidentId, nonce]);

  const reload = useCallback(() => setNonce(value => value + 1), []);
  return { ...state, reload };
}