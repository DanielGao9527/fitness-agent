// Isolated DOM/media substitutes. Never starts a real browser or microphone.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const source = fs.readFileSync(path.join(__dirname, "../static/speech.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const apiSource = appSource.slice(appSource.indexOf("function apiError("), appSource.indexOf("function showAuth("));

function setup({text="two eggs", original="Lunch", maxLength=2000, denied=false, deferred=false, parse=true, serviceError=null}={}) {
  const elements = Object.fromEntries(["start","stop","cancel","consent","status","result","text","use"].map(key=>[key,{disabled:false,hidden:true,value:"",checked:false,isConnected:true}]));
  const target = {value:original,maxLength,disabled:false,isConnected:true,dispatchEvent(){},focus(){}};
  const controls = [target, {disabled:false,isConnected:true}];
  target.closest = () => ({querySelectorAll:()=>controls});
  const queries = {'input':elements.consent,'[role="status"]':elements.status,'.speech-result':elements.result,'.speech-result textarea':elements.text};
  for(const key of ["start","stop","cancel","use"]) queries[`[data-voice="${key}"]`]=elements[key];
  const root = {isConnected:true,querySelector:q=>queries[q],contains:el=>Object.values(elements).includes(el)};
  const track = {readyState:"live",stop(){this.readyState="ended";}};
  let recorder, requests=0, parsed=0, busy=false, release;
  const listeners = {};
  class Recorder {
    static isTypeSupported(){return true;}
    constructor(){recorder=this; this.state="inactive";}
    start(){this.state="recording";}
    stop(){this.state="inactive"; this.ondataavailable?.({data:new Blob(["synthetic"])}); this.onstop?.();}
  }
  const context = {window:{isSecureContext:true}, navigator:{mediaDevices:{async getUserMedia(){
    if(denied) throw Object.assign(new Error("denied"),{name:"NotAllowedError"});
    return {getTracks:()=>[track]};
  }}}, MediaRecorder:Recorder, Blob, DataView, ArrayBuffer, Float32Array, Event, AbortController,
    setTimeout,clearTimeout,setInterval,clearInterval,icon:()=>"",
    document:{addEventListener:(type,fn)=>{listeners[type]=fn;}},
    fetch:async(_url,options)=>{requests++;assert.equal(options.headers["Content-Type"],"audio/wav"); const result=deferred ? await new Promise(resolve=>{release=resolve;}) : {text}; return {ok:!serviceError,status:serviceError?503:200,json:async()=>serviceError?{detail:serviceError}:result};},
    OfflineAudioContext:class {createBufferSource(){return {connect(){},start(){}};} async startRendering(){return {getChannelData:()=>new Float32Array(1600)};}}
  };
  context.window.MediaRecorder=Recorder;
  context.window.addEventListener=()=>{};
  context.window.AudioContext=class {async decodeAudioData(){return {length:1600,duration:.1};} async close(){}};
  vm.runInNewContext(apiSource,context);
  vm.runInNewContext(source,context);
  context.window.FitnessSpeech.mount(root,target,true,value=>{busy=value;},parse?async()=>{parsed++;assert.equal(busy,false);assert.equal(track.readyState,"ended");assert.equal(target.disabled,false);}:null);
  elements.consent.checked=true;
  return {elements,target,track,voice:context.window.FitnessSpeech,apiError:context.apiError,
    state:()=>({recorder,requests,parsed,busy}),release:result=>release(result)};
}
async function until(predicate) {for(let i=0;i<100&&!predicate();i++) await new Promise(setImmediate); assert.ok(predicate());}
async function run(s) {const promise=s.elements.start.onclick();await until(()=>s.state().recorder?.state==="recording");s.elements.stop.onclick();return promise;}

