# Деплой на Netlify — Имплант-Дент (Gemini)

## Env (обязательно)
Site settings → Configuration → Environment variables → Add:
- `GEMINI_API_KEY` = `твой ключ с https://aistudio.google.com/app/apikey`  (Secret, All scopes, Same for all contexts)
- `GEMINI_MODELS` = `gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite`
- (опц. фолбэк) `OPENROUTER_API_KEY` = `sk-or-v1-...`
- `OPENROUTER_MODELS` = `minimax/minimax-m3:free,liquid/lfm-2.5-2.6b:free`

## Вариант A — CLI
```
npm i -g netlify-cli
netlify deploy --prod --dir demo/static --functions netlify/functions --site startling-conkies-4c42ae
```

## Вариант B — Git
Push репо, Netlify автодеплой (publish `demo/static`, functions `netlify/functions` из `netlify.toml`).

## Вариант C — Drag&Drop
Перетащи `implant-dent-static.zip` (в нём `index.html` в корне + `netlify/functions`) на https://app.netlify.com/sites/startling-conkies-4c42ae/deploys

После любого варианта: `Deploys → Trigger deploy → Clear cache` если менял env.
Проверка: `POST https://<домен>/.netlify/functions/chat {"messages":[{"role":"user","content":"Где вы находитесь?"}]}` → `200 {"reply":"ул. Интернациональная, 83..."}`

## Локально без Netlify
```
cd demo && pip install -r requirements.txt && uvicorn app:app --port 8000
```
