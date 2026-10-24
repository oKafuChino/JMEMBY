import re
import shutil
import subprocess
import unittest

from jmemby.web.membership import SCRIPT
from jmemby.web.ui import PAGE


@unittest.skipUnless(shutil.which('node'), 'Node.js required for UI behavior checks')
class MembershipUiTests(unittest.TestCase):
    def test_form_save_disable_failure_and_logout_races(self):
        harness = r'''
const assert=require('node:assert/strict'), elements=new Map();
let adminId=1,view='membership',calls=0,stamp=()=>{};
const el=id=>{if(!elements.has(id))elements.set(id,{value:'',dataset:{}});return elements.get(id);};
let api, safe=fn=>fn(), confirm=()=>true;
'''
        checks = r'''
const saved={enabled:true,channel:{chat_id:-1001,join_url:'https://t.me/channel'},group:{chat_id:-1002,join_url:'https://t.me/readers'}};
(async()=>{
  api=async()=>({enabled:false,channel:null,group:null});
  await loadMembership();assert.equal(el('membershipStatus').textContent,'限制已关闭');
  el('membershipChannel').value=' @channel ';el('membershipGroup').value=' @readers ';
  api=async(path,payload)=>{calls++;assert.equal(path,'membership');assert.equal(payload.channel.chat_id,'@channel');assert.equal(payload.group.chat_id,'@readers');return saved;};
  await saveMembership(true);assert.equal(calls,1);assert.equal(membershipBusy,false);
  assert.equal(el('membershipStatus').textContent,'限制已启用');
  api=async(path,payload)=>{assert.deepEqual(payload,{enabled:false});return {...saved,enabled:false};};
  await saveMembership(false);assert.equal(el('membershipStatus').textContent,'限制已关闭');
  // Inputs retain the user's failed changes for correction.
  el('membershipChannel').value='@bad_channel';
  api=async()=>{throw Error('validation failed');};
  await assert.rejects(saveMembership(true),/validation failed/);
  assert.equal(el('membershipChannel').value,'@bad_channel');assert.equal(membershipBusy,false);
  let finishSave;
  el('membershipChannel').value='@channel';
  api=()=>{calls++;return new Promise(resolve=>{finishSave=resolve;});};
  const saving=saveMembership(true), count=calls;
  await loadMembership();await saveMembership(true);assert.equal(calls,count);
  adminId=null;clearMembership();finishSave(saved);await saving;
  assert.equal(el('membershipChannel').value,'');assert.equal(el('membershipSave').disabled,true);
  adminId=1;
  let finishLoad;
  api=()=>new Promise(resolve=>{finishLoad=resolve;});
  const loading=loadMembership();adminId=null;clearMembership();finishLoad(saved);await loading;
  assert.equal(el('membershipChannelUrl').value,'');assert.equal(el('membershipStatus').textContent,'等待加载');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node')], input=harness + SCRIPT + checks,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_includes_form_with_unique_ids_and_valid_script(self):
        ids = re.findall(r'\bid="([^"]+)"', PAGE)
        self.assertEqual(len(ids), len(set(ids)))
        for element in ('membership', 'membershipForm', 'membershipSave', 'membershipDisable',
                        'membershipChannel', 'membershipChannelUrl', 'membershipGroup', 'membershipGroupUrl'):
            self.assertIn(element, ids)
        script = re.search(r'<script[^>]*>(.*?)</script>', PAGE, re.S).group(1)
        result = subprocess.run([shutil.which('node'), '--check'], input=script,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
