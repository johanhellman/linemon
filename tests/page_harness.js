// Test helper (tests/test_linemon.py, PageScript): runs the page's own script in a minimal fake DOM against a
// given /api/status document and prints what it put in the banners and the health card.
// Usage: node page_harness.js <script.js> <status.json>
const fs = require('fs');
const [,, scriptPath, statusPath] = process.argv;
const status = JSON.parse(fs.readFileSync(statusPath, 'utf8'));
function el(tag) {
  const e = { tag, children: [], style: {}, className: '', hidden: false, _text: '', dataset: {},
    classList: { add() {}, remove() {} },
    appendChild(c) { this.children.push(c); return c; },
    prepend(c) { this.children.unshift(c); },
    replaceChildren(...c) { this.children = c.flatMap(x => x && x.children && x.tag === 'frag' ? x.children : [x]); },
    querySelector() { return el('x'); },
    get firstChild() { return this.children[0]; },
    set textContent(v) { this._text = String(v); }, get textContent() { return this._text; },
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; },
    getContext() { return new Proxy({}, { get: () => () => ({ width: 0 }), set: () => true }); },
    getBoundingClientRect() { return { width: 100, height: 100 }; }, width: 100, height: 100,
  };
  return e;
}
const byId = {};
global.document = {
  getElementById: id => byId[id] || (byId[id] = el('div')),
  createElement: tag => el(tag),
  createDocumentFragment: () => el('frag'),
  documentElement: {},
};
global.window = global; global.getComputedStyle = () => ({ getPropertyValue: () => '#888' });
global.fetch = async () => ({ json: async () => status });
global.addEventListener = () => {}; global.setInterval = () => {}; global.devicePixelRatio = 1;
const code = fs.readFileSync(scriptPath, 'utf8').replace(/\nrefresh\(\);.*$/m, '');
eval(code + '\n;global.__refresh = refresh;');
global.__refresh().then(() => {
  const g = id => byId[id] || el('div');
  console.log(JSON.stringify({
    banner: [g('banner').className, g('banner').textContent],
    healthBanner: [g('health-banner').hidden, g('health-banner').className, g('health-banner').textContent],
    card: g('health-card').hidden, pill: [g('health-pill').textContent, g('health-pill').className],
    meta: g('health-meta').textContent, rows: (g('health-signals').children || []).length,
  }));
}).catch(e => { console.log('ERROR ' + e.stack); process.exit(1); });
