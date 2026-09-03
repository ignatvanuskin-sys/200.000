# Имплант-Дент — ИИ-администратор (Петропавловск) — Railway + Gemini

Папка: `C:\TGOD\200 000 tg+cloud`

## Архитектура (всё на Railway)

```
WhatsApp Cloud API → POST /webhook/whatsapp (Railway) → Gemini → ответ в WhatsApp
Веб-демо           → GET / (Railway) → чат → POST /api/chat (Railway) → Gemini
Dentica (демо)     → my-clinic/index.html → POST /api/chat-dentica (Railway) → Gemini
```

**Один сервис на Railway, никаких посредников.**

## Что внутри
- `RESEARCH_Имплант-Дент.md` — ресёрч по 2ГИС (цены 25.02.2026, отзывы, контакты)
- `PROMPT_Имплант-Дент.md` — заполненный промпт (7345 симв, без выдуманного графика, с полным прайсом)
- `demo/` — FastAPI-приложение (веб-демо + WhatsApp webhook + Dentica endpoint)
- `my-clinic/index.html` — статический фронтенд Dentica
- `railway.json` — конфиг деплоя Railway (rootDirectory=demo)

## Эндпоинты (Railway)
- `GET  /webhook/whatsapp` — верификация webhook от Meta (hub.challenge / hub.verify_token)
- `POST /webhook/whatsapp` — приём входящих WhatsApp-сообщений → Gemini → ответ
- `GET  /` — веб-демо (Имплант-Дент)
- `POST /api/chat` — чат для веб-демо (Имплант-Дент)
- `POST /api/chat-dentica` — чат для Dentica
- `GET  /health` — healthcheck

## Модель: почему Gemini и хватает на весь день
- `gemini-3.6-flash` (primary) — 1500 RPD / 15 RPM / 1M токенов, free tier. Фолбэки `gemini-3.5-flash`, `gemini-3.5-flash-lite`.
- На демо-трафике (100 чатов/день) хватает с запасом. OpenRouter оставлен как фолбэк если квота Gemini.

## Демо — как запустить локально
```bat
cd "C:\TGOD\200 000 tg+cloud\demo"
pip install -r requirements.txt
uvicorn app:app --port 8000  # http://localhost:8000
# или консоль: python bot.py
```
`.env` уже содержит `GEMINI_API_KEY` и `GEMINI_MODELS=gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite`

## Деплой на Railway
1. `railway.json` уже настроен: `rootDirectory=demo`, `startCommand=uvicorn app:app --host 0.0.0.0 --port $PORT`
2. Environment variables в Railway:
   - `GEMINI_API_KEY` — ключ Gemini
   - `GEMINI_MODELS` — `gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite`
   - `WHATSAPP_TOKEN` — токен WhatsApp Cloud API
   - `WHATSAPP_PHONE_NUMBER_ID` — ID номера телефона
   - `WHATSAPP_VERIFY_TOKEN` — токен верификации webhook (придумывается вами, одинаковый в Meta и здесь)
   - `WHATSAPP_APP_SECRET` — App Secret от Meta (для проверки подписи)
   - (опц.) `OPENROUTER_API_KEY` — фолбэк LLM
   - (опц.) `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — уведомления о лидах
3. Webhook URL в Meta Business Suite: `https://<railway-domain>/webhook/whatsapp`

## Как бот себя ведёт (промпт)
- Сначала отвечает, потом **один вопрос**. Коротко, 1-2 строки.
- Цены только из прайса 25.02.2026 с оговоркой `точную скажут после осмотра`.
- График не выдумывает: `приём по записи, точное время подскажет администратор`.
- Не ставит диагнозы, не называет окна. Финал: `Передал администратору, перезвонят и подберут время`.
- Срочность (`опухла щека`, `температура`) → сразу имя+телефон.
