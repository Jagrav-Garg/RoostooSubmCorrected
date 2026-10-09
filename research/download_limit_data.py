"""Checksum-verified minute candles for price-grid execution research."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import io
import json
import urllib.request
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ["BONK", "PEPE", "SHIB", "1000CHEEMS", "WLFI", "STO", "HEMI", "LISTA"]
COLS = ["timestamp", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
KEEP = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_base"]


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sentinel-research/0.2"}), timeout=30).read()


def fetch(task):
    coin, period, kind = task
    name = f"{coin}USDT-1m-{period}.zip"
    url = f"https://data.binance.vision/data/spot/{kind}/klines/{coin}USDT/1m/{name}"
    cache = ROOT / "data/limit_archives"
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / name
    b = p.read_bytes() if p.exists() else get(url)
    cp = cache / (name + ".CHECKSUM")
    checksum = cp.read_bytes() if cp.exists() else get(url + ".CHECKSUM")
    digest = hashlib.sha256(b).hexdigest()
    if checksum.decode().split()[0] != digest:
        raise ValueError("Checksum mismatch: " + name)
    p.write_bytes(b)
    cp.write_bytes(checksum)
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        frame = pd.read_csv(io.BytesIO(z.read(name[:-4] + ".csv")), names=COLS, header=None)[KEEP]
    ts = frame.timestamp.to_numpy(dtype=np.int64)
    frame["timestamp"] = np.where(ts > 10**14, ts // 1000, ts)
    return coin, frame, {"coin": coin, "period": period, "url": url, "sha256": digest, "rows": len(frame)}


def main():
    tasks = [(c, str(m), "monthly") for c in PAIRS for m in pd.period_range("2026-01", "2026-09", freq="M")]
    tasks += [(c, d.strftime("%Y-%m-%d"), "daily") for c in PAIRS for d in pd.date_range("2026-10-01", "2026-10-08")]
    groups = {c: [] for c in PAIRS}
    manifest, errors = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(fetch, t): t for t in tasks}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                c, df, entry = f.result()
                groups[c].append(df)
                manifest.append(entry)
            except Exception as e:
                errors.append({"task": futs[f], "error": type(e).__name__ + ": " + str(e)[:100]})
            if n % 10 == 0:
                print(json.dumps({"completed": n, "total": len(tasks), "errors": len(errors)}), flush=True)
    dest = ROOT / "data/limit_minutes"
    dest.mkdir(parents=True, exist_ok=True)
    for c, parts in groups.items():
        if not parts:
            continue
        df = pd.concat(parts).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        arr = df[KEEP].to_numpy(dtype=float)
        if not np.isfinite(arr).all() or (df[["open", "high", "low", "close"]] <= 0).any().any():
            raise ValueError("Malformed data: " + c)
        if ((df.high < df[["open", "close", "low"]].max(axis=1)) | (df.low > df[["open", "close", "high"]].min(axis=1))).any():
            raise ValueError("Malformed OHLC: " + c)
        if not (df.timestamp % 60000 == 0).all():
            raise ValueError("Minute alignment: " + c)
        gaps = int((df.timestamp.diff().iloc[1:] != 60000).sum())
        df.to_csv(dest / f"{c}_USD.csv.gz", index=False, compression="gzip")
        print(json.dumps({"coin": c, "rows": len(df), "gaps": gaps, "start": str(pd.to_datetime(df.timestamp.iloc[0], unit="ms", utc=True)), "end": str(pd.to_datetime(df.timestamp.iloc[-1], unit="ms", utc=True))}), flush=True)
    (dest / "manifest.json").write_text(json.dumps({"archives": manifest, "errors": errors}, indent=2))


if __name__ == "__main__":
    main()
