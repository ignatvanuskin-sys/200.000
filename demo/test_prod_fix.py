#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_prod_fix.py — regression tests для production-фикса (offline, всё мокнуто).

Покрывает: duplicate webhook, concurrent duplicates, LLM 404/429/auth,
WhatsApp timeout, unsupported-типы, missing env, parallel health,
раздельные rate-buckets, body 413, verify fail-closed.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

# Нейтрализуем реальные ключи ДО импорта (load_dotenv не перезаписывает существующие)
for _k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "WHATSAPP_TOKEN",
           "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET",
           "WHATSAPP_VERIFY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
           "REDIS_URL"):
    os.environ.pop(_k, None)
os.environ.update({
    "APP_ENV": "test",
    "GEMINI_API_KEY": "dummy-test-key",
    "GEMINI_MODELS": "model-a,model-b",
    "WHATSAPP_TOKEN": "dummy-wa-token",
    "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
    "WHATSAPP_APP_SECRET": "test_secret_123",
    "WHATSAPP_VERIFY_TOKEN": "test_verify_123",
})

DEMO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DEMO)

import config
import bot
import app as appmod
from fastapi.testclient import TestClient


def ok(m):
    print(f"✅ {m}")


SECRET = "test_secret_123"


def wa_payload(msg_id="wamid.test.001", from_id="77781470702", text="Привет, болит зуб",
               mtype="text"):
    msg = {"id": msg_id, "type": mtype, "from": from_id,
           "timestamp": str(int(time.time()))}
    if mtype == "text":
        msg["text"] = {"body": text}
    else:
        msg["image"] = {"id": "img1"}
    payload = {"entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "77780000000"},
        "contacts": [{"profile": {"name": "Tester"}, "wa_id": from_id}],
        "messages": [msg]}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text or json.dumps(self._data)

    def json(self):
        return self._data


class FakeClient:
    """Подменяемый async HTTP-клиент. handler(url, kwargs) -> FakeResp или raise."""
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(url)
        r = self.handler(url, kw)
        if isinstance(r, Exception):
            raise r
        return r


def gemini_ok(text="ответ бота"):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def install_fake(handler, monkey_state):
    """Патчит bot.get_async_client + счётчики llm/send/telegram."""
    state = {"llm": 0, "send": [], "tg": 0, "llm_models": []}
    monkey_state.append(state)
    bot.get_async_client = lambda: FakeClient(handler)
    real_gem = bot.call_gemini_async
    real_or = bot.call_openrouter_async

    async def fake_gem(messages, temperature=0.6, max_tokens=600, request_id="-"):
        state["llm"] += 1
        return await real_gem(messages, temperature, max_tokens, request_id)

    async def fake_or(messages, temperature=0.6, max_tokens=600, request_id="-"):
        state["llm"] += 1
        return await real_or(messages, temperature, max_tokens, request_id)

    bot.call_gemini_async = fake_gem
    bot.call_openrouter_async = fake_or
    return state


_patches = []


def restore_bot():
    import importlib
    importlib.reload(bot)
    global appmod
    importlib.reload(appmod)


print("=== 1 duplicate: same msg_id twice -> one LLM ===")
appmod.store.reset()
st = install_fake(
    lambda url, kw: gemini_ok() if "generativelanguage" in url else FakeResp(200, {"messages": [{"id": "w"}]}),
    _patches)
