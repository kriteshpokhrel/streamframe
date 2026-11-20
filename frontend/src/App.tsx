import { useCallback, useEffect, useRef, useState } from 'react';
import { describe, exportedVideos, exportFiles, failedSourceStatus, getDiagnostics, getJob, isRollingJob, jobControls, jobPath, listJobs, parseSourceTime, request, saveCandidate, startExport, streamerSource } from './api';
import type { Candidate, CandidateChanges, ExportedVideo, ExportFile, Job, Rect, Word } from './types';
import { parseRoute, routeHash, type Route } from './navigation';

const editable = ['start', 'end', 'words', 'face_crop', 'content_crop', 'opening_title'] as const;
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
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const selectedId = 'jobId' in route ? route.jobId : null;
  const [job, setJob] = useState<Job | null>(null);
  const candidateId = 'candidateId' in route ? route.candidateId : undefined;
  const [source, setSource] = useState('');
  const [sourceType, setSourceType] = useState<'streamer' | 'url' | 'file'>('url');
  const [autoExport, setAutoExport] = useState(false);
  const [autoTitleMode, setAutoTitleMode] = useState<'source' | 'none' | 'custom'>('source');
  const [autoTitle, setAutoTitle] = useState('');
  const [platform, setPlatform] = useState<'twitch' | 'youtube'>('twitch');
  const [sourceMode, setSourceMode] = useState<'auto' | 'live' | 'latest'>('auto');
  const [captureDuration, setCaptureDuration] = useState('60');
  const [liveOutput, setLiveOutput] = useState<'sample' | 'rolling'>('sample');
  const [sessionDuration, setSessionDuration] = useState('600');
  const [windowDuration, setWindowDuration] = useState('60');
  const [rangeStart, setRangeStart] = useState('');
  const [useFullVideo, setUseFullVideo] = useState(false);
  const [rangeEnd, setRangeEnd] = useState('');
  const [online, setOnline] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [webcamDrafts, setWebcamDrafts] = useState<Record<string, { crop: Rect | null }>>({});
  const [editorBusy, setEditorBusy] = useState(false);
  const [sessionExports, setSessionExports] = useState<ExportedVideo[]>([]);
  const [deletion, setDeletion] = useState<{ source: Job; file?: ExportedVideo; allExports?: boolean } | null>(null);
  const [leaveFor, setLeaveFor] = useState<Route | null>(null);
  const [libraryNotice, setLibraryNotice] = useState('');
  const deleteDialog = useRef<HTMLDialogElement>(null);
  const libraryVersion = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const exportUrls = useRef<string[]>([]);
  useEffect(() => () => { exportUrls.current.forEach((url) => URL.revokeObjectURL(url)); }, []);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const detailVersion = useRef(0);

  useEffect(() => {
    if (deletion || leaveFor) deleteDialog.current?.showModal();
    else deleteDialog.current?.close();
  }, [deletion, leaveFor]);

  function setWebcamDraft(jobId: string, value: { crop: Rect | null } | undefined) {
    setWebcamDrafts((old) => {
      if (value) return { ...old, [jobId]: value };
      const next = { ...old };
      delete next[jobId];
      return next;
    });
  }

  useEffect(() => {
    if (!Object.keys(webcamDrafts).length) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [webcamDrafts]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const id = selectedRef.current;
    const libraryRevision = libraryVersion.current;
    const nextJobs = await listJobs(signal);
    if (signal?.aborted || libraryRevision !== libraryVersion.current) return;
    setJobs(nextJobs);
    setPollError('');
    setLoading(false);
    if (!id || !nextJobs.some((item) => item.id === id)) return;
    const version = ++detailVersion.current;
    const nextJob = await getJob(id, signal);
    if (!signal?.aborted && version === detailVersion.current && selectedRef.current === id) setJob(nextJob);
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
    const version = ++detailVersion.current;
    void getJob(selectedId, controller.signal).then((next) => {
      if (!controller.signal.aborted && version === detailVersion.current) setJob(next);
    }).catch((err) => { if (!isAbort(err) && version === detailVersion.current) setError(message(err)); });
    return () => controller.abort();
  }, [selectedId]);

  const activeJob = job?.id === selectedId ? job : null;
  const candidates = activeJob?.candidates ?? [];
  const rollingJob = !!activeJob && isRollingJob(activeJob);
  const selectedCandidate = candidateId ? candidates.find((candidate) => candidate.id === candidateId) : candidates[0];
  useEffect(() => {
    if (route.view !== 'review' || candidateId || !selectedCandidate) return;
    const next = { ...route, candidateId: selectedCandidate.id };
    window.history.replaceState(null, '', routeHash(next));
    setRoute(next);
  }, [route, candidateId, selectedCandidate]);
  const libraryJobs = jobs.map((item) => item.id === job?.id ? job : item);
  const gallery = [...exportedVideos(libraryJobs), ...sessionExports.filter((file) =>
    libraryJobs.some((item) => item.id === file.jobId))].filter((file, index, all) =>
    all.findIndex((other) => other.jobId === file.jobId && other.url === file.url) === index);
  function navigate(next: Route, confirmed = false) {
    const hash = routeHash(next);
    if (hash === routeHash(route)) return;
    if (!confirmed && (busy || editorBusy || deletion || leaveFor)) return;
    if (!confirmed && dirty) { setLeaveFor(next); return; }
    window.history.pushState(null, '', hash);
    setDirty(false); setError(''); setRoute(next);
  }

  useEffect(() => {
    const onHashChange = () => {
      const next = parseRoute(window.location.hash);
      if (routeHash(next) === routeHash(route)) return;
      if (busy || editorBusy || deletion || leaveFor || dirty) {
        window.history.pushState(null, '', routeHash(route));
        if (!busy && !editorBusy && !deletion && !leaveFor && dirty) setLeaveFor(next);
        return;
      }
      setDirty(false); setError(''); setRoute(next);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [route, dirty, busy, editorBusy, deletion, leaveFor]);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [route.view, selectedId]);

  function rememberExports(jobId: string, candidateId: string, files: ExportFile[]) {
    files.forEach((file) => { if (file.url.startsWith('blob:')) exportUrls.current.push(file.url); });
    setSessionExports((old) => [...old, ...files.map((file) => ({ ...file, jobId, candidateId }))]);
  }

  function editExport(file: ExportedVideo) {
    navigate({ view: 'review', jobId: file.jobId, candidateId: file.candidateId });
  }

  async function importSource(event: React.FormEvent) {
    event.preventDefault();
    if (!source.trim() || busy || editorBusy) return;
    setBusy(true); setError('');
    try {
      const local = sourceType === 'file';
      const full = sourceType === 'url' && linkPlatform(source.trim()) === 'youtube' && useFullVideo;
      const start = full || rangeStart.trim() === '' ? undefined : parseSourceTime(rangeStart);
      const end = full || rangeEnd.trim() === '' ? undefined : parseSourceTime(rangeEnd);
      if (sourceType === 'url' && !full && (start === undefined || end === undefined)) {
        throw new Error('Public video links require both a source range start and end.');
      }
      if ((start !== undefined && (!Number.isFinite(start) || start < 0)) ||
          (end !== undefined && (!Number.isFinite(end) || end <= (start ?? 0) || end - (start ?? 0) > 1800))) {
        throw new Error('Import range requires valid times, 0 ≤ start < end, and a maximum 30-minute span.');
      }
      const duration = Number(captureDuration);
      const rolling = sourceType === 'streamer' && sourceMode === 'live' && liveOutput === 'rolling';
      if (autoExport && !rolling && autoTitleMode === 'custom' && !autoTitle.trim()) {
        throw new Error('Enter a custom opening title or choose a different title option.');
      }
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
        ? { type: 'url', url: source.trim(), ...(full
          ? { full_video: true } : { start, end }) }
        : { type: 'file', path: source.trim(), start, end };
      const created = await request<Job>('/api/jobs', {
        method: 'POST', body: JSON.stringify({ source: payload,
          ...(autoExport && !rolling ? { auto_export: {
            title: autoTitleMode === 'custom' ? autoTitle.trim() : autoTitleMode === 'none' ? null : undefined,
          } } : {}) }),
      });
      if (!created?.id) throw new Error('The server did not return a job id.');
      setDirty(false);
      setJob(created);
      setLibraryNotice(autoExport && !rolling ? 'Source added. The top moments will be exported after analysis.' : 'Source added. We’ll find moments for you to review.');
      setJobs((old) => [created, ...old.filter((item) => item.id !== created.id)]);
      navigate({ view: rolling ? 'live' : 'review', jobId: created.id }, true);
      setSource(''); setRangeStart(''); setRangeEnd('');
      setAutoExport(false); setAutoTitleMode('source'); setAutoTitle('');
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

  async function confirmDeletion() {
    if (!deletion || busy || editorBusy) return;
    const { source: item, file, allExports } = deletion;
    setBusy(true); setError(''); setLibraryNotice('');
    try {
      await request(file ? `${jobPath(item.id)}/exports/${encodeURIComponent(file.name)}` :
        allExports ? `${jobPath(item.id)}/exports` : jobPath(item.id),
        { method: 'DELETE' });
      libraryVersion.current++;
      detailVersion.current++;
      if (file || allExports) {
        const prune = (entry: Job) => entry.id === item.id
          ? { ...entry, exports: entry.exports?.filter((record) => !allExports && record.filename !== file?.name) } : entry;
        setJobs((old) => old.map(prune));
        setJob((old) => old && prune(old));
        setSessionExports((old) => old.filter((entry) => entry.jobId !== item.id || (!allExports && entry.name !== file?.name)));
        setLibraryNotice(allExports ? 'Exports deleted. Your source and edits are unchanged.' : 'Export deleted. Your source and other exports are unchanged.');
      } else {
        setJobs((old) => old.filter((other) => other.id !== item.id));
        setSessionExports((old) => old.filter((entry) => entry.jobId !== item.id));
        setWebcamDraft(item.id, undefined);
        if (selectedId === item.id) {
          setDirty(false); setJob(null);
          navigate({ view: 'sources' }, true);
        }
        setLibraryNotice('Source and its exports deleted from StreamFrame.');
      }
      setDeletion(null);
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); }
  }

  return (
    <div className="app">
      <header className="workspace-rail">
        <a className="brand" href="#/sources" onClick={(event) => {
          event.preventDefault(); navigate({ view: 'sources' });
        }}><span className="brand-icon" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M2.5 8V3h5m5 0h5v5m0 4v5h-5m-5 0h-5v-5M8 7l5 3-5 3V7Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/></svg></span> StreamFrame</a>
        <nav className="workspace-nav" aria-label="Workspace sections">
          <a href="#/sources" aria-current={route.view !== 'exports' && route.view !== 'add' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); navigate({ view: 'sources' }); }}><StudioIcon name="sources" /> Sources</a>
          <a href="#/exports" aria-current={route.view === 'exports' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); navigate({ view: 'exports' }); }}><StudioIcon name="exports" /> Exports <span>{gallery.length}</span></a>
          <a href="#/add" className="nav-add" aria-current={route.view === 'add' ? 'page' : undefined} onClick={(event) => { event.preventDefault(); navigate({ view: 'add' }); }}><StudioIcon name="plus" /> Add source</a>
        </nav>
        <ThemeToggle />
        <span className={`connection ${online === false ? 'offline' : ''}`}>
          <span aria-hidden="true">●</span> {online === null ? 'Connecting' : online ? 'Studio online' : 'Studio offline'}
        </span>
      </header>
      <main className={route.view === 'review' ? 'review-workspace' : undefined}>
        <dialog ref={deleteDialog} className="studio-dialog" aria-labelledby="delete-title" aria-describedby="delete-description"
          onCancel={(event) => { event.preventDefault(); if (!busy) { setDeletion(null); setLeaveFor(null); setError(''); } }}>
          {deletion && <>
            <h2 id="delete-title">Delete {deletion.allExports ? 'all exports from this source' : deletion.file ? 'this export' : 'this source'}?</h2>
            <strong>{exportSourceTitle(deletion.source)}</strong>
            <p id="delete-description" className="muted">{deletion.file
              ? 'Only this finished clip will be removed. Your source and other exports will stay.'
              : deletion.allExports ? 'All finished clips from this source will be removed. The source and your edits will stay.'
                : 'This removes the source, saved edits, and all its exports from StreamFrame. Your original video is not deleted.'} This cannot be undone.</p>
            {error && <p className="notice error" role="alert">{error}</p>}
            <div className="actions">
              <button autoFocus disabled={busy} onClick={() => { setDeletion(null); setError(''); }}>{deletion.allExports ? 'Keep exports' : deletion.file ? 'Keep export' : 'Keep source'}</button>
              <button className="danger" disabled={busy} onClick={() => void confirmDeletion()}>{busy ? 'Deleting…' : deletion.allExports ? 'Delete all exports' : deletion.file ? 'Delete export' : 'Delete source'}</button>
            </div>
          </>}
          {leaveFor && <>
            <h2 id="delete-title">Leave without saving?</h2>
            <p id="delete-description" className="muted">Your unsaved clip edits won’t be applied to this moment.</p>
            <div className="actions">
              <button autoFocus onClick={() => setLeaveFor(null)}>Keep editing</button>
              <button className="danger" onClick={() => { setLeaveFor(null); navigate(leaveFor, true); }}>Leave without saving</button>
            </div>
          </>}
        </dialog>
        {error && !deletion && <div className="notice error" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
        {pollError && <div className="notice warning" role="status">Status may be out of date. Reconnecting automatically. <span>{pollError}</span></div>}
        {libraryNotice && <StudioToast key={libraryNotice} text={libraryNotice} dismiss={() => setLibraryNotice('')} />}
        {route.view === 'add' && <section className="import-view">
          <div className="import-intro">
            <h1 tabIndex={-1} ref={heading}>Add source</h1>
            <p className="muted">Start with a video. Pick a moment, review it in portrait, and export when it’s ready.</p>
            <ol className="workflow-steps"><li aria-current="step">Add footage</li><li>Pick a moment</li><li>Review and export</li></ol>
          </div>
          <form id="add-video" className="import-card" onSubmit={(event) => void importSource(event)}>
            <fieldset className="source-options" disabled={busy}>
              <legend>What would you like to clip?</legend>
              {([['streamer', 'Clip a channel'], ['url', 'Use a video link'], ['file', 'Use a local file']] as const).map(([type, label]) =>
                <label key={type}><input type="radio" name="source-type" checked={sourceType === type}
                  onChange={() => { setSourceType(type); setRangeStart(''); setRangeEnd(''); setUseFullVideo(false); }} />{label}</label>)}
            </fieldset>
            <p className="muted small" id="source-help">{sourceType === 'streamer'
              ? `Enter a ${platform === 'twitch' ? 'Twitch name or channel/live link' : 'YouTube @handle or channel/live link'}. Supported public HLS sources are retrieved automatically; availability is not guaranteed.`
              : sourceType === 'url' ? 'Paste a finished-video link, then choose the part you want.'
                : 'Enter the full file path on the computer running StreamFrame. This is not a browser upload.'}</p>
            <label htmlFor="source" className="import-input-label">{sourceType === 'streamer' ? 'Streamer name or link' : sourceType === 'url' ? 'Video link' : 'File path'}</label>
            <div className="input-row"><input id="source" value={source} onChange={(event) => {
              const next = event.target.value;
              setSource(next);
              if (sourceType === 'streamer') {
                const detected = linkPlatform(next);
                if (detected) setPlatform(detected);
              }
            }}
              type={sourceType === 'url' ? 'url' : 'text'} placeholder={sourceType === 'streamer' ? platform === 'twitch' ? 'e.g. streamer_name or twitch.tv/…' : '@handle or https://youtube.com/@…' : sourceType === 'url' ? 'https://www.twitch.tv/videos/…' : '/absolute/path/to/video.mp4'} required aria-describedby="source-help" disabled={busy || editorBusy} />
            </div>
            {sourceType === 'streamer' && <div className="import-range">
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
            </div>}
            {sourceType === 'url' && linkPlatform(source.trim()) === 'youtube' &&
              <div className="quality-options"><label className="check"><input type="checkbox" checked={useFullVideo}
                disabled={busy || editorBusy} onChange={(event) => setUseFullVideo(event.target.checked)} />
                Full video (up to 5 minutes)</label>
                <p className="muted small">YouTube imports require 720p or better. Lower-quality versions are not imported.</p>
              </div>}
            {sourceType !== 'streamer' && !(sourceType === 'url' && linkPlatform(source.trim()) === 'youtube' && useFullVideo) && <div className="import-range">
              <label className="field">Source range start{sourceType === 'file' ? ', optional' : ', required'}<input type="text" required={sourceType === 'url'}
                value={rangeStart} onChange={(event) => setRangeStart(event.target.value)} disabled={busy || editorBusy} placeholder="00:00:00" /></label>
              <label className="field">Source range end{sourceType === 'file' ? ', optional' : ', required'}<input type="text" required={sourceType === 'url'}
                value={rangeEnd} onChange={(event) => setRangeEnd(event.target.value)} disabled={busy || editorBusy} placeholder={sourceType === 'file' ? 'End of file' : '00:00:00'} /></label>
              <p className="muted small">Up to 30 minutes. {sourceType === 'url' ? 'Both times are required.' : 'Leave times empty for the full file; start-only takes up to 30 minutes or end of file.'} Enter original-video times as HH:MM:SS, MM:SS, or seconds. Editor times start at 0 for the part you import.</p>
            </div>}
            <p className="source-note">If a link can’t be imported, use a local video file you have permission to clip.</p>
            <details className="import-settings"><summary>Limits & supported formats</summary>
              <p className="muted small">Full YouTube videos up to 5 minutes can use a compatible MP4 or HLS stream. Custom ranges that don’t cover the whole video require a compatible HLS archive. Private videos and broadcasts still processing aren’t supported.</p>
              <p className="muted small">Live sessions: 512 MiB transferred and 512 MiB combined temporary/output disk. Finished videos: 512 MiB / 1200 segments / 10-minute retrieval deadline, output below 2 GiB. Reaching a limit can stop the job; continuous coverage is not guaranteed.</p>
            </details>
            <p className="muted small permission-note">Use content you have permission to use, and credit its owner when required.</p>
            {!(sourceType === 'streamer' && sourceMode === 'live' && liveOutput === 'rolling') &&
              <div className="auto-export-settings">
                <label className="check"><input type="checkbox" checked={autoExport} onChange={(event) => setAutoExport(event.target.checked)} /> Automatically export the two highest-ranked moments</label>
                {autoExport && <>
                  <label className="field">Opening title for both clips
                    <select value={autoTitleMode} onChange={(event) => {
                      const mode = event.target.value;
                      if (mode === 'source' || mode === 'none' || mode === 'custom') setAutoTitleMode(mode);
                    }}>
                      <option value="source">Use video title (default)</option>
                      <option value="none">No title</option>
                      <option value="custom">Custom title</option>
                    </select>
                    {autoTitleMode === 'source' && <span className="small">Uses the imported video title, or the filename for a local video.</span>}
                  </label>
                  {autoTitleMode === 'custom' && <label className="field">Custom opening title
                    <input value={autoTitle} required maxLength={60} onChange={(event) => setAutoTitle(event.target.value)} />
                  </label>}
                  <span className="muted small">Clips without usable webcam framing are left for review.</span>
                </>}
              </div>}
            <div className="actions import-actions">
              <button type="button" disabled={busy} onClick={() => navigate({ view: 'sources' })}>Back to sources</button>
              <button className="primary" type="submit" disabled={busy || !source.trim()}>{busy ? 'Adding source…' : sourceType === 'streamer' && sourceMode === 'live' ? 'Start live capture' : 'Find moments'}</button>
            </div>
          </form>
        </section>}
        {route.view === 'exports' && <ExportLibrary jobs={libraryJobs} files={gallery} busy={busy || editorBusy}
          heading={heading} sourceId={route.sourceId}
          onFilter={(sourceId) => navigate({ view: 'exports', sourceId })}
          onEdit={editExport} onDelete={(source, file) => { setError(''); setDeletion({ source, file }); }} />}
        {route.view === 'sources' && <section className="sources-view" aria-label="Imported jobs">
          <div className="page-heading"><div><h1 tabIndex={-1} ref={heading}>Sources</h1><p className="muted">Pick a video to find a moment, refine it, and make it yours.</p></div>
            <button className="primary" onClick={() => navigate({ view: 'add' })}>+ Add source</button></div>
          {loading ? <p role="status" className="muted empty">Loading sources…</p> : !jobs.length && !pollError ?
            <div className="source-empty"><div className="empty-art" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /></div><h2>No sources yet</h2>
              <p className="muted">Start with a channel, video link, or local file. StreamFrame finds moments for you to review before exporting.</p></div> : null}
          {(['In progress', 'Ready to review', 'History'] as const).map((group) => {
            const items = jobs.filter((item) => (jobWorking(item) ? 'In progress' : item.candidates?.length && item.status !== 'failed' ? 'Ready to review' : 'History') === group);
            return items.length > 0 && <section className="source-group" key={group}><h2>{group}</h2><div className="job-list">{items.map((item) => {
              const count = gallery.filter((file) => file.jobId === item.id).length;
              return (
              <div className="library-entry" key={item.id}>
              <div className="job-ident"><strong title={sourceText(item.source)}>{exportSourceTitle(item)}</strong>
                <span className="muted small">{item.deletion_pending ? 'Deletion incomplete — try deleting again' : sourceStatus(item)}{formatJobDate(item) ? ` · Added ${formatJobDate(item)}` : ''}</span>
                {count > 0 && !item.deletion_pending && <a className="small" href={routeHash({ view: 'exports', sourceId: item.id })}>
                  {count} {count === 1 ? 'export' : 'exports'}
                </a>}</div>
              <div className="actions source-actions">
              <button className="library-open" disabled={busy || !!item.deletion_pending} onClick={() => navigate({ view: isRollingJob(item) ? 'live' : 'review', jobId: item.id })}>
                {jobWorking(item) ? 'Monitor' : item.status === 'failed' ? 'See what went wrong' : isRollingJob(item) ? 'View session' : item.candidates?.length ? 'Review moments' : 'View source'}
              </button>
              <button className="quiet destructive" disabled={busy || editorBusy || jobWorking(item)}
                title={jobWorking(item) ? 'Available after this source finishes processing' : undefined}
                aria-label={`Delete source: ${exportSourceTitle(item)}`}
                onClick={() => { setError(''); setDeletion({ source: item }); }}>Delete source</button>
              </div>
              </div>
            ); })}</div></section>;
          })}
        </section>}
        {(route.view === 'review' || route.view === 'live') && <section className="work-area" aria-label="Job workspace">
            <div className="work-heading"><button className="back-to-sources" onClick={() => navigate({ view: 'sources' })}>Back to Sources</button>
              <h1 tabIndex={-1} ref={heading}>{route.view === 'live' ? 'Live session' : 'Review moments'}</h1>
              {rollingJob && <button onClick={() => navigate({ view: route.view === 'live' ? 'review' : 'live', jobId: route.jobId })}>
                {route.view === 'live' ? 'Review retained moments' : 'Monitor live session'}</button>}</div>
            {activeJob && (!loading && !jobs.some((item) => item.id === activeJob.id)) && <p className="notice warning">This source is no longer in the library. {dirty ? 'Your open edits have not been discarded.' : 'Return to Sources to choose another.'}</p>}
            {!activeJob || !job ? <p className="muted empty" role="status">{loading || (!error && jobs.some((item) => item.id === selectedId)) ? 'Loading source…' : 'This source is unavailable. Return to Sources to choose another.'}</p> : <>
              <section className="panel job-summary">
                <div className="section-heading source-summary-heading"><h2 className="source-title" title={sourceText(job.source)}>{exportSourceTitle(job)}</h2>
                  <span className="status">{sourceStatus(job)}</span>
                  {route.view === 'review' && <div className="source-summary-actions">
                    <ReviewSourceDetails key={job.id} job={job} />
                    <button className="text-button source-exports-link" onClick={() => navigate({ view: 'exports', sourceId: job.id })}>View exports</button>
                  </div>}
                </div>
                <SourceProgress job={job} />
                {typeof job.auto_export_notice === 'string' && <p className="notice warning">{job.auto_export_notice}</p>}
                {route.view === 'live' && <details className="source-details" open><summary>Source details</summary>
                  <p className="muted small">Source status: {sourceResolution(job)}</p>
                  {job.resolved_source && <><p className="muted small">Chosen source: {job.resolved_source.title || job.resolved_source.url}</p>
                    <p className="muted small source-url">{job.resolved_source.url}</p>
                    <p className="muted small">Platform: {job.resolved_source.platform} · Selection: {job.resolved_source.selection}</p></>}
                  {job.capture_started_at && <p className="muted small">Capture started: {job.capture_started_at}</p>}
                  {typeof job.source !== 'string' && job.source.type === 'streamer' &&
                    <p className="muted small">Requested mode: {job.source.mode} · {job.source.rolling ? `Rolling session: ${job.source.session_duration}s / ${job.source.window_duration}s windows` : `Single sample: ${job.source.duration ?? 'backend default'}s`}. Editor timestamps are zero-based relative to captured media.</p>}
                </details>}
                {typeof job.source !== 'string' &&
                  (job.source.start !== undefined || job.source.end !== undefined) &&
                  <p className="source-range-note muted">Requested range: {job.source.start ?? 0}s to {job.source.end ?? 'end'}. All editor timestamps are relative to this imported range, if available.</p>}
                <div className="summary-footer">
                  <div className="actions">{jobControls(job).cancel && <button disabled={busy || (editorBusy && !rollingJob && job.status !== 'exporting')} onClick={() => void controlJob('cancel')}>{rollingJob || job.stage === 'capture' ? 'Stop capture' : 'Stop processing'}</button>}{jobControls(job).resume && <button disabled={busy || editorBusy || dirty} title={dirty ? 'Save or discard edits before trying again.' : undefined} onClick={() => void controlJob('resume')}>{job.status === 'failed' ? 'Try again' : 'Resume'}</button>}
                  {route.view === 'live' && gallery.some((file) => file.jobId === job.id) && <a className="button" href={routeHash({ view: 'exports', sourceId: job.id })}>View exports</a>}</div></div>
                {job.error ? <p className="notice error" role="alert">{sourceFailure(job)}</p> : null}
                {(job.status === 'failed' || candidates.some((candidate) => candidate.export_status === 'failed')) &&
                  <details className="source-details"><summary>Technical details</summary>{job.error ? <p className="muted small">{describe(job.error)}</p> : null}<Diagnostics key={job.id} jobId={job.id} /></details>}
                {job.warnings ? <p className="notice warning" role="status">{describe(job.warnings)}</p> : null}
              </section>
              {route.view === 'live' && rollingJob &&
                <LiveSession key={job.id} job={job} onSaved={refresh}
                  onEdit={(id) => navigate({ view: 'review', jobId: job.id, candidateId: id })} />}
              {route.view === 'live' && !rollingJob && <button onClick={() => navigate({ view: 'review', jobId: job.id })}>Review this source</button>}
              {route.view === 'review' && <div className="review-grid"><section className="candidate-section">
                <div className="moments-heading"><h2>Moments</h2><span className="count">{candidates.length}</span></div>
                {!candidates.length ? <div className="empty"><p>{jobWorking(job) ? 'No moments to review yet' : 'No suggested moments'}</p><p className="muted">{jobWorking(job) ? 'Moments will appear here after analysis.' : 'Check the source status or try another source.'}</p></div> :
                  <div className="candidate-list" aria-label="Candidates">{candidates.map((candidate, index) => (
                    <button className={`candidate-card ${selectedCandidate?.id === candidate.id ? 'selected' : ''}`} key={candidate.id}
                      disabled={busy || editorBusy} aria-pressed={selectedCandidate?.id === candidate.id} onClick={() => {
                        navigate({ view: 'review', jobId: job.id, candidateId: candidate.id });
                      }}>
                      <span className="candidate-top"><span>Moment {index + 1}</span>{Number.isFinite(candidate.score) && <span className="moment-score" title="Ranking score, not a probability">Score {candidate.score.toFixed(2)}</span>}</span>
                      <strong className="candidate-caption">{candidate.words?.map(captionText).filter(Boolean).join(' ') || 'No transcript preview'}</strong>
                      <span className="candidate-time">{time(candidate.start)} to {time(candidate.end)}</span>
                      {rollingJob && <span className="muted small">{retainedSource(candidate)
                        ? 'Source retained · correction available' : 'Source unavailable · evidence only'}</span>}
                    </button>
                  ))}</div>}
              </section>
              {selectedCandidate && (!rollingJob || retainedSource(selectedCandidate)) && <CandidateEditor key={`${job.id}/${selectedCandidate.id}`} job={job}
                candidate={selectedCandidate} momentNumber={candidates.indexOf(selectedCandidate) + 1} rolling={rollingJob} onDirty={setDirty} onBusy={setEditorBusy} onExport={rememberExports}
                webcamDraft={webcamDrafts[job.id]} onWebcamDraft={(value) => setWebcamDraft(job.id, value)}
                onPersisted={(saved) => {
                  ++detailVersion.current;
                  setJob(saved);
                }} onSaved={async () => { await refresh(); }} />}
              {selectedCandidate && rollingJob && !retainedSource(selectedCandidate) &&
                <section className="evidence-only">
                  <h2>This moment is evidence only</h2>
                  <p className="muted">The recording is no longer available to preview, edit or re-export. Finished exports can still be downloaded.</p>
                  <details className="evidence"><summary>Moment evidence</summary><pre>{describe(selectedCandidate.evidence) || 'No evidence supplied.'}</pre></details>
                </section>}
              {candidateId && !selectedCandidate && <p className="notice warning">This moment is unavailable. Select another moment from the list.</p>}
              </div>}
              {route.view === 'review' && <ReviewExports key={job.id} jobs={[job]} files={gallery} busy={busy || editorBusy}
                sourceId={job.id} onDelete={(source, file) => { setError(''); setDeletion({ source, file }); }}
                onDeleteAll={() => { setError(''); setDeletion({ source: job, allExports: true }); }} />}
            </>}
          </section>}
      </main>
    </div>
  );
}

