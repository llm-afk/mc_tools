// In-memory HTTP requests only. No sockets, printer queries or RAW jobs.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Readable } = require('node:stream');
const { createService } = require('./index');
const { printerState } = require('./windows-printer');
const config = require('./config.json');
const queue = { name: 'USB 标签机 & 1', portName: 'USB001', workOffline: false, printerStatus: 3, detectedErrorState: 2 };
function service(overrides={}) {
  return createService({ ...config, printer: { ...config.printer, name: queue.name } }, {
    listWindowsPrinters: async()=>[queue],generateLabelBMP:async()=>Buffer.from('fake-tspl'),
    sendToPrinter:async()=>({ success:true,outcome:'SUBMITTED',jobId:123 }),...overrides });
}
function request(server,method,url,body,headers={}) {
  return new Promise(resolve=>{
    const req=Readable.from(body===undefined ? [] : [Buffer.from(typeof body==='string' ? body : JSON.stringify(body))]);
    req.method=method;req.url=url;req.headers=headers;
    const res={ destroyed:false,writableEnded:false,writeHead(code){this.code=code;},
      end(bytes){this.writableEnded=true;resolve({status:this.code,body:JSON.parse(bytes || '{}')});} };
    server.emit('request',req,res);
  });
}
test('query parameter selects the exact queue; a different TSC name never substitutes',async()=>{
  const server=service();
  const ok=await request(server,'GET','/api/status?printerName='+encodeURIComponent(queue.name));
  assert.equal(ok.body.data.online,true);assert.equal(ok.body.data.printerName,queue.name);
  const missing=await request(server,'GET','/api/status?printerName=TSC%20Other');
  assert.equal(missing.body.data.online,false);assert.equal(missing.body.data.found,false);
});
test('queue offline and lack of paper are distinct, while warnings remain visible',()=>{
  assert.equal(printerState([{...queue,workOffline:true}],queue.name).online,false);
  assert.equal(printerState([{...queue,detectedErrorState:4}],queue.name).reason,'缺纸');
  assert.equal(printerState([{...queue,detectedErrorState:3}],queue.name).online,true);
});
test('status shares an in-flight read; printing rechecks the physical queue and carries the host ID',async()=>{
  let calls=0, release, writes=0;
  const server=service({listWindowsPrinters:async()=>{
    calls++;
    if(calls===1) await new Promise(resolve=>{release=resolve;});
    return [{...queue,workOffline:calls>1}];
  },sendToPrinter:async()=>{writes++;return {success:true};}});
  const pending=[request(server,'GET','/api/status'),request(server,'GET','/api/status')];
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,1);release();
  const first=(await Promise.all(pending))[0].body.data;
  const second=(await request(server,'GET','/api/status')).body.data;
  assert.equal(second.lastCheck,first.lastCheck);assert.equal(calls,1);
  const reply=(await request(server,'POST','/api/print',{items:[{sn:'TEST'}]},{'x-request-id':'test-job-42'})).body;
  assert.equal(calls,2);assert.equal(writes,0);assert.equal(reply.data.results[0].success,false);
  assert.equal(reply.data.requestId,'test-job-42');
});
test('queue enumeration failure is not silently converted to an empty printer list',async()=>{
  const result=await request(service({listWindowsPrinters:async()=>{throw new Error('CIM unavailable');}}),'GET','/api/status');
  assert.equal(result.body.data.online,false);
  assert.equal(result.body.data.errorCode,'WINDOWS_PRINTER_QUERY_FAILED');
});
test('health and preview never query queues or submit a job',async()=>{
  const forbidden=async()=>{throw new Error('Unexpected printer operation');};
  const server=service({listWindowsPrinters:forbidden,sendToPrinter:forbidden});
  assert.equal((await request(server,'GET','/api/health')).body.data.service,'tsc-print-service');
  const preview=await request(server,'POST','/api/preview',{items:[{sn:'TEST-SN-0001'}]});
  assert.equal(preview.status,200);assert.ok(preview.body.data.tspCommands.length);
});
test('invalid copies and malformed items never enter the printer',async()=>{
  let calls=0;
  const server=service({sendToPrinter:async()=>{calls++;}});
  for(const copies of [0,51,1.5,true]) assert.equal((await request(server,'POST','/api/print',{
    items:[{sn:'TEST-SN-0001'}],copies})).status,400);
  assert.equal((await request(server,'POST','/api/print',{items:[null]})).status,400);
  assert.equal(calls,0);
});
test('a successful RAW receipt keeps per-item result and job ID without claiming paper output',async()=>{
  const result=(await request(service(),'POST','/api/print',{items:[{sn:'TEST-SN-0001'}],copies:2})).body;
  assert.equal(result.data.success,2);assert.equal(result.data.results.length,1);
  assert.equal(result.data.results[0].success,true);assert.equal(result.data.results[0].jobs[0].jobId,123);
  assert.equal(result.data.physicalOutputConfirmed,false);
});
test('unknown outcome stops later copies and later items',async()=>{
  let calls=0;
  const result=(await request(service({sendToPrinter:async()=>{
    calls++;return {success:false,outcome:'UNKNOWN',message:'WritePrinter not confirmed'};
  }}),'POST','/api/print',{items:[{sn:'TEST-1'},{sn:'TEST-2'}],copies:3})).body;
  assert.equal(calls,1);assert.equal(result.data.results[0].outcome,'UNKNOWN');
  assert.equal(result.data.results[1].success,false);
});
test('concurrent print requests serialize RAW jobs',async()=>{
  let active=0,max=0;
  const server=service({sendToPrinter:async()=>{
    active++;max=Math.max(max,active);
    await new Promise(resolve=>setTimeout(resolve,5));active--;
    return {success:true,outcome:'SUBMITTED'};
  }});
  await Promise.all(['TEST-1','TEST-2'].map(sn=>request(server,'POST','/api/print',{items:[{sn}]})));
  assert.equal(max,1);await server.waitForJobs();
});
test('custom saved route paths work without changing payload format',async()=>{
  const server=createService({...config,httpRoutes:{status:'/custom/status',print:'/custom/labels'}},
    {listWindowsPrinters:async()=>[queue],sendToPrinter:async()=>({success:true}),generateLabelBMP:async()=>Buffer.from('x')});
  assert.equal((await request(server,'GET','/custom/status?printerName='+encodeURIComponent(queue.name))).status,200);
});
