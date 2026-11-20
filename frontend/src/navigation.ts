export type Route =
  | { view: 'sources' | 'add' }
  | { view: 'exports'; sourceId?: string }
  | { view: 'review' | 'live'; jobId: string; candidateId?: string };

export function routeHash(route: Route): string {
  const parts: string[] = [route.view];
  if (route.view === 'exports' && route.sourceId) parts.push(encodeURIComponent(route.sourceId));
  if ('jobId' in route) {
    parts.push(encodeURIComponent(route.jobId));
    if (route.view === 'review' && route.candidateId) parts.push(encodeURIComponent(route.candidateId));
  }
  return `#/${parts.join('/')}`;
}

export function parseRoute(hash: string): Route {
  const [view, job, candidate, extra] = hash.replace(/^#\/?/, '').split('/');
  if (view === 'add' || view === 'add-video') return { view: 'add' };
  if (view === 'exports' || view === 'exported-videos') {
    try { if (job && !candidate && !extra) return { view: 'exports', sourceId: decodeURIComponent(job) }; }
    catch { /* Invalid external URL: show all exports. */ }
    return { view: 'exports' };
  }
  if ((view === 'review' || view === 'live') && job && !extra && !(view === 'live' && candidate)) {
    try {
      return { view, jobId: decodeURIComponent(job), candidateId: candidate ? decodeURIComponent(candidate) : undefined };
    } catch { /* Invalid external URL: return to the source list. */ }
  }
  return { view: 'sources' };
}
