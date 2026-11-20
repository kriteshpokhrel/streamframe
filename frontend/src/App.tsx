import { useCallback, useEffect, useRef, useState } from 'react';
import { candidateJobExports, describe, exportedVideos, exportFiles, failedSourceStatus, getDiagnostics, getJob, isRollingJob, jobControls, jobPath, listJobs, request, saveCandidate, startExport, streamerSource } from './api';
import type { Candidate, CandidateChanges, ExportedVideo, ExportFile, Job, Rect, Word } from './types';

const editable = ['start', 'end', 'words', 'face_crop', 'content_crop'] as const;
const time = (value: number) => {
  if (!Number.isFinite(value)) return '—';
  return `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, '0')}`;
};
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const progress = (value: number) => Math.round(Math.max(0, Math.min(100,
  Number.isFinite(value) ? (value <= 1 ? value * 100 : value) : 0)));
const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
const captionText = (word: Word) => word.correction ?? word.text;
const retainedSource = (candidate: Candidate) => candidate.editable === true &&
  [candidate.media_url, candidate.source_media_url].some((url) => typeof url === 'string' && url.trim().length > 0);
const jobWorking = (job: Job) => !job.deletion_pending && (
  ['queued', 'running', 'exporting', 'cancelling'].includes(job.status) ||
  (job.candidates ?? []).some((candidate) => ['queued', 'running', 'exporting'].includes(candidate.export_status ?? '')));
function linkPlatform(value: string): 'twitch' | 'youtube' | null {
  try {
    const url = new URL(value.trim());
    if (!['https:', 'http:'].includes(url.protocol)) return null;
    const host = url.hostname.toLowerCase();
    if (host === 'twitch.tv' || host.endsWith('.twitch.tv')) return 'twitch';
    if (host === 'youtu.be' || host === 'youtube.com' || host.endsWith('.youtube.com')) return 'youtube';
  } catch { /* Names and handles require an explicit platform selection. */ }
  return null;
}
const sourceText = (source: Job['source']) => typeof source === 'string'
  ? source : source?.type === 'file' ? source.path : source?.type === 'streamer'
    ? `${source.platform}: ${source.identifier}` : source?.url;

function changesBetween(base: Candidate, draft: Candidate): CandidateChanges {
  const result: Record<string, unknown> = {};
  for (const field of editable) {
    if (JSON.stringify(base[field]) !== JSON.stringify(draft[field])) result[field] = draft[field];
  }
  return result as CandidateChanges;
}

function validate(candidate: Candidate): string | null {
  if (!Number.isFinite(candidate.start) || !Number.isFinite(candidate.end) ||
      candidate.start < 0 || candidate.end <= candidate.start) {
    return 'Clip start and end must be valid seconds, with 0 ≤ start < end.';
  }
  for (const [index, word] of candidate.words.entries()) {
    if (!captionText(word).trim()) return `Word ${index + 1} needs caption text.`;
    if (!Number.isFinite(word.start) || !Number.isFinite(word.end) ||
        word.start < 0 || word.end <= word.start) {
      return `Word ${index + 1} must have 0 ≤ start < end.`;
    }
    if (index && word.start < candidate.words[index - 1].start) {
      return `Word ${index + 1} starts before the previous word. Keep words in chronological order.`;
    }
  }
  for (const field of ['face_crop', 'content_crop'] as const) {
    const rect = candidate[field];
    if (!rect) continue;
    if (Object.values(rect).some((n) => !Number.isFinite(n)) ||
        rect.x < 0 || rect.y < 0 || rect.width <= 0 || rect.height <= 0 ||
        rect.x + rect.width > 1.000001 || rect.y + rect.height > 1.000001) {
      return `${field === 'face_crop' ? 'Face' : 'Content'} crop must fit inside the image (0–1 coordinates, positive width and height).`;
    }
  }
  return null;
}

