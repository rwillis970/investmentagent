'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'dashboard/static/admin_console_link.js'), 'utf8');
const commandCenterHtml = fs.readFileSync(
  path.join(root, 'dashboard/static/agent_command_center.html'),
  'utf8'
);

test('admin_console_link.js targets the exact loopback Admin Console URL with opener isolation', () => {
  assert.match(script, /ADMIN_CONSOLE_URL\s*=\s*"http:\/\/127\.0\.0\.1:8766"/);
  assert.match(script, /link\.href\s*=\s*ADMIN_CONSOLE_URL/);
  assert.match(script, /link\.target\s*=\s*"_blank"/);
  assert.match(script, /link\.rel\s*=\s*"noopener noreferrer"/);
});

test('admin_console_link.js is navigation only: no fetch/XHR/proxy, no shared auth mechanism', () => {
  assert.doesNotMatch(script, /fetch\(/);
  assert.doesNotMatch(script, /XMLHttpRequest/);
  assert.doesNotMatch(script, /\/api\//);
  assert.doesNotMatch(script, /csrf/i);
});

test('admin_console_link.js only builds one DOM element and appends it once, never polls', () => {
  assert.match(script, /document\.createElement\("a"\)/);
  assert.match(script, /document\.body\.appendChild\(link\)/);
  assert.doesNotMatch(script, /setInterval/);
  assert.doesNotMatch(script, /setTimeout/);
});

test('agent_command_center.html loads admin_console_link.js exactly once, immediately before dashboard_bind.js\'s own closing tags', () => {
  assert.strictEqual(
    (commandCenterHtml.match(/<script src="admin_console_link\.js"><\/script>/g) || []).length,
    1
  );
});
