"""Pricing pipeline behind the API: dropdown inputs -> the model's 24 features -> price, +-Rs 5k band and the model's range.

Model and data come from app/model (manifest.json names the files) and app/data (see app/settings.py); nothing outside app/ is
read, and nothing is written. Bundled by scripts/export_app_bundle.py: Pure MAXFIT + Optuna settings, the model of
reports/Best_Model_Predictions.xlsx, with the catalogue (master_2w_clean.csv, model_key precomputed), the popularity classes and
the model's training bikes (pure data).

Features are built exactly as the training notebook (notebooks/catboost_price_model.ipynb, data cell) builds them:
  variant   v_ex_showroom, v_engine_cc, v_power_bhp, v_mileage_kmpl, v_segment, v_status, v_year_start, m2w_model_id, make
            from the catalogue row (data/catalogue.csv); cc / power / mileage missing -> the model's median (as in training)
  model     m_exsh_median / min / max, m_on_sale, first year: aggregates over the model's non-upcoming variants
  derived   vehicle_age = quote year + (quote month - 1) / 12 - (mfg year + 0.5); model_years_on_market = quote year - first year;
            years_since_discontinued = quote year - discontinued year (0 when the variant is on sale)
  defaults  variant not known -> the model's cheapest variant valid for the year (variant_basis 'model_base_variant', as in
            training); category -> the model's most common category in the training data (else EV / Scooter / Bike from the
            catalogue); popularity_class -> scraped-listing class of the model (missing when unknown); city -> as entered
  odometer  readings the training data would call implausible (> 150,000 km, > 40,000 km/yr, < 300 km/yr at age >= 1) are held at
            the nearest plausible value with a warning, so more km never gives a higher price; blank = unknown (as in training)
Dropdowns (mfg year first): a variant is offered from 1 year before its launch to 1 year after its discontinuation (on sale ->
  this year). Launch = the earlier of the catalogue start year and bmc_launch_date (the latter is often the latest refresh, e.g.
  Activa DLX 2025 though sold since 2020); discontinuation = the later of bmc_discontinued_date and the catalogue end year. The
  1-year margin covers catalogue dates that are slightly off (98.6% of the pure-data bikes can be picked; strict: 96.2%). Kept
  inside the window the model accepts (year_start - 3 .. year_end + 3), so every offered year can be priced; makes / models are
  those with at least one variant offered for the chosen year.
Depreciation rules: the model holds them exactly (monotonic: older / more km / more owners -> never dearer). The model's range
  (Low / High = price x exp(-/+ w), w per age band) would step at the band edges, so Low and High are made monotonic over mfg
  year as well (envelope over all years: the range can only widen, never narrow).
"""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor, Pool

from settings import MODEL_DIR, data_file, manifest

MODEL_NAME = manifest()["name"]
BAND_INR = int(manifest()["price_band_inr"])
CATS = ["make", "model_key", "m2w_model_id", "category", "city", "v_segment", "v_status", "m_on_sale", "variant_basis", "popularity_class"]
AGE_BANDS = [("0-3 yrs", 3), ("3-6 yrs", 6), ("6-9 yrs", 9), ("9+ yrs", np.inf)]
ODO_MAX, ODO_MAX_PER_YEAR, ODO_MIN_PER_YEAR = 150_000, 40_000, 300          # the training notebook's plausibility rules
YEAR_TOL = 3                                                                 # config.yaml variant_matching tolerance_years
AVAIL_MARGIN = 1                                                             # dropdowns: years around launch / discontinuation
FIRST_YEAR = 1990
TM_LEVELS = [(["mfg_year", "model_key", "variant_id", "city"], 3, "year + model + variant + city"),
             (["mfg_year", "model_key", "variant_id"], 3, "year + model + variant"),
             (["mfg_year", "model_key"], 3, "year + model"),
             (["mfg_year", "model_key"], 1, "year + model (1-2 bikes)")]


class InputError(ValueError):
    """Bad request input (unknown make / model / variant, year outside the catalogue window)."""