export default function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [candidateId, setCandidateId] = useState<string | null>(null);
  const [source, setSource] = useState('');
  const [sourceType, setSourceType] = useState<'streamer' | 'url' | 'file'>('streamer');
  const [platform, setPlatform] = useState<'twitch' | 'youtube'>('twitch');
  const [sourceMode, setSourceMode] = useState<'auto' | 'live' | 'latest'>('auto');
  const [captureDuration, setCaptureDuration] = useState('60');
  const [liveOutput, setLiveOutput] = useState<'sample' | 'rolling'>('sample');
  const [sessionDuration, setSessionDuration] = useState('600');
  const [windowDuration, setWindowDuration] = useState('60');
  const [rangeStart, setRangeStart] = useState('');
  const [rangeEnd, setRangeEnd] = useState('');
  const [online, setOnline] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [editorBusy, setEditorBusy] = useState(false);
  const [sessionExports, setSessionExports] = useState<ExportedVideo[]>([]);
  const [openEditor, setOpenEditor] = useState(false);
  const pendingEditor = useRef<{ jobId: string; candidateId?: string } | null>(null);
  const exportUrls = useRef<string[]>([]);
  useEffect(() => () => { exportUrls.current.forEach((url) => URL.revokeObjectURL(url)); }, []);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const id = selectedRef.current;
    const [nextJobs, nextJob] = await Promise.all([
      listJobs(signal), id ? getJob(id, signal) : Promise.resolve(null),
    ]);
    if (signal?.aborted) return;
    setJobs(nextJobs);
    if (selectedRef.current === id && nextJob) setJob(nextJob);
    if (!id && nextJobs.length) setSelectedId(nextJobs[0].id);
    setPollError('');
    setLoading(false);
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const tick = async () => {
      try { await refresh(controller.signal); }
      catch (err) {
        if (!isAbort(err) && !stopped) {
          setPollError(message(err));
          setLoading(false);
        }
      }
      if (!stopped) timer = setTimeout(tick, 2500);
    };
    void tick();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const check = async () => {
      try {
        const response = await fetch('/api/health', { signal: controller.signal });
        if (!stopped) setOnline(response.ok);
      } catch (err) { if (!isAbort(err) && !stopped) setOnline(false); }
      if (!stopped) timer = setTimeout(check, 15000);
    };
    void check();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setJob(null);
    setCandidateId(pendingEditor.current?.jobId === selectedId ? pendingEditor.current.candidateId ?? null : null);
    setError('');
    void getJob(selectedId, controller.signal).then((next) => {
      if (!controller.signal.aborted) setJob(next);
    }).catch((err) => { if (!isAbort(err)) setError(message(err)); });
    return () => controller.abort();
  }, [selectedId]);

  const candidates = job?.candidates ?? [];
  const rollingJob = !!job && isRollingJob(job);
  const selectedCandidate = candidates.find((candidate) => candidate.id === candidateId) ?? candidates[0];
  const canLeave = () => !editorBusy && (!dirty || window.confirm('Discard unsaved candidate changes?'));
  const libraryJobs = jobs.map((item) => item.id === job?.id ? job : item);
  const gallery = [...exportedVideos(libraryJobs), ...sessionExports.filter((file) =>
    libraryJobs.some((item) => item.id === file.jobId))].filter((file, index, all) =>
    all.findIndex((other) => other.jobId === file.jobId && other.url === file.url) === index);
  useEffect(() => {
    if (!openEditor || !job || job.id !== selectedId || !selectedCandidate) return;
    const editor = document.querySelector<HTMLElement>('[aria-label="Candidate editor"]');
    if (editor) {
      editor.scrollIntoView({ block: 'start' }); editor.focus();
      pendingEditor.current = null; setOpenEditor(false);
    }
  }, [openEditor, job, selectedId, selectedCandidate]);

  function rememberExports(jobId: string, candidateId: string, files: ExportFile[]) {
    files.forEach((file) => { if (file.url.startsWith('blob:')) exportUrls.current.push(file.url); });
    setSessionExports((old) => [...old, ...files.map((file) => ({ ...file, jobId, candidateId }))]);
  }

  function editExport(file: ExportedVideo) {
    if (!canLeave()) return;
    setDirty(false);
    setSelectedId(file.jobId);
    pendingEditor.current = { jobId: file.jobId, candidateId: file.candidateId };
    setCandidateId(file.candidateId ?? null);
    setOpenEditor(true);
  }

  async function importSource(event: React.FormEvent) {
    event.preventDefault();
    if (!source.trim() || !canLeave()) return;
    setBusy(true); setError('');
    try {
      const local = sourceType === 'file';
      const start = rangeStart === '' ? undefined : Number(rangeStart);
      const end = rangeEnd === '' ? undefined : Number(rangeEnd);
      if (sourceType === 'url' && (start === undefined || end === undefined)) {
        throw new Error('Public video links require both a source range start and end.');
      }
      if ((start !== undefined && (!Number.isFinite(start) || start < 0)) ||
          (end !== undefined && (!Number.isFinite(end) || end <= (start ?? 0) || end - (start ?? 0) > 1800))) {
        throw new Error('Import range requires 0 ≤ start < end and a maximum 1800-second span, in original-file seconds.');
      }
      const duration = Number(captureDuration);
      const rolling = sourceType === 'streamer' && sourceMode === 'live' && liveOutput === 'rolling';
      const session = Number(sessionDuration);
      const window = Number(windowDuration);
      if (rolling && (!Number.isFinite(session) || session < 1 || session > 1800 ||
          !Number.isFinite(window) || window < 1 || window > 60 || window > session)) {
        throw new Error('Rolling sessions require 1–1800 seconds total, 1–60 seconds per window, and a window no longer than the session.');
      }
      if (sourceType === 'streamer' && !rolling && (!Number.isFinite(duration) || duration < 1 || duration > 300)) {
        throw new Error('Streamer capture duration must be between 1 and 300 seconds.');
      }
      let payload;
      if (sourceType === 'streamer') {
        const resolved = streamerSource(source, platform, sourceMode, duration);
        if (rolling) {
          const { duration: _duration, ...identity } = resolved;
          payload = { ...identity, mode: 'live', rolling: true, session_duration: session, window_duration: window };
        } else payload = resolved;
      } else payload = !local
        ? { type: 'url', url: source.trim(), start, end } : { type: 'file', path: source.trim(), start, end };
      const created = await request<Job>('/api/jobs', {
        method: 'POST', body: JSON.stringify({ source: payload }),
      });
      if (!created?.id) throw new Error('The server did not return a job id.');
      setDirty(false);
      setSelectedId(created.id); setSource(''); setRangeStart(''); setRangeEnd('');
      await refresh();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }

  async function controlJob(action: 'cancel' | 'resume') {
    if (!job || !jobControls(job)[action]) return;
    setBusy(true); setError('');
    try {
      await request(`${jobPath(job.id)}/${action}`, { method: 'POST' });
      await refresh();
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }

  async function deleteJob(item: Job) {
    if (busy || editorBusy || jobWorking(item)) return;
    if (!window.confirm(`Permanently delete "${sourceText(item.source) || item.id}" from the library?\n\nThis removes the StreamFrame job, retained windows, and all exports. Your original file is not deleted.${dirty && selectedId === item.id ? '\n\nUnsaved candidate edits will be discarded.' : ''}\n\nThis cannot be undone.`)) return;
    setBusy(true); setError('');
    try {
      await request(jobPath(item.id), { method: 'DELETE' });
      const remaining = jobs.filter((other) => other.id !== item.id);
      setJobs(remaining);
      if (selectedId === item.id) {
        setDirty(false); setJob(null); setCandidateId(null);
        selectedRef.current = remaining[0]?.id ?? null;
        setSelectedId(remaining[0]?.id ?? null);
      }
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); }
  }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="./" onClick={(event) => {
          event.preventDefault();
          if (canLeave()) window.location.reload();
        }}><span className="brand-icon" aria-hidden="true">▥</span> StreamFrame <span className="tag">LOCAL</span></a>
        <nav className="topnav" aria-label="Workspace sections">
          <a href="#add-video">Add video</a>
          <a href="#source-library">Sources <span>{jobs.length}</span></a>
          <a href="#exported-videos">Exports <span>{gallery.length}</span></a>
        </nav>
        <span className={`connection ${online === false ? 'offline' : ''}`}>
          <span aria-hidden="true">●</span> {online === null ? 'Connecting' : online ? 'Backend connected' : 'Backend unavailable'}
        </span>
      </header>
      <main>
        <section className="intro">
          <div className="intro-copy"><p className="eyebrow">YOUR LOCAL CLIPPING WORKSPACE</p><h1>Find the moment.<br /><span>Make it yours.</span></h1>
            <p className="muted">Turn a supported stream or video into a vertical clip, with your webcam and captions in view.</p>
            <ol className="workflow-steps" aria-label="How it works">
              <li><span>01</span><div><strong>Add a source</strong><small>Choose a channel, video range, or file.</small></div></li>
              <li><span>02</span><div><strong>Review the moments</strong><small>Adjust your framing and captions.</small></div></li>
              <li><span>03</span><div><strong>Export your clip</strong><small>Preview and download when ready.</small></div></li>
            </ol>
          </div>
          <form id="add-video" className="import-card" onSubmit={(event) => void importSource(event)}>
            <div className="import-heading"><div><p className="eyebrow">START HERE</p><h2>Add a video</h2></div><span className="import-badge">Local processing</span></div>
            <label className="field import-type">What would you like to add?<select aria-label="What would you like to add?" value={sourceType} onChange={(event) => {
              setSourceType(event.target.value as typeof sourceType); setSource(''); setRangeStart(''); setRangeEnd('');
            }} disabled={busy || editorBusy}><option value="streamer">A streamer or channel — name or live link</option><option value="url">Part of a finished video — link + times</option><option value="file">A file on this computer — optional</option></select></label>
            <p className="muted small" id="source-help">{sourceType === 'streamer'
              ? `Enter a ${platform === 'twitch' ? 'Twitch name or channel/live link' : 'YouTube @handle or channel/live link'}. Supported public HLS sources are retrieved automatically; availability is not guaranteed.`
              : sourceType === 'url' ? 'Paste a finished-video link, then choose the part you want.'
                : 'Enter the full file path on the computer running StreamFrame. This is not a browser upload.'}</p>
            <label htmlFor="source" className="import-input-label">{sourceType === 'streamer' ? 'Streamer name or link' : sourceType === 'url' ? 'Video link' : 'File path'}</label>
            <div className="input-row"><input id="source" aria-label="Video or streamer" value={source} onChange={(event) => {
              const next = event.target.value;
              setSource(next);
              if (sourceType === 'streamer') {
                const detected = linkPlatform(next);
                if (detected) setPlatform(detected);
              }
            }}
              type={sourceType === 'url' ? 'url' : 'text'} placeholder={sourceType === 'streamer' ? platform === 'twitch' ? 'e.g. streamer_name or twitch.tv/…' : '@handle or https://youtube.com/@…' : sourceType === 'url' ? 'https://www.twitch.tv/videos/…' : '/absolute/path/to/video.mp4'} required aria-describedby="source-help" disabled={busy || editorBusy} />
              <button className="primary" type="submit" disabled={busy || editorBusy || !source.trim()}>{busy ? 'Working…' : 'Import →'}</button></div>
            {sourceType === 'streamer' && <details className="import-settings"><summary>Capture options · {platform === 'twitch' ? 'Twitch' : 'YouTube'} · {sourceMode === 'auto' ? 'Auto' : sourceMode === 'live' ? 'Live only' : 'Latest finished'}{sourceMode === 'live' && liveOutput === 'rolling' ? ' · automatic clips' : ` · ${captureDuration}s sample`}</summary><div className="import-range">
              {linkPlatform(source) ? <p className="muted small">Platform detected from link: {platform === 'twitch' ? 'Twitch' : 'YouTube'}</p> :
                <label className="field">Platform for names and handles<select aria-label="Platform" value={platform} disabled={busy || editorBusy} onChange={(event) => setPlatform(event.target.value as typeof platform)}>
                  <option value="twitch">Twitch</option><option value="youtube">YouTube</option></select></label>}
              <label className="field">Which video?<select aria-label="Which video?" value={sourceMode} disabled={busy || editorBusy} onChange={(event) => setSourceMode(event.target.value as typeof sourceMode)}>
                <option value="auto">Auto: live now, or latest finished video</option><option value="live">Live now only</option><option value="latest">Latest finished video only</option></select></label>
              {sourceMode === 'live' && <label className="field">What should happen?<select aria-label="What should happen?" value={liveOutput} disabled={busy || editorBusy} onChange={(event) => setLiveOutput(event.target.value as typeof liveOutput)}>
                <option value="sample">Take one sample — I’ll edit and export</option>
                <option value="rolling">Make clips automatically for a bounded session</option>
              </select></label>}
              {sourceMode === 'live' && liveOutput === 'rolling' ? <>
                <label className="field">Session duration (s)<input type="number" min="1" max="1800" step="1" required value={sessionDuration} disabled={busy || editorBusy} onChange={(event) => setSessionDuration(event.target.value)} /></label>
                <label className="field">Window duration (s)<input type="number" min="1" max="60" step="1" required value={windowDuration} disabled={busy || editorBusy} onChange={(event) => setWindowDuration(event.target.value)} /></label>
                <p className="muted small">Up to 30 minutes requested; each processing window is at most 60 seconds. The session may stop earlier at 512 MiB transfer or combined disk, 900 cumulative media segments, or 900 windows. Gaps and slow processing can also stop it. No automatic restart or budget reset. Review every clip. Missing face crops skip automatic export; retained candidates can be corrected manually.</p>
              </> : <>
                <label className="field">Capture duration (s)<input type="number" min="1" max="300" step="1" required value={captureDuration} disabled={busy || editorBusy} onChange={(event) => setCaptureDuration(event.target.value)} /></label>
                <p className="muted small">Take up to 5 minutes. You review the results, make corrections, and export. Auto tries live first, then the latest finished video.</p>
              </>}
            </div></details>}
            {sourceType !== 'streamer' && <div className="import-range">
              <label className="field">Source range start (s){sourceType === 'file' ? ', optional' : ', required'}<input type="number" min="0" step="0.01" required={sourceType === 'url'}
                value={rangeStart} onChange={(event) => setRangeStart(event.target.value)} disabled={busy || editorBusy} placeholder="0" /></label>
              <label className="field">Source range end (s){sourceType === 'file' ? ', optional' : ', required'}<input type="number" min="0" step="0.01" required={sourceType === 'url'}
                value={rangeEnd} onChange={(event) => setRangeEnd(event.target.value)} disabled={busy || editorBusy} placeholder="End of file" /></label>
              <p className="muted small">Up to 30 minutes. {sourceType === 'url' ? 'Both times are required.' : 'Leave times empty for the full file; start-only takes up to 30 minutes or end of file.'} Enter seconds from the original video. Editor times start at 0 for the part you import.</p>
            </div>}
            <p className="source-note">Twitch works best. Most YouTube recordings are not supported yet; live sources also require a compatible public stream.</p>
            <details className="import-settings"><summary>Limits & supported formats</summary>
              <p className="muted small">YouTube live requires supported public HLS. Finished YouTube ranges work only for public, clear, muxed MPEG-TS HLS with a complete verified timeline; only the selected segments are fetched. Post-live processing is unavailable. There is no DASH/MP4 fallback, sign-in, or access-control bypass.</p>
              <p className="muted small">Retrieval supports guarded HLS streams, not every provider or video format. Unsupported sources fail instead of silently downloading an entire video.</p>
              <p className="muted small">Live sessions: 512 MiB transferred and 512 MiB combined temporary/output disk. Finished videos: 512 MiB / 1200 segments / 10-minute retrieval deadline, output below 2 GiB. Reaching a limit can stop the job; continuous coverage is not guaranteed.</p>
            </details>
            <p className="muted small permission-note">Use content you have permission to use, and credit its owner when required.</p>
          </form>
        </section>
        {error && <div className="notice error" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
        {pollError && <div className="notice warning" role="status">Unable to refresh: {pollError} <span>Retrying automatically.</span></div>}
        <ExportLibrary jobs={libraryJobs} files={gallery} busy={busy || editorBusy} onEdit={editExport} />
        <div className="workspace">
          <aside id="source-library" className="sidebar panel" aria-label="Imported jobs">
            <div className="section-heading"><h2>Source library</h2><span className="count">{jobs.length}</span></div>
            {loading ? <p className="muted">Loading jobs…</p> : !jobs.length ? <p className="muted empty">Your imported videos will appear here.</p> : null}
            <div className="job-list">{jobs.map((item) => (
              <div className="library-entry" key={item.id}>
              <button className={`job-item ${selectedId === item.id ? 'selected' : ''}`}
                aria-pressed={selectedId === item.id} disabled={busy || editorBusy} onClick={() => {
                  if (item.id !== selectedId && canLeave()) { setDirty(false); setSelectedId(item.id); }
                }}>
                <span className="job-name" title={sourceText(item.source)}>{sourceText(item.source) || item.id}</span>
                <span className="job-meta"><span>{item.status}</span><span>{progress(item.progress)}%</span></span>
                <span className="mini-track"><span style={{ width: `${progress(item.progress)}%` }} /></span>
              </button>
              <button className="library-delete" disabled={busy || editorBusy || jobWorking(item)}
                aria-label={`Delete ${sourceText(item.source) || item.id} from library`}
                title={jobWorking(item) ? 'Stop the job and wait for clip exports to finish before deleting.' : 'Permanently delete this StreamFrame job, not the original file'}
                onClick={() => void deleteJob(item)}>Delete</button>
              {jobWorking(item) && <span className="muted small delete-hint">Stop processing before deleting</span>}
              {!!item.deletion_pending && <span className="muted small delete-hint">Deletion incomplete — retry Delete</span>}
              </div>
            ))}</div>
          </aside>
          <section className="work-area" aria-label="Job workspace">
            {!job ? <div className="panel welcome"><div className="welcome-icon" aria-hidden="true">▶</div><h2>{selectedId ? 'Loading your workspace' : 'Start with a video'}</h2><p className="muted">{selectedId ? 'Fetching job details from the local backend.' : 'Import a source above to begin reviewing its transcript and candidate clips.'}</p></div> : <>
              <section className="panel job-summary">
                <div className="section-heading"><div><p className="eyebrow">CURRENT SOURCE</p><h2 className="source-title" title={sourceText(job.source)}>{sourceText(job.source) || job.id}</h2></div>
                  <span className="status">{job.status}</span></div>
                <div className="progress-row"><progress max="100" value={progress(job.progress)} aria-label="Job progress" /><span>{progress(job.progress)}%</span></div>
                {job.stage ? <p className="muted small">Processing stage: {describe(job.stage)}</p> : null}
                <div className="source-details" aria-label="Source status">
                  <p className="muted small">Source status: {job.resolved_source
                    ? job.resolved_source.is_live ? 'Live' : 'Finished video (VOD)'
                    : typeof job.source !== 'string' && job.source.type === 'file' ? 'Local file'
                      : job.status === 'failed' ? failedSourceStatus(job)
                        : job.status === 'cancelled' ? 'Cancelled before source was confirmed'
                          : job.status === 'interrupted' ? 'Interrupted before source was confirmed'
                            : job.status === 'succeeded' ? 'Completed without source resolution metadata'
                              : job.status === 'cancelling' ? 'Cancelling — source not confirmed'
                                : 'Awaiting backend resolution'}</p>
                  {job.resolved_source && <><p className="muted small">Chosen source: {job.resolved_source.title || job.resolved_source.url}</p>
                    <p className="muted small source-url">{job.resolved_source.url}</p>
                    <p className="muted small">Platform: {job.resolved_source.platform} · Selection: {job.resolved_source.selection}</p></>}
                  {job.capture_started_at && <p className="muted small">Capture started: {job.capture_started_at}</p>}
                  {typeof job.source !== 'string' && job.source.type === 'streamer' &&
                    <p className="muted small">Requested mode: {job.source.mode} · {job.source.rolling ? `Rolling session: ${job.source.session_duration}s / ${job.source.window_duration}s windows` : `Single sample: ${job.source.duration ?? 'backend default'}s`}. Editor timestamps are zero-based relative to captured media.</p>}
                </div>
                {typeof job.source !== 'string' &&
                  (job.source.start !== undefined || job.source.end !== undefined) &&
                  <p className="notice warning">Imported original-file range {job.source.start ?? 0}s–{job.source.end ?? 'end'}s. All editor timestamps are relative to this imported range, not the original file.</p>}
                <div className="summary-footer"><span className="muted small">{candidates.length} candidate{candidates.length === 1 ? '' : 's'} discovered · Job {job.id}</span>
                  <div className="actions">{jobControls(job).cancel && <button disabled={busy || (editorBusy && !rollingJob && job.status !== 'exporting')} onClick={() => void controlJob('cancel')}>{job.live_session ? 'Stop session' : job.stage === 'capture' ? 'Stop capture' : 'Cancel job'}</button>}{jobControls(job).resume && <button disabled={busy || editorBusy || dirty} title={dirty ? 'Save or discard candidate edits before resuming the job.' : undefined} onClick={() => void controlJob('resume')}>Resume job</button>}</div></div>
                {job.error ? <p className="notice error" role="alert">{describe(job.error)}</p> : null}
                {(job.status === 'failed' || candidates.some((candidate) => candidate.export_status === 'failed')) &&
                  <Diagnostics key={job.id} jobId={job.id} />}
                {job.warnings ? <p className="notice warning" role="status">{describe(job.warnings)}</p> : null}
              </section>
              {(job.live_session || (typeof job.source !== 'string' && job.source.type !== 'file' && job.source.rolling)) &&
                <LiveSession key={job.id} job={job} onSaved={refresh} />}
              <section className="candidate-section">
                <div className="section-heading"><h2>Candidate moments</h2><span className="muted small">{rollingJob ? 'Select retained source to edit' : 'Select a moment to edit'}</span></div>
                {rollingJob && <p className="notice warning">Rolling corrections are available only for candidates whose source is retained and marked editable by the backend. Retention uses the session disk budget. Changes and re-exports affect the selected clip, not other clips or capture. Session crop controls separately affect future windows.</p>}
                {!candidates.length ? <div className="panel empty"><p>No candidates yet.</p><p className="muted">Results appear here as the backend processes your source. Check job status and errors above.</p></div> :
                  <div className="candidate-list" aria-label="Candidates">{candidates.map((candidate, index) => (
                    <button className={`candidate-card ${selectedCandidate?.id === candidate.id ? 'selected' : ''}`} key={candidate.id}
                      disabled={busy || editorBusy} aria-pressed={selectedCandidate?.id === candidate.id} onClick={() => {
                        if (selectedCandidate?.id !== candidate.id && canLeave()) { setDirty(false); setCandidateId(candidate.id); }
                      }}>
                      <span className="candidate-top"><span className="eyebrow">MOMENT {String(index + 1).padStart(2, '0')}</span><span className="score">Score {Number.isFinite(candidate.score) ? candidate.score.toFixed(2) : '—'}</span></span>
                      <strong>{time(candidate.start)} <span className="muted">→</span> {time(candidate.end)}</strong>
                      <span className="candidate-caption">{candidate.words?.map(captionText).join(' ') || 'Review candidate evidence and captions'}</span>
                      {rollingJob && <span className="candidate-caption">Evidence: {describe(candidate.evidence) || 'No evidence supplied.'}</span>}
                      {rollingJob && <span className="muted small">{retainedSource(candidate)
                        ? 'Source retained · correction available' : 'Source unavailable · evidence only'}</span>}
                      <span className="muted small">{(candidate.end - candidate.start).toFixed(1)} seconds</span>
                    </button>
                  ))}</div>}
              </section>
              {selectedCandidate && (!rollingJob || retainedSource(selectedCandidate)) && <CandidateEditor key={`${job.id}/${selectedCandidate.id}`} job={job}
                candidate={selectedCandidate} rolling={rollingJob} onDirty={setDirty} onBusy={setEditorBusy} onExport={rememberExports} onSaved={async () => { await refresh(); }} />}
              {selectedCandidate && rollingJob && !retainedSource(selectedCandidate) &&
                <p className="notice warning" role="status">Source unavailable for this candidate: editing, preview, and re-export are not offered. Retained completed clips remain downloadable. Availability is determined by the backend, not assumed from an export.</p>}
            </>}
          </section>
        </div>
      </main>
      <footer>StreamFrame <span>Local workspace · Your backend handles processing and export</span></footer>
    </div>
  );
}

