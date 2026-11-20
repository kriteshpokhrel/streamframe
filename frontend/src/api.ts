import type { CandidateChanges, ExportedVideo, ExportFile, Job, JobExport } from './types';

export const jobPath = (id: string) => `/api/jobs/${encodeURIComponent(id)}`;

export function streamerSource(value: string, platform: 'twitch' | 'youtube',
  mode: 'auto' | 'live' | 'latest', duration: number) {
  const identifier = value.trim();
  if (!identifier.includes('://')) {
    return { type: 'streamer' as const, platform, identifier, mode, duration };
  }
  const hostname = new URL(identifier).hostname.toLowerCase();
  const urlPlatform = hostname === 'twitch.tv' || hostname.endsWith('.twitch.tv') ? 'twitch'
    : hostname === 'youtube.com' || hostname.endsWith('.youtube.com') || hostname === 'youtu.be' ? 'youtube'
    : null;
  if (urlPlatform && urlPlatform !== platform) {
    throw new Error('URL does not match the selected platform.');
  }
  return { type: 'url' as const, url: identifier, mode, duration };
}

export function describe(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  return JSON.stringify(value, null, 2);
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const body = JSON.parse(text);
      detail = describe(body.detail ?? body.error ?? body.message ?? body);
    } catch { /* Non-JSON errors are displayed as text, never HTML. */ }
    throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail.slice(0, 1200)}` : ''}`);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function listJobs(signal?: AbortSignal): Promise<Job[]> {
  const data = await request<Job[] | { jobs: Job[] }>('/api/jobs', { signal });
  const jobs = Array.isArray(data) ? data : data.jobs;
  if (!Array.isArray(jobs)) throw new Error('Invalid jobs response: expected an array or { jobs: [] }.');
  return jobs;
}

export const getJob = (id: string, signal?: AbortSignal) =>
  request<Job>(jobPath(id), { signal });

export async function getDiagnostics(id: string): Promise<string> {
  const result = await request<{ text: string }>(`${jobPath(id)}/diagnostics`);
  if (typeof result?.text !== 'string') throw new Error('Invalid diagnostics response: expected { text: string }.');
  return result.text;
}

export function isRollingJob(job: Job): boolean {
  return !!job.live_session || (typeof job.source !== 'string' &&
    job.source.type !== 'file' && !!job.source.rolling);
}

export function jobControls(job: Job) {
  return {
    cancel: !job.deletion_pending && ['queued', 'running', 'exporting'].includes(job.status),
    resume: !job.deletion_pending && !isRollingJob(job) &&
      ['failed', 'cancelled', 'interrupted'].includes(job.status),
  };
}

export function failedSourceStatus(job: Job): string {
  const error = describe(job.error);
  if (/post[_ -]live|post[_ -]processing|still processing/i.test(error)) {
    return 'YouTube post-live processing unavailable — see job error';
  }
  if (/unsupported|not supported|not implemented|bounded.{0,100}(?:unavailable|not available)/i.test(error)) {
    return 'Source acquisition unsupported — see job error';
  }
  if (/source resolution failed|SourceResolutionError/i.test(error)) return 'Resolution failed — source not confirmed';
  return 'Source metadata unavailable — job failed (see error)';
}

export function exportedVideos(jobs: Job[]): ExportedVideo[] {
  return jobs.flatMap((job) => {
    const records: ExportedVideo[] = [];
    const add = (value: unknown, candidateId?: string) => {
      if (Array.isArray(value)) { value.forEach((item) => add(item, candidateId)); return; }
      const record = value && typeof value === 'object' ? value as Partial<JobExport> : undefined;
      const owner = typeof record?.candidate_id === 'string' ? record.candidate_id : candidateId;
      exportFiles(value, job.id).forEach((file) => records.push({
        ...file, jobId: job.id, candidateId: owner, provenance: record as JobExport | undefined,
      }));
    };
    add(job.exports ?? job.outputs);
    job.candidates?.forEach((candidate) => add(candidate.exports ?? candidate.outputs, candidate.id));
    return records.filter((file, index) => records.findIndex((other) => other.url === file.url) === index);
  });
}

export const saveCandidate = (job: string, candidate: string, changes: CandidateChanges) =>
  request<unknown>(`${jobPath(job)}/candidates/${encodeURIComponent(candidate)}`, {
    method: 'PATCH', body: JSON.stringify(changes),
  });

// Only serve downloads from this backend; never follow arbitrary response URLs.
function downloadUrl(value: string): string | null {
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith('/api/')
      ? `${url.pathname}${url.search}` : null;
  } catch { return null; }
}

export function exportFiles(value: unknown, job: string): ExportFile[] {
  if (value == null) return [];
  if (Array.isArray(value)) return value.flatMap((item) => exportFiles(item, job));
  if (typeof value === 'string') {
    if (/^(https?:|\/)/.test(value)) {
      const url = downloadUrl(value);
      return url ? [{ name: decodeName(url), url }] : [];
    }
    // Filenames, not filesystem paths, are accepted by the download endpoint.
    const name = value.split(/[/\\]/).pop() || value;
    return [{ name, url: `${jobPath(job)}/exports/${encodeURIComponent(name)}` }];
  }
  if (typeof value !== 'object') return [];
  const item = value as Record<string, unknown>;
  for (const key of ['files', 'exports', 'outputs']) {
    if (item[key]) return exportFiles(item[key], job);
  }
  if (typeof item.filename === 'string') return exportFiles(item.filename, job);
  for (const key of ['download_url', 'url']) {
    if (typeof item[key] === 'string') return exportFiles(item[key], job);
  }
  return [];
}

export function candidateJobExports(value: unknown, job: string, candidate: string): ExportFile[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item: unknown) =>
    item !== null && typeof item === 'object' &&
    (item as Record<string, unknown>).candidate_id === candidate
      ? exportFiles(item, job) : []);
}

function exportResponseFiles(value: unknown, job: string, candidate: string): ExportFile[] {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const item = value as Record<string, unknown>;
    if ('exports' in item || 'outputs' in item) {
      return candidateJobExports(item.exports ?? item.outputs, job, candidate);
    }
  }
  return exportFiles(value, job);
}

function decodeName(url: string): string {
  try { return decodeURIComponent(url.split('?')[0].split('/').pop() || 'Export'); }
  catch { return 'Export'; }
}

export async function startExport(job: string, candidate: string): Promise<{ files: ExportFile[]; pending: boolean }> {
  const response = await fetch(`${jobPath(job)}/candidates/${encodeURIComponent(candidate)}/export`, {
    method: 'POST',
  });
  if (!response.ok) {
    throw new Error(`Export failed (${response.status}): ${(await response.text()).slice(0, 1200)}`);
  }
  const pending = response.status === 202 || response.status === 204;
  if (response.status === 204) return { files: [], pending };
  const type = response.headers.get('content-type') || '';
  if (type.includes('json')) return { files: exportResponseFiles(await response.json(), job, candidate), pending };
  if (type.startsWith('video/') || type === 'application/octet-stream' || type.includes('zip')) {
    return { files: [{ name: `streamframe-${candidate}.${type.includes('zip') ? 'zip' : 'mp4'}`,
      url: URL.createObjectURL(await response.blob()) }], pending: false };
  }
  const text = (await response.text()).trim();
  if (!text) return { files: [], pending };
  try { return { files: exportResponseFiles(JSON.parse(text), job, candidate), pending }; }
  catch { throw new Error('Export completed but returned an unsupported response. Expected JSON export filenames or video bytes.'); }
}
