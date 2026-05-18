# Jaratrade API

Backend for the Jaratrade marketplace - Nigeria↔UK B2B trade. Reverse-engineered from the original Postman collection and BRD, then re-implemented from scratch.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Framework | **FastAPI** | Async, OpenAPI built-in, Pydantic validation. |
| ORM | **SQLAlchemy 2.0** | Mature, type-safe `Mapped[]` API. |
| DB | **SQLite** for dev, **PostgreSQL**-ready | One env var swap (`DATABASE_URL`). |
| Auth | **JWT (HS256)** via `python-jose` | Stateless, role in claim, attached as Bearer. |
| Hashing | **bcrypt** (cost 12) | Direct `bcrypt` (avoids passlib/bcrypt 5.x compat issue). |
| Payments | **Flutterwave Inline + verify** via `httpx` | Returns Inline-checkout config from `/imp/payment/init`. |
| Uploads | **Cloudinary REST** | No SDK dependency; data-URL fallback when creds missing. |
| Email | **stdlib SMTP** | Stdout-fallback in dev. |
| Server | **uvicorn** | ASGI, auto-reload in dev. |
| Validation | **Pydantic v2** | Built into FastAPI. |

## Project layout

```
app/
├── main.py             ← FastAPI app, CORS, error envelopes, lifespan (auto-create tables + seed)
├── config.py           ← Pydantic Settings (env-driven)
├── database.py         ← Engine, Session, declarative Base
├── deps.py             ← get_current_user, require_importer/exporter/admin
├── security.py         ← bcrypt + JWT + ID helpers
├── envelope.py         ← {status, message, payload} response wrapper
├── constants.py
├── seed.py             ← Idempotent seed (markets, banks, plans, demo users, demo products)
│
├── models/             ← SQLAlchemy ORM
│   ├── base.py            (Base, TimestampMixin)
│   ├── user.py            (User, BusinessProfile, EmailVerificationToken,
│   │                       PasswordResetToken, ShippingAddress, FavouriteProduct)
│   ├── catalog.py         (Category, Market, Bank, LogisticsCompany,
│   │                       LogisticsRate, ImporterPlan, ExporterPlan)
│   ├── product.py         (Store, Product, Cart, CartItem)
│   ├── order.py           (Order, OrderItem, Payment, Review)
│   └── misc.py            (SupportTicket, Setting)
│
├── routers/
│   ├── public.py          ← /public/* - home, products, reference data, support, password reset
│   ├── auth.py            ← /imp/login, /exp/login, /adm/login + register + verify
│   ├── importer.py        ← /imp/* - cart, orders, payments, shipping, profile, favourites
│   ├── exporter.py        ← /exp/* - products, stores, profile, change-password, image upload
│   ├── admin.py           ← /adm/* - markets, categories, banks, logistics, plans
│   ├── settings_router.py ← /settings/commision_account
│   ├── bank_router.py     ← /bank/:id (legacy contract - PATCH/DELETE)
│   └── logs.py            ← /logs/orders (logistics partner facing)
│
├── services/
│   ├── flutterwave.py     ← Inline-checkout config + verify_by_reference
│   ├── cloudinary.py      ← Image upload (REST, data-URL fallback)
│   └── email.py           ← SMTP send + templates (stdout fallback)
│
└── schemas/               ← Pydantic DTOs
```

## Getting started

