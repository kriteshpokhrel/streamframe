import assert from 'node:assert/strict';
import { test } from 'node:test';
import { candidateJobExports, exportedVideos, exportFiles, failedSourceStatus, getDiagnostics, jobControls, startExport } from '../src/api.ts';

const outputs = [
  { candidate_id: 'first', filename: 'first.mp4' },
  { candidate_id: 'second', filename: 'second.mp4' },
  { candidate_id: 'first', filename: 'first-revised.mp4' },
  { filename: 'unassigned.mp4' },
  null,
];

test('job exports belong only to the selected candidate, including existing revisions', () => {
  assert.deepEqual(candidateJobExports(outputs, 'job', 'first'), [
    { name: 'first.mp4', url: '/api/jobs/job/exports/first.mp4' },
    { name: 'first-revised.mp4', url: '/api/jobs/job/exports/first-revised.mp4' },
  ]);
  assert.deepEqual(candidateJobExports(outputs, 'job', 'second'), [
    { name: 'second.mp4', url: '/api/jobs/job/exports/second.mp4' },
  ]);
  assert.deepEqual(candidateJobExports(outputs, 'job', 'new'), []);
});

test('missing or malformed job exports are not assigned to a candidate', () => {
  for (const value of [undefined, null, {}, 'clip.mp4', [42, 'clip.mp4']]) {
    assert.deepEqual(candidateJobExports(value, 'job', 'first'), []);
  }
});

test('candidate-scoped outputs still support filenames without candidate metadata', () => {
  assert.deepEqual(exportFiles([{ filename: 'scoped.mp4' }], 'job'), [
    { name: 'scoped.mp4', url: '/api/jobs/job/exports/scoped.mp4' },
  ]);
});

test('async export response does not leak other candidates into local export state', async (context) => {
  context.mock.method(globalThis, 'fetch', async () =>
    Response.json({ id: 'job', status: 'exporting', exports: outputs }, { status: 202 }));
  const result = await startExport('job', 'second');
  assert.equal(result.pending, true);
  assert.deepEqual(result.files, [{ name: 'second.mp4', url: '/api/jobs/job/exports/second.mp4' }]);
});

test('text JSON job responses and direct candidate export responses remain supported', async (context) => {
  const fetchMock = context.mock.method(globalThis, 'fetch', async () =>
    new Response(JSON.stringify({ outputs }), { headers: { 'Content-Type': 'text/plain' } }));
  assert.deepEqual((await startExport('job', 'second')).files, [
    { name: 'second.mp4', url: '/api/jobs/job/exports/second.mp4' },
  ]);
  fetchMock.mock.mockImplementation(async () => Response.json({ filename: 'direct.mp4' }));
  assert.deepEqual((await startExport('job', 'second')).files, [
    { name: 'direct.mp4', url: '/api/jobs/job/exports/direct.mp4' },
  ]);
});

test('export library preserves job, candidate and historical revision ownership without inventing metadata', () => {
  const candidates = [{ id: 'first', exports: ['scoped.mp4'] }, { id: 'second' }];
  const files = exportedVideos([
    { id: 'one', exports: [...outputs, { candidate_id: 'second', filename: 'revision.mp4', revision: 3 }], candidates },
    { id: 'two', exports: [{ candidate_id: 'first', filename: 'first.mp4' }], candidates: [] },
  ]);
  assert.equal(files.length, 7);
  assert.equal(files.find((file) => file.name === 'revision.mp4').provenance.revision, 3);
  assert.equal(files.find((file) => file.name === 'unassigned.mp4').candidateId, undefined);
  assert.equal(files.find((file) => file.name === 'scoped.mp4').candidateId, 'first');
  assert.equal(files.filter((file) => file.name === 'first.mp4').length, 2);
  assert.equal(files.at(-1).jobId, 'two');
});

test('terminal jobs never offer cancellation; only supported nonrolling states offer resume', () => {
  for (const status of ['succeeded', 'failed', 'cancelled', 'interrupted', 'cancelling']) {
    const controls = jobControls({ status, source: 'source', candidates: [] });
    assert.equal(controls.cancel, false, status);
    assert.equal(controls.resume, ['failed', 'cancelled', 'interrupted'].includes(status), status);
    assert.equal(jobControls({ status, source: { type: 'url', rolling: true }, candidates: [] }).resume, false);
    assert.equal(jobControls({ status, source: 'source', live_session: {}, candidates: [] }).resume, false);
  }
  for (const status of ['queued', 'running', 'exporting']) {
    assert.deepEqual(jobControls({ status, source: 'source' }), { cancel: true, resume: false });
  }
  assert.deepEqual(jobControls({ status: 'failed', source: 'source', deletion_pending: true }),
    { cancel: false, resume: false });
});

test('diagnostics fetches encoded per-job text and rejects malformed or failed responses', async (context) => {
  const fetchMock = context.mock.method(globalThis, 'fetch', async (url) => {
    assert.equal(url, '/api/jobs/job%2Fone/diagnostics');
    return Response.json({ text: 'Sanitized diagnostic text' });
  });

  assert.equal(await getDiagnostics('job/one'), 'Sanitized diagnostic text');
  fetchMock.mock.mockImplementation(async () => Response.json({ text: null }));
  await assert.rejects(getDiagnostics('job/one'), /Invalid diagnostics response/);
  fetchMock.mock.mockImplementation(async () => Response.json({ detail: 'Not available' }, { status: 404 }));
  await assert.rejects(getDiagnostics('job/one'), /404.*Not available/);
});

test('legacy unsupported bounded acquisition failures are not mislabeled as resolution failures', () => {
  assert.equal(failedSourceStatus({ error: 'YouTube post_live is unavailable until processing completes' }),
    'YouTube post-live processing unavailable — see job error');
  assert.equal(failedSourceStatus({ error: 'YouTube bounded VOD acquisition is unsupported' }),
    'Source acquisition unsupported — see job error');
  assert.equal(failedSourceStatus({ error: { message: 'Bounded source acquisition not supported' } }),
    'Source acquisition unsupported — see job error');
  assert.equal(failedSourceStatus({ error: 'SourceResolutionError: missing channel' }),
    'Resolution failed — source not confirmed');
  assert.equal(failedSourceStatus({ error: 'FFmpeg export failed' }),
    'Source metadata unavailable — job failed (see error)');
});
