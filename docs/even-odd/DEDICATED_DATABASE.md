# Even/Odd Lab dedicated database

Even/Odd Lab uses a separate Supabase project. It must never fall back to the
application's primary Supabase credentials.

## Backend environment variables

Configure these server-side on the backend deployment only:

- `EVEN_ODD_SUPABASE_URL`: URL of the dedicated Even/Odd Supabase project.
- `EVEN_ODD_SUPABASE_SECRET_KEY`: project secret key (preferred), or
- `EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY`: legacy service-role key if the project
  still uses one.

Do not prefix these variables with `NEXT_PUBLIC_` and do not configure them in
the Next.js frontend. Do not reuse `SUPABASE_URL`,
`SUPABASE_SECRET_KEY`, or `SUPABASE_SERVICE_ROLE_KEY`, which belong to
other application integrations.

## Storage model

- `even_odd_collection_runs`: one row per bounded collection request.
- `even_odd_ticks`: ordered tick observations belonging to a collection.

Both tables have RLS enabled and no public or authenticated policies. The
backend uses the dedicated server-side secret/service-role key, which bypasses
RLS. Explicit Data API grants are restricted to `service_role`; neither
`anon` nor `authenticated` receives table privileges.

## Operational notes

The current collector is request-scoped and bounded to 10–300 ticks. This is
not a continuous stream worker. Collection data is research data only and
does not authorize trade execution. Do not train or evaluate a model using
random train/test splits; preserve chronological order and use a final
out-of-sample test period.
