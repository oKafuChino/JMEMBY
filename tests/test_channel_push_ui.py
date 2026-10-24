import re
import shutil
import subprocess
import unittest

from jmemby.web.channel_push import SCRIPT
from jmemby.web.ui import PAGE


@unittest.skipUnless(shutil.which('node'), 'Node.js required for UI behavior checks')
class ChannelPushUiTests(unittest.TestCase):
    def test_save_disable_failure_and_stale_responses(self):
        harness = r'''
const assert=require('node:assert/strict'), elements=new Map();
let adminId=1,view='channelPush',calls=0,stamp=()=>{},rendered;
const el=id=>{if(!elements.has(id))elements.set(id,{value:'',dataset:{}});return elements.get(id);};
let api,safe=fn=>fn(),confirm=()=>true,empty=()=>{},mono=x=>x,badge=x=>x,stack=(a,b)=>[a,b];
const table=(id,heads,rows)=>{rendered={id,heads,rows};};
'''
        checks = r'''
const saved={enabled:true,channel:{chat_id:-1001,title:'Updates'},include_batch:false,counts:{PENDING:1},recent:[{title:'Book',source_id:'42',chat_id:-1001,status:'PENDING',attempts:1,next_at:1000,last_error:'TimeoutError'}]};
(async()=>{
  api=async()=>({...saved,enabled:false,channel:null,recent:[],counts:{}});
  await loadChannelPush();assert.equal(el('channelPushStatus').textContent,'推送已关闭');
  el('channelPushId').value=' @updates ';el('channelPushBatch').value='false';
  api=async(path,payload)=>{calls++;assert.equal(path,'channel-push');assert.deepEqual(payload,{enabled:true,chat_id:'@updates',include_batch:false});return saved;};
  await saveChannelPush(true);assert.equal(calls,1);assert.equal(channelPushBusy,false);
  assert.equal(el('channelPushStatus').textContent,'推送已启用');
  assert.equal(rendered.heads.length,5);assert.equal(rendered.rows[0].length,5);
  assert.match(el('channelPushCounts').textContent,/待发送 1/);
  el('channelPushBatch').value='true';
  api=async(path,payload)=>{assert.equal(payload.include_batch,true);return {...saved,include_batch:true};};
  await saveChannelPush(true);
  api=async(path,payload)=>{assert.deepEqual(payload,{enabled:false});return {...saved,enabled:false};};
  await saveChannelPush(false);assert.equal(el('channelPushDisable').disabled,true);
  el('channelPushId').value='@bad_channel';api=async()=>{throw Error('validation failed');};
  await assert.rejects(saveChannelPush(true),/validation failed/);
  assert.equal(el('channelPushId').value,'@bad_channel');assert.equal(channelPushBusy,false);
  let finishSave;api=()=>{calls++;return new Promise(resolve=>{finishSave=resolve;});};
  const saving=saveChannelPush(true),before=calls;
  await loadChannelPush();await saveChannelPush(true);assert.equal(calls,before);
  adminId=null;clearChannelPush();finishSave(saved);await saving;
  assert.equal(el('channelPushId').value,'');assert.equal(el('channelPushSave').disabled,true);
  adminId=1;let finishLoad;api=()=>new Promise(resolve=>{finishLoad=resolve;});
  const loading=loadChannelPush();adminId=null;clearChannelPush();finishLoad(saved);await loading;
  assert.equal(el('channelPushId').value,'');assert.equal(el('channelPushStatus').textContent,'等待加载');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node')], input=harness + SCRIPT + checks,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_contains_channel_form_and_valid_script(self):
        ids = re.findall(r'\bid="([^"]+)"', PAGE)
        self.assertEqual(len(ids), len(set(ids)))
        for element in ('channelPush', 'channelPushForm', 'channelPushSave', 'channelPushDisable',
                        'channelPushId', 'channelPushBatch', 'channelPushTable'):
            self.assertIn(element, ids)
        script = re.search(r'<script[^>]*>(.*?)</script>', PAGE, re.S).group(1)
        result = subprocess.run([shutil.which('node'), '--check'], input=script,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