function ThemeToggle() {
  const [preference, setPreference] = useState<'light' | 'dark' | null>(() => {
    try {
      const saved = localStorage.getItem('streamframe-theme');
      return saved === 'light' || saved === 'dark' ? saved : null;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'SecurityError')) throw error;
      return null;
    }
  });
  const [systemDark, setSystemDark] = useState(() => matchMedia('(prefers-color-scheme: dark)').matches);
  const dark = preference ? preference === 'dark' : systemDark;

  useEffect(() => {
    const query = matchMedia('(prefers-color-scheme: dark)');
    const sync = () => setSystemDark(query.matches);
    query.addEventListener('change', sync);
    sync();
    return () => query.removeEventListener('change', sync);
  }, []);

  useEffect(() => {
    if (preference) document.documentElement.dataset.theme = preference;
  }, [preference]);

  return <button type="button" className="theme-toggle quiet" aria-pressed={dark} onClick={() => {
    const next = dark ? 'light' : 'dark';
    setPreference(next);
    try {
      localStorage.setItem('streamframe-theme', next);
    } catch (error) {
      if (!(error instanceof DOMException && ['SecurityError', 'QuotaExceededError'].includes(error.name))) throw error;
      console.warn('StreamFrame could not save the theme preference.', error);
    }
  }}><StudioIcon name="moon" /><span>Dark mode</span></button>;
}

