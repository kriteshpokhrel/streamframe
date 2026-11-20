import assert from 'node:assert/strict';
import { test } from 'node:test';
import { streamerSource } from '../src/api.ts';

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
