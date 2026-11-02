"""Self-test of the API pipeline (pipeline.py). Run: python selftest.py  (from the parent: python -m app.selftest); exit code 1
if a check fails.

Uses only the bundled files (app/model, app/data), so it also checks a copied / deployed app.
1. features   the 24 features rebuilt by the API from (variant, model, mfg year, owners, odometer, city, pickup date) equal the
              training features of every training bike (data/training_data.csv; category / m2w_model_id passed as stored: in
              training they come from the sheet / mapping, the API derives them; their agreement rate is reported)
2. new bikes  the API reproduces the model's evaluated prices for the 196 priced bikes of Evaluation Sheet (1).xlsx
              (data/selftest_eval.csv)
3. rules      through the full API call: more km, more owners or an older mfg year never gives a higher price, Low or High;
              checked on bikes of the 40 most common models + 40 random catalogue models
4. dropdowns  every (mfg year, make, model, variant) the web form offers - mfg year first, then only what was on sale that year
              (launch .. discontinuation) - is accepted by the price pipeline, with and without the variant
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd

from pipeline import CATS, Engine, InputError, engine
from settings import data_file

TOL_INR = 0.5


def variant_row(E: Engine, vid):
    r = E.cat.loc[vid]
    return r.iloc[0] if isinstance(r, pd.DataFrame) else r


def same(a: pd.Series, b: pd.Series, cat: bool) -> np.ndarray:
    if cat:
        return (a.map(lambda v: "missing" if pd.isna(v) else str(v)).values == b.map(lambda v: "missing" if pd.isna(v) else str(v)).values)
    u, v = pd.to_numeric(a, errors="coerce").to_numpy(float), pd.to_numeric(b, errors="coerce").to_numpy(float)
    return np.isclose(u, v) | (np.isnan(u) & np.isnan(v))


def check_features(E: Engine) -> bool:
    p = E.pure.copy()
    p["pickup_date"] = pd.to_datetime(p["pickup_date"])
    rows = []
    for x in p.itertuples(index=False):
        r = variant_row(E, x.variant_id)
        odo = None if pd.isna(x.odometer_km) else x.odometer_km
        rows.append(E.feature_rows(x.model_key, r, x.variant_basis, [x.mfg_year], x.owner_count, odo, x.city, x.pickup_date.date(),
                                   override={"category": x.category, "m2w_model_id": x.m2w_model_id}))
    f = pd.concat(rows, ignore_index=True)
    ok = True
    bad = {c: int((~same(f[c], p[c], c in CATS)).sum()) for c in E.features}
    for c, n in bad.items():
        if n:
            ok = False
            print(f"  FAIL feature {c}: {n} of {len(p)} bikes differ")
    d = np.abs(E.predict_rows(f) - E.predict_rows(p))
    print(f"[features] {len(p):,} training bikes: all 24 features identical: {ok}; price difference max ₹{d.max():.2f}")
    # what the API derives on its own for the two passed-through features
    cat_api = p.apply(lambda x: E.category(x.model_key, variant_row(E, x.variant_id)), axis=1)
    m2w_api = p["variant_id"].map(lambda v: variant_row(E, v).m2w_model_id)
    print(f"           API-derived category = training category for {100 * (cat_api == p['category']).mean():.1f}% of bikes; "
          f"m2w_model_id for {100 * (m2w_api == p['m2w_model_id']).mean():.1f}%")
    return ok and d.max() < TOL_INR


def check_new_bikes(E: Engine) -> bool:
    ep = pd.read_csv(data_file("selftest_eval"))
    rows = []
    for x in ep.itertuples(index=False):
        r = variant_row(E, x.variant_id)
        odo = None if pd.isna(x.odometer_km) else x.odometer_km
        rows.append(E.feature_rows(x.model_key, r, x.variant_basis, [x.mfg_year], x.owner_count, odo, x.city,
                                   pd.Timestamp(x.pickup_date).date(), override={"category": x.category, "m2w_model_id": x.m2w_model_id}))
    f = pd.concat(rows, ignore_index=True)
    d = np.abs(E.predict_rows(f) - ep["expected_price"].values)
    print(f"[new bikes] {len(ep)} bikes of Evaluation Sheet (1): API price = evaluated price, max difference ₹{d.max():.2f}")
    return d.max() < TOL_INR


def check_rules(E: Engine, quote=dt.date(2026, 11, 2)) -> bool:
    rng = np.random.default_rng(7)
    keys = list(E.train_bikes.index[:40]) + list(rng.choice(E.cat["model_key"].unique(), 40, replace=False))
    viol, checks = [], 0
    for mk in keys:
        vs = E.variants(mk)
        v = vs[int(rng.integers(len(vs)))]
        years = [y for y in E.years(mk, v["variant_id"]) if y <= quote.year]
        if not years:
            continue
        make, city = E.make_of[mk], str(rng.choice(E.cities))
        y0 = years[len(years) // 2]
        kms = [None, 0, 1_000, 5_000, 10_000, 20_000, 30_000, 50_000, 80_000, 120_000, 150_000, 200_000, 400_000]
        series = {
            "odometer": [E.price(make, mk, v["variant_id"], y0, 1, km, city, quote) for km in kms[1:]],
            "owners": [E.price(make, mk, v["variant_id"], y0, o, 20_000, city, quote) for o in (1, 2, 3, 4)],
            "mfg year (older first)": [E.price(make, mk, v["variant_id"], y, 1, 20_000, city, quote) for y in sorted(years)],
            "mfg year, no odometer": [E.price(make, mk, v["variant_id"], y, 1, None, city, quote) for y in sorted(years)],
        }
        for name, res in series.items():
            sign = 1 if name.startswith("mfg") else -1          # price should rise with year, fall with km / owners
            for key, get in (("price", lambda r: r["predicted_price"]), ("Low", lambda r: r["model_range"]["low"]),
                             ("High", lambda r: r["model_range"]["high"]), ("±5k low", lambda r: r["price_band_5k"]["low"])):
                vals = np.array([get(r) for r in res])
                checks += max(len(vals) - 1, 0)
                if (np.diff(vals) * sign < -TOL_INR).any():
                    viol.append(f"{mk} / {v['variant']}: {name} -> {key} {vals.tolist()}")
    print(f"[rules] {len(keys)} bike models, {checks:,} neighbouring steps (km 0-400k, owners 1-4, every catalogue mfg year): "
          f"{len(viol)} steps against the depreciation order")
    for v in viol[:10]:
        print("  FAIL", v)
    return not viol


def check_dropdowns(E: Engine) -> bool:
    bad, n = [], 0
    for y in E.years():
        for make in E.makes(y):
            for m in E.models(make, y):
                vs = E.variants(m["model_key"], y)
                if not vs:
                    bad.append(f"{y} {m['model_key']}: model listed without variants")
                for vid in [None] + [v["variant_id"] for v in vs]:
                    n += 1
                    try:
                        E.resolve_variant(m["model_key"], vid, y)
                    except InputError as e:
                        bad.append(f"{y} {m['model_key']} {vid}: {e}")
    print(f"[dropdowns] {len(E.years())} mfg years, {n:,} offered (year, model, variant / base variant) choices: "
          f"{len(bad)} not accepted by the price pipeline")
    for b in bad[:10]:
        print("  FAIL", b)
    return not bad


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    E = engine()
    results = [check_features(E), check_new_bikes(E), check_rules(E), check_dropdowns(E)]
    print("SELFTEST", "PASSED" if all(results) else "FAILED")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
