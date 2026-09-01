# Имплант-Дент — демо ИИ-администратора (Петропавловск) — Gemini

Папка: `C:\TGOD\200 000 tg+cloud`

## Что внутри
- `RESEARCH_Имплант-Дент.md` — ресёрч по 2ГИС (цены 25.02.2026, отзывы, контакты)
- `PROMPT_Имплант-Дент.md` — **новый** заполненный промпт (7345 симв, без выдуманного графика, с полным прайсом)
- `demo/` — демо-бот на **Gemini** (free 1500 запр/день) + OpenRouter фолбэк
- `netlify/functions/chat.js` — serverless прокси Gemini

## Модель: почему Gemini и хватает на весь день
- `gemini-3.6-flash` (primary) — 1500 RPD / 15 RPM / 1M токенов, free tier. Фолбэки `gemini-3.5-flash`, `gemini-3.5-flash-lite`.
- На демо-трафике (100 чатов/день) хватает с запасом. OpenRouter оставлен как фолбэк если квота Gemini.
- Ключ Gemini: `AQ.Ab8RN...tJMA` (в `demo/.env` локально, в Netlify → `GEMINI_API_KEY`)

## Демо — как запустить локально
```bat
cd "C:\TGOD\200 000 tg+cloud\demo"
pip install -r requirements.txt
uvicorn app:app --port 8000  # http://localhost:8000
# или консоль: python bot.py
```
`.env` уже содержит `GEMINI_API_KEY` и `GEMINI_MODELS=gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite`

## Деплой на Netlify — см. README_NETLIFY.md
- `netlify.toml` `publish=demo/static` `functions=netlify/functions`
- Функция `netlify/functions/chat.js` — Gemini primary, 12с timeout, 401 = сразу "ключ отклонён"
- Env в Netlify UI: `GEMINI_API_KEY`, `GEMINI_MODELS`, `OPENROUTER_API_KEY` (фолбэк)
- ZIP: `implant-dent-netlify.zip` (полный проект) и `implant-dent-static.zip` (для drag&drop)

## Как бот себя ведёт (новый промпт)
- Сначала отвечает, потом **один вопрос**. Коротко, 1-2 строки.
- Цены только из прайса 25.02.2026 с оговоркой `точную скажут после осмотра`.
- График не выдумывает: `приём по записи, точное время подскажет администратор`.
- Не ставит диагнозы, не называет окна. Финал: `Передал администратору, перезвонят и подберут время`.
- Срочность (`опухла щека`, `температура`) → сразу имя+телефон.
