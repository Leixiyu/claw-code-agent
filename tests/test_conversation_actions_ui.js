const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../src/gui/static/app.js'),'utf8');
const actions=source.slice(source.indexOf('function resetConversationView('),source.indexOf('// Transcript rendering'));
function fixture(fail=false) {
  const calls=[];
  const State={activeSessionId:'old',isBusy:false,sessions:['saved']};
  const els={input:{value:'draft',focus(){}},usageMeta:{textContent:'old tokens'}};
  const ctx={State,els,window:{refreshHarnessUsage(){calls.push('usage')}},autoSizeInput(){},
    clearChat(){calls.push('clear-view')},clearPasteStash(){calls.push('clear-paste')},renderSessions(){},
    setStatus(...s){calls.push(s)},setBusy(b){State.isBusy=b},
    async apiPost(url){calls.push(url);if(fail)throw new Error('busy conflict')},
  };
  vm.createContext(ctx);vm.runInContext(actions,ctx);
  return {ctx,calls,State,els};
}
test('new conversation clears draft and current ID locally, without deleting saved history',()=>{
  const {ctx,calls,State,els}=fixture();ctx.newSession();
  assert.equal(State.activeSessionId,null);assert.deepEqual(State.sessions,['saved']);
  assert.equal(els.input.value,'');assert.equal(els.usageMeta.textContent,'');
  assert.equal(calls.some(c=>typeof c==='string' && c.startsWith('/api/')),false);
});
test('clear state uses only the runtime endpoint; failure preserves the conversation and draft',async()=>{
  for(const fail of [false,true]){
    const {ctx,calls,State,els}=fixture(fail);await ctx.clearRuntimeState();
    assert.equal(calls.filter(c=>c==='/api/clear').length,1);
    assert.equal(State.activeSessionId,fail?'old':null);assert.equal(els.input.value,fail?'draft':'');
    assert.deepEqual(State.sessions,['saved']);assert.equal(State.isBusy,false);
  }
});
test('busy browser cannot start a new conversation or reset runtime state',async()=>{
  const {ctx,calls,State}=fixture();State.isBusy=true;ctx.newSession();await ctx.clearRuntimeState();
  assert.equal(State.activeSessionId,'old');assert.equal(calls.includes('/api/clear'),false);
  assert.equal(calls.includes('clear-view'),false);
});
test('slash clear result resets the current session; explicit null never resurrects an old ID',()=>{
  const {ctx,calls,State}=fixture();
  ctx.applyConversationResult({session_id:null,stop_reason:'state_cleared'});
  assert.equal(State.activeSessionId,null);assert.equal(calls.includes('clear-view'),true);
  State.activeSessionId='old';ctx.applyConversationResult({session_id:null});assert.equal(State.activeSessionId,null);
  ctx.applyConversationResult({session_id:'new'});assert.equal(State.activeSessionId,'new');
});
