# v10.3.1 Vercel sync + URL clear fix

- URL clear × is pinned inside the input field on desktop and mobile.
- Frontend and backend are bundled together using the single-request `/api/analyze` flow.
- This avoids the old in-memory/background-job API mismatch that can surface as `Not Found` on Vercel.
- Railway Worker is not included; keep your current worker deployment.
