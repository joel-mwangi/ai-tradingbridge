# Even/Odd Lab live collector

Even/Odd Lab now accepts **live-captured Deriv ticks only**. Historical backfills are retired.

## Runtime architecture

The collector is a separate, continuously running Python background worker. Do not run it inside a Vercel request handler: Vercel's serverless request lifecycle is not a durable process for a continuous WebSocket subscription.

The worker:
- subscribes to Deriv's public `ticks` stream for the configured markets;
- never calls `ticks_history`;
- writes batches to the dedicated Even/Odd Supabase project;
- records a heartbeat for each market;
- reconnects with bounded exponential backoff;
- does not backfill missed intervals after a disconnect.

A browser tab is not required to remain open. The worker must remain online.

## Deploy as an always-on background worker

Create a Background Worker service in a host that supports persistent Python processes (for example, Render Background Workers).

- Repository: `joel-mwangi/ai-tradingbridge`
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python -m scripts.even_odd_live_collector`
- Do not deploy this process as a Vercel serverless function.
- Use an always-on instance; a sleeping/free web service is not suitable for continuous collection.

Configure these environment variables on the worker only:

| Variable | Example / purpose |
| --- | --- |
| `EVEN_ODD_SUPABASE_URL` | URL of the dedicated Database 2 project |
| `EVEN_ODD_SUPABASE_SECRET_KEY` | Database 2 secret key; never expose to the browser |
| `EVEN_ODD_LIVE_SYMBOLS` | Comma-separated symbols, e.g. `R_50,1HZ100V` |
| `EVEN_ODD_WORKER_ID` | Optional stable worker label |

Do not use the primary TradeBridge Supabase credentials.

## Run locally for a controlled test

PowerShell:

```powershell
$env:EVEN_ODD_SUPABASE_URL = "https://YOUR-DB2-PROJECT.supabase.co"
$env:EVEN_ODD_SUPABASE_SECRET_KEY = "YOUR-DB2-SECRET"
$env:EVEN_ODD_LIVE_SYMBOLS = "R_50"
python -m scripts.even_odd_live_collector
```

Keep the process running while collecting. Stop it with Ctrl+C.

## Database migration

Apply `supabase/migrations/20261120040000_even_odd_live_stream_only.sql` to Database 2 before starting the worker. The migration:
- adds `capture_mode` and a unique ingestion `capture_id` to tick rows;
- marks all existing rows as `historical` by default;
- creates a service-role-only collector heartbeat table;
- adds an index for live-captured observations.

The research read path filters to `capture_mode = 'live_stream'`. Existing historical rows are retained in the database but excluded from the research dataset. No existing records are deleted by this change.

## Data integrity limits

- Each received tick gets a stable capture ID so retrying a failed database batch does not insert it twice.
- Deriv tick IDs and subscription IDs are stored separately when supplied.
- No uniqueness assumption is made about optional tick IDs.
- Repeated ticks with the same second and quote are retained; second-resolution timestamps are not sufficient to prove two observations are duplicates.
- Disconnects create observable collection gaps. The worker does not silently fill them with historical data.
- Live capture improves source provenance; it does not by itself establish statistical independence, predictive edge, or profitability.
- Broker execution remains disabled.
