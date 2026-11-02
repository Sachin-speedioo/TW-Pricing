"""TW Retail pricing API (FastAPI) - the Pure MAXFIT + Optuna model behind a login.

Run (inside this folder):  python -m uvicorn main:app --host 127.0.0.1 --port 8000
    (from its parent:      python -m uvicorn app.main:app ...)
Web:   http://127.0.0.1:8000/app        (login, dropdowns, price)
Docs:  http://127.0.0.1:8000/docs       (Swagger; 'Authorize' with a username / password)
Users: python manage_users.py add <username>
Check: python selftest.py               (features = training, prices = evaluation, depreciation rules hold)

Endpoints (all /api/* need 'Authorization: Bearer <token>' from POST /auth/token):
  POST /auth/token   form username + password -> JWT          GET /auth/me
  GET  /api/years | /api/makes?year= | /api/models?make=&year= | /api/variants?model_key=&year= | /api/cities
       (mfg year first: makes / models / variants on offer that year, by launch and discontinuation year)
  POST /api/predict  bike -> predicted price, +-Rs 5k band, the model's 80 / 90 / 95% range, training reference, features used
"""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from auth import USERS_FILE, authenticate, bootstrap_admin, create_token, current_user, load_users
from pipeline import BAND_INR, MODEL_NAME, InputError, engine
from settings import APP_DIR, MODEL_DIR, manifest

STATIC = APP_DIR / "static"
log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_):
    engine()                      # load the model and catalogue once at start-up
    user = bootstrap_admin()
    if user:
        log.info("created the first user '%s' (TWR_ADMIN_PASSWORD)", user)
    if not load_users():
        log.warning("no users in %s - add one: python manage_users.py add <name>", USERS_FILE)
    log.info("model '%s' from %s (exported %s)", MODEL_NAME, MODEL_DIR, manifest().get("exported"))
    yield


app = FastAPI(title="TW Retail pricing API", version="1.0.0", lifespan=lifespan,
              description=f"Procurement price for a used two-wheeler. Model: {MODEL_NAME}. Log in via **Authorize** first.")


class Bike(BaseModel):
    make: str = Field(examples=["Royal Enfield"])
    model_key: str = Field(description="from /api/models", examples=["royalenfield|classic 350"])
    variant_id: str | None = Field(None, description="from /api/variants; empty = not known (the model's base variant is used)",
                                   examples=["BW-V136"])
    mfg_year: int = Field(ge=1980, le=2100, examples=[2021])
    owner_count: int = Field(1, ge=1, le=4, description="4 = 4 or more")
    odometer_km: float = Field(ge=0, le=2_000_000, examples=[11249])
    city: str = Field(examples=["Pune"])
    quote_date: dt.date | None = Field(None, description="price as of this date (default today)")


def _call(fn, *a):
    try:
        return fn(*a)
    except InputError as e:
        raise HTTPException(422, str(e))


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app")


@app.get("/app", include_in_schema=False)
def web_app():
    return FileResponse(STATIC / "index.html")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "model": MODEL_NAME, "model_exported": manifest().get("exported"), "price_band_inr": BAND_INR}


@app.post("/auth/token", tags=["auth"])
def login(form: OAuth2PasswordRequestForm = Depends()):
    if not authenticate(form.username, form.password):
        raise HTTPException(401, "Wrong username or password", headers={"WWW-Authenticate": "Bearer"})
    token, ttl = create_token(form.username)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl}


@app.get("/auth/me", tags=["auth"])
def me(user: str = Depends(current_user)):
    return {"username": user}


YEAR = Query(None, ge=1980, le=2100, description="mfg year: only what was on sale that year (launch .. discontinuation)")


@app.get("/api/years", tags=["catalogue"])
def years(model_key: str | None = Query(None), variant_id: str | None = Query(None), _: str = Depends(current_user)):
    return _call(engine().years, model_key or None, variant_id or None)


@app.get("/api/makes", tags=["catalogue"])
def makes(year: int | None = YEAR, _: str = Depends(current_user)):
    return engine().makes(year)


@app.get("/api/models", tags=["catalogue"])
def models(make: str = Query(...), year: int | None = YEAR, _: str = Depends(current_user)):
    return _call(engine().models, make, year)


@app.get("/api/variants", tags=["catalogue"])
def variants(model_key: str = Query(...), year: int | None = YEAR, _: str = Depends(current_user)):
    return _call(engine().variants, model_key, year)


@app.get("/api/cities", tags=["catalogue"])
def cities(_: str = Depends(current_user)):
    return engine().cities


@app.post("/api/predict", tags=["price"])
def predict(bike: Bike, _: str = Depends(current_user)):
    E = engine()
    if bike.city not in E.cities:
        raise HTTPException(422, f"city must be one of {E.cities}")
    return _call(E.price, bike.make, bike.model_key, bike.variant_id or None, bike.mfg_year, bike.owner_count, bike.odometer_km,
                 bike.city, bike.quote_date)
