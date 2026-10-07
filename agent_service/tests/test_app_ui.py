from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resources/web/local_agent"


def test_bundled_local_ui_has_owner_workflows_and_no_remote_csp() -> None:
    html = (PAGE / "index.html").read_text()
    script = (PAGE / "app.js").read_text()
    assert "connect-src 'none'" in html
    assert 'id="printer-form"' in html and 'id="profile-form"' in html
    assert 'id="reference-form"' in html and 'id="cfs-form"' in html
    assert 'id="cfs-slots"' in html and 'id="cfs-add-slot"' in html
    assert "Slots JSON" not in html and 'name="slots"' not in html
    assert 'id="conversation-history"' in html
    for action in (
        "create_job", "select_model", "prepare_job", "queue_job", "start_job",
        "pause_job", "cancel_job", "approve_resume", "approve_budget", "open_project",
        "enroll_printer", "save_profile", "enroll_reference", "save_cfs_inventory", "answer_question",
        "save_policy", "get_conversation",
    ):
        assert f'"{action}"' in script
    assert "innerHTML" not in script


def test_local_ui_resume_and_untrusted_values_use_native_bridge_safely(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for bundled UI behavioral fixture")
    app = (PAGE / "app.js").read_text()
    harness = r"""
const fs = require('fs'), vm = require('vm');
class Node {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.listeners={}; this.dataset={};
    this.classList={contains:()=>false,toggle:()=>{}}; this.elements={}; this.options=[]; this.hidden=false; this.value=''; }
  append(...xs) { for (const x of xs) this.appendChild(x); }
  appendChild(x) { this.children.push(x); x.parentNode=this; if (x.tagName==='option') { this.options.push(x); if (this.tagName==='select' && !this.value) this.value=x.value; } return x; }
  replaceChildren(...xs) { this.children=[]; this.options=[]; if (this.tagName==='select') this.value=''; this.append(...xs); this.textContent=''; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute() {}
  get childElementCount() { return this.children.length; }
  set textContent(v) { this._text=String(v); this.children=[]; this.options=[]; }
  get textContent() { return this._text || this.children.map(x=>x.textContent).join(''); }
  fire(type, event={}) { for (const fn of this.listeners[type]||[]) fn({preventDefault(){},currentTarget:this,target:this,...event}); }
  remove() { if (this.parentNode) this.parentNode.children=this.parentNode.children.filter(x=>x!==this); }
}
const ids = [...fs.readFileSync(process.argv[2], 'utf8').matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
const nodes = Object.fromEntries(ids.map(id=>[id,new Node()]));
nodes['cfs-printer']=new Node('select');
for (const id of ['model-form','policy-form','printer-form','reference-form','cfs-form','profile-form','job-form','chat-form']) {
  nodes[id].elements = new Proxy({}, {get: (o,k) => o[k] || (o[k]=new Node('input'))});
}
const select = new Node('select');
const sent=[];
const document={getElementById:id=>nodes[id] || (nodes[id]=new Node()),
  createElement:tag=>new Node(tag), querySelectorAll:selector=>selector==='.printer-select'?[nodes['cfs-printer']]:[], addEventListener(){}};
const window={wx:{postMessage:s=>sent.push(JSON.parse(s))}, crypto:{randomUUID:()=>`key-${sent.length}`},
  setInterval(){}, clearTimeout(){}, setTimeout(){return 1}, confirm:()=>true};
const context={window,document,console,Date,Math,JSON,FormData:class{constructor(form){this.form=form} entries(){return Object.entries(this.form.entriesMap||{})}},Number,String,Array,Object,Map};
vm.runInNewContext(process.argv[3], context);
const initial=sent.shift();
if (!initial || initial.data.action!=='state' || 'token' in initial.data) throw Error('bootstrap bridge request malformed');
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:initial.data.request_id,ok:true,result:{
  jobs:[{id:'job-1',state:'paused',printer_id:'p',holds:['owner resume'],request:{request:'<img src=x onerror=alert(1)>'},
    observation:{progress:0.25},estimates:{hours:2,grams:40},gcode_sha256:'abcdef1234567890'}],
  printers:[{id:'printer-1',name:'Printer 1',cfs_slots:[{slot_id:'A1',material:'PLA',color:'Blue',remaining_grams:120,verified:true}]}],
  profiles:[],alerts:[{id:1,acknowledged:true,title:'old alert'}],
  questions:[{id:'question-1',job_id:null,question:'<svg onload=bad()> Which material?',status:'open'}],
  conversations:[{id:'conversation-1',created:1}],policy:{max_hours:4},model:{}}}});
const historyRequest=sent.find(x=>x.data.action==='get_conversation');
if (!historyRequest || historyRequest.data.payload.conversation_id!=='conversation-1') throw Error('history selection was not loaded');
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:historyRequest.data.request_id,ok:true,result:{id:'conversation-1',messages:[{role:'assistant',content:'<b>untrusted</b>'}]}}});
if (!nodes['conversation-list'].textContent.includes('<b>untrusted</b>')) throw Error('conversation history message was lost');
function walk(n,out=[]) { out.push(n); for (const c of n.children) walk(c,out); return out; }
const nodesFound=walk(nodes['jobs-list']);
const resume=nodesFound.find(n=>n.tagName==='button' && n.textContent.includes('Resume'));
if (!resume) throw Error('paused job has no owner resume action');
resume.fire('click');
const approval=sent.find(x=>x.data.action==='approve_resume');
if (!approval || approval.data.payload.job_id!=='job-1') throw Error('resume approval missing');
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:approval.data.request_id,ok:true,result:{}}});
if (!sent.some(x=>x.data.action==='resume_job' && x.data.payload.job_id==='job-1')) throw Error('owner approval did not trigger resume');
if (!nodesFound.some(n=>n.textContent.includes('25%'))) throw Error('progress not rendered');
if (!nodesFound.some(n=>n.textContent.includes('40 g'))) throw Error('budget estimate missing');
if (nodes['jobs-list'].textContent.includes('<img src=x onerror=alert(1)>') === false) throw Error('hostile text was dropped instead of safely rendered as text');
if (nodes['alerts-list'].textContent.includes('Acknowledge')) throw Error('acknowledged alert still presented as active');
const questionNodes=walk(nodes['questions-list']);
if (!nodes['questions-list'].textContent.includes('Asked before a job exists') ||
    !nodes['questions-list'].textContent.includes('<svg onload=bad()> Which material?')) throw Error('pre-job owner question was not rendered as text');
const questionForm=questionNodes.find(n=>n.tagName==='form');
const questionAnswer=questionNodes.find(n=>n.tagName==='textarea');
if (!questionForm || !questionAnswer) throw Error('question has no answer form');
questionAnswer.value='<script>untrusted answer</script>';
questionForm.fire('submit');
const questionRequest=sent.find(x=>x.data.action==='answer_question');
if (!questionRequest || questionRequest.data.payload.question_id!=='question-1' ||
    questionRequest.data.payload.answer!=='<script>untrusted answer</script>') throw Error('owner question answer was not forwarded');
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:questionRequest.data.request_id,ok:true,result:{}}});
const answerRefresh=sent.filter(x=>x.data.action==='state').at(-1);
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:answerRefresh.data.request_id,ok:true,result:{
  jobs:[],printers:[{id:'printer-1',name:'Printer 1',cfs_slots:[{slot_id:'A1',material:'PLA',color:'Blue',remaining_grams:120,verified:true}]}],
  profiles:[],alerts:[],questions:[{id:'question-1',job_id:null,
    question:'<svg onload=bad()> Which material?',status:'answered',answer:'<script>untrusted answer</script>'}],
  conversations:[],policy:{},model:{}}}});
if (!nodes['question-history'].textContent.includes('<script>untrusted answer</script>')) throw Error('answered question history missing');
const cfsRows=()=>nodes['cfs-slots'].children.filter(row=>row.dataset.cfsSlotRow==='true');
if (cfsRows().length!==1 || cfsRows()[0].slotFields.slot_id.value!=='A1' ||
    cfsRows()[0].slotFields.verified.checked!==true) throw Error('saved CFS inventory was not prefilled');
cfsRows()[0].slotFields.material.value='PETG';
cfsRows()[0].slotFields.material.fire('input');
nodes['cfs-add-slot'].fire('click');
if (cfsRows().length!==2) throw Error('add-slot control failed');
const second=cfsRows()[1].slotFields;
second.slot_id.value='B2'; second.material.value='PLA'; second.color.value='<img src=x>';
second.remaining_grams.value='80'; second.verified.checked=true;
second.slot_id.fire('input');
nodes['refresh'].fire('click');
const cfsPoll=sent.filter(x=>x.data.action==='state').at(-1);
window.handleSlicerEvent({command:'local_agent_result',data:{request_id:cfsPoll.data.request_id,ok:true,result:{
  jobs:[],printers:[{id:'printer-1',name:'Printer 1',cfs_slots:[{slot_id:'A1',material:'PLA',color:'Blue',remaining_grams:120,verified:true}]},
    {id:'printer-2',name:'Printer 2',cfs_slots:[{slot_id:'C1',material:'ABS',color:'Red',remaining_grams:30,verified:false}]}],
  profiles:[],alerts:[],questions:[],conversations:[],policy:{},model:{}}}});
if (cfsRows().length!==2 || cfsRows()[0].slotFields.material.value!=='PETG' ||
    cfsRows()[1].slotFields.color.value!=='<img src=x>') throw Error('state polling overwrote unsaved CFS edits');
nodes['cfs-printer'].value='printer-2';
nodes['cfs-form'].fire('change',{target:nodes['cfs-printer']});
if (cfsRows().length!==2 || cfsRows()[0].slotFields.material.value!=='PETG' ||
    !nodes['cfs-dirty-label'].textContent.includes('printer-2')) throw Error('printer change discarded CFS edits or hid the new save target');
nodes['cfs-form'].fire('submit');
const cfsSave=sent.find(x=>x.data.action==='save_cfs_inventory');
if (!cfsSave || cfsSave.data.payload.printer_id!=='printer-2' || cfsSave.data.payload.slots.length!==2 ||
    cfsSave.data.payload.slots[0].material!=='PETG' || cfsSave.data.payload.slots[1].verified!==true) throw Error('CFS rows did not serialize to the service contract');
if (nodes['cfs-slots'].textContent.includes('Approve budget')) throw Error('CFS form exposes unrelated approval actions');
nodes['model-form'].dataset.dirty='true';
nodes['model-form'].elements.base_url.value='http://localhost:1234';
nodes['model-form'].entriesMap={base_url:'http://localhost:1234',model:'local-model',api_key:''};
nodes['model-form'].fire('submit');
const modelRequest=sent.find(x=>x.data.action==='save_model');
if (!modelRequest || Object.hasOwn(modelRequest.data.payload,'api_key')) throw Error('blank model key cleared or forwarded a secret');
if (nodes['model-form'].elements.base_url.value!=='http://localhost:1234') throw Error('poll overwrote dirty settings');
if (nodes['policy-form'].elements.max_hours.value!==4) throw Error('clean settings form did not refresh');
nodes['printer-form'].entriesMap={printer_id:'printer-1',model:'K1 Max',name:'Desk printer',
  camera_url:'http://camera.local/stream',frame_time_header:'X-Frame-Time',frame_sequence_header:'X-Frame-Seq',cfs:'true'};
nodes['printer-form'].elements.camera_association_confirmed.checked=false;
nodes['printer-form'].fire('submit');
const printerRequest=sent.find(x=>x.data.action==='enroll_printer');
if (!printerRequest || printerRequest.data.payload.printer_id!=='printer-1' ||
    printerRequest.data.payload.camera_url!=='http://camera.local/stream' ||
    printerRequest.data.payload.camera_association_confirmed!==false) throw Error('printer enrollment form payload incomplete');
nodes['reference-form'].entriesMap={printer_id:'printer-1',left:'3',top:'4',right:'80',bottom:'90'};
nodes['reference-form'].elements.bed_clear_confirmed.checked=true;
nodes['reference-form'].fire('submit');
const referenceRequest=sent.find(x=>x.data.action==='enroll_reference');
if (!referenceRequest || JSON.stringify(referenceRequest.data.payload.roi)!=='[3,4,80,90]' ||
    referenceRequest.data.payload.bed_clear_confirmed!==true) throw Error('bed reference owner confirmation was not forwarded');
console.log('fixture-ok');
"""
    fixture = tmp_path / "fixture.js"
    fixture.write_text(harness)
    html_path = PAGE / "index.html"
    result = subprocess.run(
        [node, str(fixture), str(html_path), app],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "fixture-ok" in result.stdout
