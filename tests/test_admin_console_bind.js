'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'admin_console/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'admin_console/static/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'admin_console/static/style.css'), 'utf8');

test('admin UI reads the process token from same-origin HTML and sends the custom header', () => {
  assert.match(html, /name="investmentagent-csrf" content="__CSRF_TOKEN__"/);
  assert.match(script, /'X-InvestmentAgent-CSRF'/);
  assert.match(script, /api\/services[\s\S]*headers:csrfHeaders/);
  assert.match(script, /api\/utilities[\s\S]*headers:csrfHeaders/);
  assert.doesNotMatch(script, /[?&]csrf=/);
});

test('stale and unavailable states have explicit non-green visual classes', () => {
  assert.match(script, /\['STALE','NOT_YET_OBSERVED'\]/);
  assert.match(script, /\['UNAVAILABLE','UNKNOWN'\]/);
  assert.match(css, /\.caution\{color:var\(--amber\)\}/);
  assert.match(css, /\.unknown\{color:var\(--muted\)\}/);
});
