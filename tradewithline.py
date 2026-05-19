import argparse
import datetime as dt
import time
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect

from telegram_notifier import send_telegram


# =========================================================
# INDEX CONFIG
# =========================================================

INDEX_MAP = {
    "NIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "Nifty 50",
        "symboltoken": "99926000",
        "strike_step": 50,
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "Nifty Bank",
        "symboltoken": "99926009",
        "strike_step": 100,
    },
    "FINNIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "Nifty Fin Service",
        "symboltoken": "99926037",
        "strike_step": 50,
    },
    "MIDCPNIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "NIFTY MID SELECT",
        "symboltoken": "99926074",
        "strike_step": 25,
    },
}


# =========================================================
# CONFIG
# =========================================================

@dataclass
class Config:
    api_key: str
    client_code: str
    mpin: str
    totp_secret: str
    underlying: str
    interval: str
    option_strikes_each_side: int
    polling_seconds: int
    expiry: str | None


# =========================================================
# LAST SIGNAL CACHE
# =========================================================

LAST_SIGNALS = {}


# =========================================================
# LOGIN
# =========================================================

def login(cfg: Config):

    client = SmartConnect(api_key=cfg.api_key)

    totp = pyotp.TOTP(cfg.totp_secret).now()

    session = client.generateSession(
        cfg.client_code,
        cfg.mpin,
        totp,
    )

    if not session["status"]:
        raise RuntimeError(f"Login failed: {session}")

    return client


# =========================================================
# LOAD MASTER
# =========================================================

def load_master():

    url = (
        "https://margincalculator.angelbroking.com/"
        "OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    data = requests.get(url, timeout=30).json()

    return data


# =========================================================
# FETCH SPOT
# =========================================================

def fetch_spot(client, underlying):

    idx = INDEX_MAP[underlying]

    ltp = client.ltpData(
        idx["exchange"],
        idx["tradingsymbol"],
        idx["symboltoken"],
    )

    if not ltp["status"]:
        raise RuntimeError(f"LTP fetch failed: {ltp}")

    return float(ltp["data"]["ltp"])


# =========================================================
# EXPIRY
# =========================================================

def get_nearest_expiry(master, underlying):

    expiries = set()

    for row in master:

        if row.get("exch_seg") != "NFO":
            continue

        if row.get("instrumenttype") != "OPTIDX":
            continue

        if row.get("name") != underlying:
            continue

        expiry = row.get("expiry")

        if expiry:
            expiries.add(expiry)

    if not expiries:
        raise RuntimeError(f"No expiry found for {underlying}")

    expiry_dates = sorted(
        expiries,
        key=lambda x: datetime.strptime(x, "%d%b%Y")
    )

    print("\nAvailable Expiries:")
    print(expiry_dates)

    return expiry_dates[0]


# =========================================================
# ATM STRIKE
# =========================================================

def get_atm_strike(spot, step):

    return int(round(spot / step) * step)


# =========================================================
# OPTION CONTRACTS
# =========================================================

def get_option_contracts(
    master,
    underlying,
    expiry,
    atm,
    step,
    each_side=2,
):

    strikes = []

    for i in range(-each_side, each_side + 1):
        strikes.append(atm + i * step)

    contracts = []

    for row in master:

        if row.get("exch_seg") != "NFO":
            continue

        if row.get("instrumenttype") != "OPTIDX":
            continue

        if row.get("name") != underlying:
            continue

        if row.get("expiry") != expiry:
            continue

        strike = float(row.get("strike", 0)) / 100

        if strike not in strikes:
            continue

        contracts.append({
            "symbol": row["symbol"],
            "token": row["token"],
            "strike": strike,
            "option_type": row["symbol"][-2:],
        })

    return contracts


# =========================================================
# FETCH CANDLES
# =========================================================

def fetch_candles(client, token, interval):

    to_date = dt.datetime.now()
    from_date = to_date - dt.timedelta(days=5)

    params = {
        "exchange": "NFO",
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }

    res = client.getCandleData(params)

    if not res["status"]:
        return None

    data = res["data"]

    if not data:
        return None

    df = pd.DataFrame(
        data,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col])

    return df


# =========================================================
# ATR
# =========================================================

def atr(df, period=11):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return tr.rolling(period).mean()


# =========================================================
# SUPERTREND
# =========================================================

def supertrend(df, factor=2, atr_len=11):

    atr_val = atr(df, atr_len)

    hl2 = (df["high"] + df["low"]) / 2

    upperband = hl2 + factor * atr_val
    lowerband = hl2 - factor * atr_val

    st = [0] * len(df)
    direction = [1] * len(df)

    for i in range(1, len(df)):

        if df["close"].iloc[i] > upperband.iloc[i - 1]:
            direction[i] = -1

        elif df["close"].iloc[i] < lowerband.iloc[i - 1]:
            direction[i] = 1

        else:
            direction[i] = direction[i - 1]

        st[i] = (
            lowerband.iloc[i]
            if direction[i] == -1
            else upperband.iloc[i]
        )

    df["supertrend"] = st
    df["direction"] = direction

    return df