function Diagnostics({ jobId }: { jobId: string }) {
  const [text, setText] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const fallback = useRef<HTMLTextAreaElement>(null);
  async function copy() {
    setBusy(true); setError(''); setStatus('Fetching diagnostics…');
    try {
      const value = await getDiagnostics(jobId);
      setText(value);
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(value);
        setStatus(`Diagnostics copied for job ${jobId}. Review before sharing.`);
      } catch {
        setStatus('Clipboard unavailable. Select and copy the diagnostics below. Review before sharing.');
        requestAnimationFrame(() => { fallback.current?.focus(); fallback.current?.select(); });
      }
    } catch (error) { setError(message(error)); setStatus(''); }
    finally { setBusy(false); }
  }
  return <section className="diagnostics" aria-label={`Diagnostics for job ${jobId}`}>
    <button disabled={busy} onClick={() => void copy()}>{busy ? 'Fetching diagnostics…' : 'Copy diagnostics'}</button>
    {status && <p className="muted small" role="status">{status}</p>}
    {error && <p className="notice error" role="alert">Could not fetch diagnostics: {error}. Try again.</p>}
    {text && <label className="field">Diagnostics text — job {jobId}
      <textarea ref={fallback} readOnly rows={7} value={text} onFocus={(event) => event.target.select()} />
    </label>}
  </section>;
}