```bash
# 1. Create venv + install
python3 -m venv .venv
.venv/bin/pip install -r <(.venv/bin/pip freeze)  # or use the dependency list below

# 2. Configure
cp .env.example .env
# (adjust JWT_SECRET, optionally Cloudinary/Flutterwave/SMTP)

# 3. Run
./run.sh                # or:  .venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/docs> for Swagger UI.

### Dependencies

```
fastapi>=0.115
uvicorn[standard]>=0.32
sqlalchemy>=2.0
pydantic>=2.6
pydantic-settings>=2.4
python-multipart>=0.0.9
python-jose[cryptography]>=3.3
bcrypt>=4
cryptography>=42
httpx>=0.27
alembic>=1.13
email-validator>=2.2
```

## Demo accounts (seeded on first boot)

| Role | Email | Password |
|---|---|---|
| Admin | `admin@jaratrade.com` | `REDACTED-old-default` |
| Exporter | `exporter@jaratrade.com` | `REDACTED-old-default` |
| Importer | `importer@jaratrade.com` | `REDACTED-old-default` |

Plus 4 demo products on the exporter, 11 markets, 8 banks, 4 logistics partners, 5 categories and 2 plans for each role.

## What's new in v2.1

| Area | What changed |
|---|---|
| **Migrations** | Alembic wired in. Schema is versioned. Boot runs `alembic upgrade head` (falls back to `create_all` for tests). |
| **2FA** | TOTP (RFC 6238). Endpoints: `POST /auth/2fa/enroll`, `/confirm`, `/disable`, `/login`. Login returns `{requires_2fa: true}` when enabled. |
| **KYC queue** | `GET /adm/kyc/queue`, `POST /adm/kyc/:id/approve`, `POST /adm/kyc/:id/reject`. Approving activates the account + emails the applicant. |
| **Admin user search** | `GET /adm/users?role=&kyc_status=&q=` (closes the documented gap). Plus suspend/reactivate. |
| **Cart sync** | `POST /imp/cart/sync` accepts the local cart and replaces the server cart. Frontend now syncs on every change (debounced). |
| **Sponsored listings** | `/public/products` always surfaces `promote=1` first. |
| **Transaction caps** | `/imp/payment/init` enforces `transaction_limit` from the user's plan (when currencies match). 50%/80% warning emails fire automatically. |
| **Public reviews** | `GET /public/reviews/exporter/:id` returns paged reviews + rating distribution + average. |
| **Email service** | All 12 BRD templates wired to events. Idempotent via `dedupe_key`. `NotificationLog` table audits every send. |
| **Rate limiting** | `slowapi` - 10/min on login, 20/min on payment init. |
| **Request logging** | Structured access log per request with X-Request-ID propagation. |
| **Tests** | 22 pytest tests covering auth, public, cart, orders, payments, admin, KYC, 2FA. |
| **FX conversion** | `services/fx.py` - live rates via open.er-api.com (cached 6h) with hardcoded fallbacks. Used by the transaction-cap check so an NGN order can be measured against a GBP plan limit. |
| **Sentry** | `sentry-sdk[fastapi]` integration - error tracking + perf traces + SQLAlchemy spans + logging hook. Enabled when `SENTRY_DSN` is set; no-op otherwise. |
| **OpenTelemetry** | Auto-instrumentation of FastAPI + SQLAlchemy + httpx + Python logging. Console exporter in dev, OTLP/HTTP exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Every span carries the request's `X-Request-ID` (`jaratrade.request_id` attribute) for cross-system correlation. |
| **Subscription billing** | New `Subscription` table + `/imp\|exp/subscription/{,upgrade,verify,cancel}` endpoints. Upgrade returns a Flutterwave Inline config; verify confirms the payment, sets `period_end = now+30d`, and updates `user.plan_id`. Cancellation stops auto-renewal but keeps premium until `period_end`. |
| **Renewal cron** | `python -m app.cron expire_subscriptions` and `... renewal_reminders` - idempotent jobs that downgrade lapsed users and email them 3 days before renewal. CLI is one entrypoint; wire to systemd timer / Vercel Cron / Fly schedule. |

## Observability

The API has two layers of observability, both safe to ship without credentials.

### Sentry (errors + performance)
Set `SENTRY_DSN` to enable. Tunes:
```
SENTRY_TRACES_SAMPLE_RATE=0.1     # 10% of requests get a perf transaction
SENTRY_PROFILES_SAMPLE_RATE=0.0   # CPU profiles (off by default)
```
Errors raised by handlers are automatically captured. SQLAlchemy queries become spans inside the transaction. The `X-Request-ID` is attached as a tag for cross-referencing with the access log.

### OpenTelemetry (distributed tracing)
Three modes, picked automatically:

| When | Behaviour |
|---|---|
| `ENVIRONMENT=development` and no OTLP endpoint | Console exporter - spans print as JSON to stdout |
| `OTEL_EXPORTER_OTLP_ENDPOINT=https://...` | OTLP/HTTP exporter - ships to Honeycomb / Tempo / Sentry / Jaeger / etc. |
| `OTEL_CONSOLE_EXPORTER=true` | Force console exporter regardless of env |

