const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const api = fs.readFileSync(path.join(__dirname, '../src/gui/static/api.js'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../src/gui/static/app.js'), 'utf8');
const streamSource = app.slice(app.indexOf('async function streamChat('), app.indexOf('async function send()'));
function fixture(fetch) {
  const events = [];
  const context = {fetch, TextDecoder, CustomEvent: class {constructor(type) {this.type = type;}},
    window: {dispatchEvent(event) {events.push(event.type);}}};
  vm.createContext(context); vm.runInContext(api + '\n' + streamSource, context);
  return {context, events};
}
test('HTTP and stream errors produce the same client error', async () => {
  const payload = {code: 'not_found', message: 'Session not found', status: 404};
  const {context} = fixture(async () => ({ok: true, status: 200, body: {getReader() {
    return {read: async () => ({done: false, value: new TextEncoder().encode(JSON.stringify({type:'error', ...payload})+'\n')}), cancel: async () => {}, releaseLock() {}};
  }}}));
  let plain;
  try { await context.window.harnessReadResponse({ok: false, status:404, json:async () => payload}); }
  catch (e) { plain=e; }
  await assert.rejects(context.streamChat({}, () => {}), e => e.code === plain.code && e.message === plain.message && e.status === plain.status);
});
test('authentication errors request login; non-JSON failures are readable', async () => {
  const {context,events} = fixture(async () => {});
  await assert.rejects(context.window.harnessReadResponse({ok:false,status:401,json:async()=>({})}), /重新登录/);
  assert.deepEqual(events,['harness-auth-expired']);
  await assert.rejects(context.window.harnessReadResponse({ok:false,status:502,json:async()=>{throw new Error('HTML')}}), /502/);
  await assert.rejects(context.window.harnessReadResponse({ok:false,status:400,json:async()=>({detail:'legacy error'})}), /legacy error/);
});
test('failed fetch is not automatically resubmitted', async () => {
  let calls=0;
  const {context}=fixture(async()=>{calls++;throw new TypeError('Failed to fetch');});
  await assert.rejects(context.window.harnessFetch('/api/chat', {method:'POST'}), /请勿直接重复提交/);
  assert.equal(calls,1);
});
test('successful response with explicit backend error cannot appear successful', async () => {
  const {context}=fixture(async()=>{});
  await assert.rejects(context.window.harnessReadResponse({ok:true,status:200,json:async()=>({error:'backend failed'})}), /backend failed/);
});
test('legacy errors wrapped in a result event use the transport error path', async () => {
  let cancelled=0;
  const {context}=fixture(async()=>({ok:true,status:200,body:{getReader(){return {
    read:async()=>({done:false,value:new TextEncoder().encode(JSON.stringify({type:'result',data:{error:'legacy backend failure',session_id:'kept'}})+'\n')}),
    cancel:async()=>{cancelled++},releaseLock(){},
  }}}}));
  await assert.rejects(context.streamChat({},()=>{}),e=>e.message==='legacy backend failure' && e.sessionId==='kept');
  assert.equal(cancelled,1);
});
test('legacy panel mutations use the common parser and never render failed responses', async () => {
  let rendered=0;
  const statuses=[];
  const {context,events}=fixture(async()=>({ok:false,status:401,json:async()=>({message:'请重新登录',code:'authentication_required',status:401})}));
  context.renderAsk=()=>{rendered++};context.setStatus=(...args)=>statuses.push(args);
  vm.runInContext(app.slice(app.indexOf('async function removeAskQueued('),app.indexOf('async function clearAskHistory(')),context);
  await context.removeAskQueued(0);
  assert.equal(rendered,0);assert.deepEqual(events,['harness-auth-expired']);
  assert.match(statuses[0][1],/请重新登录/);
});