async function main(){
  const normal=setup();await run(normal);assert.equal(normal.target.value,"Lunch\ntwo eggs");assert.equal(normal.state().parsed,0);assert.equal(normal.elements.result.hidden,true);
  const noParser=setup({parse:false});await run(noParser);assert.equal(noParser.state().parsed,0);assert.equal(noParser.target.value,"Lunch\ntwo eggs");
  const denial=setup({denied:true});await denial.elements.start.onclick();assert.equal(denial.state().requests,0);assert.match(denial.elements.status.textContent,/权限/);
  const cancelled=setup({deferred:true});const pending=run(cancelled);await until(()=>cancelled.state().requests===1);cancelled.voice.cancel();cancelled.release({text:"late result"});await pending;assert.equal(cancelled.target.value,"Lunch");assert.equal(cancelled.state().parsed,0);assert.equal(cancelled.track.readyState,"ended");
  const long=setup({maxLength:6});await run(long);assert.equal(long.target.value,"Lunch");assert.equal(long.elements.result.hidden,false);assert.equal(long.state().parsed,0);long.target.value="";long.elements.text.value="eggs";await long.elements.use.onclick();assert.equal(long.state().parsed,0);
  const empty=setup({text:""});await run(empty);assert.equal(empty.state().parsed,0);assert.equal(empty.target.value,"Lunch");
  const failure=setup({serviceError:{code:"MODEL_AUTH_ERROR",message:"千问鉴权失败，请检查密钥、地域和模型权限。（故障编号 abcdef123456，HTTP 401）"}});
  await run(failure);
  assert.equal(failure.target.value,"Lunch");assert.equal(failure.state().busy,false);assert.equal(failure.target.disabled,false);
  assert.match(failure.elements.status.textContent,/请联系维护者/);assert.match(failure.elements.status.textContent,/abcdef123456/);
  assert.doesNotMatch(failure.elements.status.textContent,/MODEL_|密钥|HTTP|未保存/);
  const configured=failure.apiError({code:"MODEL_NOT_CONFIGURED",message:"请在本机配置千问 API Key 后再使用文字解析。"},503);
  assert.equal(configured.code,"MODEL_NOT_CONFIGURED");assert.equal(configured.status,503);assert.doesNotMatch(configured.message,/MODEL_|API Key|本机配置|（/);
  assert.match(failure.apiError({code:"MODEL_BILLING_ERROR",message:"请检查百炼账户"}).message,/服务额度暂不可用/);
  const savedFailure="千问鉴权失败，请检查密钥、地域和模型权限。（故障编号 abcdef123456，HTTP 401）";
  assert.equal(failure.apiError(savedFailure).code,"MODEL_AUTH_ERROR");
  const retry=failure.apiError({code:"MEAL_REQUEST_FAILED",message:savedFailure});assert.equal(retry.code,"MEAL_REQUEST_FAILED");assert.doesNotMatch(retry.message,/密钥|HTTP/);
  assert.match(failure.apiError({code:"MODEL_TIMEOUT",message:"千问响应超时，未保存记录"}).message,/响应超时/);
  assert.doesNotMatch(failure.apiError({code:"MODEL_TIMEOUT",message:"千问响应超时，未保存记录"}).message,/未保存/);
  assert.match(failure.apiError({code:"MODEL_CONTENT_REJECTED",message:"千问未接受本次内容"}).message,/未接受本次内容/);
  assert.match(failure.apiError({code:"MODEL_INVALID_OUTPUT",message:"raw"}).message,/格式不符合/);
  assert.match(failure.apiError({code:"MODEL_UPSTREAM_ERROR",message:"千问未接受本次请求参数或内容格式；"}).message,/请求格式/);
  assert.equal(failure.apiError({code:"INVALID_PORTION",message:"请填写本人实际食用份量"},422).message,"请填写本人实际食用份量");
  assert.equal(failure.apiError("照片无法解码，请重新选择",422).message,"照片无法解码，请重新选择");
  assert.equal(failure.apiError({code:"MODEL_AUTH_ERROR",message:"故障编号 secret-token"}).incident,null);
  console.log("PASS: 7 isolated speech cases plus shared API error mapping: safe diagnostics, cached failures, input/controls retained, validation preserved and timeout/content/format distinctions. No real microphone or provider calls.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