Pass auth headers as comma-separated key=value:
```
OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=abc123,x-honeycomb-dataset=jaratrade
```

Auto-instruments: FastAPI requests, SQLAlchemy queries, httpx outbound (Flutterwave verify, FX, Cloudinary), Python logging. The middleware tags each span with `jaratrade.request_id` so you can pivot from an access-log line to a full trace in one click.

## Endpoint coverage

97+ endpoints. All 87 from the legacy Postman collection plus the new ones above. Highlights:

### Public (no auth)
```
GET  /public                          → home aggregate (top exporters, products, categories)
GET  /public/products[?category,p,len,sort_by,exporter,store]
GET  /public/products/:id
POST /public/:id                      → record product view
GET  /public/data/{category|market|bank|logistics|importer_plan|exporter_plan}
POST /public/support
POST /public/auth/password_reset      → request reset link
POST /public/auth/change_password     → submit new password with code
GET  /public/auth/password_reset      → verify reset code
```

### Auth
```
POST /imp/login   /exp/login   /adm/login           (JSON body)
PUT  /imp/register   /exp/register                   (multipart)
POST /adm/register
POST /imp/account_verification   /exp/account_verification
GET  /imp/account_verification   /exp/account_verification  (resend)
```

### Importer (Bearer JWT, role=importer)
```
GET  /imp/profile[?fav_prod=1|reviews=1]
POST /imp/profile
POST /imp/profile/review

POST /imp/cart                                       → add product
GET  /imp/cart                                       → list active carts
GET  /imp/cart/:cart_id
DELETE /imp/cart/:cart_id[?product_id=X]             → remove item or whole cart

POST /imp/order                                      → create from cart
GET  /imp/order   /imp/order/:order_id
DELETE /imp/order/:order_id                          → cancel

POST /imp/payment/init                               → returns Flutterwave Inline config
GET  /imp/payment/verify?tx_ref=…                    → verify against Flutterwave
GET  /imp/payment                                    → transaction history

GET  /imp/shipping
POST /imp/shipping
PATCH /imp/shipping/:id
```

### Exporter (Bearer JWT, role=exporter)
```
GET  /exp/profile[?from=…&to=…]                      → includes performance summary
POST /exp/profile
POST /exp/change_password

GET  /exp/store
PUT  /exp/store
DELETE /exp/store/:id

GET  /exp/product
PUT  /exp/product
PATCH  /exp/product/:id
DELETE /exp/product/:id
POST /exp/product/image/:id                          → upload (multipart, multi-file)
DELETE /exp/product/image/:id?image_path=…

POST /exp/update_order
```

### Admin (Bearer JWT, role=admin)
```
GET  /adm/market   PUT /adm/market   POST /adm/market/:id   DELETE /adm/market/:id
GET  /adm/category   PUT /adm/category   POST /adm/category/:id (update or delete)
GET  /adm/bank   PUT /adm/bank   PATCH /bank/:id   DELETE /bank/:id
GET  /adm/logistics[?exporter_id|importer_id|order_id|q]
PUT  /adm/logistics   POST /adm/logistics/:id   DELETE /adm/logistics/:id
PATCH /adm/logistics/:order_id                       → update delivery status
PUT  /adm/logistics_rate
PUT  /adm/importer_plan   PUT /adm/exporter_plan
GET  /adm/exporter_subscription

PUT  /settings/commision_account
GET  /logs/orders   POST /logs/orders
```

