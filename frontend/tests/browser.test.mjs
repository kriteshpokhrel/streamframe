import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { once } from 'node:events';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { chromium } from 'playwright';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.dirname(frontend);
const python = process.env.STREAMFRAME_TEST_PYTHON || path.join(repo, '.venv/bin/python');
const dist = path.resolve(frontend, process.env.STREAMFRAME_BROWSER_DIST || 'dist');

async function unusedPort() {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function until(read, predicate, description, timeout = 45000) {
  const deadline = Date.now() + timeout;
  let latest;
  while (Date.now() < deadline) {
    try {
      latest = await read();
    } catch (error) { latest = error.message; }
    if (predicate(latest)) return latest;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out: ${description}. Last result: ${JSON.stringify(latest)}`);
}

async function checkExportLibrary(page, context, origin, sourceMedia) {
  const normal = {
    id: 'library-normal', source: { type: 'url', url: 'https://youtube.com/watch?v=fixture', start: 0, end: 2 },
    status: 'failed', progress: 1, error: 'Bounded YouTube VOD acquisition is not supported', exports: [],
    candidates: [{ id: 'normal-first', start: 0, end: 2, score: 1, words: [] }],
  };
  const rolling = {
    id: 'library-rolling', source: { type: 'url', url: 'https://twitch.tv/fixture', rolling: true },
    status: 'cancelled', progress: 1, exports: [
      { candidate_id: 'retained', filename: 'historical.mp4', revision: 1, window_index: 2,
        global_start: 60, global_end: 62 },
      { candidate_id: 'expired', filename: 'source-expired.mp4', revision: 2 },
      { filename: 'unassigned.mp4' },
    ],
    candidates: [
      { id: 'expired', start: 0, end: 2, score: 1, words: [], editable: false },
      { id: 'retained', start: 0, end: 2, score: 2, words: [], editable: true,
        source_media_url: '/api/jobs/library-rolling/candidates/retained/media', revision: 4 },
    ],
  };
  let diagnosticsStatus = 200;
  let exportRequests = 0;
  const diagnosticRequests = [];
  const intercept = async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/jobs') return route.fulfill({ json: { jobs: [normal, rolling] } });
    if (pathname === '/api/jobs/library-normal/candidates/normal-first/export' && request.method() === 'POST') {
      exportRequests++;
      normal.status = 'exporting';
      normal.error = null;
      const attempt = exportRequests;
      setTimeout(() => {
        normal.status = attempt === 1 ? 'failed' : 'succeeded';
        normal.error = attempt === 1 ? 'Render failed in fixture' : null;
        if (attempt > 1) normal.exports.push({ candidate_id: 'normal-first', filename: 'normal-export.mp4', revision: 1 });
      }, 1200);
      return route.fulfill({ status: 202, json: normal });
    }
    if (pathname.endsWith('/diagnostics')) {
      diagnosticRequests.push(pathname);
      return route.fulfill({ status: diagnosticsStatus,
        json: diagnosticsStatus === 200 ? { text: 'Sanitized job library-normal\nEncoder failed: fixture.' }
          : { detail: 'Diagnostics temporarily unavailable' } });
    }
    if (pathname.endsWith('/media') || pathname.includes('/exports/')) {
      return route.fulfill({ path: sourceMedia, contentType: 'video/mp4' });
    }
    if (pathname === '/api/jobs/library-normal') return route.fulfill({ json: normal });
    if (pathname === '/api/jobs/library-rolling') return route.fulfill({ json: rolling });
    return route.fulfill({ status: 404, json: { detail: 'Unsupported library fixture request' } });
  };
  await context.route(`${origin}/api/jobs**`, intercept);
  try {
    await page.reload();
    const gallery = page.getByRole('region', { name: 'Exported videos', exact: true });
    await gallery.getByRole('heading', { name: 'historical.mp4', exact: true }).waitFor();
    assert.equal(await gallery.locator('.export-card').count(), 3);
    await page.getByRole('heading', { name: 'Source library', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Cancel job', exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Resume job', exact: true }).count(), 1);
    await page.getByLabel('Source status', { exact: true }).getByText(
      'Source status: Source acquisition unsupported — see job error', { exact: true }).waitFor();
    const diagnostics = page.getByRole('region', { name: 'Diagnostics for job library-normal', exact: true });
    await page.evaluate(() => {
      window.copiedDiagnostics = [];
      Object.defineProperty(navigator, 'clipboard', { configurable: true,
        value: { writeText: async (value) => { window.copiedDiagnostics.push(value); } } });
    });
    await diagnostics.getByRole('button', { name: 'Copy diagnostics', exact: true }).click();
    await diagnostics.getByRole('status').filter({ hasText: 'Diagnostics copied for job library-normal.' }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.copiedDiagnostics),
      ['Sanitized job library-normal\nEncoder failed: fixture.']);
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true,
      value: { writeText: async () => { throw new Error('Permission denied'); } } }));
    await diagnostics.getByRole('button', { name: 'Copy diagnostics', exact: true }).click();
    await diagnostics.getByRole('status').filter({ hasText: 'Clipboard unavailable.' }).waitFor();
    const diagnosticText = diagnostics.getByRole('textbox');
    assert.equal(await diagnosticText.inputValue(), 'Sanitized job library-normal\nEncoder failed: fixture.');
    await until(() => diagnosticText.evaluate((input) =>
      document.activeElement === input && input.selectionEnd - input.selectionStart === input.value.length),
    Boolean, 'fallback is focused and selected');
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }));
    await diagnostics.getByRole('button', { name: 'Copy diagnostics', exact: true }).click();
    await diagnostics.getByRole('status').filter({ hasText: 'Clipboard unavailable.' }).waitFor();
    diagnosticsStatus = 503;
    await diagnostics.getByRole('button', { name: 'Copy diagnostics', exact: true }).click();
    await diagnostics.getByRole('alert').filter({ hasText: 'Diagnostics temporarily unavailable' }).waitFor();
    assert.ok(diagnosticRequests.every((pathname) => pathname === '/api/jobs/library-normal/diagnostics'));
    assert.equal(await diagnostics.getByRole('button', { name: 'Copy diagnostics' }).isEnabled(), true);
    normal.error = 'YouTube post_live processing is unavailable';
    await page.getByLabel('Source status', { exact: true }).getByText(
      'Source status: YouTube post-live processing unavailable — see job error', { exact: true }).waitFor();

    const normalEditor = page.getByRole('region', { name: 'Candidate editor', exact: true });
    await normalEditor.getByRole('button', { name: 'Export clip ↗', exact: true }).click();
    await normalEditor.getByRole('button', { name: 'Exporting…', exact: true }).waitFor();
    assert.equal(await normalEditor.getByRole('button', { name: 'Exporting…', exact: true }).isDisabled(), true);
    await until(() => page.getByRole('button', { name: 'Cancel job', exact: true }).isEnabled(), Boolean,
      'Active normal export remains cancellable while candidate controls are locked');
    await normalEditor.getByRole('alert').filter({ hasText: 'Export failed:' }).waitFor();
    assert.equal(exportRequests, 1, 'Export failure unlocks the editor without silently retrying');
    await normalEditor.getByRole('button', { name: 'Export clip ↗', exact: true }).click();
    await gallery.getByRole('heading', { name: 'normal-export.mp4', exact: true }).waitFor();
    await normalEditor.getByRole('status').filter({ hasText: 'Export complete.' }).waitFor();
    assert.equal(exportRequests, 2);
    await gallery.getByLabel('Show exports from', { exact: true }).selectOption('library-normal');
    assert.equal(await gallery.locator('.export-card').count(), 1);
    await gallery.getByRole('heading', { name: 'normal-export.mp4', exact: true }).waitFor();
    await gallery.getByLabel('Show exports from', { exact: true }).selectOption('library-rolling');
    const historical = gallery.locator('.export-card').filter({ has: page.getByRole('heading', { name: 'historical.mp4', exact: true }) });
    const expired = gallery.locator('.export-card').filter({ has: page.getByRole('heading', { name: 'source-expired.mp4', exact: true }) });
    assert.equal(await expired.getByRole('button', { name: 'Open edit source / re-export' }).count(), 0);
    await expired.getByText('Edit source unavailable.', { exact: false }).waitFor();
    assert.match(await historical.innerText(), /Export revision\s+1/);
    await historical.getByText('Preview exported video', { exact: true }).click();
    const preview = historical.getByLabel('Exported video historical.mp4', { exact: true });
    await preview.evaluate((video) => { video.load(); });
    await until(() => preview.evaluate((video) => video.readyState), (value) => value >= 2,
      'gallery preview decodes real video');
    await preview.evaluate((video) => { video.muted = true; return video.play(); });
    await until(() => preview.evaluate((video) => video.currentTime), (value) => value > 0.1,
      'gallery preview actually plays');
    await preview.evaluate((video) => video.pause());
    const downloadEvent = page.waitForEvent('download');
    await historical.getByRole('link', { name: 'Download video ↓', exact: true }).click();
    assert.equal((await downloadEvent).suggestedFilename(), 'historical.mp4');
    await historical.getByRole('button', { name: 'Open edit source / re-export', exact: true }).click();
    const editor = page.getByRole('region', { name: 'Candidate editor', exact: true });
    await editor.waitFor();
    await until(() => editor.getByLabel('Source video preview', { exact: true }).getAttribute('src'),
      (src) => /library-rolling\/candidates\/retained\/media$/.test(src),
      'gallery opens the owning job and non-first retained candidate');
    assert.equal(await page.getByRole('button', { name: 'Resume job', exact: true }).count(), 0,
      'Stopped rolling sources cannot resume');
    await until(() => editor.evaluate((element) => document.activeElement === element), Boolean,
      'Opening edit source moves keyboard focus to the matching editor');
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
        `Gallery and editor fit a ${width}px viewport: ${JSON.stringify(await page.evaluate(() =>
          [...document.querySelectorAll('body *')].filter((element) =>
            element.getBoundingClientRect().right > window.innerWidth + 1).slice(0, 15).map((element) =>
            ({ tag: element.tagName, class: element.className, width: element.getBoundingClientRect().width }))))}`);
    }
    await page.reload();
    await gallery.getByRole('heading', { name: 'historical.mp4', exact: true }).waitFor();
    assert.equal(await gallery.locator('.export-card').count(), 4, 'Exports survive browser reload from backend records');
    await page.setViewportSize({ width: 1440, height: 1000 });
  } finally {
    await context.unroute(`${origin}/api/jobs**`, intercept);
  }
}

async function checkRollingSession(page, context, origin, sourceMedia) {
  let liveJob = null;
  const submissions = [];
  const cropPatches = [];
  const corrections = [];
  const reexports = [];
  let cancellations = 0;
  let deletions = 0;
  const intercept = async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/jobs' && request.method() === 'POST') {
      const payload = request.postDataJSON();
      submissions.push(payload);
      liveJob = {
        id: 'rolling-browser-fixture', source: payload.source, status: 'running',
        stage: 'capture', progress: 0.1, candidates: [{
          id: 'readonly-window-candidate', start: 5, end: 15, score: 2,
          evidence: ['Read-only rolling-window evidence.'],
          words: [{ start: 5, end: 6, text: 'Original rolling caption' }],
          face_crop: null, content_crop: null,
        }], exports: [],
        resolved_source: { url: 'https://twitch.tv/fixture_streamer', platform: 'twitch',
          title: 'Offline simulated live session', is_live: true, selection: 'live' },
        live_session: { phase: 'capturing', windows_seen: 1, windows_processed: 1,
          global_time: 60, warnings: ['Fixture gap warning: source windows are not guaranteed.',
            'Window 1: no face crop; set a manual crop for future windows.'],
          windows: [{ index: 1, start: 0, end: 60, status: 'no_face',
            warning: 'No detected or manual face crop.' }] },
      };
      return route.fulfill({ status: 202, json: liveJob });
    }
    if (pathname === '/api/jobs' && request.method() === 'GET') {
      return route.fulfill({ json: { jobs: liveJob ? [liveJob] : [] } });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture/cancel' && request.method() === 'POST') {
      cancellations++;
      liveJob = { ...liveJob, status: 'cancelled', live_session: { ...liveJob.live_session, phase: 'stopped' } };
      return route.fulfill({ status: 202, json: liveJob });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture/live-crop' && request.method() === 'PATCH') {
      const payload = request.postDataJSON();
      cropPatches.push(payload);
      liveJob = { ...liveJob, live_session: { ...liveJob.live_session, face_crop: payload.face_crop } };
      return route.fulfill({ json: liveJob });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture/candidates/retained-window-candidate/media' && request.method() === 'GET') {
      return route.fulfill({ path: sourceMedia, contentType: 'video/mp4' });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture/candidates/retained-window-candidate' && request.method() === 'PATCH') {
      const patch = request.postDataJSON();
      corrections.push({ status: liveJob.status, patch });
      liveJob = { ...liveJob, candidates: liveJob.candidates.map((candidate) =>
        candidate.id === 'retained-window-candidate' ? { ...candidate, ...patch, revision: (candidate.revision ?? 0) + 1 } : candidate) };
      return route.fulfill({ json: liveJob });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture/candidates/retained-window-candidate/export' && request.method() === 'POST') {
      reexports.push(liveJob.status);
      const sequence = reexports.length;
      liveJob = { ...liveJob, candidates: liveJob.candidates.map((candidate) =>
        candidate.id === 'retained-window-candidate' ? { ...candidate, export_status: 'queued' } : candidate) };
      setTimeout(() => { liveJob = { ...liveJob,
        candidates: liveJob.candidates.map((candidate) => candidate.id === 'retained-window-candidate'
          ? { ...candidate, export_status: 'succeeded' } : candidate),
        exports: [...liveJob.exports, { candidate_id: 'retained-window-candidate',
          filename: `corrected-${sequence}.mp4` }],
      }; }, 700);
      return route.fulfill({ status: 202, json: liveJob });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture' && request.method() === 'GET') {
      return route.fulfill({ json: liveJob });
    }
    if (pathname === '/api/jobs/rolling-browser-fixture' && request.method() === 'DELETE') {
      deletions++;
      if (deletions === 1) {
        liveJob = { ...liveJob, deletion_pending: true };
        return route.fulfill({ status: 500, json: { detail: 'Deletion incomplete; retry deleting this job.' } });
      }
      liveJob = null;
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ status: 404, json: { detail: 'No network access in rolling-session fixture' } });
  };
  await context.route(`${origin}/api/jobs**`, intercept);
  try {
    await page.reload();
    await page.getByText(/^Capture options ·/).click();
    await page.getByLabel('Which video?', { exact: true }).selectOption('live');
    assert.equal(await page.getByLabel('What should happen?', { exact: true }).inputValue(), 'sample',
      'Rolling automation must require explicit opt-in');
    await page.getByLabel('What should happen?', { exact: true }).selectOption('rolling');
    await page.getByLabel('Video or streamer', { exact: true }).fill('https://www.twitch.tv/fixture_streamer');
    await page.getByLabel('Session duration (s)', { exact: true }).fill('1801');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    assert.equal(await page.getByLabel('Session duration (s)', { exact: true }).evaluate((input) => input.validity.rangeOverflow), true);
    assert.equal(submissions.length, 0);
    await page.getByLabel('Session duration (s)', { exact: true }).fill('1800');
    await page.getByLabel('Window duration (s)', { exact: true }).fill('61');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    assert.equal(await page.getByLabel('Window duration (s)', { exact: true }).evaluate((input) => input.validity.rangeOverflow), true);
    assert.equal(submissions.length, 0);
    await page.getByLabel('Window duration (s)', { exact: true }).fill('60');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    const session = page.getByRole('region', { name: 'Rolling live session', exact: true });
    await session.waitFor();
    assert.deepEqual(submissions, [{ source: {
      type: 'url', url: 'https://www.twitch.tv/fixture_streamer', mode: 'live',
      rolling: true, session_duration: 1800, window_duration: 60,
    } }]);
    await session.getByText('No completed clips yet.', { exact: false }).waitFor();
    assert.equal(await page.getByRole('button', { name: /Delete .* from library/ }).isDisabled(), true,
      'Active sessions cannot be deleted');
    await page.getByText('Stop processing before deleting', { exact: true }).waitFor();
    assert.equal(await page.getByRole('region', { name: 'Candidate editor', exact: true }).count(), 0,
      'Rolling candidates must not expose unsupported editing/export controls');
    await page.getByText('Source unavailable for this candidate:', { exact: false }).waitFor();
    await page.getByText('Evidence: [', { exact: false }).filter({ hasText: 'Read-only rolling-window evidence.' }).waitFor();
    await session.getByText('Fixture gap warning:', { exact: false }).waitFor();
    await session.getByRole('region', { name: 'Skipped no-face windows', exact: true })
      .getByText('Window 1: automated export skipped (no_face).', { exact: false }).waitFor();
    await session.getByText('Select this window’s retained candidate below', { exact: false }).waitFor();
    assert.equal(await session.getByRole('link', { name: 'Download automated clip ↓', exact: true }).count(), 0,
      'A no-face window produces no fallback export');
    const crop = session.getByRole('region', { name: 'Future-window face crop', exact: true });
    await crop.getByLabel('Enable', { exact: true }).check();
    await crop.getByLabel('Future-window face crop width', { exact: true }).fill('0.4');
    await crop.getByLabel('Future-window face crop height', { exact: true }).fill('0.5');
    await crop.getByLabel('Future-window face crop x', { exact: true }).fill('0.8');
    await crop.getByText('Face crop must fit within', { exact: false }).waitFor();
    await crop.getByLabel('Future-window face crop x', { exact: true }).fill('0.1');
    await crop.getByLabel('Future-window face crop y', { exact: true }).fill('0.1');
    assert.equal(await crop.getByRole('button', { name: 'Apply to future windows', exact: true }).isEnabled(), true);
    await crop.getByText('UNSAVED CROP', { exact: true }).waitFor();
    liveJob = {
      ...liveJob,
      live_session: { ...liveJob.live_session, phase: 'processing', windows_seen: 2, windows_processed: 2, global_time: 120,
        windows: [...liveJob.live_session.windows, { index: 2, start: 60, end: 120, status: 'exported' }] },
      candidates: [...liveJob.candidates, { id: 'retained-window-candidate', start: 0.2, end: 1.8,
        score: 3, words: [{ start: 0.4, end: 0.9, text: 'Retained original' }], evidence: ['Retained fixture source.'],
        face_crop: { x: 0.1, y: 0.1, width: 0.4, height: 0.5 }, content_crop: null,
        editable: true, media_url: '/api/jobs/rolling-browser-fixture/candidates/retained-window-candidate/media',
        window_index: 2, window_duration: 60, revision: 0, export_status: 'succeeded' },
        { id: 'legacy-no-source', editable: true, start: 0, end: 1, score: 0,
          evidence: ['Historical candidate without retained source metadata.'], words: [] }],
      exports: [{ candidate_id: 'retained-window-candidate', filename: 'automated-clip.mp4',
        url: '/api/jobs/rolling-browser-fixture/exports/automated-clip.mp4',
        window_start: 60, window_end: 120, global_start: 65, global_end: 75 }],
    };
    const download = session.getByRole('link', { name: 'Download automated clip ↓', exact: true }).first();
    await download.waitFor();
    assert.equal(await crop.getByLabel('Future-window face crop x', { exact: true }).inputValue(), '0.1',
      'Active session polling must not overwrite manual crop drafts');
    assert.equal(await crop.getByLabel('Future-window face crop width', { exact: true }).isEnabled(), true,
      'Future-window crop controls remain editable while processing');
    await crop.getByRole('button', { name: 'Apply to future windows', exact: true }).click();
    await crop.getByText('Face crop saved for windows', { exact: false }).waitFor();
    assert.deepEqual(cropPatches, [{ face_crop: { x: 0.1, y: 0.1, width: 0.4, height: 0.5 } }]);
    await crop.getByText('SAVED SESSION CROP', { exact: true }).waitFor();
    await crop.getByLabel('Enable', { exact: true }).uncheck();
    await crop.getByRole('button', { name: 'Apply to future windows', exact: true }).click();
    await crop.getByText('Face crop saved for windows', { exact: false }).waitFor();
    assert.deepEqual(cropPatches[1], { face_crop: null }, 'Disabling restores automatic face detection');
    await crop.getByText('AUTOMATIC FRAMING', { exact: true }).waitFor();
    assert.equal(await download.count(), 1, 'Crop changes must not remove completed exports');
    await crop.getByLabel('Enable', { exact: true }).check();
    await crop.getByLabel('Future-window face crop width', { exact: true }).fill('0.4');
    await crop.getByRole('button', { name: 'Apply to future windows', exact: true }).click();
    await crop.getByText('Face crop saved for windows', { exact: false }).waitFor();
    assert.equal((await page.locator('.job-summary .status').innerText()).toLowerCase(), 'running');
    assert.equal(await download.getAttribute('href'), '/api/jobs/rolling-browser-fixture/exports/automated-clip.mp4');
    await session.getByText('Window range: 1:00.0–2:00.0', { exact: true }).waitFor();
    await session.getByText('Session clip range: 1:05.0–1:15.0', { exact: true }).waitFor();
    await session.getByText('Framing unverified', { exact: false }).waitFor();
    assert.equal(await download.count(), 1, 'Only the exported window has a completed clip');
    await page.getByRole('button', { name: /MOMENT 02/ }).click();
    const editor = page.getByRole('region', { name: 'Candidate editor', exact: true });
    await editor.waitFor();
    const preview = editor.getByLabel('Source video preview', { exact: true });
    assert.equal(await preview.getAttribute('src'),
      '/api/jobs/rolling-browser-fixture/candidates/retained-window-candidate/media');
    await until(() => preview.evaluate((video) => video.duration), (duration) => duration > 0, 'retained candidate preview');
    await editor.getByLabel('Word 1 caption correction', { exact: true }).fill('During capture');
    await editor.getByLabel('Face crop width', { exact: true }).fill('0.35');
    await editor.getByRole('button', { name: 'Save changes', exact: true }).click();
    await editor.getByText('Changes saved.', { exact: true }).waitFor();
    assert.equal(corrections[0].status, 'running');
    assert.equal(corrections[0].patch.words[0].text, 'Retained original');
    assert.equal(corrections[0].patch.words[0].correction, 'During capture');
    assert.equal(corrections[0].patch.face_crop.width, 0.35);
    await editor.getByRole('button', { name: 'Export clip ↗', exact: true }).click();
    await editor.getByText('Candidate export: queued', { exact: false }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Stop session', exact: true }).isEnabled(), true,
      'Candidate correction export must not prevent stopping capture');
    await editor.getByText('corrected-1.mp4', { exact: true }).waitFor();
    assert.deepEqual(reexports, ['running']);
    await page.getByRole('button', { name: 'Stop session', exact: true }).click();
    await until(() => page.locator('.job-summary .status').innerText(), (status) => status.toLowerCase() === 'cancelled', 'rolling cancellation');
    assert.equal(cancellations, 1);
    assert.equal(await download.count(), 1, 'Completed clips must remain after stop');
    await page.reload();
    await page.getByRole('link', { name: 'Download automated clip ↓', exact: true }).first().waitFor();
    assert.equal(await page.getByRole('region', { name: 'Candidate editor', exact: true }).count(), 0,
      'Unavailable candidate remains read-only after reload');
    assert.equal(await page.getByLabel('Future-window face crop width', { exact: true }).inputValue(), '0.4');
    assert.equal(await page.getByRole('button', { name: 'Apply to future windows', exact: true }).isDisabled(), true,
      'Stopped sessions cannot apply future-window crops');
    assert.equal(submissions.length, 1, 'Reload must not restart rolling capture');
    await page.getByRole('button', { name: /MOMENT 02/ }).click();
    await editor.getByLabel('Word 1 caption correction', { exact: true }).fill('After stop');
    await editor.getByRole('button', { name: 'Save changes', exact: true }).click();
    await editor.getByText('Changes saved.', { exact: true }).waitFor();
    assert.equal(corrections[1].status, 'cancelled');
    await editor.getByRole('button', { name: 'Export clip ↗', exact: true }).click();
    await editor.getByText('corrected-2.mp4', { exact: true }).waitFor();
    assert.deepEqual(reexports, ['running', 'cancelled']);
    await page.getByRole('button', { name: /MOMENT 03/ }).click();
    assert.equal(await page.getByRole('region', { name: 'Candidate editor', exact: true }).count(), 0,
      'Even editable=true cannot claim correction support without retained source metadata');
    await page.getByText('Source unavailable for this candidate:', { exact: false }).waitFor();
    for (const [status, expected] of [
      ['failed', 'Resolution failed — source not confirmed'],
      ['cancelled', 'Cancelled before source was confirmed'],
      ['interrupted', 'Interrupted before source was confirmed'],
    ]) {
      liveJob = { ...liveJob, status, resolved_source: null,
        source: { type: 'streamer', platform: 'youtube', identifier: '@fixture', mode: 'auto', duration: 60 },
        error: status === 'failed' ? 'YouTube source resolution failed in offline regression.' : null };
      await page.reload();
      const sourceStatus = page.getByLabel('Source status', { exact: true });
      await sourceStatus.getByText(`Source status: ${expected}`, { exact: true }).waitFor();
      assert.equal(await sourceStatus.getByText('Awaiting backend resolution', { exact: false }).count(), 0,
        `Unresolved ${status} source must not appear to still be resolving`);
      if (status === 'failed') {
        await page.getByRole('alert').filter({ hasText: 'YouTube source resolution failed in offline regression.' }).waitFor();
      }
    }
    const deleteButton = page.getByRole('button', { name: /Delete .* from library/ });
    await Promise.all([page.waitForEvent('dialog').then(async (dialog) => {
      assert.match(dialog.message(), /retained windows, and all exports/);
      assert.match(dialog.message(), /original file is not deleted/);
      await dialog.dismiss();
    }), deleteButton.click()]);
    assert.equal(deletions, 0, 'Dismissing confirmation must preserve the library job');
    await Promise.all([page.waitForEvent('dialog').then((dialog) => dialog.accept()), deleteButton.click()]);
    await page.getByRole('alert').filter({ hasText: 'Deletion incomplete; retry deleting this job.' }).waitFor();
    assert.equal(await deleteButton.count(), 1, 'Failed deletion preserves a retryable library entry');
    await until(() => page.getByText('Deletion incomplete — retry Delete', { exact: true }).count(),
      (count) => count === 1, 'pending deletion retry guidance');
    await Promise.all([page.waitForEvent('dialog').then((dialog) => dialog.accept()), deleteButton.click()]);
    await until(async () => page.getByRole('button', { name: /Delete .* from library/ }).count(),
      (count) => count === 0, 'deleted job removed from library');
    assert.equal(deletions, 2);
    await page.getByRole('heading', { name: 'Start with a video', exact: true }).waitFor();
  } finally {
    await context.unroute(`${origin}/api/jobs**`, intercept);
  }
}

for (const realAnalysis of [false, true]) {
test(`real browser import/edit/crop/export/cancel (${realAnalysis ? 'real local speech analysis' : 'deterministic analysis fixture'})`, { timeout: 180000 }, async () => {
  assert.ok(existsSync(path.join(dist, 'index.html')), 'Build the browser test frontend first.');
  const workspace = await mkdtemp(path.join(frontend, '.browser-test-'));
  const previousTmpdir = process.env.TMPDIR;
  process.env.TMPDIR = workspace;
  let backend;
  let browser;
  let logs = '';
  try {
    await mkdir(path.join(workspace, 'inputs'));
    const source = path.join(workspace, 'inputs', 'generated.mp4');
    const ffmpeg = process.env.STREAMFRAME_TEST_FFMPEG ||
      (existsSync('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg')
        ? '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg' : 'ffmpeg');
    const filters = execFileSync(ffmpeg, ['-hide_banner', '-filters'], { encoding: 'utf8' });
    assert.match(filters, /\bass\s+V->V/, 'Real caption export requires FFmpeg with the libass ass filter.');
    let speech;
    if (realAnalysis) {
      speech = path.join(workspace, 'speech.aiff');
      execFileSync(process.env.STREAMFRAME_SAY || 'say', ['-r', '155', '-o', speech,
        'Hello this is a local video test. We edit captions and create short clips.']);
    }
    execFileSync(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
      '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=5',
      ...(speech ? ['-i', speech] : ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=5']),
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', source]);
    const port = await unusedPort();
    const origin = `http://127.0.0.1:${port}`;
    backend = spawn(python, ['-B', path.join(frontend, 'tests/browser_backend.py')], {
      cwd: repo,
      env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1',
        PATH: `${path.dirname(ffmpeg)}${path.delimiter}${process.env.PATH}`,
        STREAMFRAME_BROWSER_REAL_ANALYSIS: realAnalysis ? '1' : '0',
        STREAMFRAME_BROWSER_DIST: dist,
        HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1',
        STREAMFRAME_BROWSER_WORKSPACE: workspace, STREAMFRAME_BROWSER_PORT: String(port) },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stdout.on('data', (data) => { logs += data; });
    backend.stderr.on('data', (data) => { logs += data; });
    await until(async () => (await fetch(`${origin}/api/health`)).ok, Boolean, 'backend health', 15000);

    const chrome = process.env.STREAMFRAME_CHROME ||
      (existsSync('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
    const context = await chromium.launchPersistentContext(path.join(workspace, 'profile'), {
      headless: true, executablePath: chrome, acceptDownloads: true,
      downloadsPath: path.join(workspace, 'downloads'),
    });
    browser = context;
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const browserErrors = [];
    page.on('pageerror', (error) => browserErrors.push(error.message));
    const externalRequests = [];
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      if (url.origin === origin || url.protocol === 'blob:' || url.protocol === 'data:') return route.continue();
      externalRequests.push(url.href);
      return route.abort();
    });
    await page.goto(origin);
    const navigation = page.getByRole('navigation', { name: 'Workspace sections' });
    assert.equal(await navigation.getByRole('link', { name: 'Add video' }).getAttribute('href'), '#add-video');
    assert.equal(await navigation.getByRole('link', { name: /Sources/ }).getAttribute('href'), '#source-library');
    assert.equal(await navigation.getByRole('link', { name: /Exports/ }).getAttribute('href'), '#exported-videos');
    assert.equal(await page.getByRole('list', { name: 'How it works' }).getByRole('listitem').count(), 3);
    assert.equal(await page.getByText('Twitch works best.', { exact: false }).isVisible(), true);
    assert.equal(await page.getByLabel('What would you like to add?', { exact: true }).inputValue(), 'streamer');
    assert.equal(await page.getByLabel('Platform', { exact: true }).isVisible(), false, 'Less-used options start collapsed');
    await page.getByText(/^Capture options ·/).click();
    assert.equal(await page.getByLabel('Platform', { exact: true }).inputValue(), 'twitch');
    assert.equal(await page.getByLabel('Which video?', { exact: true }).inputValue(), 'auto');
    let importRequests = 0;
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url() === `${origin}/api/jobs`) importRequests++;
    });
    await page.getByLabel('Video or streamer', { exact: true }).fill('fixture_streamer');
    await page.getByLabel('Which video?', { exact: true }).selectOption('live');
    await page.getByLabel('Capture duration (s)', { exact: true }).fill('301');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    assert.equal(await page.getByLabel('Capture duration (s)', { exact: true }).evaluate((input) => input.validity.rangeOverflow), true);
    assert.equal(importRequests, 0, 'Unbounded/over-limit capture is rejected before resolving streams');
    await page.getByLabel('Which video?', { exact: true }).selectOption('latest');
    await page.getByLabel('Platform', { exact: true }).selectOption('youtube');
    await page.getByLabel('What would you like to add?', { exact: true }).selectOption('url');
    await page.getByLabel('Video or streamer', { exact: true }).fill('https://www.youtube.com/watch?v=fixture');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    assert.equal(await page.getByLabel('Source range start (s), required', { exact: true }).evaluate((input) => input.validity.valueMissing), true);
    assert.equal(importRequests, 0, 'URL range is required before contacting the backend');
    await page.getByLabel('Source range start (s), required', { exact: true }).fill('4');
    await page.getByLabel('Source range end (s), required', { exact: true }).fill('2');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Import range requires' }).waitFor();
    assert.equal(importRequests, 0, 'Invalid URL range is rejected without external retrieval');
    await page.getByLabel('What would you like to add?', { exact: true }).selectOption('streamer');
    await page.getByText(/^Capture options ·/).click();
    const interceptStreamerImport = async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      return route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Offline browser regression: source resolution intentionally not started.' }),
      });
    };
    await context.route(`${origin}/api/jobs`, interceptStreamerImport);
    try {
      for (const fixture of [
        { platform: 'twitch', url: 'https://www.twitch.tv/fixture_streamer', mode: 'auto', duration: 45 },
        { platform: 'youtube', url: 'https://www.youtube.com/@fixture_streamer/live', mode: 'live', duration: 60 },
      ]) {
        await page.getByLabel('Video or streamer', { exact: true }).fill('');
        await page.getByLabel('Platform', { exact: true }).selectOption('twitch');
        await page.getByLabel('Which video?', { exact: true }).selectOption(fixture.mode);
        await page.getByLabel('Capture duration (s)', { exact: true }).fill(String(fixture.duration));
        await page.getByLabel('Video or streamer', { exact: true }).fill(fixture.url);
        await page.getByText(`Platform detected from link: ${fixture.platform === 'twitch' ? 'Twitch' : 'YouTube'}`, { exact: true }).waitFor();
        assert.equal(await page.getByLabel('Platform', { exact: true }).count(), 0,
          'Recognized links auto-select platform without asking the user');
        const submission = page.waitForResponse((response) =>
          response.url() === `${origin}/api/jobs` && response.request().method() === 'POST');
        await page.getByRole('button', { name: 'Import →', exact: true }).click();
        const intercepted = await submission;
        assert.equal(intercepted.status(), 422, 'Intercepted import must not reach live source resolution');
        assert.deepEqual(intercepted.request().postDataJSON(), {
          source: { type: 'url', url: fixture.url, mode: fixture.mode, duration: fixture.duration },
        }, 'Links pasted into streamer UI must use URL source schema with mode and duration');
        assert.equal(await page.getByLabel('What would you like to add?', { exact: true }).inputValue(), 'streamer');
      }
    } finally {
      await context.unroute(`${origin}/api/jobs`, interceptStreamerImport);
    }
    await page.getByLabel('What would you like to add?', { exact: true }).selectOption('file');
    await page.getByLabel('Video or streamer', { exact: true }).fill(source);
    await page.getByLabel('Source range start (s), optional', { exact: true }).fill('1');
    await page.getByLabel('Source range end (s), optional', { exact: true }).fill('4');
    const createdResponse = page.waitForResponse((response) =>
      response.url() === `${origin}/api/jobs` && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    const response = await createdResponse;
    assert.equal(response.status(), 202, await response.text());
    const created = await response.json();
    assert.equal(created.source.start, 1);
    assert.equal(created.source.end, 4);
    const readJob = async () => (await fetch(`${origin}/api/jobs/${created.id}`)).json();
    const analyzed = await until(readJob, (job) => {
      if (job?.status === 'failed') throw new Error(job.error);
      return job?.status === 'succeeded';
    }, 'real range import and analysis', 90000);
    assert.ok(analyzed.candidates.length >= 1);
    assert.ok(analyzed.candidates[0].words.length >= 1, 'Analysis must produce actual timestamped words');
    const originalText = analyzed.candidates[0].words[0].text;
    if (!realAnalysis) assert.equal(analyzed.candidates.length, 2);
    await page.getByRole('heading', { name: 'Moment editor' }).waitFor();
    await page.getByText('All editor timestamps are relative to this imported range', { exact: false }).waitFor();
    await page.getByLabel('Source status', { exact: true }).getByText('Source status: Local file', { exact: true }).waitFor();
    const preview = page.getByLabel('Source video preview', { exact: true });
    await until(() => preview.evaluate((video) => video.duration), (duration) => duration > 2.8 && duration < 3.2,
      'range-relative source video duration');
    await preview.evaluate((video) => { video.muted = true; });
    await page.getByRole('button', { name: '▶ Play clip', exact: true }).click();
    await until(() => preview.evaluate((video) => video.currentTime), (time) => time > 0.3, 'actual video playback');
    await preview.evaluate((video) => video.pause());

    await page.getByLabel('Word 1 caption correction', { exact: true }).fill('Corrected');
    await page.getByLabel('Clip start (s)', { exact: true }).fill('0.2');
    await page.getByLabel('Clip end (s)', { exact: true }).fill('1.8');
    const face = page.locator('.crop-controls').filter({ has: page.getByRole('heading', { name: 'Face crop', exact: true }) });
    await face.getByLabel('Enable', { exact: true }).check();
    await page.getByLabel('Face crop width', { exact: true }).fill('0.4');
    await page.getByLabel('Face crop height', { exact: true }).fill('0.5');
    await page.getByLabel('Face crop x', { exact: true }).fill('0.1');
    await page.getByLabel('Face crop y', { exact: true }).fill('0.1');
    const content = page.locator('.crop-controls').filter({ has: page.getByRole('heading', { name: 'Content crop', exact: true }) });
    await content.getByLabel('Enable', { exact: true }).check();
    await page.getByLabel('Content crop width', { exact: true }).fill('0.8');
    await page.getByLabel('Content crop height', { exact: true }).fill('0.8');
    await page.getByLabel('Content crop x', { exact: true }).fill('0.1');
    await page.getByLabel('Content crop y', { exact: true }).fill('0.1');
    await page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await page.getByText('Changes saved.', { exact: true }).waitFor();
    const saved = (await readJob()).candidates[0];
    assert.equal(saved.words[0].text, originalText);
    assert.equal(saved.words[0].correction, 'Corrected');
    assert.deepEqual(saved.face_crop, { x: 0.1, y: 0.1, width: 0.4, height: 0.5 });
    assert.deepEqual(saved.content_crop, { x: 0.1, y: 0.1, width: 0.8, height: 0.8 });
    assert.deepEqual([saved.start, saved.end], [0.2, 1.8]);
    await page.reload();
    await until(() => page.getByLabel('Word 1 caption correction', { exact: true }).inputValue(),
      (value) => value === 'Corrected', 'persisted caption after reload');
    assert.equal(await page.getByLabel('Face crop width', { exact: true }).inputValue(), '0.4');
    await page.getByRole('button', { name: 'Export clip ↗', exact: true }).click();
    const rendered = await until(readJob, (job) => {
      if (job.status === 'failed') throw new Error(job.error);
      return job.status === 'succeeded' && job.exports.length > 0;
    }, 'real FFmpeg export', 90000);
    assert.equal(rendered.exports[0].candidate_id, analyzed.candidates[0].id);
    const downloadLink = page.getByRole('link', { name: 'Download ↓', exact: true });
    await downloadLink.waitFor({ timeout: 15000 });
    const downloadEvent = page.waitForEvent('download');
    await downloadLink.click();
    const download = await downloadEvent;
    const output = path.join(workspace, 'download.mp4');
    await download.saveAs(output);
    assert.ok((await stat(output)).size > 1000);
    const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams',
      '-show_format', '-of', 'json', output], { encoding: 'utf8' }));
    assert.ok(probe.streams.some((stream) => stream.codec_type === 'video'));
    assert.ok(probe.streams.some((stream) => stream.codec_type === 'audio'));
    assert.ok(Math.abs(Number(probe.format.duration) - 1.6) < 0.3);
    if (analyzed.candidates.length > 1) {
      await page.getByRole('button', { name: /MOMENT 02/ }).click();
      assert.equal(await page.getByRole('link', { name: 'Download ↓', exact: true }).count(), 0);
    }

    await writeFile(path.join(workspace, 'hold-analysis'), 'deterministic cancellation fixture');
    await page.getByLabel('What would you like to add?', { exact: true }).selectOption('file');
    await page.getByLabel('Video or streamer', { exact: true }).fill(source);
    await page.getByLabel('Source range start (s), optional', { exact: true }).fill('1');
    await page.getByLabel('Source range end (s), optional', { exact: true }).fill('4');
    const cancelImport = page.waitForResponse((response) =>
      response.url() === `${origin}/api/jobs` && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Import →', exact: true }).click();
    const cancellable = await (await cancelImport).json();
    const readCancellable = async () => (await fetch(`${origin}/api/jobs/${cancellable.id}`)).json();
    await until(readCancellable, (job) => job.message === 'Waiting for browser cancellation check', 'cancellable analyzer stage');
    await until(() => page.locator('.job-summary').innerText(), (text) => text.includes(cancellable.id), 'new selected job');
    await page.getByRole('button', { name: 'Cancel job', exact: true }).click();
    await until(readCancellable, (job) => job.status === 'cancelled', 'real backend cancellation');
    await until(() => page.locator('.job-summary .status').innerText(), (status) => status.toLowerCase() === 'cancelled', 'cancelled status in editor');
    if (!realAnalysis) {
      await checkRollingSession(page, context, origin, source);
      await checkExportLibrary(page, context, origin, source);
    }
    assert.deepEqual(browserErrors, []);
    assert.deepEqual(externalRequests, []);
  } catch (error) {
    error.message += `\nBackend logs:\n${logs}`;
    throw error;
  } finally {
    if (browser) await browser.close();
    if (backend && backend.exitCode === null) {
      const stopped = once(backend, 'exit');
      backend.kill('SIGTERM');
      await stopped;
    }
    await rm(workspace, { recursive: true, force: true });
    if (previousTmpdir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = previousTmpdir;
  }
});
}
