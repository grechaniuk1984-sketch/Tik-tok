# TikTok video factory (безкоштовно)

Щодня в хмарі GitHub створює відео для 10 каналів. Ви лише завантажуєте їх у TikTok.

## Запуск (15 хвилин)
1. Ключі (безкоштовні): Gemini — aistudio.google.com/apikey, Pexels — pexels.com/api
2. Створіть репозиторій на GitHub (публічний = безлімітні хвилини) і завантажте всі ці файли, зберігши папку .github
3. Settings -> Secrets and variables -> Actions -> New secret: GEMINI_API_KEY та PEXELS_API_KEY
4. Вкладка Actions -> generate-videos -> Run workflow (далі працюватиме щодня сам)
5. Після завершення відкрийте запуск і завантажте архів videos-... внизу сторінки (Artifacts)

У кожній папці: video.mp4, caption.txt (підпис + хештеги), script.txt.

## Під час завантаження в TikTok
- Увімкніть позначку "AI-generated content"
- Планувальник TikTok (веб) дозволяє виставити публікацію наперед
- Дивіться відео перед публікацією: перевірте факти й відповідність кадрів темі

## Налаштування
- channels.yaml: ніші, голоси, videos_per_day
- Тест одного каналу: python generate.py money_basics
