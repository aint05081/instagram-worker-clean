# Instagram Comment Analyzer v11.0 — Mobile Background Jobs

## What changed
- Mobile no longer keeps one long `/api/analyze` browser request open.
- `/api/job-start` returns a `job_id` immediately.
- Railway continues Instagram scraping in a server-side background task even when the phone screen is off or the browser is backgrounded.
- Railway calls Vercel `/api/job-callback` server-to-server for OpenAI analysis and Google Sheets saving.
- Job state/result is persisted under Railway volume `/data/jobs`.
- The browser stores the active `job_id` in `localStorage` and resumes status polling on `visibilitychange`, `pageshow`, or reconnect.

## Required deployment settings
### Railway
- Existing `WORKER_API_KEY` must stay configured.
- Existing Railway volume `/data` must stay mounted.
- Optional: `JOB_ROOT=/data/jobs`.

### Vercel
- `INSTAGRAM_WORKER_URL` = Railway worker URL
- `INSTAGRAM_WORKER_KEY` = same value as Railway `WORKER_API_KEY`
- Existing OpenAI and Google Sheets environment variables stay unchanged.

Both Railway and Vercel must be redeployed because worker + Vercel code changed.