function ExportLibrary({ jobs, files, busy, onEdit }: {
  jobs: Job[]; files: ExportedVideo[]; busy: boolean; onEdit: (file: ExportedVideo) => void;
}) {
  const [filter, setFilter] = useState('all');
  useEffect(() => {
    if (filter !== 'all' && !jobs.some((job) => job.id === filter)) setFilter('all');
  }, [jobs, filter]);
  const filtered = files.filter((file) => filter === 'all' || file.jobId === filter);
  const working = jobs.flatMap<{ job: Job; candidate?: Candidate }>((job) => {
    const active = job.candidates.filter((candidate) =>
      ['queued', 'running', 'exporting'].includes(candidate.export_status ?? ''));
    if (active.length) return active.map((candidate) => ({ job, candidate }));
    return job.status === 'exporting' ? [{ job, candidate: undefined }] : [];
  });
  return <section id="exported-videos" className="export-library panel" aria-label="Exported videos">
    <div className="section-heading"><div><p className="eyebrow">FINISHED OUTPUTS</p><h2>Exported videos <span className="count">{files.length}</span></h2></div>
      <label className="field">Show exports from<select aria-label="Show exports from" value={filter} onChange={(event) => setFilter(event.target.value)}>
        <option value="all">All sources</option>{jobs.map((job) =>
          <option value={job.id} key={job.id}>{sourceText(job.source) || job.id} · {job.id}</option>)}
      </select></label></div>
    <p className="muted small">Rendered files, separate from editable source videos. Preview the actual output before sharing. Re-exporting uses the current saved source edits, not a reconstruction of an older render.</p>
    <div role="status" aria-live="polite">
      {working.filter(({ job }) => filter === 'all' || filter === job.id).map(({ job, candidate }) =>
        <p className="export-activity" key={`${job.id}/${candidate?.id ?? ''}`}>
          <progress aria-label={`Export in progress for ${job.id}${candidate ? ` / ${candidate.id}` : ''}`} />
          Export {candidate?.export_status ?? 'rendering'} · Job {job.id}{candidate ? ` · Candidate ${candidate.id}` : ''}. Earlier outputs remain available.
        </p>)}
    </div>
    {jobs.filter((job) => filter === 'all' || filter === job.id).flatMap((job) =>
      job.candidates.filter((candidate) => candidate.export_status === 'failed').map((candidate) =>
        <p className="notice error" key={`${job.id}/${candidate.id}`}>Export failed · Job {job.id} · Candidate {candidate.id}: {candidate.export_error || 'No details supplied.'} Previous exports are unchanged. Open the source job for diagnostics and retry.</p>))}
    {!filtered.length ? <p className="empty muted">No exported videos {filter === 'all' ? 'yet' : 'for this source'}. Choose a candidate, save edits, then Export clip. Rolling sessions publish completed clips here automatically.</p> :
      <ul className="export-grid">{filtered.map((file) => {
        const job = jobs.find((item) => item.id === file.jobId);
        const candidate = job?.candidates.find((item) => item.id === file.candidateId);
        const canEdit = job && candidate && !job.deletion_pending && (!isRollingJob(job) || retainedSource(candidate));
        const record = file.provenance;
        const video = /\.(mp4|webm|m4v|mov)$/i.test(file.name);
        return <li className="export-card" key={`${file.jobId}/${file.url}`}>
          <h3>{file.name}</h3><span className="export-ready">Ready to preview & download</span>
          <p className="muted small">{job && sourceText(job.source)}</p>
          <dl className="export-provenance">
            <div><dt>Source job</dt><dd>{file.jobId}</dd></div>
            <div><dt>Candidate</dt><dd>{file.candidateId ?? 'Not supplied by backend'}</dd></div>
            {record?.revision !== undefined && <div><dt>Export revision</dt><dd>{record.revision}</dd></div>}
            {record?.window_index !== undefined && <div><dt>Source window</dt><dd>{record.window_index}</dd></div>}
            {record?.global_start !== undefined && record.global_end !== undefined &&
              <div><dt>Session clip range</dt><dd>{time(record.global_start)}–{time(record.global_end)}</dd></div>}
            {record?.start !== undefined && record.end !== undefined &&
              <div><dt>Export source range</dt><dd>{time(record.start)}–{time(record.end)}</dd></div>}
            {record?.created_at && <div><dt>Exported</dt><dd>{record.created_at}</dd></div>}
          </dl>
          {!record?.revision && record?.revision !== 0 && <p className="muted small">Export revision not supplied; current editor settings may differ.</p>}
          {video && <details className="render-preview"><summary>Preview exported video</summary>
            <video controls playsInline preload="none" src={file.url} aria-label={`Exported video ${file.name}`} onError={(event) => {
              const notice = event.currentTarget.parentElement?.querySelector<HTMLElement>('[role="status"]');
              if (notice) notice.hidden = false;
            }} /><p className="muted small" role="status" hidden>Preview unavailable or format unsupported. Try downloading the file.</p>
          </details>}
          <div className="actions"><a className="button" href={file.url} download={file.name}>Download video ↓</a>
            {canEdit && <button disabled={busy} onClick={() => onEdit(file)}>Open edit source / re-export</button>}</div>
          <p className="muted small">{canEdit ? 'Opens this candidate’s current edits. Save and export again to create a new render; existing files are unchanged.'
            : 'Edit source unavailable. This render remains downloadable, but cannot restore its original editable media.'}</p>
        </li>;
      })}</ul>}
  </section>;
}

