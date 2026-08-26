# Worker v10.9

Fixes intermittent 500 errors when starting analysis after Instagram login.

- `/scrape` now shares the same per-client operation lock as login screenshot/status/click/type endpoints.
- Prevents concurrent navigation and screenshot polling on the same Playwright page.
- Serializes persistent-profile opening.
- Uses `wait_until=commit` for faster/more resilient Instagram post navigation.
- Returns explicit 401/502/503 error details instead of opaque Internal Server Error.
