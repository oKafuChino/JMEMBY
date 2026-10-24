import re
import shutil
import subprocess
import unittest

from jmemby.web.ui import PAGE


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class QueueUiTests(unittest.TestCase):
    def test_queue_rendering_polling_and_stale_requests(self):
        script = re.search(r'<script[^>]*>(.*?)</script>', PAGE, re.S).group(1)
        # Load the actual UI helpers without booting a browser/session.
        helpers = script.split("document.querySelectorAll('[data-view],[data-go]')")[0]
        harness = r'''
const assert = require('node:assert/strict');
const elements = new Map(), timers = new Map();
let serial = 0, rendered = null, renders = 0;
function element(){return {value:'all',dataset:{},children:[],classList:{toggle(){}},
  append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},
  setAttribute(){},removeAttribute(){}};}
const document = {body:element(),createElement:element,querySelectorAll:()=>[],
  getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
function setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,ms});return id;}
function clearTimeout(id){timers.delete(id);}
function clearNodes(){} function clearLibrary(){} function clearReports(){}
'''
        checks = r'''
table=(id,heads,rows)=>{rendered={id,heads,rows};renders++;};
const row={id:'task-id',title:'Book',source_id:'42',created_at:1000,user_id:2,
  priority:0,status:'DOWNLOADING',node:{id:'vps-1',label:'First node'},node_current:true};
(async()=>{
  adminId=1;setView('tasks');
  api=async path=>{assert.equal(path,'tasks?page=1&queue=all');return [row];};
  await loadTasks();
  assert.equal(rendered.heads.length,9);
  assert.equal(rendered.rows[0].length,9);
  assert.equal(rendered.heads[2],'提交时间');
  assert.equal(rendered.rows[0][2],new Date(1000000).toLocaleString());
  assert.equal(rendered.rows[0][5].children[0].textContent,'First node');
  assert.equal(rendered.rows[0][5].children[1].textContent,'vps-1');
  assert.equal(taskNode({...row,node_current:false}).children[1].textContent,'vps-1 · 最近执行');
  assert.equal(taskNode({...row,node:null,status:'QUEUED'}),'待分配');
  assert.equal(taskNode({...row,node:null,status:'COMPLETED'}),'未记录');
  assert.equal(timers.size,1);assert.equal(timers.get(taskTimer).ms,5000);
  const initialRenders=renders;
  await timers.get(taskTimer).fn();
  assert.equal(renders,initialRenders+1);assert.equal(timers.size,1);
  api=async()=>{throw Error('offline');};
  await assert.rejects(loadTasks(true),/offline/);
  assert.equal(renders,initialRenders+1);assert.equal(timers.size,1);
  setView('users');assert.equal(timers.size,0);
  setView('tasks');
  let finishOld;
  api=()=>new Promise(resolve=>{finishOld=resolve;});
  const stale=loadTasks();
  taskPage=2;el('queue').value='white';
  api=async path=>{assert.equal(path,'tasks?page=2&queue=white');return [{...row,title:'New page'}];};
  await loadTasks();
  finishOld([{...row,title:'Old page'}]);await stale;
  assert.equal(rendered.rows[0][0].children[0].textContent,'New page');
  assert.equal(timers.size,1);
  let finishNavigation;
  api=()=>new Promise(resolve=>{finishNavigation=resolve;});
  const navigation=loadTasks();const beforeNavigation=renders;
  setView('users');finishNavigation([row]);await navigation;
  assert.equal(renders,beforeNavigation);assert.equal(timers.size,0);
  setView('tasks');
  let finishLogout;
  api=()=>new Promise(resolve=>{finishLogout=resolve;});
  const logout=loadTasks();
  logged(false);finishLogout([row]);await logout;
  assert.equal(adminId,null);assert.equal(renders,beforeNavigation);assert.equal(timers.size,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node')], input=harness + helpers + checks,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