function LiveSession({ job, onSaved }: { job: Job; onSaved: () => Promise<void> }) {
  const session = job.live_session;
  const outputs = job.exports ?? [];
  const [faceDraft, setFaceDraft] = useState<Rect | null>(session?.face_crop ?? null);
  const [cropTouched, setCropTouched] = useState(false);
  const [cropSaving, setCropSaving] = useState(false);
  const [cropError, setCropError] = useState('');
  const [cropNotice, setCropNotice] = useState('');
  const invalidCrop = faceDraft && (Object.values(faceDraft).some((value) => !Number.isFinite(value)) ||
    faceDraft.x < 0 || faceDraft.y < 0 || faceDraft.width <= 0 || faceDraft.height <= 0 ||
    faceDraft.x + faceDraft.width > 1.000001 || faceDraft.y + faceDraft.height > 1.000001);
  const active = ['queued', 'running'].includes(job.status);
  useEffect(() => {
    if (!cropTouched && !cropSaving) setFaceDraft(session?.face_crop ?? null);
  }, [session?.face_crop, cropTouched, cropSaving]);
  async function applyFaceCrop() {
    if (!active || invalidCrop || cropSaving) return;
    setCropSaving(true); setCropError(''); setCropNotice('');
    try {
      const saved = await request<Job>(`${jobPath(job.id)}/live-crop`, {
        method: 'PATCH', body: JSON.stringify({ face_crop: faceDraft }),
      });
      setFaceDraft(saved.live_session?.face_crop ?? null);
      await onSaved();
      setCropTouched(false);
      setCropNotice('Face crop saved for windows that enter processing next. Existing clips and in-progress windows are unchanged.');
    } catch (error) { setCropError(message(error)); }
    finally { setCropSaving(false); }
  }
  return <section className="panel rolling-session" aria-label="Rolling live session">
    <div className="section-heading"><h2>Rolling session · automated clips</h2><span className="status">{session?.phase ?? 'Waiting for session status'}</span></div>
    <dl className="session-metrics">
      <div><dt>Windows seen</dt><dd>{session?.windows_seen ?? '—'}</dd></div>
      <div><dt>Windows processed</dt><dd>{session?.windows_processed ?? '—'}</dd></div>
      <div><dt>Session media time</dt><dd>{session ? time(session.global_time) : '—'}</dd></div>
    </dl>
    <p className="muted small">Automated clips are distinct from single samples awaiting manual export. Completed output links remain available after stop. Session-global times are relative to this session; window times identify captured ranges, not wall-clock coverage.</p>
    <p className="notice warning">Finite processing only: up to 30 minutes requested, not guaranteed continuous coverage. Backlog, missing source windows, 512 MiB transfer / 512 MiB combined temporary-output disk, or the 900 cumulative segment / 900 window limits may stop the session earlier. No automatic restart or budget reset. No detected or manual face crop means automated export is skipped (no_face), not a full-frame fallback. To recover a skipped window, select its retained candidate, set a face crop, then export manually. Session crop changes affect future windows only and do not automatically export earlier skipped windows.</p>
    {session?.warnings && describe(session.warnings) && <p className="notice warning" role="status">{describe(session.warnings)}</p>}
    {session?.windows?.some((window) => window.status === 'no_face') &&
      <section className="session-skipped" aria-label="Skipped no-face windows">
        <h3>Skipped windows · no face crop</h3>
        <ul>{session.windows.filter((window) => window.status === 'no_face').map((window) =>
          <li className="muted small" key={window.index}>Window {window.index}: automated export skipped (no_face).
            {' Select this window’s retained candidate below to correct its face crop and export manually, if source is available. Use session crop controls for future windows.'}
            {window.warning ? ` Backend warning: ${window.warning}` : ''}</li>)}</ul>
      </section>}
    <section className="session-crop" aria-label="Future-window face crop">
      <div className="section-heading"><h3>Face crop for subsequent windows</h3><span className="tag">{cropTouched ? 'UNSAVED CROP' : faceDraft ? 'SAVED SESSION CROP' : 'AUTOMATIC FRAMING'}</span></div>
      <p className="muted small">Adjust independently of the candidate editor, including while a session is processing. These normalized coordinates refer to the full source frame. They are intended only for future windows, not completed clips or work already in progress.</p>
      <div className="session-crop-grid">
        <fieldset className="editor-fields" disabled={cropSaving}><legend className="sr-only">Session face crop coordinates</legend>
          <CropControls label="Future-window face crop" color="purple" value={faceDraft} onChange={(rect) => {
            setFaceDraft(rect); setCropTouched(true); setCropNotice(''); setCropError('');
          }} /></fieldset>
        <div>
          <div className="crop-diagram" role="img" aria-label="Schematic source frame and draft face crop; not a live preview">
            <span>Source frame · schematic only</span>
            {faceDraft && !invalidCrop && <div className="crop-outline face_crop" style={{
              left: `${faceDraft.x * 100}%`, top: `${faceDraft.y * 100}%`,
              width: `${faceDraft.width * 100}%`, height: `${faceDraft.height * 100}%`,
            }}><span>DRAFT FACE</span></div>}
          </div>
          <p className="muted small">No live-frame preview is assumed. Verify the region against actual source media; a valid rectangle does not establish that it contains the streamer.</p>
        </div>
      </div>
      {invalidCrop && <p className="notice warning" role="status">Face crop must fit within the source frame: 0 ≤ x, y; positive width and height; x + width ≤ 1 and y + height ≤ 1.</p>}
      <p className="muted small">Apply persists this session crop for windows that enter processing next, without waiting for capture to stop. Disabling the crop and applying sends null to restore automatic framing. Unsaved drafts do not affect processing and are lost on reload or changing jobs.</p>
      {cropError && <p className="notice error" role="alert">{cropError}</p>}
      {cropNotice && <p className="notice success" role="status">{cropNotice}</p>}
      {!active && <p className="muted small">This session is not active. A future-window crop cannot change its completed outputs.</p>}
      <div className="actions"><button type="button" disabled={!active || !cropTouched || !!invalidCrop || cropSaving} onClick={() => void applyFaceCrop()}>{cropSaving ? 'Applying crop…' : 'Apply to future windows'}</button>
        <button type="button" disabled={!cropTouched || cropSaving} onClick={() => { setFaceDraft(session?.face_crop ?? null); setCropTouched(false); setCropError(''); setCropNotice(''); }}>Discard crop draft</button></div>
    </section>
    <h3>Completed automated clips ({outputs.length})</h3>
    {!outputs.length ? <p className="muted small">No completed clips yet. Session progress does not guarantee an output.</p> :
      <ul className="session-outputs">{outputs.map((output, index) => {
        const files = exportFiles(output, job.id);
        return <li key={`${output.filename}-${index}`}>
          <strong>{output.filename}</strong>
          <div className="muted small">Candidate: {output.candidate_id}</div>
          {output.window_start !== undefined && output.window_end !== undefined &&
            <p className="muted small">Window range: {time(output.window_start)}–{time(output.window_end)}</p>}
          {output.global_start !== undefined && output.global_end !== undefined &&
            <p className="muted small">Session clip range: {time(output.global_start)}–{time(output.global_end)}</p>}
          <p className="muted small">Framing unverified — review the detected or manual crop before sharing. An export is not proof of correct face framing.</p>
          {files.map((file) => <a className="button" key={file.url} href={file.url} download={file.name}>Download automated clip ↓</a>)}
        </li>;
      })}</ul>}
  </section>;
}

