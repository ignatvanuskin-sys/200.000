import pathlib, subprocess, sys, json
sys.stdout.reconfigure(encoding='utf-8')
import requests, time
# test netlify function via node
import subprocess as sp
print("=== netlify function GET ===")
path = pathlib.Path(r"C:\TGOD\200 000 tg+cloud\netlify\functions\chat.js")
# simulate handler via node inline
js = """
const path=require('path');
process.env.GEMINI_API_KEY=process.env.GEMINI_API_KEY||'test';
process.env.OPENROUTER_API_KEY=process.env.OPENROUTER_API_KEY||'test';
const h=require(path.join('C:','TGOD','200 000 tg+cloud','netlify','functions','chat.js')).handler;
(async()=>{
  let r = await h({httpMethod:'GET'});
  console.log('GET', r.statusCode, r.body.slice(0,120));
  let r2 = await h({httpMethod:'POST', body: JSON.stringify({messages:[{role:'user', content:'Сколько стоит чистка?'}]})});
  console.log('POST status', r2.statusCode);
  let j=JSON.parse(r2.body);
  console.log('POST reply', j.reply.slice(0,150));
  console.log(j.reply.includes('24 000')?'PASS':'FAIL');
  let r3 = await h({httpMethod:'POST', body: JSON.stringify({messages:[{role:'user', content:'Ребёнку 5 лет'}]})});
  let j3=JSON.parse(r3.body);
  console.log('child', j3.reply.slice(0,100), j3.reply.includes('взросл')?'PASS':'FAIL');
})();
"""
open(r"C:\Users\73B5~1\AppData\Local\Temp\opencode\n_test.js","w",encoding="utf-8").write(js)
sp.run([sys.executable,"-c","import subprocess,sys; subprocess.run(['node', r'C:\\Users\\73B5~1\\AppData\\Local\\Temp\\opencode\\n_test.js'])"])
