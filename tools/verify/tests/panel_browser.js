// Optional real-browser instrument: Node's built-in WebSocket, no npm packages.
const endpoint = process.argv[2], base = process.argv[3], mode = process.argv[4] || "capabilities";
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
async function fixture(source, state = 'live', hasFrame = true, seq = null, extra = {}) {
  await fetch(`${base}/fixture?source=${source}&state=${state}&frame=${hasFrame ? 1 : 0}${seq == null ? '' : '&seq=' + seq}&${new URLSearchParams(extra)}`);
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
  if (mode === 'no-frame-failure') await fetch(`${base}/fixture?source=hardware&state=server_error&frame=0`);
  await call('Page.navigate', {url:base});
  await pause(800);
  if (mode === 'no-frame-failure') {
    const label = () => evaluate("document.querySelector('#note span').textContent");
    assert((await label()).includes('waiting'), 'first status failure must wait for a verified frame');
    await fixture('hardware','connecting',false);
    await fixture('hardware','server_error',false);
    assert((await label()).includes('waiting'), 'no-frame status followed by transport failure must keep waiting');
    await fixture('hardware','live',true);
    await evaluate(`new Promise((resolve, reject) => {
      const deadline = Date.now() + 3000;
      const check = () => hasVerifiedFrame ? resolve(true) : Date.now() > deadline ? reject(new Error('image did not load')) : setTimeout(check, 20);
      check();
    })`);
    await fixture('hardware','server_error',true);
    assert((await label()).includes('last verified'), 'transport failure after a frame must label the retained frame');
    console.log('PASS: real Chromium first-status/no-frame failure and verified-frame preservation');
  } else if (['lifecycle', 'image-failure', 'late-image'].includes(mode)) {
    const screens = async () => (await (await fetch(`${base}/requests?full=1`)).json()).filter(p => p.startsWith('/screen.png?'));
    const label = () => evaluate("document.querySelector('#note span').textContent");
    const phase = () => evaluate("document.querySelector('#phase').textContent");
    const pixel = () => evaluate(`(() => {
      const img = document.querySelector('#screen');
      if (!img.complete || !img.naturalWidth) return null;
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
      const context = canvas.getContext('2d'); context.drawImage(img, 0, 0);
      return context.getImageData(0, 0, 1, 1).data[0];
    })()`);
    async function until(condition, message, timeout = 4000) {
      const deadline = Date.now() + timeout;
      while (!(await condition())) { assert(Date.now() < deadline, message); await pause(20); }
    }
    const shown = red => until(async () => (await pixel()) === red, `rendered PNG must be red=${red}`);
    await shown(0);
    if (mode === 'lifecycle') {
      // A fast host restart can occur wholly between successful status polls.
      await fixture('hardware', 'live', true, 1, {instance_id:'second', image_red:255});
      await shown(255);
      await fixture('hardware', 'server_error');
      assert((await label()).includes('last verified'), 'HTTP failure retains verified display label');
      await fixture('hardware', 'connecting', false, 1, {instance_id:'third', image_red:0});
      assert((await label()).includes('last verified'), 'new backend without a frame retains old pixels honestly');
      await fixture('hardware', 'live'); await shown(0);
      const count = (await screens()).length;
      await fixture('hardware'); await fixture('hardware'); await pause(400);
      assert((await screens()).length === count, 'unchanged lifecycle/publication avoids image fetch');
      assert((await phase()) === 'live', 'loaded current publication is live');
      console.log('PASS: Chromium same-seq restart with/without observed HTTP failure/no-frame status');
    } else if (mode === 'image-failure') {
      await fixture('hardware', 'live', true, 2, {image_fail:1});
      await until(async () => (await phase()) === 'image unavailable', 'failed PNG must not be labelled live');
      assert((await pixel()) === 0 && (await label()).includes('last verified'), 'failed PNG preserves last decoded pixels with stale label');
      const count = (await screens()).length;
      for (let i=0; i<5; i++) await fixture('hardware');
      assert((await screens()).length === count, 'failure retry is rate bounded across later polls');
      await fixture('hardware', 'live', true, null, {image_fail:0});
      await shown(255);
      assert((await phase()) === 'live', 'successful later retry restores live');
      assert((await screens()).length > count, 'retry reaches the no-store server instead of reusing a failed response');
      // A first-frame failure must not claim a locally verified display.
      await fetch(`${base}/fixture?source=hardware&state=live&frame=1&seq=1&image_fail=1`);
      await call('Page.navigate', {url:base});
      await until(async () => (await phase()) === 'image unavailable', 'first failed image must report unavailable');
      assert((await label()).includes('waiting'), 'first failed PNG waits for verified pixels');
      await fixture('hardware', 'live', true, null, {image_fail:0}); await shown(0);
      console.log('PASS: Chromium invalid PNG retains pixels, bounded poll retry recovers, first-frame failure remains waiting');
    } else {
      await fetch(`${base}/image-gate?hold=1`);
      const count = (await screens()).length;
      await fixture('hardware', 'live', true, 2);
      await until(async () => (await screens()).length > count, 'delayed image must reach server');
      assert((await pixel()) === 0 && (await phase()) !== 'live', 'pending image retains pixels with non-live status');
      await fixture('hardware', 'live', true, 3, {image_red:128});
      await fetch(`${base}/image-gate?hold=0`); await shown(128); await pause(150);
      assert((await pixel()) === 128, 'late superseded image must not replace newer publication');
      await fetch(`${base}/image-gate?hold=1`);
      await fixture('hardware', 'live', true, 4, {image_red:255});
      await until(async () => (await phase()) === 'image unavailable', 'in-flight image timeout must be finite', 7000);
      assert((await pixel()) === 128, 'timed-out image retains previous pixels');
      await fixture('legacy', 'live', true, 1);
      await fetch(`${base}/image-gate?hold=0`); await shown(0); await pause(200);
      assert((await pixel()) === 0, 'late hardware image cannot replace emulator screen');
      console.log('PASS: Chromium finite image timeout, newer-publication and source-switch late-completion guards');
    }
  } else if (mode === 'publication') {
    const screens = async () => (await (await fetch(`${base}/requests?full=1`)).json()).filter(p => p.startsWith('/screen.png?'));
    const pixel = () => evaluate(`(() => {
      const img = document.querySelector('#screen');
      if (!img.complete || !img.naturalWidth) return null;
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
      const context = canvas.getContext('2d'); context.drawImage(img, 0, 0);
      return context.getImageData(0, 0, 1, 1).data[0];
    })()`);
    async function shown(red) {
      const deadline = Date.now() + 3000;
      while ((await pixel()) !== red) {
        assert(Date.now() < deadline, `published PNG must actually render red=${red}; requests=${await screens()}; image=${await evaluate("JSON.stringify({ready:document.readyState, src:document.querySelector('#screen').src, width:document.querySelector('#screen').naturalWidth,phase:document.querySelector('#phase').textContent})")}`);
        await pause(20);
      }
    }
    async function requested(count) {
      const deadline = Date.now() + 3000;
      while ((await screens()).length < count) {
        assert(Date.now() < deadline, `expected ${count} PNG requests; got ${await screens()}`);
        await pause(20);
      }
    }
    await shown(0);
    assert((await screens()).length === 1, 'initial hardware publication must request one PNG');
    await fixture('hardware', 'disconnected');
    assert((await pixel()) === 0, 'disconnect retains rendered verified PNG');
    assert((await evaluate("document.querySelector('#note span').textContent")).includes('last verified'), 'retained PNG is labelled stale');
    await fixture('hardware', 'live', true, 2);
    await shown(255);
    assert(JSON.stringify((await screens()).map(p => { const q = new URL(p, base).searchParams; return [q.get('instance'), q.get('seq')]; })) === JSON.stringify([['first', '1'], ['first', '2']]), 'same device generation with new host seq/epoch/connection must fetch new publication');
    await fixture('hardware'); await fixture('hardware'); await pause(400);
    assert((await screens()).length === 2, 'unchanged host publication must not refetch PNG');
    await fixture('legacy', 'live', true, 1); await requested(3); await shown(0);
    assert((await screens()).length === 3, 'source transition renders emulator publication');
    await fixture('hardware', 'live', true, 2); await requested(4); await shown(255);
    assert((await screens()).length === 4, 'returning hardware source renders current publication');
    await call('Page.navigate', {url:base}); await pause(600); await shown(255);
    assert((await screens()).length === 5, 'reload renders the current hardware publication');
    assert((await evaluate("document.querySelector('#source').textContent")).includes('READ ONLY'), 'reconnected/reloaded hardware remains read only');
    console.log('PASS: Chromium renders distinct PNG pixels across same-generation reconnect, retains stale pixels, suppresses unchanged seq, and preserves source-switch/reload rendering');
  } else if (mode === 'delayed-audio') {
    await fixture('legacy');
    await fetch(`${base}/audio-gate?hold=1`);
    await evaluate('window.pendingAudio = refreshAudioStatus(); true');
    const deadline = Date.now() + 3000;
    while (!(await (await fetch(`${base}/requests`)).json()).includes('/audio/status')) {
      assert(Date.now() < deadline, 'emulator audio request must reach the delayed fake server');
      await pause(20);
    }
    await fixture('hardware');
    assert((await evaluate("document.querySelector('#hp').title")) === '', 'hardware transition clears HEAD-PHONES title');
    await fetch(`${base}/audio-gate?hold=0`);
    await evaluate('window.pendingAudio.then(() => true)');
    assert((await evaluate("document.querySelector('#hp').title")) === '', 'delayed emulator audio response must not restore hardware HEAD-PHONES control title');
    assert(await evaluate("document.querySelector('#soundbtn').disabled && !document.querySelector('#soundbtn').title"), 'delayed audio response keeps hardware SOUND disabled without a control title');
    console.log('PASS: real Chromium delayed emulator audio response cannot restore hardware control tooltips');
  } else {
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
  assert((await evaluate("document.querySelector('#note span').textContent")).includes('last verified'), 'backend without a frame retains the locally verified pixels honestly');
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
  }
} finally { ws.close(); }