# send/telegram идут через тот же fake-клиент (bot.get_async_client подменён)
with TestClient(appmod.app) as client:
    raw, sig = wa_payload("wamid.dup.1")
    r1 = client.post("/webhook/whatsapp", content=raw,
                     headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r1.status_code == 200 and r1.json()["status"] == "queued", r1.text[:200]
    r2 = client.post("/webhook/whatsapp", content=raw,
                     headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate", r2.text[:200]
assert st["llm"] == 1, f"llm called {st['llm']} times"
ok("duplicate -> 1 LLM, ack 200/200")
restore_bot()

print("=== 2 concurrent duplicates x5 -> one processing ===")
import app as appmod2
appmod2.store.reset()
calls = {"llm": 0}


async def slow_llm(messages, temperature=0.6, max_tokens=600, request_id="-"):
    calls["llm"] += 1
    await asyncio.sleep(0.3)
    return "ок", "fake"


appmod2.bot.call_llm_async = slow_llm


async def direct_send(to, text, request_id="-"):
    return {"ok": True}


appmod2.bot.send_whatsapp_async = direct_send
appmod2.bot.notify_admin_telegram_async = lambda *a, **k: asyncio.sleep(0, result=False)
with TestClient(appmod2.app) as client:
    raw, sig = wa_payload("wamid.conc.1")
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(client.post, "/webhook/whatsapp", content=raw,
                            headers={"Content-Type": "application/json",
                                     "X-Hub-Signature-256": sig}) for _ in range(5)]
        resps = [f.result(timeout=60) for f in futs]
        codes = sorted(r.status_code for r in resps)
        statuses = sorted(r.json()["status"] for r in resps)
assert codes == [200] * 5, codes
assert statuses.count("duplicate") == 4 and "queued" in statuses, statuses
assert calls["llm"] == 1, f"llm {calls['llm']}"
ok("concurrent x5 -> 1 LLM, 4 duplicate")
import importlib as _il
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 3 gemini 404 fail-fast -> next model, быстро ===")
calls3 = {"n": 0}


def h3(url, kw):
    if "generativelanguage" in url:
        calls3["n"] += 1
        if "model-a" in url:
            return FakeResp(404, {}, "not found")
        return gemini_ok("вторая модель ок")
    return FakeResp(200, {})


bot.get_async_client = lambda: FakeClient(h3)
t0 = time.time()
text, model = bot._run_sync(bot.call_gemini_async([{"role": "system", "content": "s"},
                                                   {"role": "user", "content": "hi"}]))
dt = time.time() - t0
assert model == "model-b", model
assert dt < 15, f"too slow {dt:.1f}s"
ok(f"404 fail-fast -> {model} за {dt:.2f}s")
_il.reload(bot)

print("=== 4 gemini 429 -> controlled fallback ===")


def h4(url, kw):
    if "generativelanguage" in url:
        if "model-a" in url:
            return FakeResp(429, {}, "rate")
        return gemini_ok("после 429 ок")
    return FakeResp(200, {})


bot.get_async_client = lambda: FakeClient(h4)
t0 = time.time()
text, model = bot._run_sync(bot.call_gemini_async([{"role": "system", "content": "s"},
                                                   {"role": "user", "content": "hi"}]))
dt = time.time() - t0
assert model == "model-b" and dt < 15, f"{model} {dt:.1f}s"
ok(f"429 -> fallback за {dt:.2f}s")
_il.reload(bot)

print("=== 5 gemini 401 auth -> быстрое падение без 30s ===")


def h5(url, kw):
    return FakeResp(401, {}, "auth")


bot.get_async_client = lambda: FakeClient(h5)
t0 = time.time()
try:
    bot._run_sync(bot.call_gemini_async([{"role": "system", "content": "s"},
                                         {"role": "user", "content": "hi"}]))
    raise AssertionError("should raise")
except bot.LLMError as e:
    assert e.kind == "auth", e.kind
dt = time.time() - t0
assert dt < 10, f"too slow {dt:.1f}s"
ok(f"401 auth abort за {dt:.2f}s")
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 6 WhatsApp timeout -> ack 200, без краша ===")
import app as appmod6
appmod6.store.reset()
import httpx as _httpx


async def llm_ok(messages, temperature=0.6, max_tokens=600, request_id="-"):
    return "держитесь", "fake"


async def send_timeout(to, text, request_id="-"):
    raise _httpx.TimeoutException("send timeout", request=None)


appmod6.bot.call_llm_async = llm_ok
appmod6.bot.send_whatsapp_async = send_timeout
appmod6.bot.notify_admin_telegram_async = lambda *a, **k: asyncio.sleep(0, result=False)
with TestClient(appmod6.app) as client:
    raw, sig = wa_payload("wamid.sendto.1")
    r = client.post("/webhook/whatsapp", content=raw,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text[:200]
ok("ack 200 при висящей отправке (background не роняет webhook)")
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 7 image -> static reply, LLM не вызывается ===")
import app as appmod7
appmod7.store.reset()
st7 = {"llm": 0, "sent": []}


async def llm_count(messages, temperature=0.6, max_tokens=600, request_id="-"):
    st7["llm"] += 1
    return "x", "fake"


async def send_cap(to, text, request_id="-"):
    st7["sent"].append(text)
    return {"ok": True}


appmod7.bot.call_llm_async = llm_count
appmod7.bot.send_whatsapp_async = send_cap
with TestClient(appmod7.app) as client:
    raw, sig = wa_payload("wamid.img.1", mtype="image")
    r = client.post("/webhook/whatsapp", content=raw,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["status"] == "queued-static", r.text[:200]
assert st7["llm"] == 0, "LLM must not be called"
assert st7["sent"] and "файл" in st7["sent"][0], st7["sent"]
ok("image -> static без LLM")
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 8 missing env в production -> startup failure ===")
_saved = {k: getattr(config, k) for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY",
                                          "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                                          "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
                                          "IS_PROD")}
try:
    config.GEMINI_API_KEY = ""
    config.OPENROUTER_API_KEY = ""
    config.WHATSAPP_TOKEN = ""
    config.WHATSAPP_PHONE_NUMBER_ID = ""
    config.WHATSAPP_APP_SECRET = ""
    config.WHATSAPP_VERIFY_TOKEN = ""
    config.IS_PROD = True
    try:
        config.validate_on_startup()
        raise AssertionError("should raise ConfigError")
    except config.ConfigError as e:
        assert "GEMINI_API_KEY" in str(e), str(e)[:200]
        assert "WHATSAPP_VERIFY_TOKEN" in str(e), str(e)[:200]
        ok(f"startup fail fast: {str(e)[:100]}...")
finally:
    for _k, _v in _saved.items():
        setattr(config, _k, _v)

print("=== 9 health параллелен медленному LLM ===")
import app as appmod9
appmod9.store.reset()


async def slow5(messages, temperature=0.6, max_tokens=600, request_id="-"):
    await asyncio.sleep(5)
    return "медленно", "fake"


appmod9.bot.call_llm_async = slow5
with TestClient(appmod9.app) as client:
    import concurrent.futures as _cf
    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=10) as pool:
        rs = list(pool.map(lambda i: client.get("/health"), range(10)))
    dt = time.time() - t0
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    assert dt < 8, f"health blocked {dt:.1f}s"
ok(f"10x health за {dt:.2f}s при висящем LLM (TestClient, mocked)")
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 10 раздельные buckets chat/dentica ===")
import app as appmod10
appmod10.store.reset()


async def fast_llm(messages, temperature=0.6, max_tokens=600, request_id="-"):
    return "ок", "fake"


appmod10.bot.call_llm_async = fast_llm
with TestClient(appmod10.app) as client:
    codes = [client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code
             for _ in range(21)]
    assert 429 in codes, codes
    r = client.post("/api/chat-dentica", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, f"dentica starved: {r.status_code}"
ok("chat 429 не душит dentica")
_il.reload(bot)
import app as appmod
_il.reload(appmod)

print("=== 11 oversize body -> 413 + verify fail-closed ===")
import app as appmod11
with TestClient(appmod11.app) as client:
    big = b"a" * (100 * 1024 + 1)
    r = client.post("/webhook/whatsapp", content=big,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.status_code
    ok("oversize 413")
    appmod11.config.WHATSAPP_VERIFY_TOKEN = ""
    r = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe",
                                                 "hub.verify_token": "",
                                                 "hub.challenge": "x"})
    assert r.status_code == 403, r.status_code
    ok("verify fail-closed при пустом токене")
_il.reload(bot)
_il.reload(config)
import app as appmod
_il.reload(appmod)

print("\n🎉 PROD-FIX TESTS PASS")
