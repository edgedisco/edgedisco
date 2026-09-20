"""Execute dashboard JavaScript against a tiny DOM stub (Node is optional)."""
import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for dashboard JavaScript tests")
    def test_grouping_filtering_expansion_and_escaping(self):
        script = Path(__file__).resolve().parents[1] / "src/ai_asset_inventory/dashboard.html"
        result = subprocess.run(["node", "-e", r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'',innerHTML:'',textContent:'',hidden:false,listeners:{},addEventListener(name,fn){this.listeners[name]=fn}});return elements.get(id)};
const ctx=vm.createContext({document:{querySelector:element},fetch:()=>new Promise(()=>{})});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
vm.runInContext(`
const base={name:'Codex',vendor:'OpenAI',device_id:'one',hostname:'same-host',os:'Darwin',first_seen:'2026-09-20T10:00:00Z',last_seen:'2026-09-20T11:00:00Z',metadata:{}};
items=[{...base,kind:'application'}, {...base,kind:'process',running:true}, {...base,kind:'agent_runtime',running:true,metadata:{instance_count:3}}, {...base,kind:'process',device_id:'two'}, {...base,kind:'process',metadata:{demo_lab:true}}];
`,ctx);
const check=expr=>assert.equal(vm.runInContext(expr,ctx),true,expr);
check('groupEvidence(items).length===3');
check('groupEvidence(items,{device:"one",status:"Running"})[0].rows.length===2');
check('groupEvidence(items,{kind:"application"}).length===1');
check('groupEvidence(items,{source:"Process inference"})[0].rows.length===1');
check('groupEvidence(items,{query:"nothing"}).length===0');
check('evidenceStatus({...items[1],running:false})==="Stopped"');
check('evidenceStatus({...items[1],stale:true})==="Stale"');
vm.runInContext('populateFilters();render()',ctx);
assert(!element('#rows').innerHTML.includes('evidence-child'));
element('#expand-all').listeners.click();
assert(element('#rows').innerHTML.includes('3 process instance(s)'));
assert(element('#rows').innerHTML.includes('aria-expanded="true"'));
element('#collapse-all').listeners.click();
assert(!element('#rows').innerHTML.includes('evidence-child'));
element('#rows').listeners.click({target:{closest:()=>({dataset:{group:'0'}})}});
assert(element('#rows').innerHTML.includes('evidence-child'));
element('#status-filter').value='Running';element('#status-filter').listeners.change();
assert(element('#evidence-count').textContent.startsWith('1 group · 2 matching'));
vm.runInContext('items=[{...items[0],name:"<img src=x onerror=alert(1)>"}];render()',ctx);
element('#status-filter').value='';vm.runInContext('render()',ctx);
assert(!element('#rows').innerHTML.includes('<img'));
assert(element('#rows').innerHTML.includes('&lt;img'));
console.log('Dashboard grouping, filters, expansion, escaping passed');
''', str(script)], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