### v2.1 additions
```
GET  /adm/users[?role=&is_active=&kyc_status=&q=&p=&len=]
GET  /adm/users/:id
POST /adm/users/:id/suspend         POST /adm/users/:id/reactivate
GET  /adm/kyc/queue
POST /adm/kyc/:id/approve           POST /adm/kyc/:id/reject

POST /auth/2fa/enroll               POST /auth/2fa/confirm
POST /auth/2fa/disable              POST /auth/2fa/login

POST /imp/cart/sync                 → bulk-sync local cart to server
GET  /public/reviews/exporter/:id   → paged reviews + rating distribution
```

## Running the test suite

```bash
.venv/bin/pytest
```

22 tests: auth (login/role guard/2FA), public reference data, cart, orders, payments init, admin user search + KYC.

## Deployment

### Docker (any host)
```bash
docker compose up --build      # API + Postgres on 8000/5433
```

The `Dockerfile` uses `python:3.12-slim` and runs `alembic upgrade head` before starting uvicorn. The compose file boots a Postgres alongside; production wires `DATABASE_URL` to a managed Postgres instead.

### Fly.io
```bash
flyctl launch --no-deploy            # uses fly.toml
flyctl secrets set JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
flyctl secrets set DATABASE_URL=postgresql+psycopg://...
flyctl secrets set FLW_PUBLIC_KEY=... FLW_SECRET_KEY=...
flyctl secrets set CLOUDINARY_CLOUD_NAME=... CLOUDINARY_API_KEY=... CLOUDINARY_API_SECRET=...
flyctl secrets set SMTP_HOST=smtp.sendgrid.net SMTP_USER=apikey SMTP_PASSWORD=...
flyctl deploy
```

### Environment variables (production)
```
DATABASE_URL=postgresql+psycopg://...
JWT_SECRET=<48-byte URL-safe>
CORS_ORIGINS=["https://jaratrade.com"]
SITE_URL=https://jaratrade.com

CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...

FLW_PUBLIC_KEY=FLWPUBK-...
FLW_SECRET_KEY=FLWSECK-...
FLW_COMMISSION_SUBACCOUNT_ID=RS_...

SMTP_HOST=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USER=apikey
SMTP_PASSWORD=...
SMTP_FROM=no-reply@jaratrade.com
```

### CI

`.github/workflows/api.yml` runs pytest, an import smoke test, and a schema-drift check (autogenerate against current models - flags missing migrations).

## Key design choices

### Response envelope
Every response is wrapped as `{ status: bool, message: str, payload: <data> }` - same shape as the legacy API. Errors include an `errors: [str]` array.

### Multipart by default
All mutating endpoints accept `multipart/form-data` (matching the legacy contract and what the existing frontend client sends). Login endpoints accept JSON for cleanliness.

### IDs
Public IDs are 32-character hex UUIDs. The frontend treats them as opaque strings, so it works identically with these or the legacy Fernet tokens.

### Plans, fees, commission
Plan limits use `-1` to mean "unlimited" (mirrored from the legacy seed). Order creation auto-applies a 2% platform fee + 5% logistics estimate when a logistics partner is selected.

### Auth model
- One `users` table with a `role` column (`importer | exporter | admin`).
- Importer accounts auto-activate on email verify.
- Exporter accounts require manual admin activation (`is_active = False` until set).
- JWT subject is the user's UUID; role is in a custom claim and verified by `require_role`.

### Auto-seed on boot
The lifespan handler runs `Base.metadata.create_all()` then idempotently seeds reference data and three demo accounts. This means a fresh checkout boots into a usable state with one command.

