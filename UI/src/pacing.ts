/**
 * Pacing for the Console's step-by-step progress. Real backend calls run while the
 * steps reveal, but each step stays visible long enough to read. Pauses collapse to
 * zero under test so unit tests stay fast and deterministic.
 */
const SCALE = import.meta.env.MODE === 'test' ? 0 : 1;

export const pause = (ms: number): Promise<void> =>
  SCALE === 0 ? Promise.resolve() : new Promise(resolve => window.setTimeout(resolve, ms * SCALE));

/** Resolves with the promise's value, but never sooner than `ms` (so a step is visible). */
export const atLeast = async <T,>(promise: Promise<T>, ms: number): Promise<T> => {
  const [value] = await Promise.all([promise, pause(ms)]);
  return value;
};