# v10.2 Vercel sync fix

- Removes in-memory job polling on Vercel.
- `/api/analyze` runs Worker scrape -> OpenAI -> Google Sheets in a single request.
- CSV is generated in the browser, so it does not depend on Vercel `/tmp` surviving between requests.
- Railway Worker code does not need to change.
