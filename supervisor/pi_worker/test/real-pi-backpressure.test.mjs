import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { createOpenRouterBackpressureFetch } from "../src/openrouter-backpressure.mjs";
import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";
import { waitFor } from "./helpers.mjs";

const MODEL={id:"offline-smoke",name:"Offline",provider:"openrouter",api:"openai-completions",
  baseUrl:"https://openrouter.ai/api/v1",reasoning:false,input:["text"],cost:{input:1,output:1,cacheRead:0,cacheWrite:0},contextWindow:32768,maxTokens:1024};
const transient=()=>new Response(JSON.stringify({error:{code:402,message:"fixed offline 402",metadata:{limit_source:"openrouter_in_flight_budget",reason:"in_flight_budget_exhausted"}}}),{status:402,headers:{"content-type":"application/json","retry-after":"0"}});
function sse(tool=false){
  const delta=Array.isArray(tool)?{role:"assistant",tool_calls:tool}:tool?{role:"assistant",tool_calls:[{index:0,id:"call-one",type:"function",function:{name:"offline_check",arguments:'{"value":3}'}}]}:{role:"assistant",content:"Finished once."};
  const payload={id:"completion-one",object:"chat.completion.chunk",model:"offline-smoke",created:1,choices:[{index:0,delta,finish_reason:null}]};
  const finish={...payload,choices:[{index:0,delta:{},finish_reason:tool?"tool_calls":"stop"}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}};
  return new Response(`data: ${JSON.stringify(payload)}\n\ndata: ${JSON.stringify(finish)}\n\ndata: [DONE]\n\n`,{headers:{"content-type":"text/event-stream"}});
}

test("real pinned Pi HTTP adapter sees one accepted stream after transient 402",async()=>{
  let requests=0; const bodies=[];
  const fetch=createOpenRouterBackpressureFetch(async(_url,init)=>{requests++;bodies.push(init.body);return requests===1?transient():sse();});
  const stream=streamSimple(MODEL,{messages:[{role:"user",content:"Offline only",timestamp:1}]},{apiKey:"offline-not-real",fetch});
  const events=[]; for await(const event of stream)events.push(event);
  const result=await stream.result();
  assert.equal(requests,2);assert.equal(bodies[0],bodies[1]);assert.equal(result.stopReason,"stop");
  assert.equal(events.filter(e=>e.type==="start").length,1);assert.equal(events.filter(e=>e.type==="done").length,1);
  assert.equal(events.filter(e=>e.type==="error").length,0);
  assert.equal(result.content.filter(b=>b.type==="text").map(b=>b.text).join(""),"Finished once.");
});

test("real pinned Pi adapter keeps unknown 402 code/body and does not retry",async()=>{
  let requests=0;
  const fetch=createOpenRouterBackpressureFetch(async()=>{requests++;return new Response(JSON.stringify({error:{code:402,message:"fixed permanent rejection",metadata:{limit_source:"openrouter_credits",reason:"weight_exceeds_budget"}}}),{status:402,headers:{"content-type":"application/json","retry-after":"0"}});});
  const result=await streamSimple(MODEL,{messages:[{role:"user",content:"Offline only",timestamp:1}]},{apiKey:"offline-not-real",fetch}).result();
  assert.equal(requests,1);assert.equal(result.stopReason,"error");assert.match(result.errorMessage,/402/);assert.match(result.errorMessage,/weight_exceeds_budget/);
});

test("real pinned Pi adapter never replays accepted stream after partial output and error",async()=>{
  let requests=0;
  const fetch=createOpenRouterBackpressureFetch(async()=>{
    requests++;
    const first={id:"partial",object:"chat.completion.chunk",model:"offline-smoke",created:1,choices:[{index:0,delta:{role:"assistant",content:"partial"},finish_reason:null}]};
    const failure={error:{code:402,message:"midstream failure",metadata:{limit_source:"openrouter_in_flight_budget",reason:"in_flight_budget_exhausted"}},choices:[{index:0,delta:{},finish_reason:"error"}]};
    return new Response(`data: ${JSON.stringify(first)}\n\ndata: ${JSON.stringify(failure)}\n\ndata: [DONE]\n\n`,{headers:{"content-type":"text/event-stream","retry-after":"0"}});
  });
  const stream=streamSimple(MODEL,{messages:[{role:"user",content:"Offline only",timestamp:1}]},{apiKey:"offline-not-real",fetch});
  const events=[];for await(const event of stream)events.push(event);
  assert.equal(requests,1);assert.equal(events.filter(e=>e.type==="start").length,1);
  assert.equal((await stream.result()).stopReason,"error");
});