interface EditorProps {
  job: Job;
  candidate: Candidate;
  onDirty: (dirty: boolean) => void;
  onBusy: (busy: boolean) => void;
  onSaved: () => Promise<void>;
  rolling?: boolean;
  onExport: (jobId: string, candidateId: string, files: ExportFile[]) => void;
}

function CandidateEditor({ job, candidate, rolling = false, onDirty, onBusy, onSaved, onExport }: EditorProps) {
  const [base, setBase] = useState(candidate);
  const [draft, setDraft] = useState<Candidate>({ ...candidate, words: candidate.words ?? [] });
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [current, setCurrent] = useState(candidate.start);
  const [duration, setDuration] = useState(0);
  const [mediaError, setMediaError] = useState(false);
  const [cropView, setCropView] = useState<'both' | 'face_crop' | 'content_crop' | 'none'>('both');
  const [captions, setCaptions] = useState(true);
  const [loop, setLoop] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const clipPlayback = useRef(false);
  const [waitingExport, setWaitingExport] = useState(false);
  const submittedJob = useRef(job);
  const latestJob = useRef(job);
  latestJob.current = job;
  const changes = changesBetween(base, draft);
  const dirty = Object.keys(changes).length > 0;
  const validation = validate(draft);
  const candidateExporting = ['queued', 'running', 'exporting'].includes(candidate.export_status ?? '') ||
    (!rolling && job.status === 'exporting');
  const locked = saving || exporting || candidateExporting || waitingExport;

  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => { onBusy(locked); return () => onBusy(false); }, [locked, onBusy]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    if (dirty || locked) window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, locked]);
  useEffect(() => {
    // Polls may update scores/evidence, but must never overwrite local edits.
    if (!dirty && !locked) {
      setBase(candidate);
      setDraft({ ...candidate, words: candidate.words ?? [] });
    }
  }, [candidate, dirty, locked]);
  useEffect(() => {
    if (!waitingExport || submittedJob.current === job || candidateExporting) return;
    if (candidate.export_status === 'failed' || (!rolling && job.status === 'failed')) {
      setWaitingExport(false); setError(`Export failed: ${candidate.export_error || describe(job.error) || 'See job diagnostics.'}`);
    } else if (candidate.export_status === 'cancelled' || (!rolling && ['cancelled', 'interrupted'].includes(job.status))) {
      setWaitingExport(false); setNotice('Export stopped. Previous completed outputs remain available.');
    } else if (candidate.export_status === 'succeeded' || (!rolling && job.status === 'succeeded')) {
      setWaitingExport(false); setNotice('Export complete. Find the rendered file in Exported videos or downloads below.');
    }
  }, [waitingExport, job, candidate, candidateExporting, rolling]);

  function seek(value: number) {
    if (!video.current || !Number.isFinite(value)) return;
    video.current.currentTime = Math.max(0, Math.min(value, duration || value));
    setCurrent(video.current.currentTime);
  }

  function editWord(index: number, patch: Partial<Word>) {
    setDraft((old) => ({ ...old, words: old.words.map((word, i) => i === index ? { ...word, ...patch } : word) }));
    setNotice('');
  }

  async function save(): Promise<boolean> {
    if (validation) { setError(validation); return false; }
    if (!dirty) return true;
    setSaving(true); setError(''); setNotice('');
    try {
      const payload = { ...changes };
      if (payload.words) payload.words = payload.words.map((word) => !word.text.trim() && word.correction
        ? { ...word, text: word.correction, correction: undefined } : word);
      await saveCandidate(job.id, candidate.id, payload);
      // Fetch the persisted candidate (PATCH may return 204 or a full job).
      const persistedJob = await getJob(job.id);
      const persisted = persistedJob.candidates.find((item) => item.id === candidate.id);
      if (!persisted) throw new Error('Saved candidate could not be found. Refresh the job before continuing.');
      setBase(persisted); setDraft({ ...persisted, words: persisted.words ?? [] });
      onDirty(false);
      setNotice('Changes saved.');
      await onSaved().catch(() => { /* The regular poll will retry the job list. */ });
      return true;
    } catch (err) { setError(message(err)); return false; }
    finally { setSaving(false); }
  }

  async function exportClip() {
    setExporting(true); setError(''); setNotice('');
    try {
      if (!(await save())) return;
      const result = await startExport(job.id, candidate.id);
      submittedJob.current = latestJob.current;
      onExport(job.id, candidate.id, result.files);
      setWaitingExport(result.pending);
      setFiles((old) => [...old, ...result.files].filter((file, i, all) => all.findIndex((other) => other.url === file.url) === i));
      setNotice(result.pending
        ? 'Export queued. Job status and output links update automatically; any links already listed may be from previous exports.'
        : result.files.length ? 'Export ready to download.' : 'Export requested. Waiting for the backend to publish output filenames.');
      await onSaved().catch(() => { /* Polling retries; an accepted export did not fail. */ });
    } catch (err) { setError(message(err)); }
    finally { setExporting(false); }
  }

  const persistedFiles = exportFiles(candidate.exports ?? candidate.outputs, job.id);
  const jobFiles = candidateJobExports(job.exports ?? job.outputs, job.id, candidate.id);
  const allFiles = [...files, ...persistedFiles, ...jobFiles].filter((file, i, all) => all.findIndex((other) => other.url === file.url) === i);
  const activeWords = draft.words.filter((word) => current >= word.start && current < word.end);
  const visibleRects = (['face_crop', 'content_crop'] as const).filter((field) => cropView === 'both' || cropView === field);
  const timelineEnd = duration || Math.max(draft.end, ...draft.words.map((word) => word.end), 1);
  const conflict = dirty && Object.keys(changes).some((field) =>
    JSON.stringify(candidate[field]) !== JSON.stringify(base[field]));

  return <section className="editor panel" aria-label="Candidate editor" aria-busy={locked} tabIndex={-1}>
    <div className="section-heading editor-heading"><div><p className="eyebrow">REFINE YOUR CLIP</p><h2>Moment editor <span className={`save-state ${dirty ? 'unsaved' : ''}`}>{dirty ? 'Unsaved changes' : 'Saved'}</span></h2></div>
      <div className="actions"><button disabled={!dirty || locked} onClick={() => {
        setBase(candidate); setDraft({ ...candidate, words: candidate.words ?? [] }); setError(''); setNotice('Changes discarded.');
      }}>Discard</button><button disabled={!dirty || locked || !!validation} onClick={() => void save()}>{saving ? 'Saving…' : 'Save changes'}</button>
        <button className="primary" disabled={locked || !!validation} onClick={() => void exportClip()}>{exporting || candidateExporting || waitingExport ? 'Exporting…' : 'Export clip ↗'}</button></div>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <p className="notice success" role="status">{notice}</p>}
    {validation && <p className="notice warning" role="status">{validation}</p>}
    {conflict && <p className="notice warning" role="alert">This candidate changed on the server while you were editing. Saving replaces the fields you edited; discard to load the server version.</p>}
    {candidate.warnings ? <p className="notice warning" role="status">{describe(candidate.warnings)}</p> : null}
    {rolling && <p className="notice success">Retained window {candidate.window_index ?? '—'} source · timestamps are relative to that window. Corrections can be saved and re-exported during capture or after stop; previous downloads are unchanged.</p>}
    {(candidateExporting || waitingExport) && <p className="export-activity" role="status"><progress aria-label="Selected candidate export progress" />Rendering candidate {candidate.id} · Job {job.id}. Output links appear when rendering completes.</p>}
    {candidate.export_status && <p className="muted small editor-export-status" role="status">Candidate export: {candidate.export_status}{candidate.revision !== undefined ? ` · Revision ${candidate.revision}` : ''}. Capture/session status is independent.</p>}
    {candidate.export_error && <p className="notice error" role="alert">{candidate.export_error}</p>}
    <fieldset disabled={locked} className="editor-fields">
      <legend className="sr-only">Candidate controls</legend>
      <div className="editor-grid">
        <div className="preview-column">
          <div className="video-stage">
            <div className="video-frame">
              <video ref={video} src={rolling
                ? `${jobPath(job.id)}/candidates/${encodeURIComponent(candidate.id)}/media`
                : `${jobPath(job.id)}/media`} controls playsInline preload="metadata"
                aria-label="Source video preview" onLoadedMetadata={(event) => {
                  setMediaError(false);
                  setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0);
                  event.currentTarget.currentTime = draft.start;
                }} onError={() => setMediaError(true)} onSeeking={(event) => {
                  const position = event.currentTarget.currentTime;
                  if (position < draft.start || position > draft.end) clipPlayback.current = false;
                }} onTimeUpdate={(event) => {
                  const player = event.currentTarget;
                  setCurrent(player.currentTime);
                  if (clipPlayback.current && player.currentTime >= draft.end) {
                    if (loop) player.currentTime = draft.start;
                    else { player.pause(); player.currentTime = draft.end; clipPlayback.current = false; }
                  }
                }} />
              <div className="video-overlays" aria-hidden="true">
                {visibleRects.map((field) => {
                  const rect = draft[field];
                  return rect ? <div key={field} className={`crop-outline ${field}`} style={{
                    left: `${rect.x * 100}%`, top: `${rect.y * 100}%`,
                    width: `${rect.width * 100}%`, height: `${rect.height * 100}%`,
                  }}><span>{field === 'face_crop' ? 'FACE' : 'CONTENT'}</span></div> : null;
                })}
                {captions && activeWords.length > 0 && <div className="caption-preview">{activeWords.map(captionText).join(' ')}</div>}
              </div>
            </div>
          </div>
          {mediaError && <p className="notice warning" role="alert">Source preview unavailable. The backend must serve browser-playable media and support byte-range requests. {rolling ? 'Retention may have changed; the backend will validate correction and re-export requests. No playable source is guaranteed.' : 'Editing and exports are still available.'}</p>}
          <div className="preview-toolbar">
            <button type="button" disabled={mediaError || !!validation} onClick={() => {
              const player = video.current;
              if (!player) return;
              seek(draft.start);
              clipPlayback.current = true;
              void player.play().catch((err) => setError(`Playback failed: ${message(err)}`));
            }}>▶ Play clip</button>
            <label className="check"><input type="checkbox" checked={loop} onChange={(event) => setLoop(event.target.checked)} /> Loop</label>
            <label className="check"><input type="checkbox" checked={captions} onChange={(event) => setCaptions(event.target.checked)} /> Captions</label>
            <span className="timecode">{time(current)} / {time(duration)}</span>
          </div>
          <div className="timeline-section">
            <div className="section-heading"><h3>Timeline</h3><span className="muted small">Source time · seconds</span></div>
            <div className="timeline-track" aria-hidden="true"><div className="timeline-selection" style={{
              left: `${Math.min(100, Math.max(0, draft.start / timelineEnd * 100))}%`,
              width: `${Math.min(100, Math.max(0, (draft.end - draft.start) / timelineEnd * 100))}%`,
            }} /><span className="playhead" style={{ left: `${Math.min(100, current / timelineEnd * 100)}%` }} /></div>
            <label className="sr-only" htmlFor="playhead">Seek source video</label>
            <input id="playhead" type="range" min="0" max={timelineEnd} step="0.01"
              value={Math.min(current, timelineEnd)} onChange={(event) => { clipPlayback.current = false; seek(Number(event.target.value)); }} />
            <div className="time-inputs"><NumberField label="Clip start (s)" value={draft.start} min={0} step={0.01} onChange={(start) => setDraft({ ...draft, start })} />
              <NumberField label="Clip end (s)" value={draft.end} min={0} step={0.01} onChange={(end) => setDraft({ ...draft, end })} />
              <div className="duration"><span className="muted small">Duration</span><strong>{Number.isFinite(draft.end - draft.start) ? (draft.end - draft.start).toFixed(2) : '—'}s</strong></div></div>
            {duration > 0 && draft.end > duration + 0.1 && <p className="notice warning">Clip end exceeds the source video duration.</p>}
            <p className="muted small">Changing the clip range does not shift transcript timestamps. Words outside the clip are marked below.</p>
          </div>
          <details className="evidence" open><summary>Why this moment? <span className="score">Score {Number.isFinite(candidate.score) ? candidate.score.toFixed(2) : '—'}</span></summary>
            <p className="muted small">Source range: {draft.start.toFixed(2)}–{draft.end.toFixed(2)} seconds. Scores are backend ranking values, not probabilities or verified quality ratings.</p>
            <pre>{describe(candidate.evidence) || 'No evidence was supplied by the backend.'}</pre></details>
        </div>
        <div className="crop-column">
          <div className="section-heading"><h3>Framing</h3><span className="tag">NORMALIZED</span></div>
          <label className="field">Preview guides<select value={cropView} onChange={(event) => setCropView(event.target.value as typeof cropView)}>
            <option value="both">Face + content</option><option value="face_crop">Face only</option><option value="content_crop">Content only</option><option value="none">Hide guides</option>
          </select></label>
          <p className="muted small">Guides show source regions, not the final composite. Coordinates are fractions of the original frame.</p>
          {(['face_crop', 'content_crop'] as const).map((field) => <CropControls key={field}
            label={field === 'face_crop' ? 'Face crop' : 'Content crop'} value={draft[field]}
            color={field === 'face_crop' ? 'purple' : 'teal'}
            onChange={(rect) => setDraft((old) => ({ ...old, [field]: rect }))} />)}
        </div>
      </div>
      <section className="transcript">
        <div className="section-heading"><div><h3>Transcript & captions</h3><p className="muted small">Edit the recognized words and their source timestamps. Click a time to seek. Caption corrections change displayed/exported text only; they never alter source speech or audio.</p></div><span className="count">{draft.words.length} words</span></div>
        {!draft.words.length ? <p className="muted empty">No transcript supplied. Add words to create captions.</p> :
          <div className="word-table-wrap"><table className="word-table"><thead><tr><th scope="col"># / Seek</th><th scope="col">Caption text</th><th scope="col">Start (s)</th><th scope="col">End (s)</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{draft.words.map((word, index) => {
              const outside = word.end <= draft.start || word.start >= draft.end;
              return <tr key={index} className={`${current >= word.start && current < word.end ? 'active-word' : ''} ${outside ? 'outside-word' : ''}`}>
                <td><button className="seek-word" title={outside ? 'Outside clip; seek source' : 'Seek to word'} aria-label={`Seek to word ${index + 1}${outside ? ', outside clip' : ''}`} type="button" onClick={() => { clipPlayback.current = false; seek(word.start); }}>{index + 1} ▶{outside ? ' *' : ''}</button></td>
                <td><input aria-label={`Word ${index + 1} caption correction`} value={captionText(word)} onChange={(event) => editWord(index, { correction: event.target.value })} />
                  {word.correction !== undefined && word.text && <div className="original-word"><span>Original: {word.text}</span><button type="button" className="text-button" onClick={() => editWord(index, { correction: undefined })} aria-label={`Use original text for word ${index + 1}`}>Restore</button></div>}</td>
                <td><NumberField label={`Word ${index + 1} start`} hideLabel value={word.start} min={0} step={0.01} onChange={(start) => editWord(index, { start })} /></td>
                <td><NumberField label={`Word ${index + 1} end`} hideLabel value={word.end} min={0} step={0.01} onChange={(end) => editWord(index, { end })} /></td>
                <td><button className="icon-button" title="Remove word" aria-label={`Remove word ${index + 1}`} type="button" onClick={() => setDraft((old) => ({ ...old, words: old.words.filter((_, i) => i !== index) }))}>×</button></td>
              </tr>;
            })}</tbody></table></div>}
        <div className="summary-footer"><p className="muted small">* Outside the selected clip. Original word metadata is preserved when editing.</p><button type="button" onClick={() => {
          const last = draft.words[draft.words.length - 1];
          const start = last ? Math.max(last.end, draft.start) : draft.start;
          setDraft((old) => ({ ...old, words: [...old.words, { start, end: start + 0.3, text: '' }] }));
        }}>+ Add word</button></div>
      </section>
    </fieldset>
    <section className="exports" aria-label="Export downloads"><div className="section-heading"><h3>Exports</h3><span className="muted small">Rendered by your backend</span></div>
      {allFiles.length ? <ul>{allFiles.map((file) => <li key={file.url}><span aria-hidden="true">↳</span><span>{file.name}</span><a className="button" href={file.url} download={file.name}>Download ↓</a>
        {/\.(mp4|webm|m4v|mov)$/i.test(file.name) && <details className="render-preview"><summary>Preview rendered output</summary><video controls playsInline preload="none" src={file.url} aria-label={`Rendered export ${file.name}`} /></details>}
      </li>)}</ul> : <p className="muted small">Save and export your clip to generate a downloadable output. Preview guides are not an export render.</p>}
    </section>
  </section>;
}

