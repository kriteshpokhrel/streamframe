import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSourceTime, streamerSource } from '../src/api.ts';

test('source times accept seconds and timecodes without accepting invalid fields', () => {
  assert.equal(parseSourceTime('15:06:19'), 54379);
  assert.equal(parseSourceTime('15:07:09'), 54429);
  assert.equal(parseSourceTime(' 90:05.25 '), 5405.25);
  assert.equal(parseSourceTime('1.25'), 1.25);
  for (const value of ['', '1:60', '1:60:00', '1:02:60', '-1', '1:2:3:4', '1:2e2', 'Infinity']) {
    assert.ok(Number.isNaN(parseSourceTime(value)), value);
  }
});

test('streamer names and channel links use their respective API schemas', () => {
  assert.deepEqual(streamerSource(' example ', 'twitch', 'auto', 60), {
    type: 'streamer', platform: 'twitch', identifier: 'example', mode: 'auto', duration: 60,
  });
  assert.deepEqual(streamerSource(' https://www.twitch.tv/example ', 'twitch', 'live', 30), {
    type: 'url', url: 'https://www.twitch.tv/example', mode: 'live', duration: 30,
  });
  assert.deepEqual(streamerSource('https://www.youtube.com/watch?v=abcdefghijk', 'youtube', 'live', 30), {
    type: 'url', url: 'https://www.youtube.com/watch?v=abcdefghijk', mode: 'live', duration: 30,
  });
  assert.throws(() => streamerSource('https://youtube.com/@example', 'twitch', 'auto', 60),
    /does not match the selected platform/);
});
import { parseRoute, routeHash } from '../src/navigation.ts';

test('review links keep exact source and moment IDs, even with URL separators', () => {
  const route = { view: 'review', jobId: 'live / #1', candidateId: 'window/5?' };
  assert.deepEqual(parseRoute(routeHash(route)), route);
  assert.deepEqual(parseRoute('#/review/%zz'), { view: 'sources' });
});
