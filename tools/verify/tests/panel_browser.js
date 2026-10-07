// Optional real-browser instrument: Node's built-in WebSocket, no npm packages.
const endpoint = process.argv[2], base = process.argv[3];
const ws = new WebSocket(endpoint);
await new Promise(resolve => ws.addEventListener('open', resolve, {once: true}));
let id = 0;
const pending = new Map();
ws.addEventListener('message', e => {
  const r = JSON.parse(e.data);
  if (r.id) { const p = pending.get(r.id); pending.delete(r.id); r.error ? p.reject(r.error) : p.resolve(r.result); }
});
function call(method, params = {}, socket = ws) { return new Promise((resolve, reject) => { const n = ++id; pending.set(n, {resolve, reject}); socket.send(JSON.stringify({id: n, method, params})); }); }
async function evaluate(expression, socket = ws) {
  const r = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, socket);
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function assert(value, message) { if (!value) throw new Error(message); }
async function fixture(source, state = 'live', hasFrame = true) {
  await fetch(`${base}/fixture?source=${source}&state=${state}&frame=${hasFrame ? 1 : 0}`);
  await evaluate('poll()');
}
const events = `(() => {
  const key = document.querySelector('.k[data-id="yes"]');
  const knob = document.querySelector('.knob.live');
  const bed = document.querySelector('#fbed');
  for (const target of [key, knob, bed, document.querySelector('#card'), document.querySelector('#hp'), document.querySelector('.knob.pot')].filter(Boolean)) {
    target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, altKey:true, clientX:100, clientY:100}));
    target.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:-100}));
    target.dispatchEvent(new MouseEvent('dblclick', {bubbles:true}));
    target.dispatchEvent(new TouchEvent('touchstart', {bubbles:true}));
    target.dispatchEvent(new MouseEvent('click', {bubbles:true}));
  }
  for (const name of ['Shift','a','z','ArrowLeft','ArrowUp','Escape']) {
    document.dispatchEvent(new KeyboardEvent('keydown', {key:name,bubbles:true}));
    document.dispatchEvent(new KeyboardEvent('keyup', {key:name,bubbles:true}));
  }
  document.dispatchEvent(new MouseEvent('mousemove', {bubbles:true,clientX:250,clientY:40}));
  document.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
  window.dispatchEvent(new Event('blur'));
  const transfer = new DataTransfer(); transfer.items.add(new File(['synthetic'], 'test.wav'));
  document.querySelector('#card').dispatchEvent(new DragEvent('drop', {bubbles:true,dataTransfer:transfer}));
  togglePool(true); toggleMon(true); monToggle(); soundToggle(); reinsert(); insertCard();
  sendKey(38,1,true); sendKnob(48,1); sendFader(2); faderRefresh();
  return document.querySelectorAll('.held,.down').length;
})()`;
try {
  await call('Page.enable');
  await call('Page.navigate', {url:base});
  await pause(800);
  // CDP delivers browser input events in addition to DOM dispatch coverage.
  const point = await evaluate(`(() => { const r=document.querySelector('.k[data-id="yes"]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
  await call('Input.dispatchMouseEvent',{type:'mouseWheel',deltaY:-100,deltaX:0,...point});
  await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]});
  await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Shift',code:'ShiftLeft',windowsVirtualKeyCode:16});
  await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Shift',code:'ShiftLeft',windowsVirtualKeyCode:16});
  assert((await evaluate("document.querySelector('#phase').textContent")) === 'live', 'hardware live status must use connection_state');
  assert((await evaluate("document.querySelector('#source').textContent")).includes('READ ONLY'), 'physical read-only badge');
  assert((await evaluate("document.querySelector('#imginfo').textContent")).includes('unknown'), 'unknown model must not use skin model');
  await evaluate(events); await pause(250);
  let requests = await (await fetch(`${base}/requests`)).json();
  const allowed = new Set(['/','/skin.js','/screen.png','/screen.txt','/status','/map','/leds','/leds/stream','/favicon.ico']);
  assert(requests.every(p => allowed.has(p)), `hardware forbidden requests: ${requests.filter(p => !allowed.has(p))}`);
  assert(!requests.includes('/leds/stream') && !requests.includes('/leds'), 'unnegotiated LEDs must not be fetched');
  assert(await evaluate("!document.querySelector('.knob.live').title && !document.querySelector('#fbed').title"), 'hardware must remove inactive control titles');
  await fixture('hardware','disconnected');
  assert((await evaluate("document.querySelector('#note span').textContent")).includes('last verified'), 'disconnect labels preserved frame');
  await fixture('hardware','live');
  assert(await evaluate("!document.querySelector('#note').classList.contains('show')"), 'static generation reconnect remains live');
  await fixture('hardware','unsupported',false);
  assert((await evaluate("document.querySelector('#note span').textContent")).includes('waiting'), 'no valid frame distinguished');
  await fixture('legacy');
  assert((await evaluate("document.querySelector('#source').textContent")) === 'EMULATOR', 'historical backend=port without source remains interactive');
  await evaluate(`document.querySelector('.knob.live').dispatchEvent(new WheelEvent('wheel',{bubbles:true,deltaY:-100})); setFader(11);`);
  await pause(120);
  requests = await (await fetch(`${base}/requests`)).json();
  assert(requests.includes('/knob'), 'emulator wheel must send encoder turn');
  const full = await (await fetch(`${base}/requests?full=1`)).json();
  assert(full.includes('/xfader?pos=11'), 'emulator fader timer must send its requested position');

  await evaluate(`document.querySelector('.k[data-id="yes"]').dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Shift',bubbles:true}));`);
  await pause(100);
  requests = await (await fetch(`${base}/requests?clear=1`)).json();
  assert(requests.includes('/key') && requests.includes('/xfader'), 'emulator key event and periodic fader must remain interactive');
  await evaluate(`document.querySelector('.knob.live').dispatchEvent(new WheelEvent('wheel',{bubbles:true,deltaY:-100})); setFader(10);`);
  // Before 60/80 ms coalescers fire, a real status response revokes controls.
  await fixture('hardware');
  await pause(160);
  await evaluate(events);
  requests = await (await fetch(`${base}/requests`)).json();
  assert(!requests.includes('/key') && !requests.includes('/xfader'), `transition must not release keys or flush queued fader: ${requests}`);
  assert(!requests.includes('/knob'), 'capability transition cancels queued encoder');
  assert(await evaluate("document.querySelectorAll('.held,.down').length === 0 && !shiftHeld && pending.size === 0 && !FADER.timer"), 'capability transition clears modifiers and pending controls');
  await fetch(`${base}/requests?clear=1`);
  // Two simultaneous real browser tabs consume the shared fake backend.
  const target = await call('Target.createTarget', {url:base});
  await pause(500);
  const debugBase = 'http://' + new URL(endpoint).host;
  const tabs = await (await fetch(debugBase + '/json/list')).json();
  const second = new WebSocket(tabs.find(t => t.id === target.targetId).webSocketDebuggerUrl);
  await new Promise(resolve => second.addEventListener('open',resolve,{once:true}));
  second.addEventListener('message', e => { const r = JSON.parse(e.data); if (r.id) { const p = pending.get(r.id); pending.delete(r.id); r.error ? p.reject(r.error) : p.resolve(r.result); } });
  await evaluate(events,second); await evaluate(events); await pause(150);
  assert((await evaluate("document.querySelector('#source').textContent", second)).includes('READ ONLY'), 'second tab must identify hardware');
  second.close(); await call('Target.closeTarget',{targetId:target.targetId});
  await call('Page.navigate',{url:base}); await pause(500); await evaluate(events); await pause(150);
  requests = await (await fetch(`${base}/requests`)).json();
  assert(requests.every(p => allowed.has(p)), 'hardware reload must remain read only');
  console.log('PASS: Chromium DOM pointer/wheel/touch/keyboard/drop, primitive guards, modifier release, source transition, static live/disconnect/reconnect, emulator key/wheel/fader, two simultaneous hardware tabs/reload');
} finally { ws.close(); }
