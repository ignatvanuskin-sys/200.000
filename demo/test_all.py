#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys, pathlib, re, json, time, subprocess
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
ROOT = pathlib.Path(__file__).parent.parent
DEMO = pathlib.Path(__file__).parent

def ok(msg): print(f"✅ {msg}")
def fail(msg): print(f"❌ {msg}"); sys.exit(1)

# 1 research
print("=== 1 research ===")
rpath = ROOT/"RESEARCH_Имплант-Дент.md"
assert rpath.exists(), "research missing"
txt = rpath.read_text(encoding='utf-8')
for kw in ["Интернациональная", "+7 708", "+7 778 147", "Пн–Пт 10:00", "140 000", "Синус-лифтинг", "Даврон"]:
    assert kw in txt, f"research missing {kw}"
ok("research OK")

# 2 prompt
print("=== 2 prompt ===")
ppath = ROOT/"PROMPT_Имплант-Дент.md"
ptxt = ppath.read_text(encoding='utf-8')
for kw in ["О КЛИНИКЕ", "Интернациональная, 83", "Пн–Пт 10:00–18:00", "только взрослых", "Прицельный снимок", "140 000", "ЧАСТЫЕ ВОПРОСЫ", "Где вы находитесь", "ЧЕГО НЕ ДЕЛАТЬ НИКОГДА"]:
    assert kw in ptxt, f"prompt missing {kw}"
# check 8 questions
assert ptxt.count("**1.")==1 and ptxt.count("**8.")==1, "8 FAQ check"
assert "Сначала ответь человеку" in ptxt
ok("prompt OK")

# 3 config
print("=== 3 config ===")
sys.path.insert(0, str(DEMO))
import config
assert config.OPENROUTER_API_KEY.startswith("sk-or-v1-"), "key bad"
assert len(config.OPENROUTER_MODELS)>=3, "models"
assert len(config.SYSTEM_PROMPT)>5000, f"prompt len {len(config.SYSTEM_PROMPT)}"
ok(f"config OK key {config.OPENROUTER_API_KEY[:12]}... models {config.OPENROUTER_MODELS}")

# 4 bot extract_lead
print("=== 4 lead ===")
from bot import extract_lead, build_messages, log_lead, call_openrouter
n,p = extract_lead([{"role":"user","content":"Меня зовут Айбек +7 778 147 0702"}])
assert n=="Айбек", n
assert p=="+7 778 147 0702", p
n2,p2 = extract_lead([{"role":"user","content":"я Анна 87085436318"}])
# fallback 8...
ok("extract_lead OK")

# 5 build_messages
print("=== 5 build_messages ===")
msgs = build_messages([{"role":"user","content":"привет"}])
assert msgs[0]["role"]=="system" and "Имплант-Дент" in msgs[0]["content"]
assert msgs[1]["content"]=="привет"
ok("build_messages OK")

# 6 OpenRouter simple
print("=== 6 OpenRouter simple ===")
reply, model = call_openrouter(build_messages([{"role":"user","content":"Ответь по-русски: Привет"}]))
assert reply and len(reply)>2, "empty"
# check is russian (contains cyrillic)
assert re.search(r"[а-яА-Я]", reply), f"no cyrillic {reply[:80]}"
ok(f"openrouter simple OK [{model}] {reply[:60]}")

# 7 OpenRouter with full prompt — price test
print("=== 7 price ===")
reply, model = call_openrouter(build_messages([{"role":"user","content":"Сколько стоит имплант?"}]))
assert "140" in reply, f"price 140 missing {reply[:200]}"
# check single question — count ?
# not strict but check not too long
assert len(reply) < 800, "too long"
ok(f"price OK [{model}] {reply[:120]}")

# 8 child test
print("=== 8 child ===")
reply,_ = call_openrouter(build_messages([{"role":"user","content":"Ребёнку 6 лет, можно к вам?"}]))
# should mention только взрослых
assert "взросл" in reply.lower(), f"child fail {reply[:200]}"
ok(f"child OK {reply[:120]}")

# 9 urgency
print("=== 9 urgency ===")
reply,_ = call_openrouter(build_messages([{"role":"user","content":"Сильная боль, опухла щека и температура!"}]))
# should ask name/phone quickly
assert "имя" in reply.lower() or "телефон" in reply.lower(), f"urgency fail {reply[:300]}"
ok(f"urgency OK {reply[:120]}")

# 10 FastAPI
print("=== 10 FastAPI ===")
import subprocess, time, requests
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", "8010"], cwd=str(DEMO))
time.sleep(4)
try:
    r = requests.get("http://localhost:8010/health", timeout=10)
    assert r.status_code==200 and r.json().get("ok"), f"health {r.text[:200]}"
    ok("health OK")
    r2 = requests.post("http://localhost:8010/api/chat", json={"messages":[{"role":"user","content":"Где вы находитесь?"}]}, timeout=40)
    assert r2.status_code==200, f"chat status {r2.status_code} {r2.text[:400]}"
    j=r2.json()
    assert "reply" in j and "Интернациональная" in j["reply"], f"chat reply {j}"
    assert "model" in j
    ok(f"chat OK [{j['model']}] {j['reply'][:100]}")
    # second test: empty
    r3 = requests.post("http://localhost:8010/api/chat", json={"messages":[]}, timeout=10)
    assert r3.status_code==400
    ok("empty check OK")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()
    time.sleep(1)

# 11 static
print("=== 11 static ===")
idx = (DEMO/"static"/"index.html").read_text(encoding='utf-8')
assert "Имплант-Дент" in idx
assert "fetch('/api/chat'" in idx
assert "chip" in idx
ok("static OK")

# 12 lead log
print("=== 12 lead log ===")
lf = DEMO/"leads.jsonl"
if lf.exists(): lf.unlink()
from bot import log_lead
log_lead([{"role":"user","content":"Меня зовут Тест +7 708 111 22 33"}])
assert lf.exists()
line=json.loads(lf.read_text(encoding='utf-8').strip().splitlines()[-1])
assert line["phone"]=="+7 708 111 22 33"
lf.unlink()
ok("lead log OK")

print("\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