function StudioIcon({ name }: { name: 'sources' | 'exports' | 'plus' | 'check' | 'moon' | 'framing' | 'captions' | 'title' | 'play' | 'pause' }) {
  const paths = {
    sources: 'M3 6h7l2 2h9v12H3z', exports: 'M4 5h16v14H4z M10 9l5 3-5 3z',
    plus: 'M12 5v14M5 12h14', check: 'm5 12 4 4L19 6',
    moon: 'M20.5 14.1A8.7 8.7 0 0 1 9.9 3.5a8.7 8.7 0 1 0 10.6 10.6Z',
    framing: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M8 8h8v8H8z',
    captions: 'M3 5h18v14H3zM7 11h4m2 0h4M7 15h2m2 0h6',
    title: 'M5 7V5h14v2M12 5v14m-3 0h6',
    play: 'm9 5 10 7-10 7z', pause: 'M9 5v14m6-14v14',
  };
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}

function StudioToast({ text, dismiss }: { text: string; dismiss: () => void }) {
  const timer = useRef<ReturnType<typeof setTimeout>>();
  function pause() { clearTimeout(timer.current); }
  function resume() { pause(); timer.current = setTimeout(dismiss, 6500); }
  useEffect(() => {
    resume();
    return pause;
  }, []);
  return <div className="studio-toast" onMouseEnter={pause} onMouseLeave={resume}
    onFocus={pause} onBlur={resume}>
    <span className="toast-icon"><StudioIcon name="check" /></span>
    <p role="status">{text}</p>
    <button className="quiet" type="button" aria-label="Dismiss notification" onClick={dismiss}>×</button>
  </div>;
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

const exportDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const exportTimestamp = (file: ExportedVideo) => Date.parse(file.provenance?.created_at ?? '');
const formatJobDate = (job: Job) => typeof job.created_at === 'number' &&
  Number.isFinite(new Date(job.created_at * 1000).getTime()) ? exportDate.format(job.created_at * 1000) : '';
const sourceLabels: Record<string, string> = {
  succeeded: 'Ready', failed: 'Needs attention', interrupted: 'Interrupted', cancelled: 'Stopped',
};
function sourceStatus(job: Job) {
  if (!jobWorking(job)) return sourceLabels[job.status] ?? job.status;
  if (job.status === 'cancelling') return 'Stopping';
  if (job.status === 'queued') return job.operation === 'export' ? 'Export queued' : 'Queued';
  if (job.operation === 'export' || job.stage === 'export') return 'Exporting clip';
  if (job.stage === 'analysis') return 'Analyzing video';
  if (job.stage === 'download') return 'Downloading video';
  if (job.stage === 'capture') return 'Capturing live video';
  if (job.stage === 'rolling') return 'Processing stream';
  if (job.stage === 'trim') return 'Preparing video range';
  return 'Preparing source';
}

function SourceProgress({ job }: { job: Job }) {
  if (!jobWorking(job) || job.operation === 'export' || job.auto_export_pending !== undefined || job.status === 'cancelling') return null;
  const analysis = job.stage === 'analysis';
  const local = typeof job.source !== 'string' && job.source.type === 'file';
  if (isRollingJob(job)) return null;
  const steps = [local ? 'Prepare file' : 'Import video', 'Find moments', 'Ready to review'];
  return <div className="source-progress">
    <ol aria-label="Source preparation steps">
      {steps.map((step, index) => {
        const current = job.status !== 'queued' && index === (analysis ? 1 : 0);
        const done = analysis && index === 0;
        return <li key={step} data-state={done ? 'done' : current ? 'active' : 'pending'} aria-current={current ? 'step' : undefined}>
          <span className="step-marker" aria-hidden="true">{done ? <StudioIcon name="check" /> : index + 1}</span>
          <span>{step}{done && <span className="sr-only"> complete</span>}</span>
        </li>;
      })}
    </ol>
    <p className="small muted" role="status">{job.status === 'queued' ? 'Waiting to start.' :
      analysis ? `${local ? 'File ready' : 'Import complete'}. Finding highlights and preparing captions.` :
        job.stage === 'download' ? 'Fetching video. The source doesn’t report a download percentage.' :
          job.stage === 'capture' ? 'Recording the live stream before analysis.' : 'Getting the video ready for analysis.'}</p>
    {analysis && progress(job.progress) > 0 && <div className="progress-row">
      <progress max={100} value={progress(job.progress)} aria-label="Video analysis progress" />
      <span>{progress(job.progress)}%</span>
    </div>}
  </div>;
}
function sourceFailure(job: Job) {
  const reason = describe(job.error);
  if (job.operation === 'export') return 'This clip didn’t export. Your edits are still saved. Try exporting again.';
  if (job.auto_export_pending?.length) return 'Auto-export stopped before all clips were ready. Finished clips are still available. Try again to resume the remaining exports.';
  if (failedSourceStatus(job) === 'HD unavailable') return 'StreamFrame couldn’t retrieve a usable 720p or higher version. Try again later, use another HD source, or add the original recording.';
  if (reason.includes('limited to 5 minutes')) return 'This video is longer than 5 minutes. Add it again and choose a shorter range.';
  if (failedSourceStatus(job).includes('post-live')) return 'YouTube is still preparing the recording. Try again later.';
  if (failedSourceStatus(job).includes('unsupported')) return 'StreamFrame couldn’t import this format. For a short YouTube video, try Full video, or add a local copy you have permission to clip.';
  return 'StreamFrame couldn’t finish preparing this source. Try again, or add another video.';
}
const exportSourceTitle = (job?: Job) => job?.resolved_source?.title?.trim() ||
  (job && (linkPlatform(sourceText(job.source)) === 'youtube' ? 'YouTube video' :
    linkPlatform(sourceText(job.source)) === 'twitch' ? 'Twitch video' :
      sourceText(job.source).split(/[/\\]/).pop())) || 'Untitled source';

function sourceResolution(job: Job) {
  if (job.resolved_source) return job.resolved_source.is_live ? 'Live' : 'Finished video (VOD)';
  if (typeof job.source !== 'string' && job.source.type === 'file') return 'Local file';
  if (job.status === 'failed') return failedSourceStatus(job);
  if (job.status === 'cancelled') return 'Cancelled before source was confirmed';
  if (job.status === 'interrupted') return 'Interrupted before source was confirmed';
  if (job.status === 'succeeded') return 'Completed without source resolution metadata';
  if (job.status === 'cancelling') return 'Cancelling — source not confirmed';
  return 'Awaiting backend resolution';
}

function ReviewSourceDetails({ job }: { job: Job }) {
  const resolved = job.resolved_source;
  const source = typeof job.source === 'string' ? null : job.source;
  return <>
      <button type="button" className="source-facts-trigger" {...{ popovertarget: 'review-source-facts' }}>Source details</button>
      <section id="review-source-facts" className="source-facts" {...{ popover: 'auto' }} aria-labelledby="review-source-facts-title">
        <header className="source-facts-header">
          <h2 id="review-source-facts-title">Source details</h2>
          <button type="button" className="text-button" {...{ popovertarget: 'review-source-facts', popovertargetaction: 'hide' }}>Close</button>
        </header>
        <p className="source-facts-name">{exportSourceTitle(job)}</p>
        <dl>
          <div><dt>Source status</dt><dd>{sourceResolution(job)}</dd></div>
          {resolved && <>
            <div><dt>Platform</dt><dd>{resolved.platform}</dd></div>
            <div><dt>Selection</dt><dd>{resolved.selection}</dd></div>
            <div><dt>Source URL</dt><dd>{resolved.url}</dd></div>
          </>}
          {!resolved && source?.type === 'file' && <div><dt>File path</dt><dd>{source.path}</dd></div>}
          {job.capture_started_at && <div><dt>Capture started</dt><dd>{job.capture_started_at}</dd></div>}
          {source?.type === 'streamer' && <>
            <div><dt>Requested mode</dt><dd>{source.mode}</dd></div>
            <div><dt>Capture</dt><dd>{source.rolling
              ? `Rolling session: ${source.session_duration}s / ${source.window_duration}s windows`
              : source.duration === undefined ? 'Single sample: backend default' : `Single sample: ${source.duration}s`}</dd></div>
          </>}
        </dl>
        {source?.type === 'streamer' && <p className="muted small">Editor timestamps start at zero for the captured media.</p>}
      </section>
  </>;
}

function ReviewExports(props: React.ComponentProps<typeof ExportLibrary>) {
  const [open, setOpen] = useState(false);
  const count = props.files.filter((file) => file.jobId === props.sourceId).length;
  return <details className="review-export-history" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Export history <span className="muted">{count} finished</span></summary>
    {open && <ExportLibrary {...props} compact />}
  </details>;
}

function ExportLibrary({ jobs, files, busy, heading, sourceId, onFilter, onEdit, onDelete, compact, onDeleteAll }: {
  jobs: Job[]; files: ExportedVideo[]; busy: boolean; heading?: React.RefObject<HTMLHeadingElement>;
  sourceId?: string; onFilter?: (sourceId?: string) => void;
  onEdit?: (file: ExportedVideo) => void; onDelete: (source: Job, file: ExportedVideo) => void;
  compact?: boolean; onDeleteAll?: () => void;
}) {
  const filter = sourceId ?? 'all';
  const filtered = files.filter((file) => filter === 'all' || file.jobId === filter)
    .sort((a, b) => (exportTimestamp(b) || -Infinity) - (exportTimestamp(a) || -Infinity));
  const working = jobs.flatMap<{ job: Job; candidate?: Candidate; queued: boolean }>((job) => {
    const active = job.candidates.filter((candidate) =>
      ['queued', 'running', 'exporting'].includes(candidate.export_status ?? ''));
    if (active.length) return active.map((candidate) => ({ job, candidate, queued: candidate.export_status === 'queued' }));
    if (!jobWorking(job)) return [];
    if (job.auto_export_pending !== undefined && job.operation !== 'export') {
      return job.auto_export_pending.map((id) => ({ job, candidate: job.candidates.find((item) => item.id === id),
        queued: job.status !== 'exporting' || job.export_candidate_id !== id }));
    }
    return job.stage === 'export' || job.operation === 'export' ? [{
      job, candidate: job.candidates.find((item) => item.id === job.export_candidate_id), queued: job.status === 'queued',
    }] : [];
  });
  const pending = working.filter(({ job }) => filter === 'all' || filter === job.id);
  const sourceOptions = jobs.filter((job) => job.id === sourceId || files.some((file) => file.jobId === job.id)
    || working.some((entry) => entry.job.id === job.id)
    || job.candidates.some((candidate) => candidate.export_status === 'failed'));
  return <section id="exported-videos" className={`export-library${compact ? ' review-exports' : ''}`} aria-label={compact ? 'Exports from this source' : 'Exported videos'}>
    {compact ? <div className="section-heading">
      <h2>Exports from this source <span className="count">{filtered.length + pending.length}</span></h2>
      {filtered.some((file) => jobs[0]?.exports?.some((record) => record.filename === file.name)) &&
        <button className="quiet destructive" disabled={busy || jobs.some(jobWorking)} onClick={onDeleteAll}>Delete all exports</button>}
    </div> : <div className="page-heading"><div><h1 tabIndex={-1} ref={heading}>Exported videos <span className="count">{filtered.length + pending.length}</span></h1>
      <p className="muted">Watch your finished clips before you download and share them.</p></div>
      <label className="field">Show exports from<select aria-label="Show exports from" value={filter} onChange={(event) => onFilter?.(event.target.value === 'all' ? undefined : event.target.value)}>
        <option value="all">All sources</option>{sourceOptions.map((job) =>
          <option value={job.id} key={job.id}>{exportSourceTitle(job)}{formatJobDate(job) ? ` · ${formatJobDate(job)}` : ''}</option>)}
        {sourceId && !sourceOptions.some((job) => job.id === sourceId) && <option value={sourceId}>Selected source</option>}
      </select></label></div>}
    {jobs.filter((job) => filter === 'all' || filter === job.id).flatMap((job) =>
      job.candidates.filter((candidate) => candidate.export_status === 'failed').map((candidate) =>
        <details className="notice error" key={`${job.id}/${candidate.id}`}>
          <summary>Couldn’t export a clip from {exportSourceTitle(job)}. Previous exports are unchanged.</summary>
          <p>Open the source to try again. {candidate.export_error}</p>
        </details>))}
    {!filtered.length && !pending.length ? <p className="empty muted">{filter === 'all'
      ? 'No clips yet. Open a source, choose a moment, and select Export clip.'
      : 'No finished clips for this source yet.'}</p> :
      <ul className="export-grid">{pending.map(({ job, candidate, queued }) =>
        <li className="export-card pending-export" key={`${job.id}/${candidate?.id ?? 'pending'}`}>
          <div className="pending-export-media" role="status">
            <StudioIcon name="exports" /><strong>{queued ? 'Waiting to export' : 'Exporting clip'}</strong>
            {!queued && <progress aria-label={`Export in progress for ${exportSourceTitle(job)}`} />}
          </div>
          <div className="export-card-body"><h3>{candidate?.opening_title || exportSourceTitle(job)}</h3>
            {candidate && <p className="small muted">{time(candidate.start)} – {time(candidate.end)}</p>}
            <p className="small muted">Preview and download appear when the clip is ready.</p>
          </div>
        </li>)}
      {filtered.map((file, index) => {
        const job = jobs.find((item) => item.id === file.jobId);
        const candidate = job?.candidates.find((item) => item.id === file.candidateId);
        const canEdit = job && candidate && !job.deletion_pending && (!isRollingJob(job) || retainedSource(candidate));
        const record = file.provenance;
        const video = /\.(mp4|webm|m4v|mov)$/i.test(file.name);
        const title = exportSourceTitle(job);
        const stamp = exportTimestamp(file);
        return <li className="export-card" key={`${file.jobId}/${file.url}`}>
          {video && <div className="export-media">
            <video controls playsInline preload={index < 6 ? 'metadata' : 'none'} src={file.url}
              aria-label={`Exported video from ${title}`} onPlay={(event) => {
                const player = event.currentTarget;
                player.closest('.export-grid')?.querySelectorAll('video').forEach((other) => {
                  if (other !== player) other.pause();
                });
              }} onError={(event) => {
                const notice = event.currentTarget.parentElement?.querySelector<HTMLElement>('[role="status"]');
                if (notice) notice.hidden = false;
              }} />
            <p className="muted small" role="status" hidden>Preview unavailable. Try downloading the file.</p>
          </div>}
          <div className="export-card-body">
          <h3>{compact && record?.start !== undefined && record.end !== undefined
            ? `${time(record.start)} – ${time(record.end)}` : title}</h3>
          <p className="muted small">{Number.isFinite(stamp) && record?.created_at
            ? <>{record.date_from_file ? 'File date' : 'Exported'} <time dateTime={record.created_at}>{exportDate.format(stamp)}</time></>
            : 'Export date unavailable'}</p>
          <div className="actions"><a className="button primary" href={file.url} download={file.name}>Download</a>
            {canEdit && onEdit && <button disabled={busy} onClick={() => onEdit(file)}>Edit source</button>}</div>
          <div className="export-card-footer">
          <details className="export-details"><summary>File details</summary>
          <dl className="export-provenance">
            <div><dt>File</dt><dd>{file.name}</dd></div>
            <div><dt>Source job</dt><dd>{file.jobId}</dd></div>
            <div><dt>Candidate</dt><dd>{file.candidateId ?? 'Not supplied by backend'}</dd></div>
            {record?.revision !== undefined && <div><dt>Export revision</dt><dd>{record.revision}</dd></div>}
            {record?.window_index !== undefined && <div><dt>Source window</dt><dd>{record.window_index}</dd></div>}
            {record?.global_start !== undefined && record.global_end !== undefined &&
              <div><dt>Session clip range</dt><dd>{time(record.global_start)}–{time(record.global_end)}</dd></div>}
            {record?.start !== undefined && record.end !== undefined &&
              <div><dt>Export source range</dt><dd>{time(record.start)}–{time(record.end)}</dd></div>}
          </dl>
          <p className="muted small">{canEdit ? 'Edit source opens the current edits. Exporting again creates a new file.'
            : 'The editable source is no longer available. You can still download this clip.'}</p>
          </details>
          {job && !job.deletion_pending && job.exports?.some((entry) => entry.filename === file.name) &&
            <button className="quiet destructive" disabled={busy || jobWorking(job)}
              title={jobWorking(job) ? 'Available after this source finishes processing' : undefined}
              onClick={() => onDelete(job, file)}>Delete export</button>}
          </div>
          </div>
        </li>;
      })}</ul>}
  </section>;
}

function LiveSession({ job, onSaved, onEdit }: { job: Job; onSaved: () => Promise<void>; onEdit: (id: string) => void }) {
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
  const requestedDuration = typeof job.source !== 'string' && job.source.type !== 'file' ? job.source.session_duration : undefined;
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
      <div><dt>Media captured</dt><dd>{session ? time(session.global_time) : '—'}</dd></div>
      {requestedDuration !== undefined && <div><dt>Session limit</dt><dd>{time(requestedDuration)}</dd></div>}
    </dl>
    <p className="muted small">Automated clips are distinct from single samples awaiting manual export. Completed output links remain available after stop. Session-global times are relative to this session; window times identify captured ranges, not wall-clock coverage.</p>
    <p className="notice warning">Finite processing only: up to 30 minutes requested, not guaranteed continuous coverage. Backlog, missing source windows, 512 MiB transfer / 512 MiB combined temporary-output disk, or the 900 cumulative segment / 900 window limits may stop the session earlier. No automatic restart or budget reset. No detected or manual face crop means automated export is skipped (no_face), not a full-frame fallback. To recover a skipped window, select its retained candidate, set a face crop, then export manually. Session crop changes affect future windows only and do not automatically export earlier skipped windows.</p>
    {session?.warnings && describe(session.warnings) && <p className="notice warning" role="status">{describe(session.warnings)}</p>}
    {session?.windows?.some((window) => window.status === 'no_face') &&
      <section className="session-skipped" aria-label="Skipped no-face windows">
        <h3>Skipped windows · no face crop</h3>
        <ul>{session.windows.filter((window) => window.status === 'no_face').map((window) =>
          <li className="muted small" key={window.index}>Window {window.index}: automated export skipped (no_face).
            {' Open Review retained moments to correct its crop and export manually if source is available.'}
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
      {cropNotice && <StudioToast key={cropNotice} text={cropNotice} dismiss={() => setCropNotice('')} />}
      {!active && <p className="muted small">This session is not active. A future-window crop cannot change its completed outputs.</p>}
      <div className="actions"><button type="button" disabled={!active || !cropTouched || !!invalidCrop || cropSaving} onClick={() => void applyFaceCrop()}>{cropSaving ? 'Applying crop…' : 'Apply to future windows'}</button>
        <button type="button" disabled={!cropTouched || cropSaving} onClick={() => { setFaceDraft(session?.face_crop ?? null); setCropTouched(false); setCropError(''); setCropNotice(''); }}>Discard crop draft</button></div>
    </section>
    <h3>Completed automated clips ({outputs.length})</h3>
    {!outputs.length ? <p className="muted small">No completed clips yet. Session progress does not guarantee an output.</p> :
      <ul className="session-outputs">{outputs.map((output, index) => {
        const files = exportFiles(output, job.id);
        const candidate = job.candidates.find((item) => item.id === output.candidate_id);
        return <li key={`${output.filename}-${index}`}>
          <strong>{output.filename}</strong>
          <div className="muted small">Candidate: {output.candidate_id}</div>
          {output.window_start !== undefined && output.window_end !== undefined &&
            <p className="muted small">Window range: {time(output.window_start)}–{time(output.window_end)}</p>}
          {output.global_start !== undefined && output.global_end !== undefined &&
            <p className="muted small">Session clip range: {time(output.global_start)}–{time(output.global_end)}</p>}
          <p className="muted small">Framing unverified — review the detected or manual crop before sharing. An export is not proof of correct face framing.</p>
          <div className="actions">
            {files.map((file) => <a className="button" key={file.url} href={file.url} download={file.name}>Download automated clip ↓</a>)}
            {candidate && retainedSource(candidate) && <button type="button" onClick={() => onEdit(candidate.id)}>Edit this clip</button>}
          </div>
        </li>;
      })}</ul>}
  </section>;
}

interface EditorProps {
  momentNumber: number;
  job: Job;
  candidate: Candidate;
  onDirty: (dirty: boolean) => void;
  onBusy: (busy: boolean) => void;
  webcamDraft?: { crop: Rect | null };
  onWebcamDraft: (value: { crop: Rect | null } | undefined) => void;
  onPersisted: (job: Job) => void;
  onSaved: () => Promise<void>;
  rolling?: boolean;
  onExport: (jobId: string, candidateId: string, files: ExportFile[]) => void;
}

function PortraitPreview({ player, webcam, content }: {
  player: React.RefObject<HTMLVideoElement>; webcam: Rect; content?: Rect | null;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const video = player.current;
    const ctx = canvas.current?.getContext('2d');
    if (!video || !ctx) return;
    let frame = 0;
    const draw = () => {
      if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) return;
      ctx.fillStyle = '#141c22';
      ctx.fillRect(0, 0, 540, 960);
      const paint = (crop: Rect, top: number, height: number, fit: boolean) => {
        const w = crop.width * video.videoWidth, h = crop.height * video.videoHeight;
        if (!(w > 0 && h > 0)) return;
        const scale = (fit ? Math.min : Math.max)(540 / w, height / h);
        ctx.save();
        ctx.beginPath(); ctx.rect(0, top, 540, height); ctx.clip();
        ctx.drawImage(video, crop.x * video.videoWidth, crop.y * video.videoHeight, w, h,
          (540 - w * scale) / 2, top + (height - h * scale) / 2, w * scale, h * scale);
        ctx.restore();
      };
      paint(webcam, 0, 365, true);
      paint(content ?? { x: 0, y: 0, width: 1, height: 1 }, 365, 595, false);
      ctx.fillStyle = '#c9afff';
      ctx.fillRect(0, 363, 540, 4);
    };
    const tick = () => {
      draw();
      if (!video.paused && !video.ended) frame = requestAnimationFrame(tick);
    };
    const play = () => { cancelAnimationFrame(frame); tick(); };
    video.addEventListener('loadeddata', draw);
    video.addEventListener('seeked', draw);
    video.addEventListener('play', play);
    play();
    return () => {
      cancelAnimationFrame(frame);
      video.removeEventListener('loadeddata', draw);
      video.removeEventListener('seeked', draw);
      video.removeEventListener('play', play);
    };
  }, [player, webcam, content]);
  return <canvas ref={canvas} width="540" height="960" role="img" aria-label="Portrait framing preview: webcam above gameplay" />;
}

function CandidateEditor({ job, candidate, momentNumber, rolling = false, onDirty, onBusy, webcamDraft, onWebcamDraft, onPersisted, onSaved, onExport }: EditorProps) {
  const [base, setBase] = useState(candidate);
  const [clipDraft, setDraft] = useState<Candidate>({ ...candidate, words: candidate.words ?? [] });
  const sharedCrop = !rolling ? webcamDraft : undefined;
  const draft = sharedCrop ? { ...clipDraft, face_crop: sharedCrop.crop } : clipDraft;
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const titleWarning = useRef<HTMLDialogElement>(null);
  const openingTitleInput = useRef<HTMLInputElement>(null);
  const captionInput = useRef<HTMLInputElement>(null);
  const addWordButton = useRef<HTMLButtonElement>(null);
  const [current, setCurrent] = useState(candidate.start);
  const [duration, setDuration] = useState(0);
  const [aspect, setAspect] = useState<number | null>(null);
  const [dimensions, setDimensions] = useState<{ width: number; height: number } | null>(null);
  const [mediaError, setMediaError] = useState(false);
  const [cropView, setCropView] = useState<'both' | 'face_crop' | 'content_crop' | 'none'>('both');
  const [captions, setCaptions] = useState(true);
  const [loop, setLoop] = useState(false);
  const [trimZoom, setTrimZoom] = useState(false);
  const [inspector, setInspector] = useState<'framing' | 'captions' | 'opening' | null>(null);
  const [previewMode, setPreviewMode] = useState<'portrait' | 'source'>('portrait');
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [wordIndex, setWordIndex] = useState(0);
  const [precision, setPrecision] = useState(false);
  const drag = useRef<{ field: 'face_crop' | 'content_crop'; x: number; y: number; width: number; height: number; rect: Rect; resize: boolean } | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const clipPlayback = useRef(false);
  const [waitingExport, setWaitingExport] = useState(false);
  const submittedJob = useRef(job);
  const latestJob = useRef(job);
  latestJob.current = job;
  const clipChanges = changesBetween(base, clipDraft);
  const clipDirty = Object.keys(clipChanges).length > 0;
  const changes = sharedCrop ? { ...clipChanges, face_crop: sharedCrop.crop } : clipChanges;
  const dirty = clipDirty || !!sharedCrop;
  const validation = validate(draft);
  const candidateExporting = ['queued', 'running', 'exporting'].includes(candidate.export_status ?? '') ||
    (!rolling && job.status === 'exporting');
  const committing = saving || exporting;
  const locked = committing || candidateExporting || waitingExport;

  useEffect(() => { onDirty(clipDirty); }, [clipDirty, onDirty]);
  useEffect(() => { onBusy(committing); return () => onBusy(false); }, [committing, onBusy]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    if (clipDirty || committing) window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [clipDirty, committing]);
  useEffect(() => {
    // Polls may update scores/evidence, but must never overwrite local edits.
    if (!clipDirty && !locked) {
      setBase(candidate);
      setDraft({ ...candidate, words: candidate.words ?? [] });
    }
  }, [candidate, clipDirty, locked]);
  useEffect(() => {
    if (!waitingExport || submittedJob.current === job || candidateExporting) return;
    if (candidate.export_status === 'failed' || (!rolling && job.status === 'failed')) {
      setWaitingExport(false); setError(`Export failed: ${candidate.export_error || describe(job.error) || 'See job diagnostics.'}`);
    } else if (candidate.export_status === 'cancelled' || (!rolling && ['cancelled', 'interrupted'].includes(job.status))) {
      setWaitingExport(false); setNotice('Export stopped. Previous completed outputs remain available.');
    } else if (candidate.export_status === 'succeeded' || (!rolling && job.status === 'succeeded')) {
      setWaitingExport(false); setNotice('Export complete. Open View exports to review or download the rendered clip.');
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

  function editCrop(field: 'face_crop' | 'content_crop', crop: Rect | null) {
    if (field === 'face_crop' && !rolling) onWebcamDraft({ crop });
    else setDraft((old) => ({ ...old, [field]: crop }));
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
      if (sharedCrop) onWebcamDraft(undefined);
      onPersisted(persistedJob);
      onDirty(false);
      setNotice('Changes saved.');
      await onSaved().catch(() => { /* The regular poll will retry the job list. */ });
      return true;
    } catch (err) { setError(message(err)); return false; }
    finally { setSaving(false); }
  }

  async function exportClip(withoutTitle = false) {
    if (!draft.face_crop) { setError('Trace the full webcam frame in Framing before exporting.'); return; }
    if (!withoutTitle && !draft.opening_title?.trim()) { titleWarning.current?.showModal(); return; }
    setExporting(true); setError(''); setNotice('');
    try {
      if (!(await save())) return;
      const result = await startExport(job.id, candidate.id);
      submittedJob.current = latestJob.current;
      onExport(job.id, candidate.id, result.files);
      setWaitingExport(result.pending);
      setNotice(result.pending
        ? 'Export queued. Job status and output links update automatically; any links already listed may be from previous exports.'
        : result.files.length ? 'Export ready to download.' : 'Export requested. Waiting for the backend to publish output filenames.');
      await onSaved().catch(() => { /* Polling retries; an accepted export did not fail. */ });
    } catch (err) {
      setError(`Could not confirm the export: ${message(err)}. Check the export status before retrying.`);
      await onSaved().catch((refreshError) => setError(`Could not confirm the export or refresh its status: ${message(refreshError)}. Check Exports when the connection returns.`));
    }
    finally { setExporting(false); }
  }

  const activeWords = draft.words.filter((word) => current >= word.start && current < word.end);
  const visibleRects = (['face_crop', 'content_crop'] as const).filter((field) => cropView === 'both' || cropView === field);
  const timelineEnd = duration || draft.words.reduce((end, word) => Math.max(end, word.end), Number.isFinite(draft.end) ? Math.max(draft.end, 1) : 1);
  const clipLength = draft.end - draft.start;
  const canZoomTrim = Number.isFinite(clipLength) && clipLength > 0 && draft.start >= 0 && draft.end <= timelineEnd && clipLength < timelineEnd / 4;
  const zoomedTrim = trimZoom && canZoomTrim;
  const seekMin = zoomedTrim ? Math.max(0, Math.floor((draft.start - clipLength) * 100) / 100) : 0;
  const seekMax = zoomedTrim ? Math.min(timelineEnd, Math.ceil((draft.end + clipLength) * 100) / 100) : timelineEnd;
  const timelinePosition = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(100, (value - seekMin) / (seekMax - seekMin) * 100)) : 0;
  const captionIndex = Math.min(wordIndex, Math.max(0, draft.words.length - 1));
  const selectedWord = draft.words[captionIndex];
  const conflict = dirty && Object.keys(changes).some((field) =>
    JSON.stringify(candidate[field]) !== JSON.stringify(base[field]));
  const portrait = previewMode === 'portrait' && !!draft.face_crop && !mediaError;
  const tools = [
    ['framing', 'Framing', 'Webcam and screen'],
    ['captions', 'Captions', 'Words and timing'],
    ['opening', 'Opening title', 'Text at the start'],
  ] as const;

  return <section className="editor panel" aria-label="Candidate editor" aria-busy={locked} tabIndex={-1}>
    <dialog ref={titleWarning} className="studio-dialog" aria-labelledby="missing-title-heading" aria-describedby="missing-title-description">
      <h2 id="missing-title-heading">Export without an opening title?</h2>
      <p id="missing-title-description" className="muted">This clip won’t show a title at the start. Add one or export the clip as is.</p>
      <div className="actions">
        <button autoFocus onClick={() => titleWarning.current?.close()}>Cancel</button>
        <button onClick={() => { titleWarning.current?.close(); setInspector('opening'); requestAnimationFrame(() => openingTitleInput.current?.focus()); }}>Add a title</button>
        <button onClick={() => { titleWarning.current?.close(); void exportClip(true); }}>Export without title</button>
      </div>
    </dialog>
    <div className="section-heading editor-heading"><h2>Moment {momentNumber} {dirty && <span className="save-state unsaved">Unsaved changes</span>}</h2>
      <div className="actions">{dirty && <><button className="text-button" disabled={locked} onClick={() => {
        setBase(candidate); setDraft({ ...candidate, words: candidate.words ?? [] });
        if (sharedCrop) onWebcamDraft(undefined);
        setError(''); setNotice('Changes discarded.');
      }}>Discard</button><button disabled={locked || !!validation} onClick={() => void save()}>{saving ? 'Saving…' : 'Save changes'}</button></>}
        <button className="primary" disabled={locked || !!validation} onClick={() => void exportClip()}>{exporting ? 'Submitting…' : candidate.export_status === 'queued' ? 'Export queued' : candidateExporting || waitingExport ? 'Rendering…' : 'Export clip'}</button></div>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <StudioToast key={notice} text={notice} dismiss={() => setNotice('')} />}
    {validation && <p className="notice warning" role="status">{validation}</p>}
    {conflict && <p className="notice warning" role="alert">The server also changed: {Object.keys(changes).filter((field) => JSON.stringify(candidate[field]) !== JSON.stringify(base[field])).map((field) => field.replace(/_/g, ' ')).join(', ')}. Saving applies your edits to those fields; Discard loads the server version.</p>}
    {candidate.warnings ? <p className="notice warning" role="status">{describe(candidate.warnings)}</p> : null}
    {rolling && <p className="notice success">Retained window {candidate.window_index ?? '—'} source · timestamps are relative to that window. Corrections can be saved and re-exported during capture or after stop; previous downloads are unchanged.</p>}
    {(candidateExporting || waitingExport) && <p className="export-activity" role="status"><progress aria-label="Selected moment export progress" />Making your clip. It’ll appear in Exports when it’s ready.</p>}
    {candidate.export_error && <details className="notice error"><summary>This clip couldn’t export. Your edits are still saved.</summary><p>{candidate.export_error}</p></details>}
    <div className="editor-fields">
      <div className="editor-grid">
        <div className="preview-column">
          <h3 className="sr-only">Clip preview</h3>
          {portrait && draft.face_crop && <div className="portrait-stage">
            <PortraitPreview player={video} webcam={draft.face_crop} content={draft.content_crop} />
          </div>}
          <div className="video-stage" hidden={portrait}>
            <div className="video-frame" style={aspect ? { maxWidth: `calc(var(--stage-height) * ${aspect})` } : undefined}>
              <video ref={video} src={rolling
                ? `${jobPath(job.id)}/candidates/${encodeURIComponent(candidate.id)}/media`
                : `${jobPath(job.id)}/media`} controls={!portrait} playsInline preload="auto"
                aria-label="Source video preview" onLoadedMetadata={(event) => {
                  setMediaError(false);
                  const { videoWidth, videoHeight } = event.currentTarget;
                  if (videoWidth && videoHeight) {
                    setAspect(videoWidth / videoHeight);
                    setDimensions({ width: videoWidth, height: videoHeight });
                  }
                  setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0);
                  event.currentTarget.currentTime = draft.start;
                }} onVolumeChange={(event) => {
                  setMuted(event.currentTarget.muted);
                  setVolume(event.currentTarget.volume);
                }} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)}
                onError={() => { setMediaError(true); setPlaying(false); }} onSeeking={(event) => {
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
                  }} onPointerDown={(event) => {
                    if (locked || mediaError || inspector !== 'framing' || event.button !== 0) return;
                    const box = event.currentTarget.parentElement?.getBoundingClientRect();
                    if (!box?.width || !box.height) return;
                    event.preventDefault();
                    event.currentTarget.setPointerCapture(event.pointerId);
                    drag.current = { field, x: event.clientX, y: event.clientY, width: box.width, height: box.height,
                      rect, resize: (event.target as HTMLElement).classList.contains('crop-resize') };
                  }} onPointerMove={(event) => {
                    const move = drag.current;
                    if (!move || move.field !== field || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
                    const dx = (event.clientX - move.x) / move.width;
                    const dy = (event.clientY - move.y) / move.height;
                    const { rect: origin } = move;
                    const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
                    const next = move.resize
                      ? { ...origin, width: clamp(origin.width + dx, 0.01, 1 - origin.x), height: clamp(origin.height + dy, 0.01, 1 - origin.y) }
                      : { ...origin, x: clamp(origin.x + dx, 0, 1 - origin.width), y: clamp(origin.y + dy, 0, 1 - origin.height) };
                    editCrop(field, next);
                  }} onLostPointerCapture={() => { drag.current = null; }}><span>{field === 'face_crop' ? 'Webcam' : 'Content'}</span>
                    {inspector === 'framing' && !mediaError && !locked && <i className="crop-resize" />}</div> : null;
                })}
                {captions && activeWords.length > 0 && <div className="caption-preview">{activeWords.map(captionText).join(' ')}</div>}
              </div>
            </div>
          </div>
          {mediaError && <p className="notice warning" role="alert">Can’t play this source preview. {rolling ? 'This part of the recording may no longer be saved.' : 'Try reloading the source.'}</p>}
          <div className="preview-toolbar">
            <button className="play-clip" type="button" disabled={mediaError || !!validation} onClick={() => {
              const player = video.current;
              if (!player) return;
              if (!player.paused) { player.pause(); return; }
              seek(draft.start);
              clipPlayback.current = true;
              void player.play().catch((err) => setError(`Playback failed: ${message(err)}`));
            }}>{playing ? 'Pause clip' : 'Play clip'}</button>
            <button type="button" className="text-button" disabled={mediaError} onClick={() => {
              const player = video.current;
              if (!player) return;
              if (player.muted || player.volume === 0) { if (player.volume === 0) player.volume = 1; player.muted = false; }
              else player.muted = true;
            }}>{muted || volume === 0 ? 'Unmute' : 'Mute'}</button>
            <label className="preview-volume"><span className="sr-only">Playback volume</span><input type="range" min="0" max="1" step="0.05" value={muted ? 0 : volume}
              disabled={mediaError} onChange={(event) => {
                if (video.current) { video.current.volume = Number(event.target.value); video.current.muted = false; }
              }} /></label>
            <label className="check"><input type="checkbox" checked={loop} onChange={(event) => setLoop(event.target.checked)} /> Loop</label>
            {!portrait && <label className="check"><input type="checkbox" checked={captions} onChange={(event) => setCaptions(event.target.checked)} /> Captions</label>}
          </div>
          <div className="preview-switch" role="group" aria-label="Preview view">
            <button type="button" aria-pressed={portrait} disabled={!draft.face_crop || mediaError}
              onClick={() => setPreviewMode('portrait')}>Portrait framing</button>
            <button type="button" aria-pressed={!portrait} onClick={() => setPreviewMode('source')}>Source video</button>
          </div>
          <div className="timeline-section">
            <div className="section-heading"><h3>Clip timeline</h3>
              <span className="timecode">{time(current)} / {time(duration)}</span>
              {canZoomTrim && <button className="text-button" type="button" onClick={() => setTrimZoom(!zoomedTrim)}>{zoomedTrim ? 'Show full source' : 'Zoom to clip'}</button>}</div>
            <div className="timeline-scrubber">
              <div className="timeline-track" aria-hidden="true"><div className="timeline-selection" style={{
                left: `${timelinePosition(draft.start)}%`,
                width: `${Math.max(0, timelinePosition(draft.end) - timelinePosition(draft.start))}%`,
              }} /></div>
              <label className="sr-only" htmlFor="playhead">Seek source video</label>
              <input id="playhead" type="range" min={seekMin} max={seekMax} step="0.01" aria-valuetext={time(current)}
                value={Math.max(seekMin, Math.min(current, seekMax))} disabled={mediaError} onChange={(event) => { clipPlayback.current = false; seek(Number(event.target.value)); }} />
            </div>
            <div className="timeline-ticks" aria-hidden="true"><span>{time(seekMin)}</span><span className="timeline-legend">Selected clip</span><span>{time(seekMax)}</span></div>
            <fieldset className="trim-fields" disabled={locked}><legend className="sr-only">Trim clip</legend>
            <div className="time-inputs"><NumberField label="Clip start (s)" value={draft.start} min={0} step={0.01} onChange={(start) => setDraft({ ...draft, start })} />
              <NumberField label="Clip end (s)" value={draft.end} min={0} step={0.01} onChange={(end) => setDraft({ ...draft, end })} />
              <div className="duration"><span className="muted small">Duration</span><strong>{Number.isFinite(draft.end - draft.start) ? (draft.end - draft.start).toFixed(2) : '—'}s</strong></div></div>
            <details className="trim-adjust"><summary>Adjust trim with sliders</summary><div className="trim-ranges">
              <label className="field">Adjust trim start<input type="range" min="0" max={timelineEnd} step="0.01"
                value={Number.isFinite(draft.start) ? draft.start : 0} onChange={(event) => {
                  const start = Number(event.target.value);
                  if (start < draft.end) { setDraft((old) => ({ ...old, start })); clipPlayback.current = false; if (!mediaError) seek(start); }
                }} /></label>
              <label className="field">Adjust trim end<input type="range" min="0" max={timelineEnd} step="0.01"
                value={Number.isFinite(draft.end) ? draft.end : timelineEnd} onChange={(event) => {
                  const end = Number(event.target.value);
                  if (end > draft.start) { setDraft((old) => ({ ...old, end })); clipPlayback.current = false; if (!mediaError) seek(end); }
                }} /></label>
            </div></details>
            </fieldset>
            {duration > 0 && draft.end > duration + 0.1 && <p className="notice warning">Clip end exceeds the source video duration.</p>}
          </div>
          {!mediaError && dimensions && Math.min(dimensions.width, dimensions.height) < 720 &&
            <p className="notice warning">Source quality: {dimensions.width} × {dimensions.height}. The Reel exports at 1080 × 1920, but this recording may look soft. Use a sharper source if you have one.</p>}
          <details className="evidence"><summary>Why this moment?{Number.isFinite(candidate.score) && <span className="moment-score">Score {candidate.score.toFixed(2)}</span>}</summary>
            <p className="muted small">Source range: {draft.start.toFixed(2)}–{draft.end.toFixed(2)} seconds. Scores are backend ranking values, not probabilities or verified quality ratings.</p>
            <pre>{describe(candidate.evidence) || 'No evidence was supplied by the backend.'}</pre></details>
        </div>
        <aside className="inspector" aria-labelledby="edit-clip-heading">
          <h3 className="inspector-title" id="edit-clip-heading">Edit clip</h3>
          <div className="inspector-nav" role="group" aria-label="Editing tools">
            {tools.map(([tool, label, description]) => <button key={tool} type="button" aria-label={label} aria-expanded={inspector === tool} aria-controls="clip-correction-fields" onClick={() => {
              setInspector(inspector === tool ? null : tool);
              if (tool === 'framing' && inspector !== tool) setPreviewMode('source');
            }}><span>{label}</span><small>{description}</small></button>)}
          </div>
          <p className="inspector-hint muted small">{portrait ? 'Framing only. The render includes branding, title and captions.' : !draft.face_crop && !mediaError ? 'Set the webcam frame to enable portrait framing and export.' : ''}</p>
        <fieldset id="clip-correction-fields" className="editor-fields" disabled={locked} hidden={!inspector}><legend className="sr-only">Correction fields</legend>
        {inspector === 'opening' && <section className="crop-column">
          <label className="field">Opening title (optional)
            <input ref={openingTitleInput} maxLength={60} value={draft.opening_title ?? ''} placeholder="e.g. One button changed everything"
              onChange={(event) => {
                const opening_title = event.target.value;
                setDraft((old) => ({ ...old, opening_title }));
              }} />
          </label>
          <p className="muted small">{(draft.opening_title ?? '').length}/60 characters. Shown for the first 2.4 seconds over the playing clip.</p>
        </section>}
        {inspector === 'framing' && <div className="crop-column">
          <label className="field">Preview guides<select value={cropView} onChange={(event) => setCropView(event.target.value as typeof cropView)}>
            <option value="both">Webcam + screen</option><option value="face_crop">Webcam only</option><option value="content_crop">Screen only</option><option value="none">Hide guides</option>
          </select></label>
          <p className="muted small"><strong>Export:</strong> 38% webcam on top, 62% screen below, with a divider.</p>
          <p className="muted small">Outline the full webcam frame. Resize the guide or enter exact values below.</p>
          <p className="muted small">{rolling ? 'Framing applies to this retained window’s moment.' : 'Saving the webcam crop applies it to all moments from this source.'}</p>
          {!draft.face_crop && <p className="notice warning">No webcam crop is set. Enable the guide and draw around the webcam before exporting.</p>}
          {(['face_crop', 'content_crop'] as const).map((field) => <CropControls key={field}
            label={field === 'face_crop' ? 'Webcam crop' : 'Content crop'} value={draft[field]}
            color={field === 'face_crop' ? 'purple' : 'teal'}
            emptyHint={field === 'face_crop' ? 'Enable to draw the webcam guide.' : 'The screen panel uses the center of the source frame.'}
            onChange={(rect) => editCrop(field, rect)} />)}
        </div>}
      {inspector === 'captions' && <section className="transcript">
        <h3>Transcript & captions</h3>
        <p className="muted small">Correct the caption text without changing the audio. Trim does not shift word timestamps.</p>
        {selectedWord ? <>
          <label className="field">Transcript word
            <select value={captionIndex} onChange={(event) => {
              const index = Number(event.target.value);
              setWordIndex(index); clipPlayback.current = false;
              if (!mediaError) seek(draft.words[index].start);
            }}>
              {draft.words.map((word, index) => <option value={index} key={index}>
                {`${time(word.start)} · ${captionText(word) || 'Empty word'}${word.end <= draft.start || word.start >= draft.end ? ' (outside clip)' : ''}`}
              </option>)}
            </select>
          </label>
          <label className="field">Caption text<input ref={captionInput} aria-label={`Word ${captionIndex + 1} caption correction`} value={captionText(selectedWord)}
            onChange={(event) => editWord(captionIndex, { correction: event.target.value })} /></label>
          {(selectedWord.end <= draft.start || selectedWord.start >= draft.end) && <p className="muted small">This word is outside the clip.</p>}
          {selectedWord.correction !== undefined && selectedWord.text && <button type="button" className="text-button" onClick={() => editWord(captionIndex, { correction: undefined })}>Restore original: {selectedWord.text}</button>}
          <button className="precision-toggle" type="button" aria-expanded={precision} onClick={() => setPrecision((old) => !old)}>Word timing & removal</button>
          {precision && <div className="word-precision">
            <div className="crop-inputs">
              <NumberField label={`Word ${captionIndex + 1} start`} value={selectedWord.start} min={0} step={0.01} onChange={(start) => editWord(captionIndex, { start })} />
              <NumberField label={`Word ${captionIndex + 1} end`} value={selectedWord.end} min={0} step={0.01} onChange={(end) => editWord(captionIndex, { end })} />
            </div>
            <button type="button" className="quiet destructive" onClick={() => {
              setDraft((old) => ({ ...old, words: old.words.filter((_, index) => index !== captionIndex) }));
              setWordIndex(captionIndex);
              requestAnimationFrame(() => (captionInput.current ?? addWordButton.current)?.focus());
            }}>Remove word</button>
          </div>}
        </> : <p className="muted empty">No transcript supplied. Add a word to create captions.</p>}
        <div className="summary-footer"><button ref={addWordButton} type="button" onClick={() => {
          const last = draft.words[draft.words.length - 1];
          const start = last ? Math.max(last.end, draft.start) : draft.start;
          setWordIndex(draft.words.length);
          setDraft((old) => ({ ...old, words: [...old.words, { start, end: start + 0.3, text: '' }] }));
          requestAnimationFrame(() => captionInput.current?.focus());
        }}>Add word</button></div>
      </section>}
        </fieldset>
        </aside>
      </div>
    </div>
  </section>;
}