function NumberField({ label, value, min, max, step, onChange, hideLabel = false }: {
  label: string; value: number; min?: number; max?: number; step: number;
  onChange: (value: number) => void; hideLabel?: boolean;
}) {
  return <label className="field"><span className={hideLabel ? 'sr-only' : ''}>{label}</span>
    <input type="number" value={Number.isFinite(value) ? value : ''} min={min} max={max} step={step}
      onChange={(event) => onChange(event.target.value === '' ? Number.NaN : event.target.valueAsNumber)} /></label>;
}

function CropControls({ label, value, onChange, color }: {
  label: string; value?: Rect | null; onChange: (rect: Rect | null) => void; color: string;
}) {
  return <div className={`crop-controls ${color}`}>
    <div className="section-heading"><h4>{label}</h4>
      <label className="check"><input type="checkbox" checked={!!value} onChange={(event) =>
        onChange(event.target.checked ? { x: 0, y: 0, width: 1, height: 1 } : null)} /> Enable</label></div>
    {value ? <><div className="crop-inputs">{(['x', 'y', 'width', 'height'] as const).map((key) =>
      <NumberField key={key} label={`${label} ${key}`} value={value[key]} min={0} max={1} step={0.01}
        onChange={(next) => onChange({ ...value, [key]: next })} />)}</div>
      <button type="button" className="text-button" onClick={() => onChange({ x: 0, y: 0, width: 1, height: 1 })}>Reset to full frame</button></> :
      <p className="muted small">No explicit crop. Backend default framing will be used.</p>}
  </div>;
}
