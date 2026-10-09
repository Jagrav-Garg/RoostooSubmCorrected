from __future__ import annotations

import calendar
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HOUR = 3_600_000
DAY = 24 * HOUR
RAW_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]


def get_bytes(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "roostoo-sentinel/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str) -> object:
    return json.loads(get_bytes(url))


def symbol(pair: str) -> str:
    # Explicitly only our whitelisted standard coins. No automatic xStock or scaled-token mapping.
    return pair.removesuffix("/USD") + "USDT"


def pair_path(directory: str | Path, pair: str) -> Path:
    return Path(directory) / (pair.replace("/", "_") + ".csv")


def normalize_frame(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[BAR_COLUMNS].copy()
    for c in BAR_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="raise")
    ts = df["timestamp"].astype("int64")
    # Binance archive spot timestamps changed to microseconds in January 2025.
    df["timestamp"] = np.where(ts > 10**14, ts // 1000, ts).astype("int64")
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    validate_frame(df)
    return df


def validate_frame(df: pd.DataFrame, contiguous: bool = False) -> None:
    if len(df) == 0:
        raise ValueError("Empty candle data")
    if not set(BAR_COLUMNS).issubset(df.columns):
        raise ValueError(f"Required columns: {BAR_COLUMNS}")
    x = df[BAR_COLUMNS].to_numpy(dtype=float)
    if not np.isfinite(x).all():
        raise ValueError("Non-finite candles")
    if df["timestamp"].duplicated().any() or not df["timestamp"].is_monotonic_increasing:
        raise ValueError("Duplicate or unsorted candle timestamps")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Nonpositive candle prices")
    if ((df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC ordering")
    if (df[["volume", "quote_volume"]] < 0).any().any():
        raise ValueError("Negative candle volume")
    if (df["timestamp"] % HOUR != 0).any():
        raise ValueError("Candles must be aligned to UTC hour boundaries")
    if contiguous and len(df) > 1 and not (df["timestamp"].diff().iloc[1:] == HOUR).all():
        raise ValueError("Candle gap; do not invent bars or forward-fill prices")


def read_bars(directory: str | Path, pair: str) -> pd.DataFrame:
    return normalize_frame(pd.read_csv(pair_path(directory, pair)))


def save_bars(directory: str | Path, pair: str, df: pd.DataFrame) -> None:
    p = pair_path(directory, pair)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(p)


def month_sequence(start: str, end: str) -> list[str]:
    a = pd.Timestamp(start).to_period("M")
    b = pd.Timestamp(end).to_period("M")
    return [str(p) for p in pd.period_range(a, b, freq="M")]


def download_archive(pair: str, month: str, cache: Path) -> tuple[pd.DataFrame, dict]:
    name = f"{symbol(pair)}-1h-{month}.zip"
    url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol(pair)}/1h/{name}"
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / name
    b = p.read_bytes() if p.exists() else get_bytes(url)
    # Verify published integrity checksum, including when using cached bytes.
    checksum_path = cache / (name + ".CHECKSUM")
    check = checksum_path.read_bytes() if checksum_path.exists() else get_bytes(url + ".CHECKSUM")
    expected = check.decode().split()[0]
    actual = hashlib.sha256(b).hexdigest()
    if actual != expected:
        raise ValueError(f"Archive checksum mismatch: {name}")
    p.write_bytes(b)
    checksum_path.write_bytes(check)
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = pd.read_csv(io.BytesIO(z.read(name.removesuffix(".zip") + ".csv")), header=None, names=RAW_COLUMNS)
    return normalize_frame(raw), {"pair": pair, "month": month, "url": url, "sha256": actual, "rows": len(raw)}


def download_history(pairs: list[str], start: str, end: str, directory: str | Path, workers: int = 4) -> dict:
    dest = Path(directory)
    cache = dest / "archives"
    tasks = [(p, m) for p in pairs for m in month_sequence(start, end)]
    outcomes: dict[str, list[pd.DataFrame]] = {p: [] for p in pairs}
    manifest = []
    errors = []
    def fetch(task):
        p, m = task
        try:
            df, meta = download_archive(p, m, cache)
            return p, df, meta, None
        except Exception as e:
            return p, None, None, {"pair": p, "month": m, "error": str(e)[:180]}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (p, df, meta, err) in enumerate(pool.map(fetch, tasks), 1):
            if err:
                errors.append(err)
            else:
                outcomes[p].append(df)
                manifest.append(meta)
            if i % 20 == 0 or i == len(tasks):
                print(f"Historical archives: {i}/{len(tasks)}, failures={len(errors)}", flush=True)
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    for p, frames in outcomes.items():
        if frames:
            df = normalize_frame(pd.concat(frames, ignore_index=True))
            df = df[(df.timestamp >= start_ms) & (df.timestamp < end_ms)].reset_index(drop=True)
            save_bars(dest, p, df)
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "start": start, "end": end, "archives": manifest, "errors": errors}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "data_manifest.json").write_text(json.dumps(result, indent=2))
    if errors:
        raise RuntimeError(f"{len(errors)} archives unavailable; see data_manifest.json. Partial data is not accepted as a complete run.")
    return result


class LiveData:
    def __init__(self, base_url: str, directory: str | Path, spacing: float = 2.):
        self.base_url = base_url.rstrip("/")
        self.directory = Path(directory)
        self.spacing = spacing
        self.next_request = 0.

    def request(self, endpoint: str, params: dict | None = None):
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + self.spacing
        q = urllib.parse.urlencode(params or {})
        return get_json(self.base_url + endpoint + ("?" + q if q else ""))

    def update(self, pair: str, now_ms: int, days: int = 190) -> pd.DataFrame:
        path = pair_path(self.directory, pair)
        old = read_bars(self.directory, pair) if path.exists() else pd.DataFrame(columns=BAR_COLUMNS)
        start = int(old.timestamp.iloc[-1]) if len(old) else (now_ms // HOUR - days * 24) * HOUR
        final_open = (now_ms // HOUR - 1) * HOUR
        frames = [old] if len(old) else []
        while start <= final_open:
            rows = self.request("/api/v3/klines", {"symbol": symbol(pair), "interval": "1h", "startTime": start, "endTime": final_open + HOUR - 1, "limit": 1000})
            if not isinstance(rows, list) or not rows:
                raise RuntimeError(f"No usable live candles for {pair}; refusing stale history")
            chunk = normalize_frame(pd.DataFrame(rows, columns=RAW_COLUMNS))
            chunk = chunk[chunk.timestamp <= final_open]
            if chunk.empty:
                break
            frames.append(chunk)
            nxt = int(chunk.timestamp.iloc[-1]) + HOUR
            if nxt <= start:
                raise RuntimeError("Live data pagination did not advance")
            start = nxt
        if not frames:
            raise RuntimeError(f"No complete history for {pair}")
        saved = normalize_frame(pd.concat(frames, ignore_index=True))
        saved = saved[saved.timestamp <= final_open].reset_index(drop=True)
        df = saved[saved.timestamp >= final_open - (days * 24 + 500) * HOUR].reset_index(drop=True)
        if int(df.timestamp.iloc[-1]) != final_open:
            raise RuntimeError(f"Latest completed candle missing for {pair}")
        # A gap in the retained window is not silently repaired.
        validate_frame(df, contiguous=True)
        save_bars(self.directory, pair, saved)
        return df

    def reference_price(self, pair: str) -> float:
        d = self.request("/api/v3/ticker/bookTicker", {"symbol": symbol(pair)})
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        if not 0 < bid <= ask:
            raise RuntimeError("Invalid external reference quote")
        return (bid + ask) / 2
