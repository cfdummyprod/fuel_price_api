# Fuel Price API

Scrapes petrol prices for ~350 Indian cities daily from goodreturns.in and
serves them as a JSON API.

## Architecture

```
Render Cron Job (daily)          Render Web Service (always ready)
  discover 35 state pages   -->                          
  -> ~350 city URLs                    GET /prices
  -> scrape XPath price      -->  Postgres  <--   GET /prices/{slug}
  -> upsert into Postgres                          GET /states
```

Two Render services share one Postgres database. The cron job does the
scraping once a day; the web service only ever reads from the DB, so it
stays fast and never gets blocked by the source site.

## 1. Get a free Postgres database (Neon)

Render no longer offers a free Postgres tier, so use **Neon** (free,
no credit card): https://neon.tech

1. Create a project.
2. Copy the connection string — looks like:
   `postgresql://user:password@ep-xxxx.us-east-2.aws.neon.tech/dbname?sslmode=require`

## 2. Push this project to a GitHub repo

Render deploys from a git repo. Push this folder as-is (it already has
`render.yaml`, so Render will auto-detect both services).

## 3. Deploy on Render

1. New -> Blueprint -> connect your repo -> Render reads `render.yaml`
   and creates both services automatically.
2. For **both** services (`fuel-price-api` and `fuel-price-scraper`),
   set the environment variable:
   - `DATABASE_URL` = your Neon connection string from step 1
3. Optionally set `ADMIN_API_KEY` on the web service if you want the
   manual `/admin/refresh` trigger.

## 4. First run

The cron job runs on its schedule (`0 1 * * *` = daily 1am UTC by
default — edit in `render.yaml`). To seed the database immediately
instead of waiting for the first scheduled run, go to the cron job in
the Render dashboard and click **Trigger Run**.

The first run will also auto-discover all city URLs (since the
`city_urls` table starts empty) and cache them, so subsequent daily
runs just scrape — they don't need to re-crawl the state pages every
time. Pass `--rediscover` (edit the cron start command temporarily, or
call it manually) if goodreturns.in adds/removes cities and you want
to refresh the list.

## 5. Use the API

```
GET /prices                        -> all cities (petrol)
GET /prices?state=Maharashtra      -> filter by state
GET /prices/chandigarh             -> single city
GET /states                        -> list of available states
GET /meta                          -> total cities cached, last scrape time, staleness flag
```

Example response for `/prices/chandigarh`:
```json
{
  "slug": "chandigarh",
  "fuel": "petrol",
  "city_name": "Chandigarh",
  "state": "Chandigarh",
  "price": 94.30,
  "currency": "INR",
  "raw_text": "₹94.30",
  "scraped_at": "2026-07-02T01:03:11Z",
  "status": "ok"
}
```

## Local development

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://user:pass@localhost/fuelprices"

# one-off scrape to seed data
python -m scripts.run_scrape --rediscover

# run the API
uvicorn app.main:app --reload
```

## Notes / things to tune later

- **Anti-block hardening**: `app/scraper.py` now uses a shared rate
  limiter (min ~2-3.5s between *any* two requests, not per-thread) plus
  a small rotating User-Agent pool and Referer header. Concurrency is
  kept low (`max_workers=3`). At these settings a full 350-page run
  takes roughly 10-15 minutes — deliberately slow to stay under the
  radar. Adjust `min_interval` / `jitter` / `max_workers` in
  `scripts/run_scrape.py` if you want it faster, but go up gradually
  and watch the `blocked` count in the run summary.
- **Block detection vs. missing data**: a page returning 403/429/503,
  or a body containing Cloudflare/challenge/captcha markers, is now
  recorded as `status="blocked"` — distinct from `"not_found"` (page
  loaded fine, XPath just didn't match, e.g. markup changed). Blocked
  results are **not** written over existing good rows in the DB, so a
  bad run leaves yesterday's price in place instead of nulling it out.
- **Circuit breaker**: if 5 requests in a row come back blocked, the
  run stops early entirely rather than working through the rest of the
  list against a wall — protects tomorrow's run from an extended ban.
  Check the cron job logs for `Circuit breaker tripped` to know this
  happened.
- **Concurrency**: lower `max_workers` / raise `min_interval` further
  if you still see blocks; this trades runtime for safety.
- **Expanding fuels**: `discover_all_cities()` and the scraper both
  take a `fuel` parameter (`petrol`, `diesel`, `lpg`, `cng`, `png`) —
  the URL and XPath patterns are the same across fuel types on this
  site, so adding diesel is just calling the same functions with
  `fuel="diesel"`.
- **Render free tier sleep**: the free *web service* spins down after
  15 min of no traffic and wakes on the next request (a few seconds
  delay) — fine since it's read-only against Postgres. The *cron job*
  isn't subject to this; it just runs on schedule. Make sure the cron
  job's time limit/timeout on Render is set above ~15 minutes given the
  slower pacing above.
- **XPath fragility**: if goodreturns.in changes their markup, the
  `not_found` count in the scrape summary will spike — that's your
  signal to re-check the XPath.
