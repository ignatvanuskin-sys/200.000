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
for kw in ["О КЛИНИКЕ", "Интернациональная, 83", "Приём строго по предварительной записи", "140 000", "ЧАСТЫЕ ВОПРОСЫ", "Где вы находитесь", "ЧЕГО НЕ ДЕЛАТЬ НИКОГДА"]:
    assert kw in ptxt, f"prompt missing {kw}"
# 6 FAQ в новой версии (раньше 8)
assert ptxt.count("**Сколько стоит имплант?**")==1, "FAQ check"
assert "Сначала ответь человеку" in ptxt
ok("prompt OK")

# 3 config
print("=== 3 config ===")
sys.path.insert(0, str(DEMO))
import config
assert config.GEMINI_API_KEY or config.OPENROUTER_API_KEY, "no key"
assert len(config.GEMINI_MODELS)>=2, "gemini models"
assert len(config.SYSTEM_PROMPT)>5000, f"prompt len {len(config.SYSTEM_PROMPT)}"
ok(f"config OK gemini {config.GEMINI_MODELS} len {len(config.SYSTEM_PROMPT)}")

# 4 bot extract_lead
print("=== 4 lead ===")
from bot import extract_lead, build_messages, log_lead, call_llm
n,p = extract_lead([{"role":"user","content":"Меня зовут Айбек +7 778 147 0702"}])
assert n=="Айбек", n
assert p=="+7 778 147 0702", p
n2,p2 = extract_lead([{"role":"user","content":"я Анна 87085436318"}])
ok("extract_lead OK")

# 5 build_messages
print("=== 5 build_messages ===")
msgs = build_messages([{"role":"user","content":"привет"}])
assert msgs[0]["role"]=="system" and "Имплант-Дент" in msgs[0]["content"]
assert msgs[1]["content"]=="привет"
ok("build_messages OK")

# 6 LLM simple (Gemini primary)
print("=== 6 LLM simple ===")
reply, model = call_llm(build_messages([{"role":"user","content":"Ответь по-русски: Привет"}]))
assert reply and len(reply)>2, "empty"
assert re.search(r"[а-яА-Я]", reply), f"no cyrillic {reply[:80]}"
ok(f"llm simple OK [{model}] {reply[:60]}")

# 7 price test (retry once due to LLM variance)
print("=== 7 price ===")
for _ in range(2):
    reply, model = call_llm(build_messages([{"role":"user","content":"Сколько стоит имплант?"}]))
    if "140" in reply:
        break
assert "140" in reply, f"price 140 missing after retry {reply[:300]}"
assert len(reply) < 800, "too long"
ok(f"price OK [{model}] {reply[:120]}")

# 8 child test (новая версия — спрашивает возраст, не выдумывает) — retry 2
print("=== 8 child ===")
for _ in range(2):
    reply,_ = call_llm(build_messages([{"role":"user","content":"Ребёнку 6 лет, можно к вам?"}]))
    if any(k in reply.lower() for k in ["сколько лет","взросл","уточню","ребён","дет"]):
        break
assert any(k in reply.lower() for k in ["сколько лет","взросл","уточню","ребён","дет"]), f"child fail {reply[:300]}"
ok(f"child OK {reply[:120]}")

# 9 urgency — retry
print("=== 9 urgency ===")
for _ in range(2):
    reply,_ = call_llm(build_messages([{"role":"user","content":"Сильная боль, опухла щека и температура!"}]))
    if "имя" in reply.lower() or "телефон" in reply.lower():
        break
assert "имя" in reply.lower() or "телефон" in reply.lower(), f"urgency fail {reply[:300]}"
ok(f"urgency OK {reply[:120]}")

# 10 FastAPI
print("=== 10 FastAPI ===")
import subprocess, time, requests
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", "8012"], cwd=str(DEMO))
time.sleep(4)
try:
    r = requests.get("http://localhost:8012/health", timeout=10)
    assert r.status_code==200 and r.json().get("ok"), f"health {r.text[:200]}"
    ok("health OK")
    r2 = requests.post("http://localhost:8012/api/chat", json={"messages":[{"role":"user","content":"Где вы находитесь?"}]}, timeout=40)
    assert r2.status_code==200, f"chat status {r2.status_code} {r2.text[:400]}"
    j=r2.json()
    assert "reply" in j and "интернациональн" in j["reply"].lower(), f"chat reply {j}"
    assert "model" in j
    ok(f"chat OK [{j['model']}] {j['reply'][:100]}")
    # second test: empty / validation
    r3 = requests.post("http://localhost:8012/api/chat", json={"messages":[]}, timeout=10)
    assert r3.status_code in (400,422)
    ok("empty check OK")
    # rate limit smoke (should not yet be limited) — Gemini may take 6-12s + fallback
    r4 = requests.post("http://localhost:8012/api/chat", json={"messages":[{"role":"user","content":"test"}]}, timeout=35)
    assert r4.status_code==200, f"rate smoke {r4.status_code} {r4.text[:200]}"
    ok("rate limit not yet hit OK")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()
    time.sleep(1)

# 11 static
print("=== 11 static ===")
idx = (DEMO/"static"/"index.html").read_text(encoding='utf-8')
assert "Имплант-Дент" in idx
assert "fetchChat" in idx or "API_URLS" in idx
assert "chip" in idx
assert "wa-header" in idx
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