for (const enabled of [false,true]) {
test(`real Bello/Pi session asyncAfterTurn ${enabled?"ON":"OFF"} preserves payload hooks and executes a tool once`,async(t)=>{
  const directory=mkdtempSync(join(tmpdir(),"bello-backpressure-sdk-proof-"));
  const agentDir=join(directory,"agent"); const cwd=join(directory,"workspace");mkdirSync(agentDir);mkdirSync(cwd);
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const runtime=await realPiSdk.createModelRuntime({agentDir,allowModelNetwork:false});
  runtime.hasConfiguredAuth=()=>true;
  let fetchCalls=0, modelCalls=0, toolCalls=0,afterTurns=0;const sent=[];
  runtime.streamSimple=(model,context,options)=>{
    modelCalls++;
    assert.equal(typeof options.fetch,"function","Bello-installed session hook supplies fetch");
    // Invoke the real pinned HTTP adapter with a fake transport through the
    // installed fetch hook. The original global fetch is blocked below.
    return streamSimple(model,context,{...options,apiKey:"offline-not-real"});
  };
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{
    assert.equal(String(url),"https://openrouter.ai/api/v1/chat/completions");
    fetchCalls++;sent.push(init.body);
    return fetchCalls===1?transient():sse(fetchCalls===2);
  };
  t.after(()=>{globalThis.fetch=originalFetch;});
  const session=await realPiSdk.createSession({cwd,agentDir,modelRuntime:runtime,model:MODEL,thinkingLevel:"off",
    sessionManager:SessionManager.inMemory(cwd),customTools:[{name:"offline_check",label:"Offline check",description:"Offline deterministic check",
      parameters:{type:"object",properties:{value:{type:"number"}},required:["value"]},
      async execute(){toolCalls++;return{content:[{type:"text",text:"Checked once"}],details:{}};}}],
    activeToolNames:["offline_check"],developerInstructions:"Offline proof",
    requestOptions:{current:{nativeEffort:"high",serviceTier:"priority"}},
    asyncAfterTurn:enabled?async()=>{afterTurns++;}:undefined});
  t.after(()=>session.dispose());session.agent.getApiKey=async()=>"offline-not-real";
  const assistantMessages=[];
  session.subscribe(event=>{if(event.type==="message_end"&&event.message.role==="assistant")assistantMessages.push(event.message);});
  await session.prompt("Run the offline check",{expandPromptTemplates:false});
  assert.equal(modelCalls,2);assert.equal(fetchCalls,3);assert.equal(toolCalls,1);assert.equal(assistantMessages.length,2);
  assert.equal(sent[0],sent[1]);
  assert.equal(afterTurns,enabled?2:0);
  for(const body of sent){
    assert.equal(JSON.parse(body).reasoning.effort,"high");
    assert.equal(JSON.parse(body).service_tier,"priority");
  }
  assert.equal(assistantMessages.filter(m=>m.stopReason==="error").length,0);
  assert.equal(assistantMessages.filter(m=>m.content.some(b=>b.type==="toolCall")).length,1);
});
}

