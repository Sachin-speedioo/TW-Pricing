# TW Retail Pricing API

Gives the procurement price of a used two-wheeler: a predicted price, a ±₹5,000 band, and the model's 80 / 90 / 95% price
range. Model: **Pure MAXFIT + Optuna settings** (CatBoost), bundled in `model/` with the data it needs in `data/`. The folder is
self-contained: clone it, install the requirements and run.

| | URL |
|---|---|
| Web form | http://127.0.0.1:8000/app |
| Interactive docs (Swagger, try requests with **Authorize**) | http://127.0.0.1:8000/docs |
| Reference docs (ReDoc) | http://127.0.0.1:8000/redoc |
| OpenAPI schema | http://127.0.0.1:8000/openapi.json |

## Run

```bash
git clone <repo-url> tw-pricing-api && cd tw-pricing-api
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage_users.py add admin                       # first user (asks for a password, min. 8 characters)
python selftest.py                                     # optional, ~3 min: checks the install reproduces the model
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Start-up takes about 10 s (it loads the model, the catalogue and the training data). `--host 127.0.0.1` accepts connections from
this computer only; `--host 0.0.0.0` opens it to the network (put it behind HTTPS before exposing it beyond a trusted network:
logins and tokens travel in the requests).

From the parent folder (when the folder is named `app`) the package form also works: `python -m uvicorn app.main:app`,
`python -m app.selftest`, `python -m app.manage_users ...`.

### Docker

```bash
cp .env.example .env                                   # set TWR_ADMIN_PASSWORD (first user, min. 8 characters)
docker compose up -d --build                           # builds the image, starts it on http://localhost:8000
docker compose exec pricing-api python selftest.py     # optional, ~3 min
```

Or without compose:

```bash
docker build -t tw-pricing-api .
docker run -d --name tw-pricing-api -p 8000:8000 -v twr-state:/state -e TWR_ADMIN_PASSWORD=<password> tw-pricing-api
```

- The image (`python:3.14-slim` + CatBoost and its scientific-Python dependencies) holds the code, model and data; it runs as a non-root user and
  has a health check on `/health`.
- Logins live in the `/state` volume (`twr-state`): they survive rebuilds and restarts, and are never baked into the image
  (`.dockerignore` keeps `users.json`, `.jwt_secret` and `.env` out). `TWR_ADMIN_PASSWORD` only creates the first user while
  the volume has no users; later it is ignored.
- Users: `docker compose exec pricing-api python manage_users.py add <name>` (or `passwd`, `remove`, `list`).
- Port: `HOST_PORT` in `.env` changes the port on the host; platforms that set `PORT` (Cloud Run, Render, ...) are followed
  inside the container.
- New model: update `model/` and `data/` (see **Updating the model**), then `docker compose up -d --build`.
- Put HTTPS in front (a reverse proxy or the platform's load balancer) before exposing it beyond a trusted network.

### Configuration

Nothing needs configuring: every path defaults to this folder. Environment variables override them (`settings.py`):

| Variable | Default | Use |
|---|---|---|
| `TWR_MODEL_DIR` | `model/` | the model folder (manifest + files) |
| `TWR_DATA_DIR` | `data/` | catalogue, popularity, training data |
| `TWR_STATE_DIR` | this folder | where `users.json` and `.jwt_secret` live (e.g. a persistent volume) |
| `TWR_JWT_SECRET` | `.jwt_secret` file | token signing secret |
| `TWR_TOKEN_MINUTES` | 480 | login lifetime |
| `TWR_ADMIN_USER`, `TWR_ADMIN_PASSWORD` | `admin`, unset | create the first user at start-up when there are no users (servers / containers without a shell) |

### Folder

| Path | Content |
|---|---|
| `main.py` | FastAPI routes |
| `pipeline.py` | catalogue, dropdown data, features, price and ranges |
| `auth.py`, `manage_users.py` | login (password hashes, JWT), user admin |
| `settings.py` | paths and environment variables |
| `selftest.py` | the checks under **Check** |
| `static/index.html` | web form |
| `model/manifest.json` | names the model files, the range calibration, the ± band and the data files, with sha256s |
| `model/pure_optuna.cbm`, `.json` | the CatBoost model (517 trees) and its feature list / log-price offset |
| `model/ranges.json` | the 80 / 90 / 95% range widths by age band |
| `data/catalogue.csv` | the two-wheeler catalogue (5,278 variants, `model_key` precomputed) |
| `data/popularity.csv` | popularity class per model |
| `data/training_data.csv` | the model's 5,331 training bikes (features + price): training-bike counts, categories, cities, training-median reference |
| `data/selftest_eval.csv` | the 196 evaluation bikes and their evaluated prices (no personal data), for the self-test |
| `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example` | container build and run (see **Docker**) |
| `users.json`, `.jwt_secret` | created at first use; git-ignored, never commit them |

### Updating the model

The app serves whatever `model/manifest.json` describes. In the development project, `python scripts/export_app_bundle.py`
rewrites `model/` and `data/` from the latest experiment (`--tag` / `--name` pick another model); then run `python selftest.py`,
commit, and restart the server.

## Login

Every `/api/*` and `/auth/me` call needs a token. Get one with the username and password (form fields, OAuth2 password flow):

```bash
curl -X POST http://127.0.0.1:8000/auth/token -d "username=admin&password=<password>"
```
```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer", "expires_in": 28800}
```

Send it on each call as `Authorization: Bearer <access_token>`. Tokens last 8 hours (`TWR_TOKEN_MINUTES`); after that the API
answers `401` and you log in again.

Users (stored as salted PBKDF2-SHA256 hashes in `users.json`, never as plain text):

```bash
python manage_users.py list
python manage_users.py add <username>        # asks for the password (min. 8 characters), or --password
python manage_users.py passwd <username>
python manage_users.py remove <username>
```

Changes take effect at once, without a restart. Tokens are signed (HS256) with the secret in `.jwt_secret` (created on first
start) or the `TWR_JWT_SECRET` environment variable. Keep it private: anyone who has it can make valid tokens. Changing it
logs everyone out.

## Endpoints

| Method | Path | Login | Returns |
|---|---|---|---|
| GET | `/health` | no | `{"status": "ok", "model": ...}` |
| POST | `/auth/token` | no | token (above) |
| GET | `/auth/me` | yes | `{"username": "admin"}` |
| GET | `/api/years` | yes | mfg years, newest first |
| GET | `/api/makes?year=` | yes | makes |
| GET | `/api/models?make=&year=` | yes | models of a make |
| GET | `/api/variants?model_key=&year=` | yes | variants of a model |
| GET | `/api/cities` | yes | cities |
| POST | `/api/predict` | yes | price, band, ranges |

The dropdown calls follow the web form's order: **mfg year → make → model → variant**. With `year`, each list holds only what was
on sale that year: from 1 year before a variant's launch to 1 year after its discontinuation (on sale → this year). Without
`year`, the whole catalogue is returned.

### GET /api/years

Optional `model_key` and `variant_id` limit the list to that model / variant's years.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/years
```
```json
[2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, "...", 1990]
```

`/api/years?model_key=royalenfield|classic 350` → `[2026, 2025, ..., 2008]`

### GET /api/makes?year=2022

```json
["22Kymco", "22Motors", "Aahana", "ABZO", "...", "Yamaha", "Yezdi"]
```
(175 makes for 2022.)

### GET /api/models?make=Royal Enfield&year=2022

Sorted by the number of training bikes, then name. `training_bikes` = bikes of this model in the training data; `0` means the
model was never bought and the price leans on the catalogue features (the response then carries a low-confidence warning).

```json
[
  {"model_key": "royalenfield|classic 350", "model": "Classic 350", "training_bikes": 634},
  {"model_key": "royalenfield|hunter 350", "model": "Hunter 350", "training_bikes": 155},
  {"model_key": "royalenfield|bullet 350", "model": "Bullet 350", "training_bikes": 123}
]
```

Use `model_key` (not the name) in later calls. URL-encode it: `royalenfield%7Cclassic%20350`.

### GET /api/variants?model_key=royalenfield|classic 350&year=2022

Cheapest first. `year_start` / `year_end` = the catalogue's years (`9999` = on sale); `available_from` / `available_to` = the
years the form offers it (`null` = on sale).

```json
[
  {"variant_id": "BW-V136", "variant": "Standard", "year_start": 2009, "year_end": 2021, "available_from": 2008,
   "available_to": 2022, "ex_showroom": 138644.0, "status": "Discontinued"}
]
```

### GET /api/cities

```json
["Pune", "Mumbai", "Bangalore", "Delhi"]
```

The cities in the training data; `/api/predict` accepts only these.

### POST /api/predict

Request (JSON):

| Field | Type | Required | Notes |
|---|---|---|---|
| `make` | string | yes | from `/api/makes` |
| `model_key` | string | yes | from `/api/models`; must belong to `make` |
| `variant_id` | string / null | no | from `/api/variants`; null = not known → the model's cheapest variant for that year is used (as in training) |
| `mfg_year` | int | yes | 1980 – quote year; must be within the variant's catalogue years ± 3 |
| `owner_count` | int | no (default 1) | 1 – 4; 4 = 4 or more |
| `odometer_km` | number | yes | 0 – 2,000,000 |
| `city` | string | yes | from `/api/cities` |
| `quote_date` | date | no (default today) | `YYYY-MM-DD`; the price as of this date (bike age is counted to it) |

```bash
curl -X POST http://127.0.0.1:8000/api/predict \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"make": "Royal Enfield", "model_key": "royalenfield|classic 350", "variant_id": "BW-V136",
       "mfg_year": 2020, "owner_count": 1, "odometer_km": 18000, "city": "Pune", "quote_date": "2026-11-02"}'
```

Response (shortened; `features` lists all 24 model inputs):

```json
{
  "model": "Pure MAXFIT + Optuna settings",
  "quote_date": "2026-11-02",
  "bike": {"make": "Royal Enfield", "model": "Classic 350", "model_key": "royalenfield|classic 350", "variant": "Standard",
           "variant_id": "BW-V136", "variant_basis": "matched_variant", "mfg_year": 2020, "owner_count": 1,
           "odometer_km_entered": 18000.0, "city": "Pune"},
  "predicted_price": 135800.0,
  "price_band_5k": {"low": 130800.0, "high": 140800.0, "band_inr": 5000},
  "model_range": {"low": 97500.0, "high": 189300.0, "level_pct": 90, "age_band": "6-9 yrs"},
  "model_ranges": {
    "80": {"low": 107500.0, "high": 171700.0, "level_pct": 80, "age_band": "6-9 yrs"},
    "90": {"low": 97500.0, "high": 189300.0, "level_pct": 90, "age_band": "6-9 yrs"},
    "95": {"low": 84700.0, "high": 218000.0, "level_pct": 95, "age_band": "6-9 yrs"}
  },
  "training_reference": {"median_price": 131080.44, "bikes": 56, "matched_on": "year + model"},
  "training_bikes_for_model": 634,
  "features": {"v_ex_showroom": 138644.0, "vehicle_age": 6.33, "odometer_km": 18000.0, "variant_basis": "matched_variant", "...": "..."},
  "monotonic_rules_in_model": {"mfg_year": 1, "owner_count": -1, "m_exsh_median": 1, "vehicle_age": -1, "odometer_km": -1,
                               "v_ex_showroom": 1},
  "warnings": []
}
```

| Field | Meaning |
|---|---|
| `predicted_price` | the model's price, rounded to ₹100 |
| `price_band_5k` | predicted price ± ₹5,000 |
| `model_range` | the 90% range: about 9 in 10 real prices of such bikes fall inside (calibrated on held-out real prices, by age band) |
| `model_ranges` | the 80 / 90 / 95% ranges |
| `training_reference` | median real price of similar bikes in the training data (pure data), matched on the most specific of: year + model + variant + city, year + model + variant, year + model (≥ 3 bikes; else 1–2 bikes) |
| `features` | the 24 values the model received |
| `monotonic_rules_in_model` | 1 = price never falls when the feature rises; −1 = never rises |
| `warnings` | plain-text notes, below |

Warnings:
- Variant not given: the model's base variant for the year was used.
- Mfg year outside the variant's launch – discontinuation years (still priced when within its catalogue years ± 3).
- Odometer outside what the training data treats as plausible (> 150,000 km, > 40,000 km/yr, or < 300 km/yr at age ≥ 1):
  priced at the nearest plausible value, so more km never gives a higher price.
- Low confidence: the model has no or fewer than 10 training bikes.

## Errors

| Code | When | Body |
|---|---|---|
| 401 | no / expired / bad token; wrong login | `{"detail": "Not authenticated"}` or `"Not logged in or session expired"` |
| 422 | invalid input: unknown make / model / variant, variant of another model, year outside the variant's years, city not in the list, missing field (e.g. `odometer_km`) | `{"detail": "unknown make 'Nope'"}` or FastAPI's field list |

## How a price is made

1. **Variant**: the chosen catalogue variant, or (not known) the model's cheapest variant sold in that year ± 3, as in training.
2. **Features** are built exactly as the training notebook builds them, from `data/catalogue.csv` and `data/popularity.csv`: the variant's ex-showroom, cc, power, mileage, segment, status, launch year; the model's
   ex-showroom median / min / max, on-sale flag and years on the market; the bike's age (quote date − mfg year), owners,
   odometer and city. Defaults for what the form does not ask: missing cc / power / mileage → the model's median; category →
   the model's usual category in training (else EV / Scooter / Bike from the catalogue); popularity → the model's scraped-listing
   class (missing when unknown).
3. **Price** from the CatBoost model, which has monotonic rules: an older bike, more km or more owners never gets a higher price.
4. **Ranges**: price × exp(∓ width), width per age band. Low and High are then made monotonic over mfg year as well, so the range
   never moves against the depreciation order.

## Check

```bash
python selftest.py
```

Uses only the files in this folder, so it also checks a fresh clone / server. Checks that the API (1) rebuilds the 24 features of all 5,331 training bikes identically, (2) reproduces the evaluated prices of
the 196 Evaluation Sheet bikes, (3) never breaks the depreciation order (km, owners, mfg year) for price, Low, High or the ±₹5k
band, and (4) can price every year / model / variant the form offers. Last run (2026-11-02): all passed.