class Engine:
    def __init__(self):
        man = manifest()
        cfg = json.loads((MODEL_DIR / man["model_config"]).read_text(encoding="utf-8"))
        self.features, self.y0 = cfg["features"], float(cfg["y0"])
        self.model = CatBoostRegressor()
        self.model.load_model(str(MODEL_DIR / man["model"]))
        self.ranges = json.loads((MODEL_DIR / man["ranges"]).read_text(encoding="utf-8"))[man["ranges_key"]]
        mc = self.model.get_all_params().get("monotone_constraints") or {}
        mc = {self.model.feature_names_[int(k)]: v for k, v in mc.items()} if isinstance(mc, dict) else dict(zip(self.model.feature_names_, mc))
        self.mono = {k: int(v) for k, v in mc.items() if int(v)}
        self._catalogue()
        self.pure = pd.read_csv(data_file("training_data"), low_memory=False)      # the model's training bikes
        self.train_bikes = self.pure["model_key"].value_counts()
        self.category_of = self.pure.dropna(subset=["category"]).groupby("model_key")["category"].agg(lambda s: s.value_counts().index[0])
        self.cities = list(self.pure["city"].value_counts().index)

    # ------------------------------------------------------------------ catalogue (same code as the notebook's data cell)
    def _catalogue(self):
        cat = pd.read_csv(data_file("catalogue"), low_memory=False)               # model_key precomputed at export
        pop = pd.read_csv(data_file("popularity"))
        num = lambda s: pd.to_numeric(s, errors="coerce")  # noqa: E731
        year_of = lambda s: pd.to_datetime(s, errors="coerce", dayfirst=True).dt.year  # noqa: E731
        cat["ex_showroom"] = num(cat["ex_showroom"]); cat["engine_cc"] = num(cat["engine_cc"])
        cat["power_bhp"] = num(cat["mmv_power_bhp"]); cat["mileage_kmpl"] = num(cat["mmv_mileage"])
        cat["launch_year"] = year_of(cat["bmc_launch_date"]).fillna(cat["year_start"])
        cat["discontinued_year"] = year_of(cat["bmc_discontinued_date"]).fillna(cat["year_end"].where(cat["year_end"] < 9999))
        cat["is_discontinued"] = cat["status"].eq("Discontinued")
        c2 = cat[cat["status"] != "Upcoming"]
        M = c2.groupby("model_key").agg(m_exsh_min=("ex_showroom", "min"), m_exsh_median=("ex_showroom", "median"),
                                        m_exsh_max=("ex_showroom", "max"), m_cc=("engine_cc", "median"),
                                        m_power_bhp=("power_bhp", "median"), m_mileage_kmpl=("mileage_kmpl", "median"),
                                        m_first_year=("year_start", "min"), m_last_year=("year_end", "max"))
        M["m_on_sale"] = M["m_last_year"] >= 9999
        self.M = M
        self.P = pop.set_index("model_key")["popularity_class"]
        # years the web form offers a variant: launch .. discontinuation (on sale -> open) +- AVAIL_MARGIN, inside the window
        # resolve_variant accepts
        launch = np.minimum(c2["year_start"], c2["launch_year"])
        disc = np.maximum(c2["discontinued_year"].fillna(np.inf), c2["year_end"].where(c2["year_end"] < 9999, np.inf))
        c2 = c2.assign(avail_from=np.maximum(launch - AVAIL_MARGIN, c2["year_start"] - YEAR_TOL),
                       avail_to=np.minimum(disc + AVAIL_MARGIN, (c2["year_end"] + YEAR_TOL).where(c2["year_end"] < 9999, np.inf)))
        self.cat = c2.set_index("m2w_variant_id", drop=False)
        self.family = c2.groupby("model_key")["model_family"].agg(lambda s: s.value_counts().index[0])
        self.make_of = c2.groupby("model_key")["make"].agg(lambda s: s.value_counts().index[0])

    # ------------------------------------------------------------------ dropdown data
    def _available(self, year: int | None) -> pd.DataFrame:
        """Variants offered for this mfg year (all variants when year is None)."""
        c = self.cat
        return c if year is None else c[(c["avail_from"] <= year) & (c["avail_to"] >= year)]

    def mfg_years(self) -> list[int]:
        """Mfg years (newest first) with at least one variant on offer, from FIRST_YEAR to this year."""
        a = self.cat
        return [y for y in range(dt.date.today().year, FIRST_YEAR - 1, -1) if ((a["avail_from"] <= y) & (a["avail_to"] >= y)).any()]

    def makes(self, year: int | None = None) -> list[str]:
        keys = self._available(year)["model_key"].unique()
        return sorted(self.make_of[keys].unique(), key=str.lower)

    def models(self, make: str, year: int | None = None) -> list[dict]:
        keys = self.make_of[self.make_of == make].index
        if not len(keys):
            raise InputError(f"unknown make '{make}'")
        keys = keys.intersection(self._available(year)["model_key"].unique())
        out = [{"model_key": k, "model": self.family[k], "training_bikes": int(self.train_bikes.get(k, 0))} for k in keys]
        return sorted(out, key=lambda r: (-r["training_bikes"], r["model"].lower()))

    def variants(self, model_key: str, year: int | None = None) -> list[dict]:
        self._variant_rows(model_key, None)
        v = self._available(year)
        v = v[v["model_key"] == model_key].sort_values(["ex_showroom", "variant"], na_position="last")
        yr = lambda x: None if not np.isfinite(x) else int(x)  # noqa: E731
        return [{"variant_id": r.m2w_variant_id, "variant": r.variant, "year_start": int(r.year_start), "year_end": int(r.year_end),
                 "available_from": yr(r.avail_from), "available_to": yr(r.avail_to),
                 "ex_showroom": None if pd.isna(r.ex_showroom) else float(r.ex_showroom), "status": r.status} for r in v.itertuples()]

    def years(self, model_key: str | None = None, variant_id: str | None = None) -> list[int]:
        """Mfg years (newest first) the form offers: for a model / variant, else for the whole catalogue."""
        if model_key is None:
            return self.mfg_years()
        v = self._variant_rows(model_key, variant_id)
        return [y for y in self.mfg_years() if ((v["avail_from"] <= y) & (v["avail_to"] >= y)).any()]

    def _variant_rows(self, model_key, variant_id):
        v = self.cat[self.cat["model_key"] == model_key]
        if v.empty:
            raise InputError(f"unknown model '{model_key}'")
        if variant_id:
            v = v[v["m2w_variant_id"] == variant_id]
            if v.empty:
                raise InputError(f"variant '{variant_id}' is not a variant of '{model_key}'")
        return v

    # ------------------------------------------------------------------ features
    def resolve_variant(self, model_key, variant_id, mfg_year) -> tuple[pd.Series, str, list[str]]:
        """-> catalogue row, variant_basis, warnings. Unknown variant -> cheapest year-valid variant (training rule)."""
        warn = []
        if variant_id:
            r = self._variant_rows(model_key, variant_id).iloc[0]
            if not (r.year_start - YEAR_TOL <= mfg_year <= r.year_end + YEAR_TOL):
                raise InputError(f"mfg year {mfg_year} is outside the variant's catalogue years {int(r.year_start)}-"
                                 f"{'on sale' if r.year_end >= 9999 else int(r.year_end)} (±{YEAR_TOL})")
            if not (r.avail_from <= mfg_year <= r.avail_to):
                to = "on sale" if not np.isfinite(r.avail_to) else int(r.avail_to)
                warn.append(f"Mfg year {mfg_year} is outside the variant's launch - discontinuation years ({int(r.avail_from)}-{to}).")
            return r, "matched_variant", warn
        g = self._variant_rows(model_key, None)
        gy = g[(g["year_start"] - YEAR_TOL <= mfg_year) & (g["year_end"] + YEAR_TOL >= mfg_year)]
        if gy.empty:
            raise InputError(f"no variant of '{model_key}' is valid for mfg year {mfg_year}")
        r = gy.sort_values("ex_showroom", na_position="last").iloc[0]
        warn.append(f"Variant not given: the model's base variant for {mfg_year} is used ('{r.variant}'), as in training.")
        return r, "model_base_variant", warn

    def category(self, model_key, r) -> str:
        if model_key in self.category_of.index:
            return self.category_of[model_key]
        if str(r.fuel_type).lower() == "electric":
            return "EV"
        return "Scooter" if str(r.body_type).lower().startswith("scooter") else "Bike"

    @staticmethod
    def vehicle_age(mfg_year, quote: dt.date):
        return quote.year + (quote.month - 1) / 12 - (np.asarray(mfg_year, float) + 0.5)

    @staticmethod
    def odometer(km, age) -> tuple[np.ndarray, np.ndarray]:
        """Training plausibility rules as limits: -> (km used, 'cap' code: 0 ok / 1 held at the minimum / 2 held at the maximum)."""
        age = np.asarray(age, float)
        km = np.full(age.shape, np.nan) if km is None else np.full(age.shape, float(km))
        hi = np.minimum(ODO_MAX, ODO_MAX_PER_YEAR * np.clip(age, 0.25, None))
        lo = np.where(age >= 1, ODO_MIN_PER_YEAR * age, 0.0)
        code = np.where(km > hi, 2, np.where(km < lo, 1, 0))
        return np.clip(km, lo, hi), code

    def feature_rows(self, model_key, r, basis, mfg_years, owner_count, odometer_km, city, quote: dt.date, override=None) -> pd.DataFrame:
        """The 24 model features for one bike at several mfg years (same variant row); override = {feature: value} (self-test)."""
        m = self.M.loc[model_key]
        years = np.asarray(mfg_years, float)
        age = self.vehicle_age(years, quote)
        km, _ = self.odometer(odometer_km, age)
        disc = r.is_discontinued
        f = pd.DataFrame({
            "v_ex_showroom": r.ex_showroom, "m_exsh_median": m.m_exsh_median, "m_exsh_min": m.m_exsh_min, "m_exsh_max": m.m_exsh_max,
            "v_engine_cc": r.engine_cc if pd.notna(r.engine_cc) else m.m_cc,
            "v_power_bhp": r.power_bhp if pd.notna(r.power_bhp) else m.m_power_bhp,
            "v_mileage_kmpl": r.mileage_kmpl if pd.notna(r.mileage_kmpl) else m.m_mileage_kmpl,
            "vehicle_age": age, "mfg_year": years, "owner_count": float(owner_count), "odometer_km": km,
            "model_years_on_market": quote.year - m.m_first_year,
            "years_since_discontinued": max(quote.year - r.discontinued_year, 0) if disc else 0,
            "v_year_start": r.year_start, "make": r.make, "model_key": model_key, "m2w_model_id": r.m2w_model_id,
            "category": self.category(model_key, r), "city": city, "v_segment": r.body_segment, "v_status": r.status,
            "m_on_sale": m.m_on_sale, "variant_basis": basis, "popularity_class": self.P.get(model_key, np.nan)}, index=range(len(years)))
        for k, v in (override or {}).items():
            f[k] = v
        return f[self.features]

    def predict_rows(self, f: pd.DataFrame) -> np.ndarray:
        x = f[self.features].copy()
        for c in CATS:
            x[c] = x[c].map(lambda v: "missing" if pd.isna(v) else str(v)).astype(object)
        x = x.astype({c: float for c in self.features if c not in CATS})
        return np.exp(self.model.predict(Pool(x, cat_features=CATS)) + self.y0)

    def half_width(self, age, level) -> np.ndarray:
        L = self.ranges["levels"][str(level)]
        w = L["log_half_width_by_age_band"]
        return np.array([next(w.get(name, L["log_half_width_fallback"]) for name, top in AGE_BANDS if a < top) for a in np.atleast_1d(age)])

    def training_reference(self, model_key, variant_id, basis, mfg_year, city) -> dict:
        """Median pure price of similar training bikes (as in Best_Model_Predictions.xlsx)."""
        p = self.pure
        want = {"mfg_year": float(mfg_year), "model_key": model_key, "variant_id": variant_id if basis == "matched_variant" else None,
                "city": city}
        for keys, min_n, label in TM_LEVELS:
            if "variant_id" in keys and want["variant_id"] is None:
                continue
            s = p
            for k in keys:
                s = s[s[k] == want[k]]
            if len(s) >= min_n:
                return {"median_price": float(s["price"].median()), "bikes": int(len(s)), "matched_on": label}
        return {"median_price": None, "bikes": 0, "matched_on": "no similar training bikes"}

    # ------------------------------------------------------------------ the full prediction
    def price(self, make, model_key, variant_id, mfg_year, owner_count, odometer_km, city, quote: dt.date | None = None) -> dict:
        quote = quote or dt.date.today()
        if self.make_of.get(model_key) != make:
            raise InputError(f"model '{model_key}' is not a model of make '{make}'")
        if mfg_year > quote.year:
            raise InputError(f"mfg year {mfg_year} is after the quote year {quote.year}")
        r, basis, warn = self.resolve_variant(model_key, variant_id, mfg_year)
        # every mfg year from the oldest catalogue year to the quote year, same bike otherwise: the price at the asked year, and
        # the range made monotonic over years (Low / High never move against the depreciation order)
        years = np.arange(min(FIRST_YEAR, mfg_year), quote.year + 1)
        f = self.feature_rows(model_key, r, basis, years, owner_count, odometer_km, city, quote)
        mid = self.predict_rows(f)
        i = int(np.where(years == mfg_year)[0][0])
        age = f["vehicle_age"].values
        ranges = {}
        for lv in self.ranges["levels"]:
            w = self.half_width(age, lv)
            lo, hi = mid * np.exp(-w), mid * np.exp(w)
            lo_env = np.minimum.accumulate(lo[::-1])[::-1]      # Low: never above the Low of a newer year
            hi_env = np.maximum.accumulate(hi)                   # High: never below the High of an older year
            ranges[lv] = {"low": round(float(lo_env[i]), -2), "high": round(float(hi_env[i]), -2), "level_pct": int(lv),
                          "age_band": next(n for n, top in AGE_BANDS if age[i] < top)}
        km_used, code = self.odometer(odometer_km, age[i:i + 1])
        if odometer_km is None:
            warn.append("Odometer not given: priced as 'unknown odometer' (as in training).")
        elif code[0]:
            warn.append(f"Odometer {odometer_km:,.0f} km is {'below' if code[0] == 1 else 'above'} what the training data treats as "
                        f"plausible for a {age[i]:.1f}-year-old bike; priced at {km_used[0]:,.0f} km.")
        n_train = int(self.train_bikes.get(model_key, 0))
        if n_train == 0:
            warn.append("Low confidence: this model has no bikes in the training data; the price leans on the catalogue features.")
        elif n_train < 10:
            warn.append(f"Low confidence: only {n_train} training bikes of this model.")
        pred = round(float(mid[i]), -2)
        feats = f.iloc[i].to_dict()
        return {
            "model": MODEL_NAME, "quote_date": quote.isoformat(),
            "bike": {"make": make, "model": self.family[model_key], "model_key": model_key, "variant": r.variant,
                     "variant_id": r.m2w_variant_id, "variant_basis": basis, "mfg_year": int(mfg_year), "owner_count": int(owner_count),
                     "odometer_km_entered": odometer_km, "city": city},
            "predicted_price": pred,
            "price_band_5k": {"low": pred - BAND_INR, "high": pred + BAND_INR, "band_inr": BAND_INR},
            "model_range": ranges["90"], "model_ranges": ranges,
            "training_reference": self.training_reference(model_key, r.m2w_variant_id, basis, mfg_year, city),
            "training_bikes_for_model": n_train,
            "features": {k: (None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v)) for k, v in feats.items()},
            "monotonic_rules_in_model": self.mono,
            "warnings": warn,
        }


@lru_cache(maxsize=1)
def engine() -> Engine:
    return Engine()
