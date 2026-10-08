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
  if (mode === 'inputs') {
    const input = {known:7, fader:0, keys:[0,0,0,0,0,0,2,127], press_counts:Array(64).fill(0), encoder_counts:Array.from({length:7}, () => [0,0])};
    input.press_counts[49] = 65535; input.encoder_counts[0] = [65535, 100];
    const patch = async data => {
      await fetch(`${base}/input-fixture?data=${encodeURIComponent(JSON.stringify(data))}`);
      await evaluate('poll()');
    };
    const key = '.k[data-id="yes"]', knob = '.knob[data-id="a"]';
    const has = (selector, cls, socket=ws) => evaluate(`document.querySelector('${selector}').classList.contains('${cls}')`, socket);
    const fader = () => evaluate(`document.querySelector('#fader-handle').getAttribute('transform')`);
    await patch({inputs:input, inputs_epoch:1, capabilities:['screen','input_observation']});
    assert(await has(key,'physical-held'), 'held-at-start key must be visible');
    assert(await evaluate(`!['transparent','rgba(0, 0, 0, 0)'].includes(getComputedStyle(document.querySelector('${key}')).backgroundColor)`), 'held physical key has a visible highlight');
    for (const id of ['a','b','c','d','e','f','level'])
      assert(await has(`.knob[data-id="${id}"]`,'physical-held'), `held-at-start ${id} encoder push must be visible`);
    assert(!(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')), 'first observation must not replay historical counters');
    assert((await fader()) === 'translate(0 0)', 'calibrated zero must be left/scene A');
    input.keys[6] = input.keys[7] = 0; input.press_counts[49] = 0; input.press_counts[56] = 1;
    input.encoder_counts[0] = [0,101]; input.fader = 127;
    await patch({inputs:input});
    assert(!(await has(key,'physical-held')) && await has(key,'physical-tap'), 'wrapped press counter must retain a released short tap');
    assert(await has(knob,'physical-tap'), 'released encoder push must retain a short tap');
    assert(await has(knob,'turn-cw') && await has(knob,'turn-ccw'), 'opposite turns between polls must show both directions');
    assert(await evaluate(`['cw','ccw'].every(direction => getComputedStyle(document.querySelector('${knob} .direction.' + direction)).display !== 'none')`), 'both directional glyphs must actually be visible');
    assert((await fader()) === 'translate(166 0)', 'calibrated 127 must be right/scene B');
    // Repeated polls must not renew 200ms tap or 250ms direction activity.
    await pause(120); await evaluate('poll()'); await pause(160);
    assert(!(await has(key,'physical-tap')) && !(await has(knob,'physical-tap')), 'same snapshot must not replay taps after 200ms');
    assert(!(await has(knob,'turn-cw')) && !(await has(knob,'turn-ccw')), 'same snapshot must not replay directions after 250ms');
    input.encoder_counts[0] = [2,101]; await patch({inputs:input});
    assert(await has(knob,'turn-cw') && !(await has(knob,'turn-ccw')), 'clockwise-only detents must show clockwise only');
    assert(await evaluate(`document.querySelectorAll('.knob.live .cap').length === 0`), 'endless physical encoders must have no absolute position marker');
    assert(await evaluate(`(() => { const knobs=[...document.querySelectorAll('.knob.live')];knobs.forEach(knob=>knob.classList.remove('skin'));const hidden=knobs.every(knob=>!knob.querySelector('.cap'));knobs.forEach(knob=>knob.classList.add('skin'));return hidden; })()`), 'fallback endless encoder drawing must have no absolute position marker');
    assert(await evaluate(`Object.entries(SKIN_KNOBS).filter(([id])=>id!=='volume').every(([,id])=>!document.querySelector('#'+id+' .pointer'))`), 'native SVG endless encoders must have no position pointer');
    input.encoder_counts[0] = [2,104]; await patch({inputs:input});
    assert(await has(knob,'turn-ccw'), 'counterclockwise detents must show counterclockwise activity');
    input.keys[6] = 2; await patch({inputs:input}); await pause(280);
    assert(await has(key,'physical-held'), 'held keys stay lit after transient timers expire');
    const target = await call('Target.createTarget', {url:base}); await pause(600);
    const tabs = await (await fetch('http://' + new URL(endpoint).host + '/json/list')).json();
    const second = new WebSocket(tabs.find(t => t.id === target.targetId).webSocketDebuggerUrl);
    await new Promise(resolve => second.addEventListener('open',resolve,{once:true}));
    second.addEventListener('message', e => { const r=JSON.parse(e.data); if(r.id) { const p=pending.get(r.id);pending.delete(r.id);r.error?p.reject(r.error):p.resolve(r.result); } });
    assert(await has(key,'physical-held',second), 'new tab sees present holds');
    assert(!(await has(key,'physical-tap',second)) && !(await has(knob,'turn-cw',second)), 'new tab must baseline historic activity independently');
    input.press_counts[49] = 1; input.encoder_counts[0][0] = 3;
    await patch({inputs:input}); await evaluate('poll()',second);
    assert(await has(key,'physical-tap') && await has(key,'physical-tap',second), 'each tab observes fresh taps independently');
    await patch({connection_state:'stale'});
    assert(!(await has(key,'physical-held')) && !(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')), 'stale observation clears holds and activity immediately');
    assert((await fader()) === 'translate(166 0)' && await evaluate(`document.querySelector('#fbed').dataset.inputState === 'stale'`), 'stale fader keeps last calibrated position with diagnostic state');
    input.press_counts[49] = 20; input.encoder_counts[0][0] = 90;
    await patch({connection_state:'live', inputs:input});
    assert(await has(key,'physical-held') && !(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')), 'reconnect establishes a baseline without replay');
    input.press_counts[49] = 25; input.encoder_counts[0][0] = 95; await patch({inputs:input, epoch:2, inputs_epoch:2});
    assert(!(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')), 'firmware epoch resets activity baseline');
    input.press_counts[49] = 26; await patch({inputs:input, epoch:3});
    assert(await has(key,'physical-tap'), 'INFO epoch must not reset activity belonging to the accepted input frame epoch');
    input.press_counts[49] = 30; await patch({inputs:input, instance_id:'restarted'});
    assert(!(await has(key,'physical-tap')), 'backend restart resets activity baseline');
    input.known = 0; input.fader = null; input.keys.fill(0); input.press_counts.fill(0); input.encoder_counts.forEach(pair=>pair.fill(0));
    await patch({inputs:input});
    assert(!(await has(key,'physical-held')) && await evaluate(`document.querySelector('#fbed').dataset.inputState === 'unknown' && getComputedStyle(document.querySelector('#fader-handle')).visibility === 'hidden'`), 'unknown input must not appear measured');
    input.known = 7; input.fader = 64; input.press_counts[49] = 100; input.encoder_counts[0] = [500,500];
    await patch({inputs:input});
    assert(!(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')), 'newly known family starts a fresh baseline');
    assert((await fader()) === 'translate(83.65354330708661 0)', 'calibrated midpoint must retain its exact position');
    // A suspended tab can miss reconnect entirely. A reboot can reuse epoch
    // 1; accepted transport incarnation must still reset counter baselines.
    await patch({inputs_connection_id:1});
    input.press_counts[49] = 0; input.encoder_counts[0] = [0,0];
    await patch({inputs:input, inputs_connection_id:2});
    assert(!(await has(key,'physical-tap')) && !(await has(knob,'turn-cw')) && !(await has(knob,'turn-ccw')), 'reconnect incarnation resets activity even when the tab missed stale status and epoch is reused');
    input.keys[6] = 2; input.press_counts[49] = 101; await patch({inputs:input});
    await patch({connection_state:'server_error'});
    assert(!(await has(key,'physical-held')) && !(await has(key,'physical-tap')), 'HTTP failure clears physical holds and activity');
    assert((await fader()) === 'translate(83.65354330708661 0)' && await evaluate(`document.querySelector('#fbed').dataset.inputState === 'stale'`), 'HTTP failure retains the last calibrated fader with stale diagnostics');
    input.press_counts[49] = 200; await patch({connection_state:'live', inputs:input});
    assert(!(await has(key,'physical-tap')), 'HTTP recovery must baseline historic activity');
    await patch({connection_state:'disconnected', inputs:null, capabilities:[]});
    assert((await fader()) === 'translate(83.65354330708661 0)' && await evaluate(`document.querySelector('#fbed').dataset.inputState === 'stale'`), 'disconnect without negotiated capabilities retains last calibrated fader');
    await patch({connection_state:'live', inputs:input, capabilities:['screen','input_observation']});
    await fetch(`${base}/requests?clear=1`); await evaluate(events); await pause(150);
    const requests = await (await fetch(`${base}/requests`)).json();
    assert(!requests.some(p=>/^\/(key|knob|xfader|audio|samples|card)/.test(p)), 'input observation capability must never enable writable hardware routes');
    input.keys[7] = 127; input.encoder_counts.forEach((pair,i) => pair[0] += i + 1);
    await patch({inputs:input});
    for (const id of ['a','b','c','d','e','f','level']) {
      assert(await has(`.knob[data-id="${id}"]`,'physical-held'), `${id} push uses its physical key bit`);
      assert(await has(`.knob[data-id="${id}"]`,'turn-cw'), `${id} direction uses its physical encoder row`);
    }
    await patch({inputs:null, capabilities:['screen']});
    assert(!(await has(key,'physical-held')) && await evaluate(`document.querySelector('#fbed').dataset.inputState === 'unknown'`), 'schema1 fallback clears observed state');
    second.close(); await call('Target.closeTarget',{targetId:target.targetId});
    console.log('PASS: physical keys/pushes, exact activity windows, directional counters/wrap, fader calibration, independent tabs, reconnect/epoch/instance baselines, unknown families, hardware route guards');
  } else if (['fader-geometry','poll-latency'].includes(mode)) {
    const input = {known:5, fader:null, keys:Array(8).fill(0), press_counts:Array(64).fill(0), encoder_counts:Array.from({length:7},()=>[0,0])};
    const patch = data => fetch(`${base}/input-fixture?data=${encodeURIComponent(JSON.stringify(data))}`);
    const geometry = () => evaluate(`(() => {
      const handle=document.querySelector('#fader-handle'), stage=document.querySelector('#stage');
      const box=handle.getBoundingClientRect(), panel=stage.getBoundingClientRect();
      return {visibility:getComputedStyle(handle).visibility, display:getComputedStyle(handle).display,
        x:box.x,y:box.y,width:box.width,height:box.height,
        panel:{x:panel.x,y:panel.y,width:panel.width,height:panel.height},
        viewport:{width:innerWidth,height:innerHeight}, transform:handle.getAttribute('transform'), state:document.querySelector('#fbed').dataset.inputState};
    })()`);
    const statuses = () => evaluate(`performance.getEntriesByType('resource').filter(entry=>new URL(entry.name).pathname==='/status').map(entry=>({start:entry.startTime,duration:entry.duration}))`);
    const until = async (condition,message) => {
      const deadline=performance.now()+2000;
      while (!(await condition())) { assert(performance.now()<deadline,message); await pause(5); }
    };
    await patch({inputs:input,inputs_epoch:1,capabilities:['screen','input_observation']});
    await until(async () => (await statuses()).length>=4,'automatic status polling must observe negotiated inputs');
    if (mode==='fader-geometry') {
      assert((await geometry()).visibility==='hidden', 'known5/null from actual hardware cache must not invent a fader position');
      const positions=[];
      for (const [width,height] of [[1400,900],[1100,500],[900,340],[756,413]]) {
        await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
        await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
        for (const pos of [0,64,127]) {
          input.known=7;input.fader=pos;await patch({inputs:input});await evaluate('poll()');
          const g=await geometry();positions.push({pos,...g});
          assert(g.visibility==='visible' && g.display!=='none' && g.width>0 && g.height>0,'measured fader must actually have visible geometry');
          assert(g.x>=g.panel.x && g.y>=g.panel.y && g.x+g.width<=g.panel.x+g.panel.width && g.y+g.height<=g.panel.y+g.panel.height,'measured fader must remain inside the scaled panel');
          assert(g.x>=0 && g.y>=0 && g.x+g.width<=g.viewport.width && g.y+g.height<=g.viewport.height,'measured fader must remain inside desktop and short browser viewports');
          assert(g.panel.x>=0 && g.panel.y>=0 && g.panel.x+g.panel.width<=width && g.panel.y+g.panel.height<=height,'entire panel must remain inside desktop and short viewports');
          assert(await evaluate(`['#card','#hp','.knob.pot'].every(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return r.x>=0 && r.y>=0 && r.right<=innerWidth && r.bottom<=innerHeight})`),'top controls must remain visible after scaling');
        }
        const samples=positions.slice(-3),scale=samples[0].panel.width/1250;
        assert(samples[0].x<samples[1].x && samples[1].x<samples[2].x,'fader must visibly travel from scene A through midpoint to scene B');
        assert(Math.abs((samples[2].x-samples[0].x)-166*scale)<.01,'full physical range must move the handle across its complete travel');
      }
      console.log('PASS: unknown fader stays unmeasured; desktop/short Chrome endpoint/midpoint geometry '+JSON.stringify(positions.map(({pos,x,y,width,height,viewport})=>({pos,x,y,width,height,viewport}))));
    } else {
      input.known=7;input.fader=0;await patch({inputs:input});
      await until(async ()=>(await geometry()).state==='live','hardware input cadence must begin after capability negotiation');
      const latencies=[];
      for (const pos of [60,61,62,63]) {
        const count=(await statuses()).length;
        await until(async ()=>(await statuses()).length>count,'next automatic status poll must finish');
        await pause(20);
        input.fader=pos;const started=performance.now();await patch({inputs:input});
        await until(async ()=>(await geometry()).transform===`translate(${pos/127*166} 0)`,'automatic polling must show the new cached fader');
        latencies.push(performance.now()-started);
      }
      const samples=(await statuses()).slice(-8), periods=samples.slice(1).map((entry,i)=>entry.start-samples[i].start);
      const mean=periods.reduce((a,b)=>a+b,0)/periods.length;
      console.log('MEASURE: cached hardware browser periods_ms='+JSON.stringify(periods)+' mean_ms='+mean.toFixed(1)+' update_latencies_ms='+JSON.stringify(latencies));
      assert(mean<180 && Math.max(...latencies)<200,'cached hardware inputs must reach the browser within a 100ms poll cadence plus local response/render overhead');
      await patch({connection_state:'server_error'});
      await until(async()=>await evaluate(`document.querySelector('#phase').textContent==='server unreachable'`),'status failure must remain visible in header');
      const failures=(await statuses()).length;
      await until(async()=>(await statuses()).length>=failures+3,'failed status requests must continue with existing backoff');
      const failureSamples=(await statuses()).slice(-3),failurePeriods=failureSamples.slice(1).map((entry,i)=>entry.start-failureSamples[i].start);
      assert(failurePeriods.every(period=>period>=300),'status failure must revoke fast polling and retain existing 350ms backoff');
      assert(await evaluate(`document.querySelector('#phase').textContent==='server unreachable' && getComputedStyle(document.querySelector('#note')).display==='none'`),'failure backoff preserves header errors and quiet LCD');
      console.log('MEASURE: status-failure browser backoff periods_ms='+JSON.stringify(failurePeriods));
      await fetch(`${base}/fixture?source=legacy&state=live&frame=1`);
      await until(async()=> await evaluate(`source==='port'`),'automatic polling must identify the emulator');
      const count=(await statuses()).length;
      await until(async()=>(await statuses()).length>=count+4,'emulator status polling must continue');
      const emulatorSamples=(await statuses()).slice(-4), emulatorPeriods=emulatorSamples.slice(1).map((entry,i)=>entry.start-emulatorSamples[i].start);
      assert(emulatorPeriods.every(period=>period>=300),'emulator must retain its existing 350ms status cadence');
      console.log('MEASURE: unchanged emulator browser periods_ms='+JSON.stringify(emulatorPeriods));
      console.log('PASS: automatic cached HTTP input polling is responsive without manual polls or USB acquisition');
    }
  } else if (mode === 'quiet-refresh') {
    const quiet = () => evaluate(`!document.querySelector('#phase').textContent.includes('syncing') && getComputedStyle(document.querySelector('#note')).display === 'none' && getComputedStyle(document.querySelector('#screen')).opacity === '1'`);
    await fetch(`${base}/image-gate?hold=1`);
    await fixture('hardware','live',true,2);
    assert(await quiet(), 'routine refresh must leave full-brightness LCD visible without syncing label or overlay');
    await fetch(`${base}/image-gate?hold=0`); await pause(150);
    await fixture('hardware','syncing');
    assert(await quiet() && await evaluate(`document.querySelector('#phase').textContent === ''`), 'backend syncing state must not appear in header or cover the LCD');
    await fixture('hardware','server_error');
    assert(await quiet() && await evaluate(`document.querySelector('#phase').textContent === 'server unreachable'`), 'errors stay in header without an LCD overlay or dimming');
    await fetch(`${base}/image-gate?hold=1`);
    await call('Page.navigate',{url:base}); await pause(500);
    assert(await evaluate(`!document.querySelector('#screen').hasAttribute('src') && getComputedStyle(document.querySelector('#note')).display === 'none'`), 'before first verified frame the LCD stays neutral blank');
    await fetch(`${base}/image-gate?hold=0`);
    console.log('PASS: quiet refresh, full-brightness retained LCD, header-only errors, neutral initial screen');
  } else if (mode === 'no-frame-failure') {
    const label = () => evaluate("document.querySelector('#phase').textContent");
    assert((await label()).includes('unreachable'), 'first status failure must report unreachable in the header');
    await fixture('hardware','connecting',false);
    await fixture('hardware','server_error',false);
    assert((await label()).includes('unreachable'), 'transport failure remains in the header before a verified frame');
    await fixture('hardware','live',true);
    await evaluate(`new Promise((resolve, reject) => {
      const deadline = Date.now() + 3000;
      const check = () => hasVerifiedFrame ? resolve(true) : Date.now() > deadline ? reject(new Error('image did not load')) : setTimeout(check, 20);
      check();
    })`);
    await fixture('hardware','server_error',true);
    assert((await label()) === 'server unreachable', 'transport failure after a frame must report its error in the header');
    console.log('PASS: real Chromium first-status/no-frame failure and verified-frame preservation');
  } else if (['lifecycle', 'image-failure', 'late-image'].includes(mode)) {
    const screens = async () => (await (await fetch(`${base}/requests?full=1`)).json()).filter(p => p.startsWith('/screen.png?'));
    const label = () => evaluate("document.querySelector('#phase').textContent");
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
      assert((await label()) === 'server unreachable', 'HTTP failure remains visible in the header');
      await fixture('hardware', 'connecting', false, 1, {instance_id:'third', image_red:0});
      assert((await label()) === 'connecting' && (await pixel()) === 255, 'new backend without a frame keeps old pixels while the header reports connecting');
      await fixture('hardware', 'live'); await shown(0);
      const count = (await screens()).length;
      await fixture('hardware'); await fixture('hardware'); await pause(400);
      assert((await screens()).length === count, 'unchanged lifecycle/publication avoids image fetch');
      assert((await phase()) === 'live', 'loaded current publication is live');
      console.log('PASS: Chromium same-seq restart with/without observed HTTP failure/no-frame status');
    } else if (mode === 'image-failure') {
      await fixture('hardware', 'live', true, 2, {image_fail:1});
      await until(async () => (await phase()) === 'image unavailable', 'failed PNG must not be labelled live');
      assert((await pixel()) === 0 && (await label()) === 'image unavailable', 'failed PNG preserves last decoded pixels with its error in the header');
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
      assert((await label()) === 'image unavailable', 'first failed PNG reports its error in the header');
      await fixture('hardware', 'live', true, null, {image_fail:0}); await shown(0);
      console.log('PASS: Chromium invalid PNG retains pixels, bounded poll retry recovers, first-frame failure remains waiting');
    } else {
      await fetch(`${base}/image-gate?hold=1`);
      const count = (await screens()).length;
      await fixture('hardware', 'live', true, 2);
      await until(async () => (await screens()).length > count, 'delayed image must reach server');
      assert((await pixel()) === 0 && (await phase()) === 'live', 'pending image retains pixels while connection remains live');
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
    assert((await evaluate("document.querySelector('#phase').textContent")) === 'disconnected', 'header reports disconnect beside retained PNG');
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
  assert((await evaluate("document.querySelector('#phase').textContent")) === 'disconnected', 'disconnect stays visible in header');
  await fixture('hardware','live');
  assert(await evaluate("!document.querySelector('#note').classList.contains('show')"), 'static generation reconnect remains live');
  await fixture('hardware','unsupported',false);
  assert((await evaluate("document.querySelector('#phase').textContent")) === 'unsupported', 'unsupported source stays visible in header');
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
  assert(await evaluate("document.querySelectorAll('.knob.live .cap').length === 0 && document.querySelector('.knob.live').classList.contains('turn-cw')"), 'emulator encoder turns show relative direction without a position marker');
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