### Dev-friendly fallbacks
- **No SMTP creds** → emails print to stdout (verification, reset, etc.).
- **No Cloudinary creds** → uploads become base64 data URLs (so the API still functions).
- **No Flutterwave secret** → `verify_by_reference` returns a "successful" stub (so checkout can be exercised end-to-end locally).

Each fallback is logged and clearly tagged so you don't accidentally ship them.

### Schema gaps from the legacy API that we filled in
- **`/public/data/logistics`** previously returned 500 - we implemented it properly.
- **`GET /adm/users`** wasn't documented - we expose `/adm/exporter_subscription` for now and have the User table ready for a full admin user-search endpoint.
- The legacy collection had `POST /adm/category/:id` for both update *and* delete - we kept that behaviour by accepting an optional `delete=1` form field.

## Production notes

1. **Switch DB to Postgres**: set `DATABASE_URL=postgresql+psycopg://user:pass@host/db`. Tables are SQLA-portable.
2. **Generate a real JWT secret**: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
3. **Wire SMTP**: e.g. SendGrid, Postmark, Mailgun. Update `.env`.
4. **Wire Cloudinary**: provide cloud name, API key, secret.
5. **Wire Flutterwave**: secret key + commission subaccount ID.
6. **Replace SQLAlchemy `create_all()` with Alembic**: scaffold included (`alembic` package installed); generate the first revision when the schema stabilises.
7. **Containerise**: a typical `Dockerfile` would be a 4-line slim-Python + uvicorn workdir image.
8. **Deploy**: Fly.io, Railway, Render, or any container host. Set env vars + a Postgres service.

## Railway deployment (config-as-code)

This repo ships two Railway service configs. Same Dockerfile, same env vars, different deploy behaviour.

| File | Service role | Start behaviour |
|---|---|---|
| `railway.json` | **Main API** (`jaratrade-api`) - long-running uvicorn server, public HTTPS endpoint, `/health` checked on every deploy | Uses `Dockerfile`'s `CMD` (runs `alembic upgrade head` then `uvicorn app.main:app ...`). Restarts on failure up to 5 times. |
| `railway.cron.json` | **Nightly payout cron** (`jaratrade-payouts-cron`) - runs once a day, exits | `python -m app.cron process_payouts` on schedule `0 2 * * *` UTC. Never auto-restarts (it's supposed to exit). |

### One-time setup in the Railway dashboard

The config files only take effect after the services exist in the Railway project. Do this once:

1. **Main API** - already deployed. Confirm under Settings → Source that **Config Path** is `railway.json` (the default).
2. **Cron service** - create a new service in the same project:
   - **+ New** → **GitHub Repo** → pick the same `jaratrade-api` repo.
   - Rename the service to `jaratrade-payouts-cron`.
   - Settings → Source → **Config Path:** set to `railway.cron.json`.
   - Settings → Networking → disable public networking (cron has no HTTP surface).
   - Variables → reference the same env vars from `jaratrade-api` (`DATABASE_URL`, `FLW_SECRET_KEY`, `FLW_PUBLIC_KEY`, `FLW_ENCRYPT_KEY`, `FLW_WEBHOOK_SECRET`, `FLW_COMMISSION_SUBACCOUNT_ID`, SMTP vars). Use "shared variables" / "variable reference" so they stay in sync with the API service.
   - Deploy.

After that, every push to `main` updates both services from git. The cron schedule, start command, and healthcheck path all live in the repo - no clicking required.

### Adding more cron jobs later

If you need more nightly jobs (e.g. `renewal_reminders`, `inventory_reminders`), don't pile them into the existing cron service - each Railway service has one schedule + one start command. Instead:

1. Copy `railway.cron.json` to `railway.cron.<jobname>.json`, swap the start command + schedule.
2. Add another service in the Railway project, pointed at the new config path.

The shared `JOBS` registry in `app/cron.py` means each service still uses the same Docker image - they just dispatch a different job.

## License

Proprietary - Jaratrade Ltd.