# =========================================================
# SUPPORT / RESISTANCE
# =========================================================

def calculate_sr(df, lookback=20):

    resistance = df["high"].rolling(lookback).max()

    support = df["low"].rolling(lookback).min()

    df["support"] = support
    df["resistance"] = resistance

    return df


# =========================================================
# SIGNALS
# =========================================================

def generate_signal(df):

    df = supertrend(df)

    df = calculate_sr(df)

    close = df["close"]
    st = df["supertrend"]

    bull = (
        (close > st)
        & (close.shift(1) <= st.shift(1))
    )

    bear = (
        (close < st)
        & (close.shift(1) >= st.shift(1))
    )

    latest_bull = bull.iloc[-1]
    latest_bear = bear.iloc[-1]

    signal = "HOLD"

    if latest_bull:
        signal = "BUY"

    elif latest_bear:
        signal = "SELL"

    support = round(float(df["support"].iloc[-1]), 2)

    resistance = round(float(df["resistance"].iloc[-1]), 2)

    current_price = round(float(close.iloc[-1]), 2)

    return {
        "signal": signal,
        "support": support,
        "resistance": resistance,
        "price": current_price,
    }


# =========================================================
# MAIN LOOP
# =========================================================

def run_once(cfg: Config):

    print("\nConnecting to Angel One...")

    client = login(cfg)

    print("Login successful")

    master = load_master()

    print("Master contract loaded")

    spot = fetch_spot(client, cfg.underlying)

    step = INDEX_MAP[cfg.underlying]["strike_step"]

    atm = get_atm_strike(spot, step)

    if cfg.expiry:
        expiry = cfg.expiry
    else:
        expiry = get_nearest_expiry(
            master,
            cfg.underlying,
        )

    contracts = get_option_contracts(
        master,
        cfg.underlying,
        expiry,
        atm,
        step,
        cfg.option_strikes_each_side,
    )

    print("\n" + "=" * 90)

    print(f"UNDERLYING : {cfg.underlying}")
    print(f"SPOT       : {spot}")
    print(f"ATM STRIKE : {atm}")
    print(f"EXPIRY     : {expiry}")

    print("=" * 90)

    for c in sorted(
        contracts,
        key=lambda x: (x["strike"], x["option_type"])
    ):

        df = fetch_candles(
            client,
            c["token"],
            cfg.interval,
        )

        time.sleep(3)

        if df is None:
            continue

        result = generate_signal(df)

        print(
            f'{c["symbol"]:<35} '
            f'{c["option_type"]:<3} '
            f'STRIKE={c["strike"]:<8} '
            f'PRICE={result["price"]:<8} '
            f'SIGNAL={result["signal"]:<5} '
            f'SUP={result["support"]:<8} '
            f'RES={result["resistance"]:<8}'
        )

        # =====================================================
        # TELEGRAM ALERTS
        # =====================================================

        signal_key = c["symbol"]

        current_signal = result["signal"]

        previous_signal = LAST_SIGNALS.get(signal_key)

        if current_signal in ("BUY", "SELL"):

            if previous_signal != current_signal:

                msg = (
                    f'🚨 OPTION SIGNAL 🚨\n\n'
                    f'UNDERLYING: {cfg.underlying}\n'
                    f'SYMBOL: {c["symbol"]}\n'
                    f'TYPE: {c["option_type"]}\n'
                    f'STRIKE: {c["strike"]}\n'
                    f'PRICE: {result["price"]}\n'
                    f'SIGNAL: {current_signal}\n'
                    f'SUPPORT: {result["support"]}\n'
                    f'RESISTANCE: {result["resistance"]}'
                )

                send_telegram(msg)

                print(f"Telegram Alert Sent -> {signal_key}")

                LAST_SIGNALS[signal_key] = current_signal


# =========================================================
# MAIN
# =========================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--api-key", required=True)

    parser.add_argument("--client-code", required=True)

    parser.add_argument("--mpin", required=True)

    parser.add_argument("--totp-secret", required=True)

    parser.add_argument(
        "--underlying",
        default="NIFTY",
        choices=[
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
        ],
    )

    parser.add_argument(
        "--interval",
        default="FIVE_MINUTE",
    )

    parser.add_argument(
        "--expiry",
        default=None,
    )

    parser.add_argument(
        "--option-strikes-each-side",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--polling-seconds",
        type=int,
        default=300,
    )

    args = parser.parse_args()

    cfg = Config(
        api_key=args.api_key,
        client_code=args.client_code,
        mpin=args.mpin,
        totp_secret=args.totp_secret,
        underlying=args.underlying,
        interval=args.interval,
        option_strikes_each_side=args.option_strikes_each_side,
        polling_seconds=args.polling_seconds,
        expiry=args.expiry,
    )

    while True:

        try:
            run_once(cfg)

        except Exception as e:
            print(f"\nERROR: {e}")

        print(f"\nSleeping {cfg.polling_seconds} seconds...\n")

        time.sleep(cfg.polling_seconds)


if __name__ == "__main__":
    main()