test("real Bello/Pi async coordinator wakes only on ready output across HTTP backpressure",async(t)=>{
  const directory=mkdtempSync(join(tmpdir(),"bello-backpressure-async-proof-"));
  const agentDir=join(directory,"agent"),cwd=join(directory,"workspace");mkdirSync(agentDir);mkdirSync(cwd);
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const runtime=await realPiSdk.createModelRuntime({agentDir,allowModelNetwork:false});runtime.hasConfiguredAuth=()=>true;
  const scheduler=new AsyncToolCoordinator({graceMs:5});
  let finishSlow;const slow=new Promise(resolve=>{finishSlow=resolve;});
  let modelCalls=0,fetchCalls=0,toolCalls=0,afterTurns=0,session;const sent=[];
  runtime.streamSimple=(model,context,options)=>{modelCalls++;return streamSimple(model,context,{...options,apiKey:"offline-not-real"});};
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{
    assert.equal(String(url),"https://openrouter.ai/api/v1/chat/completions");
    fetchCalls++;sent.push(init.body);
    return fetchCalls===1?transient():sse(fetchCalls===2?[
      {index:0,id:"fast-call",type:"function",function:{name:"offline_check",arguments:'{"value":1}'}},
      {index:1,id:"slow-call",type:"function",function:{name:"offline_check",arguments:'{"value":3}'}}
    ]:false);
  };
  t.after(()=>{globalThis.fetch=originalFetch;});
  session=await realPiSdk.createSession({cwd,agentDir,modelRuntime:runtime,model:MODEL,thinkingLevel:"off",
    sessionManager:SessionManager.inMemory(cwd),customTools:[{name:"offline_check",label:"Offline check",description:"Offline deterministic slow check",executionMode:"parallel",
      parameters:{type:"object",properties:{value:{type:"number"}},required:["value"]},
      execute:(id,args,signal)=>scheduler.execute(id,"offline_check",async()=>{toolCalls++;if(args.value===3)await slow;return{content:[{type:"text",text:args.value===3?"Slow check finished":"Fast check finished"}],details:{}};},signal)}],
    activeToolNames:["offline_check"],developerInstructions:"Wait for requested work only.",requestOptions:{current:{}},
    asyncAfterTurn:async(turn)=>{
      afterTurns++;
      const results=await scheduler.ready({wait:!turn.message.content.some(block=>block.type==="toolCall")});
      if(results.length)await session.sendCustomMessage(lateToolMessage(results),{deliverAs:"steer"});
    }});
  t.after(async()=>{finishSlow();await scheduler.cancel();session.dispose();});
  session.agent.getApiKey=async()=>"offline-not-real";
  session.subscribe(event=>{if(event.type==="message_end"&&event.message.role==="assistant")scheduler.beginBatch(event.message.content.filter(block=>block.type==="toolCall").map(block=>block.id));});
  const running=session.prompt("Run and wait for the slow check",{expandPromptTemplates:false});
  await waitFor(()=>afterTurns===2);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(modelCalls,2);assert.equal(fetchCalls,3);assert.equal(toolCalls,2);
  assert.ok(JSON.stringify(JSON.parse(sent[2]).messages).includes("still running"));
  finishSlow();await running;
  assert.equal(modelCalls,3);assert.equal(fetchCalls,4);assert.equal(afterTurns,3);assert.equal(toolCalls,2);
  assert.equal(sent[0],sent[1]);
  const earlier=JSON.parse(sent[2]).messages,later=JSON.parse(sent[3]).messages;
  assert.deepEqual(later.slice(0,earlier.length),earlier,"accepted history remains append-only");
  assert.ok(JSON.stringify(later).includes("Slow check finished"));assert.equal(scheduler.pending,false);
});

test("real Bello/Pi non-OpenRouter session preserves normal adapter and existing stop hook",async(t)=>{
  const directory=mkdtempSync(join(tmpdir(),"bello-backpressure-other-provider-"));
  const agentDir=join(directory,"agent"),cwd=join(directory,"workspace");mkdirSync(agentDir);mkdirSync(cwd);
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const runtime=await realPiSdk.createModelRuntime({agentDir,allowModelNetwork:false});runtime.hasConfiguredAuth=()=>true;
  let providerCalls=0,stopCalls=0,fetchCalls=0;
  runtime.streamSimple=(model,context,options)=>{
    providerCalls++;assert.equal(options.fetch,undefined,"no transport injection for another provider");
    return streamSimple(model,context,{...options,apiKey:"offline-not-real"});
  };
  const originalFetch=globalThis.fetch;globalThis.fetch=async()=>{fetchCalls++;return sse();};
  t.after(()=>{globalThis.fetch=originalFetch;});
  const session=await realPiSdk.createSession({cwd,agentDir,modelRuntime:runtime,model:{...MODEL,provider:"offline-other"},thinkingLevel:"off",
    sessionManager:SessionManager.inMemory(cwd),customTools:[],activeToolNames:[],developerInstructions:"Offline",requestOptions:{current:{}}});
  t.after(()=>session.dispose());session.agent.getApiKey=async()=>"offline-not-real";
  // A public pre-existing stop hook remains authoritative under the transport
  // wrapper; this is not replaced by a provider retry or an agent-turn replay.
  session.agent.shouldStopAfterTurn=async()=>{stopCalls++;return true;};
  await session.prompt("Offline only",{expandPromptTemplates:false});
  assert.equal(providerCalls,1);assert.equal(fetchCalls,1);assert.equal(stopCalls,1);
});