function NumberField({ label, value, min, max, step, onChange }: {
  label: string; value: number; min?: number; max?: number; step: number;
  onChange: (value: number) => void;
}) {
  return <label className="field"><span>{label}</span>
    <input type="number" value={Number.isFinite(value) ? value : ''} min={min} max={max} step={step}
      onChange={(event) => onChange(event.target.value === '' ? Number.NaN : event.target.valueAsNumber)} /></label>;
}

function CropControls({ label, value, onChange, color, emptyHint = 'No manual crop. Automatic framing will be used.' }: {
  label: string; value?: Rect | null; onChange: (rect: Rect | null) => void; color: string; emptyHint?: string;
}) {
  return <div className={`crop-controls ${color}`}>
    <div className="section-heading"><h4>{label}</h4>
      <label className="check"><input type="checkbox" checked={!!value} onChange={(event) =>
        onChange(event.target.checked ? { x: 0, y: 0, width: 1, height: 1 } : null)} /> Enable</label></div>
    {value ? <><div className="crop-inputs">{(['x', 'y', 'width', 'height'] as const).map((key) =>
      <NumberField key={key} label={`${label} ${key}`} value={value[key]} min={0} max={1} step={0.01}
        onChange={(next) => onChange({ ...value, [key]: next })} />)}</div>
      <button type="button" className="text-button" onClick={() => onChange({ x: 0, y: 0, width: 1, height: 1 })}>Reset to full frame</button></> :
      <p className="muted small">{emptyHint}</p>}
  </div>;
}
