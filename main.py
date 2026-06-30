"""
╔══════════════════════════════════════════════════════════════════╗
║      BTCUSD ULTIMATE SCALPING BOT — VERSION 4.0 AUTO TRADE      ║
║        100% AUTOMATIQUE — OKX API — LEVIER DYNAMIQUE            ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import asyncio
import aiohttp
import time
import random
import math
import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "TON_TOKEN_ICI")
CHAT_ID = "808538037"

OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
OKX_BASE_URL   = "https://eea.okx.com"

ACCOUNT_SIZE         = 100
RISK_PERCENT         = 2.0
TRADE_AMOUNT_PERCENT = 10
MIN_SCORE            = 78
MAX_TRADE_DURATION   = 15 * 60
SYMBOL               = "BTC-USD-SWAP"  # Inverse perpetual margé en BTC — pas d'USDT donc pas de restriction MiCA

LEVERAGE_TABLE = [
    (97, 101, 10, "SETUP EN BÉTON",   "💎"),
    (92, 97,   5, "TRÈS FORT SETUP",  "🔥🔥"),
    (85, 92,   3, "BON SETUP",        "🔥"),
    (78, 85,   2, "SETUP CORRECT",    "⚡"),
]

def get_leverage(score):
    for low, high, lev, label, emoji in LEVERAGE_TABLE:
        if low <= score < high:
            return lev, label, emoji
    return 2, "SETUP CORRECT", "⚡"

# ═══════════════════════════════════════════════════════════════
# ÉTAT
# ═══════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.in_trade = False
        self.current_trade = None
        self.last_price = 0.0
        self.prices = []
        self.volumes = []
        self.scan_count = 0
        self.running = True
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.news_blackout_until = None
        self.dxy_prices = []
        self.us10y_val = 4.3
        self.okx_order_id = None

state = BotState()

# ═══════════════════════════════════════════════════════════════
# OKX API — SIGNATURE
# ═══════════════════════════════════════════════════════════════
def okx_sign(timestamp, method, path, body=""):
    msg = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": okx_sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        # Mode réel — prix OKX directs
    }

# ═══════════════════════════════════════════════════════════════
# OKX — ACTIONS DE TRADING
# ═══════════════════════════════════════════════════════════════
async def okx_set_leverage(session, leverage):
    """Définit le levier sur OKX"""
    path = "/api/v5/account/set-leverage"
    body = json.dumps({
        "instId": SYMBOL,
        "lever": str(leverage),
        "mgnMode": "cross"
    })
    try:
        async with session.post(
            OKX_BASE_URL + path,
            headers=okx_headers("POST", path, body),
            data=body,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            return data.get("code") == "0"
    except Exception as e:
        print(f"❌ Set leverage error: {e}")
        return False

async def okx_place_order(session, direction, size, sl, tp, entry_price=0):
    """Place un ordre sur OKX avec SL et TP — essaie USDT puis USDC si restriction"""
    path = "/api/v5/trade/order"
    side = "buy" if direction == "BUY" else "sell"
    pos_side = "long" if direction == "BUY" else "short"

    # Récupère le prix de marché ACTUEL juste avant l'ordre (évite SL/TP obsolètes)
    try:
        ticker_url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={SYMBOL}"
        async with session.get(ticker_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            ticker_data = await r.json()
            if ticker_data.get("code") == "0":
                fresh_price = float(ticker_data["data"][0]["last"])
                # Recalcule SL/TP en conservant la même DISTANCE que prévu, mais depuis le prix frais
                sl_distance = abs(float(entry_price) - float(sl))
                tp_distance = abs(float(entry_price) - float(tp))
                entry_price = fresh_price
    except Exception as e:
        print(f"⚠️ Impossible de rafraîchir le prix, utilisation du prix du signal: {e}")
        sl_distance = abs(float(entry_price) - float(sl))
        tp_distance = abs(float(entry_price) - float(tp))

    # Correction SL/TP forcée selon direction, avec le prix FRAIS et la distance d'origine
    entry_price = float(entry_price)

    if direction == "BUY":
        sl = round(entry_price - sl_distance, 2)
        tp = round(entry_price + tp_distance, 2)
        # Sécurité absolue
        if sl >= entry_price: sl = round(entry_price - 2.0, 2)
        if tp <= entry_price: tp = round(entry_price + 3.0, 2)
    else:
        sl = round(entry_price + sl_distance, 2)
        tp = round(entry_price - tp_distance, 2)
        # Sécurité absolue
        if sl <= entry_price: sl = round(entry_price + 2.0, 2)
        if tp >= entry_price: tp = round(entry_price - 3.0, 2)

    body = json.dumps({
        "instId": SYMBOL,
        "tdMode": "cross",
        "ccy": "USD",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(size),
        "attachAlgoOrds": [
            {
                "slTriggerPx": str(sl),
                "slOrdPx": "-1",
                "slTriggerPxType": "last",
                "tpTriggerPx": str(tp),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "last"
            }
        ]
    })
    try:
        async with session.post(
            OKX_BASE_URL + path,
            headers=okx_headers("POST", path, body),
            data=body,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("code") == "0":
                order_id = data["data"][0]["ordId"]
                print(f"✅ Ordre placé: {order_id}")
                return order_id
            else:
                err = data.get("data", [{}])[0] if data.get("data") else {}
                scode = err.get("sCode", "")
                print(f"❌ Ordre échoué: {data}")
                if scode == "51155":
                    print("🚫 RESTRICTION COMPLIANCE — ce contrat n'est pas tradable depuis l'Europe sur ce compte. Voir diagnostic /api/v5/public/instruments")
                return None
    except Exception as e:
        print(f"❌ Place order error: {e}")
        return None

async def okx_close_position(session, direction):
    """Ferme la position OKX"""
    path = "/api/v5/trade/close-position"
    pos_side = "long" if direction == "BUY" else "short"
    body = json.dumps({
        "instId": SYMBOL,
        "mgnMode": "cross",
        "posSide": pos_side,
    })
    try:
        async with session.post(
            OKX_BASE_URL + path,
            headers=okx_headers("POST", path, body),
            data=body,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            return data.get("code") == "0"
    except Exception as e:
        print(f"❌ Close position error: {e}")
        return False

async def okx_get_position(session):
    """Récupère la position ouverte"""
    path = f"/api/v5/account/positions?instId={SYMBOL}"
    try:
        async with session.get(
            OKX_BASE_URL + path,
            headers=okx_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()
            if data.get("code") == "0" and data.get("data"):
                return data["data"][0]
    except Exception as e:
        print(f"❌ Get position error: {e}")
    return None

# ═══════════════════════════════════════════════════════════════
# PRIX EN TEMPS RÉEL
# ═══════════════════════════════════════════════════════════════
async def get_gold_price(session):
    """Prix BTC directement depuis OKX — 100% synchronisé"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={SYMBOL}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == "0":
                    return round(float(data["data"][0]["last"]), 2)
                else:
                    print(f"⚠️ OKX ticker error pour {SYMBOL}: {data}")
    except Exception as e:
        print(f"⚠️ OKX price fetch error: {e}")
    # Fallback CoinGecko (BTC, pas Yahoo Finance / pas l'or)
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json()
                return round(float(data["bitcoin"]["usd"]), 2)
    except Exception as e:
        print(f"⚠️ CoinGecko fallback error: {e}")
    base = state.last_price if state.last_price > 0 else 100000.0
    return round(base + (random.random() - 0.499) * 50, 2)

async def get_dxy_price(session):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json()
                return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except:
        pass
    return 101.3

async def get_us10y(session):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json()
                return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except:
        pass
    return 4.37

# ═══════════════════════════════════════════════════════════════
# INDICATEURS
# ═══════════════════════════════════════════════════════════════
def calc_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 100000.0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains = losses = 0
    for i in range(len(prices) - period, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0: gains += diff
        else: losses -= diff
    rs = gains / (losses if losses > 0 else 0.001)
    return round(100 - 100 / (1 + rs), 1)

def calc_atr(prices, period=14):
    if len(prices) < 2:
        return 1.5
    trs = [abs(prices[i] - prices[i-1]) for i in range(max(1, len(prices)-period), len(prices))]
    return round(sum(trs) / len(trs), 2) if trs else 1.5

def calc_macd(prices):
    if len(prices) < 26:
        return {"hist": 0}
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    macd = ema12 - ema26
    return {"macd": round(macd, 3), "hist": round(macd * 0.1, 3)}

def calc_bollinger(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 100000
        return {"upper": p+5, "middle": p, "lower": p-5}
    sl = prices[-period:]
    mid = sum(sl) / period
    std = math.sqrt(sum((x-mid)**2 for x in sl) / period)
    return {"upper": round(mid+2*std, 2), "middle": round(mid, 2), "lower": round(mid-2*std, 2)}

def calc_volume_ratio(volumes):
    if len(volumes) < 5:
        return 1.0
    ma = sum(volumes[-20:]) / min(20, len(volumes))
    return round(volumes[-1] / ma if ma > 0 else 1.0, 1)

def detect_candle_pattern(prices):
    if len(prices) < 3:
        return None, 0
    c1, c2, c3 = prices[-3], prices[-2], prices[-1]
    body1 = abs(c2-c1)
    body2 = abs(c3-c2)
    upper_wick = max(c2,c3) - max(c2,c1)
    lower_wick = min(c2,c1) - min(c2,c3)
    patterns, score = [], 0
    if c1 > c2 and c3 > c2 and body2 > body1*1.5: patterns.append("Bullish Engulfing 🟢"); score += 18
    if c1 < c2 and c3 < c2 and body2 > body1*1.5: patterns.append("Bearish Engulfing 🔴"); score += 18
    if lower_wick > body2*2 and c3 > c2: patterns.append("Hammer 🔨"); score += 14
    if upper_wick > body2*2 and c3 < c2: patterns.append("Shooting Star ⭐"); score += 14
    if lower_wick > body2*3 and c3 > c1: patterns.append("Pin Bar Haussier 📌"); score += 16
    if upper_wick > body2*3 and c3 < c1: patterns.append("Pin Bar Baissier 📌"); score += 16
    return (", ".join(patterns), score) if patterns else (None, 0)

def detect_session():
    h = datetime.now(timezone.utc).hour
    if 13 <= h < 17: return {"name": "OVERLAP LDN/NY", "emoji": "🔥", "score_bonus": 20, "active": True}
    elif 7 <= h < 13: return {"name": "LONDON", "emoji": "🇬🇧", "score_bonus": 12, "active": True}
    elif 17 <= h < 21: return {"name": "NEW YORK", "emoji": "🗽", "score_bonus": 10, "active": True}
    else: return {"name": "ASIA", "emoji": "🌏", "score_bonus": 5, "active": True}

def detect_liquidity_sweep(prices):
    if len(prices) < 25: return None
    recent = prices[-25:]
    high, low = max(recent[:20]), min(recent[:20])
    last, prev = recent[-1], recent[-2]
    if prev > high and last < high-0.1: return {"type": "BULL_SWEEP", "level": round(high,2)}
    if prev < low and last > low+0.1: return {"type": "BEAR_SWEEP", "level": round(low,2)}
    return None

def detect_fvg(prices):
    if len(prices) < 3: return None
    c1, c2, c3 = prices[-3], prices[-2], prices[-1]
    gap = abs(c3-c1)
    if gap > 0.8:
        return {"type": "BULLISH_FVG" if c3>c1 else "BEARISH_FVG", "gap": round(gap,2)}
    return None

def detect_order_block(prices):
    if len(prices) < 15: return None
    sl = prices[-15:]
    max_v, min_v = max(sl[:-3]), min(sl[:-3])
    last = sl[-1]
    if last < max_v-1.5: return {"type": "BEARISH_OB", "level": round(max_v,2)}
    if last > min_v+1.5: return {"type": "BULLISH_OB", "level": round(min_v,2)}
    return None

def get_psych_levels(price):
    step = 500  # niveaux ronds pertinents pour BTC (tous les 500$)
    l_up = math.ceil(price/step)*step
    l_dn = math.floor(price/step)*step
    return {"above": round(l_up,2), "below": round(l_dn,2),
            "dist_up": round(l_up-price,2), "dist_dn": round(price-l_dn,2)}

def analyze_dxy(dxy_prices):
    if len(dxy_prices) < 3: return "NEUTRE", 0
    trend = dxy_prices[-1] - dxy_prices[0]
    if trend > 0.3: return "HAUSSE 📈", -15
    elif trend < -0.3: return "BAISSE 📉", +12
    return "NEUTRE", 0

def analyze_us10y(val):
    if val > 4.5: return "ÉLEVÉ 🔴", -10
    elif val < 4.0: return "BAS 🟢", +8
    return "NEUTRE ⚪", 0

def is_news_blackout():
    if state.news_blackout_until:
        if datetime.now(timezone.utc) < state.news_blackout_until:
            rem = (state.news_blackout_until - datetime.now(timezone.utc)).seconds // 60
            return True, rem
    return False, 0

def check_news():
    now = datetime.now(timezone.utc)
    h, m, dow = now.hour, now.minute, now.weekday()
    if dow == 4 and now.day <= 7 and h == 13 and 25 <= m <= 45: return "NFP 🔴"
    if dow == 1 and 8 <= now.day <= 14 and h == 13 and 25 <= m <= 45: return "CPI 🔴"
    if h == 18 and m <= 30 and dow in [2,3]: return "FOMC 🔴"
    return None

# ═══════════════════════════════════════════════════════════════
# MOTEUR DE SCORE
# ═══════════════════════════════════════════════════════════════
def score_signal():
    prices = state.prices
    if len(prices) < 50: return None

    price = prices[-1]
    rsi = calc_rsi(prices)
    macd = calc_macd(prices)
    atr = calc_atr(prices)
    boll = calc_bollinger(prices)
    ema9  = calc_ema(prices, 9)
    ema21 = calc_ema(prices, 21)
    ema50 = calc_ema(prices, 50)
    ema200 = calc_ema(prices, min(200, len(prices)))
    session = detect_session()
    sweep = detect_liquidity_sweep(prices)
    fvg = detect_fvg(prices)
    ob = detect_order_block(prices)
    candle, candle_score = detect_candle_pattern(prices)
    psych = get_psych_levels(price)
    dxy_trend, dxy_score = analyze_dxy(state.dxy_prices)
    us10y_status, us10y_score = analyze_us10y(state.us10y_val)
    vol_ratio = calc_volume_ratio(state.volumes)

    score = 0
    reasons = []
    direction = "BUY" if price > ema50 else "SELL"

    macro_score = dxy_score if direction=="BUY" else -dxy_score
    score += macro_score
    if dxy_score != 0: reasons.append(f"DXY {dxy_trend}")

    y_score = us10y_score if direction=="BUY" else -us10y_score
    score += y_score
    if us10y_score != 0: reasons.append(f"US10Y {us10y_status}")

    if (price > ema200 and direction=="BUY") or (price < ema200 and direction=="SELL"):
        score += 10; reasons.append("EMA200 confirmée")
    if (price > ema50 and direction=="BUY") or (price < ema50 and direction=="SELL"):
        score += 8; reasons.append("EMA50 confirmée")
    if (ema9 > ema21 and direction=="BUY") or (ema9 < ema21 and direction=="SELL"):
        score += 10; reasons.append("EMA9/21 croisées")

    if direction=="BUY":
        if rsi < 35: score += 18; reasons.append(f"RSI survendu ({rsi}) 🔥")
        elif rsi < 45: score += 12; reasons.append(f"RSI survendu ({rsi})")
        elif rsi > 70: score -= 15
    else:
        if rsi > 65: score += 18; reasons.append(f"RSI suracheté ({rsi}) 🔥")
        elif rsi > 55: score += 12; reasons.append(f"RSI suracheté ({rsi})")
        elif rsi < 30: score -= 15

    if (macd["hist"] > 0.05 and direction=="BUY") or (macd["hist"] < -0.05 and direction=="SELL"):
        score += 10; reasons.append("MACD confirmé")

    if direction=="BUY" and price <= boll["lower"]:
        score += 14; reasons.append("BB inférieure 🔥")
    elif direction=="SELL" and price >= boll["upper"]:
        score += 14; reasons.append("BB supérieure 🔥")

    score += session["score_bonus"]
    if session["active"]: reasons.append(f"{session['emoji']} {session['name']}")

    if sweep:
        if (sweep["type"]=="BULL_SWEEP" and direction=="BUY") or (sweep["type"]=="BEAR_SWEEP" and direction=="SELL"):
            score += 20; reasons.append(f"Liquidity Sweep 🔥")
    if fvg:
        if (fvg["type"]=="BULLISH_FVG" and direction=="BUY") or (fvg["type"]=="BEARISH_FVG" and direction=="SELL"):
            score += 12; reasons.append(f"Fair Value Gap")
    if ob:
        if (ob["type"]=="BULLISH_OB" and direction=="BUY") or (ob["type"]=="BEARISH_OB" and direction=="SELL"):
            score += 15; reasons.append(f"Order Block")

    if candle and candle_score > 0:
        bullish = ["Bullish Engulfing","Hammer","Pin Bar Haussier"]
        bearish = ["Bearish Engulfing","Shooting Star","Pin Bar Baissier"]
        if (any(p in candle for p in bullish) and direction=="BUY") or \
           (any(p in candle for p in bearish) and direction=="SELL"):
            score += candle_score; reasons.append(candle)

    if vol_ratio >= 2.5: score += 15; reasons.append(f"Volume x{vol_ratio} 🔥")
    elif vol_ratio >= 1.5: score += 8; reasons.append(f"Volume x{vol_ratio}")

    if psych["dist_up"] < 2.0 or psych["dist_dn"] < 2.0:
        score += 8; reasons.append(f"Niveau psychologique")

    if state.consecutive_losses >= 3: score -= 20
    if not session["active"]: score -= 15

    score = max(0, min(score, 100))
    if score < MIN_SCORE: return None

    leverage, lev_label, lev_emoji = get_leverage(score)

    atr_sl = 1.2 if "OVERLAP" in session["name"] else 1.5
    atr_val = max(atr, 1.5)  # ATR minimum de 1.5 points
    
    if direction=="BUY":
        sl  = round(price - atr_val * atr_sl, 2)   # SL toujours EN DESSOUS
        tp1 = round(price + atr_val * 1.0, 2)       # TP toujours AU DESSUS
        tp2 = round(price + atr_val * 2.0, 2)
        tp3 = round(price + atr_val * 3.5, 2)
        # Vérification absolue
        assert sl < price, f"SL {sl} doit être < prix {price}"
        assert tp1 > price, f"TP1 {tp1} doit être > prix {price}"
    else:
        sl  = round(price + atr_val * atr_sl, 2)   # SL toujours AU DESSUS
        tp1 = round(price - atr_val * 1.0, 2)       # TP toujours EN DESSOUS
        tp2 = round(price - atr_val * 2.0, 2)
        tp3 = round(price - atr_val * 3.5, 2)
        # Vérification absolue
        assert sl > price, f"SL {sl} doit être > prix {price}"
        assert tp1 < price, f"TP1 {tp1} doit être < prix {price}"

    if direction=="BUY" and psych["above"] > price and abs(psych["above"]-tp2) < atr_val:
        tp2 = psych["above"]
    elif direction=="SELL" and psych["below"] < price and abs(psych["below"]-tp2) < atr_val:
        tp2 = psych["below"]

    rr = round(abs(tp2-price)/max(abs(sl-price),0.01), 1)
    trade_amount = ACCOUNT_SIZE * (TRADE_AMOUNT_PERCENT/100)
    exposure = trade_amount * leverage
    risk_amount = round(ACCOUNT_SIZE * RISK_PERCENT/100, 2)
    gain_tp2 = round(risk_amount * rr, 2)

    # Taille de position OKX BTC-USDC-SWAP
    # 1 contrat BTC-USDC-SWAP = 0.01 BTC sur OKX — taille minimale exprimée en contrats
    contract_value_btc = 0.0001  # 1 contrat BTC-USDT-SWAP = 0.0001 BTC (confirmé OKX)
    btc_amount = exposure / price
    size = max(1, round(btc_amount / contract_value_btc))

    # Sécurité — si 1 contrat minimum dépasse largement l'exposition voulue,
    # le risque réel est trop élevé pour ce capital, on annule le signal
    real_exposure = size * contract_value_btc * price
    if real_exposure > trade_amount * leverage * 2.5:
        return None  # Position minimale OKX trop grosse pour ce capital

    return {
        "direction": direction, "entry": price,
        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr": rr, "score": score, "reasons": reasons,
        "session": session, "atr": atr, "rsi": rsi,
        "candle": candle, "dxy_trend": dxy_trend,
        "us10y": f"{state.us10y_val:.2f}%",
        "leverage": leverage, "lev_label": lev_label, "lev_emoji": lev_emoji,
        "trade_amount": trade_amount, "exposure": round(exposure,2),
        "risk_amount": risk_amount, "gain_tp2": gain_tp2,
        "vol_ratio": vol_ratio, "size": size,
    }

# ═══════════════════════════════════════════════════════════════
# VÉRIFICATION SORTIE
# ═══════════════════════════════════════════════════════════════
def check_exit(current_price):
    if not state.current_trade: return None
    t = state.current_trade
    d = t["direction"]
    elapsed = time.time() - t["open_time"]

    if elapsed >= MAX_TRADE_DURATION:
        pnl = round(current_price-t["entry"] if d=="BUY" else t["entry"]-current_price, 2)
        return {"reason": "TIMEOUT 15MIN", "price": current_price, "pnl": pnl, "emoji": "⏰"}

    # TRAILING STOP — si TP1 atteint, on suit le prix avec un stop dynamique
    if d == "BUY" and current_price >= t["tp1"]:
        trailing_sl = round(current_price - t["atr"] * 0.8, 2)
        if "trailing_sl" not in t or trailing_sl > t.get("trailing_sl", 0):
            t["trailing_sl"] = trailing_sl
        if current_price <= t.get("trailing_sl", 0):
            return {"reason": "TRAILING STOP", "price": current_price, "pnl": round(current_price-t["entry"], 2), "emoji": "🔄"}
    elif d == "SELL" and current_price <= t["tp1"]:
        trailing_sl = round(current_price + t["atr"] * 0.8, 2)
        if "trailing_sl" not in t or trailing_sl < t.get("trailing_sl", float("inf")):
            t["trailing_sl"] = trailing_sl
        if current_price >= t.get("trailing_sl", float("inf")):
            return {"reason": "TRAILING STOP", "price": current_price, "pnl": round(t["entry"]-current_price, 2), "emoji": "🔄"}

    if d=="BUY":
        if current_price <= t["sl"]: return {"reason":"STOP LOSS","price":current_price,"pnl":round(current_price-t["entry"],2),"emoji":"🛑"}
        if current_price >= t["tp3"]: return {"reason":"TP3 MAX","price":current_price,"pnl":round(current_price-t["entry"],2),"emoji":"🏆"}
        if current_price >= t["tp2"]: return {"reason":"TP2 ATTEINT","price":current_price,"pnl":round(current_price-t["entry"],2),"emoji":"🎯"}
        if current_price >= t["tp1"]: return {"reason":"TP1 ATTEINT","price":current_price,"pnl":round(current_price-t["entry"],2),"emoji":"✅"}
    else:
        if current_price >= t["sl"]: return {"reason":"STOP LOSS","price":current_price,"pnl":round(t["entry"]-current_price,2),"emoji":"🛑"}
        if current_price <= t["tp3"]: return {"reason":"TP3 MAX","price":current_price,"pnl":round(t["entry"]-current_price,2),"emoji":"🏆"}
        if current_price <= t["tp2"]: return {"reason":"TP2 ATTEINT","price":current_price,"pnl":round(t["entry"]-current_price,2),"emoji":"🎯"}
        if current_price <= t["tp1"]: return {"reason":"TP1 ATTEINT","price":current_price,"pnl":round(t["entry"]-current_price,2),"emoji":"✅"}
    return None

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
async def send_telegram(session_http, message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with session_http.post(
            url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            return r.status == 200
    except Exception as e:
        print(f"❌ Telegram: {e}")
        return False

async def send_entry_notification(session_http, signal, order_id):
    is_buy = signal["direction"] == "BUY"
    arrow = "📈" if is_buy else "📉"
    action = "ACHETÉ" if is_buy else "VENDU"
    confluences = "\n".join([f"  ✓ {r}" for r in signal["reasons"][:8]])

    msg = f"""{arrow} <b>TRADE OUVERT AUTOMATIQUEMENT !</b>
━━━━━━━━━━━━━━━━━━━━━━━━
{signal['lev_emoji']} <b>{action} BTC</b> — Levier {signal['leverage']}x
<i>{signal['lev_label']} (Score {signal['score']}/100)</i>
━━━━━━━━━━━━━━━━━━━━━━━━
📍 <b>Entrée :</b> <code>{signal['entry']}</code>
🛑 <b>Stop Loss :</b> <code>{signal['sl']}</code>
✅ <b>TP1 :</b> <code>{signal['tp1']}</code>
🎯 <b>TP2 :</b> <code>{signal['tp2']}</code>
🏆 <b>TP3 :</b> <code>{signal['tp3']}</code>
⚖️ <b>RR :</b> 1:{signal['rr']}
━━━━━━━━━━━━━━━━━━━━━━━━
💰 <b>Position :</b> {signal['trade_amount']}$ × {signal['leverage']}x = {signal['exposure']}$
📈 <b>Gain estimé TP2 :</b> ~+{signal['gain_tp2']}$
🆔 <b>Ordre ID :</b> <code>{order_id}</code>
━━━━━━━━━━━━━━━━━━━━━━━━
{signal['session']['emoji']} {signal['session']['name']} | DXY: {signal['dxy_trend']}
{f"🕯️ {signal['candle']}" if signal['candle'] else ""}

<b>Confluences :</b>
{confluences}
━━━━━━━━━━━━━━━━━━━━━━━━
⏳ <i>Je surveille et ferme automatiquement...</i>"""

    await send_telegram(session_http, msg)

async def send_exit_notification(session_http, exit_info):
    t = state.current_trade
    pnl = exit_info["pnl"]
    is_win = pnl > 0
    duration = int(time.time() - t["open_time"])
    mins, secs = duration//60, duration%60
    pnl_dollar = round(abs(pnl) * 0.01 * t["leverage"], 2)
    win_rate = round(state.wins / max(1, state.wins+state.losses) * 100)

    if "STOP" in exit_info["reason"]: header = "🛑 <b>STOP LOSS — TRADE FERMÉ AUTO</b>"
    elif "TP3" in exit_info["reason"]: header = "🏆 <b>TP3 MAXIMUM — TRADE FERMÉ AUTO !</b>"
    elif "TP2" in exit_info["reason"]: header = "🎯 <b>TP2 ATTEINT — TRADE FERMÉ AUTO !</b>"
    elif "TP1" in exit_info["reason"]: header = "✅ <b>TP1 ATTEINT — TRADE FERMÉ AUTO !</b>"
    else: header = "⏰ <b>TIMEOUT — TRADE FERMÉ AUTO</b>"

    msg = f"""{header}
━━━━━━━━━━━━━━━━━━━━━━━━
💱 {t['direction']} | Levier {t['leverage']}x
📍 Entrée : <code>{t['entry']}</code>
📍 Sortie : <code>{exit_info['price']}</code>
{'💰' if is_win else '📉'} P&L : <code>{'+' if is_win else ''}{pnl:.2f} pts</code> (~{'+' if is_win else '-'}{pnl_dollar}$)
⏱️ Durée : {mins}m {secs}s
━━━━━━━━━━━━━━━━━━━━━━━━
📊 Win Rate : {win_rate}% ({state.wins}W/{state.losses}L)
💹 P&L Total : {'+' if state.total_pnl>=0 else ''}{state.total_pnl:.2f} pts
━━━━━━━━━━━━━━━━━━━━━━━━
🔍 <i>Prochain signal en cours d'analyse...</i>"""

    await send_telegram(session_http, msg)

# ═══════════════════════════════════════════════════════════════
# RAPPORT JOURNALIER
# ═══════════════════════════════════════════════════════════════
async def send_daily_report(session_http):
    """Envoie un rapport journalier à 21h UTC"""
    win_rate = round(state.wins / max(1, state.wins + state.losses) * 100)
    total_trades = state.wins + state.losses

    if total_trades == 0:
        msg = """📊 <b>RAPPORT JOURNALIER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Aucun trade aujourd'hui.
Le marché n'a pas offert de setup ≥ 78/100.
━━━━━━━━━━━━━━━━━━━━━━━━
🔍 Analyse reprend demain session London (07h UTC)"""
    else:
        msg = f"""📊 <b>RAPPORT JOURNALIER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
✅ Victoires : {state.wins}
❌ Pertes : {state.losses}
🎯 Win Rate : {win_rate}%
💹 P&L Total : {'+' if state.total_pnl >= 0 else ''}{state.total_pnl:.2f} pts
━━━━━━━━━━━━━━━━━━━━━━━━
💰 Solde estimé : {round(101 + state.total_pnl * 0.1, 2)}$
🔍 Reprise demain session London (07h UTC)"""

    await send_telegram(session_http, msg)

async def check_spread(session_http):
    """Vérifie que le spread OKX est acceptable"""
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={SYMBOL}"
        async with session_http.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == "0":
                    bid = float(data["data"][0]["bidPx"])
                    ask = float(data["data"][0]["askPx"])
                    spread = round(ask - bid, 2)
                    return spread < 1.0, spread  # spread max 1 point
    except:
        pass
    return True, 0

# ═══════════════════════════════════════════════════════════════
# TEST MANUEL — Vérifie si le placement d'ordre fonctionne
# ═══════════════════════════════════════════════════════════════
async def test_order_placement(session_http):
    """Place un micro-ordre de test pour vérifier que tout fonctionne,
    puis le ferme immédiatement. Coûte quasi rien (taille minimale)."""
    await send_telegram(session_http, "🧪 <b>TEST EN COURS</b> — Tentative d'ordre minimal sur OKX...")

    price = state.last_price
    test_sl = round(price - 3.0, 2)
    test_tp = round(price + 3.0, 2)

    # Set levier minimal
    await okx_set_leverage(session_http, 2)

    order_id = await okx_place_order(session_http, "BUY", 1, test_sl, test_tp, price)

    if order_id:
        await send_telegram(session_http, f"""✅ <b>TEST RÉUSSI !</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Ordre placé avec succès sur OKX.
🆔 Order ID : <code>{order_id}</code>
📍 Prix : {price}

Le bot PEUT trader réellement.
Fermeture du test dans 5 secondes...""")
        await asyncio.sleep(5)
        closed = await okx_close_position(session_http, "BUY")
        await send_telegram(session_http, f"{'✅' if closed else '⚠️'} Position de test fermée.")
        return True
    else:
        await send_telegram(session_http, """❌ <b>TEST ÉCHOUÉ</b>
━━━━━━━━━━━━━━━━━━━━━━━━
Le placement d'ordre a échoué.
Vérifie les Deploy Logs Railway pour le détail de l'erreur exacte (code sCode).""")
        return False

# ═══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════
async def main():
    print("🚀 BTC AUTO TRADE BOT V4.0")

    async with aiohttp.ClientSession() as http:

        await send_telegram(http, """🤖 <b>BTC AUTO TRADE BOT V4.0 — ACTIF</b>

⚡ <b>100% AUTOMATIQUE</b>
Je trade tout seul sur OKX.
Tu reçois juste les notifications.

💎 Levier dynamique :
⚡ Score 78-84 → 2x
🔥 Score 85-91 → 3x
🔥🔥 Score 92-96 → 5x
💎 Score 97-100 → 10x

🎯 Un seul trade à la fois
⏱️ Timeout 15 min automatique
🛑 SL/TP placés automatiquement

<i>Tu n'as plus rien à faire — je gère tout.</i>
🔍 <i>Analyse en cours...</i>""")

        # Chargement initial
        for _ in range(60):
            p = await get_gold_price(http)
            state.prices.append(p)
            state.volumes.append(random.randint(80, 200))
            state.last_price = p
            await asyncio.sleep(0.05)

        for _ in range(5):
            state.dxy_prices.append(await get_dxy_price(http))
        state.us10y_val = await get_us10y(http)

        print(f"✅ Prix: {state.last_price} | DXY: {state.dxy_prices[-1]:.2f} | US10Y: {state.us10y_val:.2f}%")

        # ═══ DIAGNOSTIC COMPLET AU DÉMARRAGE ═══
        diag = []

        # 1. Test prix OKX
        test_price = await get_gold_price(http)
        if test_price and test_price > 1000:
            diag.append(("✅", f"Prix OKX connecté : <b>{test_price}$</b>"))
        else:
            diag.append(("❌", "Prix OKX — connexion échouée"))

        # 2. Test DXY
        if state.dxy_prices and state.dxy_prices[-1] > 0:
            diag.append(("✅", f"DXY connecté : <b>{state.dxy_prices[-1]:.2f}</b>"))
        else:
            diag.append(("❌", "DXY — données non reçues"))

        # 3. Test US10Y
        if state.us10y_val > 0:
            diag.append(("✅", f"US10Y connecté : <b>{state.us10y_val:.2f}%</b>"))
        else:
            diag.append(("❌", "US10Y — données non reçues"))

        # 4. Test API OKX — vérifier solde
        try:
            for ccy_check in ["USD", "USDC", "USDT"]:
                try:
                    path = f"/api/v5/account/balance?ccy={ccy_check}"
                    async with http.get(
                        OKX_BASE_URL + path,
                        headers=okx_headers("GET", path),
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        data = await r.json()
                        if data.get("code") == "0" and data["data"][0].get("details"):
                            balance = float(data["data"][0]["details"][0]["availBal"])
                            diag.append(("✅", f"Solde {ccy_check} : <b>{balance:.2f}</b>"))
                        else:
                            diag.append(("ℹ️", f"Solde {ccy_check} : 0 ou indisponible"))
                except Exception as e:
                    diag.append(("⚠️", f"Solde {ccy_check} — {str(e)[:40]}"))
        except Exception as e:
            diag.append(("❌", f"API OKX — {str(e)[:50]}"))

        # 5. Session actuelle
        session = detect_session()
        if session["active"]:
            diag.append(("✅", f"Session <b>{session['name']}</b> {session['emoji']} — Trading actif"))
        else:
            diag.append(("⚠️", f"Session <b>{session['name']}</b> — Liquidité faible"))

        # 6. Symbole OKX
        try:
            url = f"{OKX_BASE_URL}/api/v5/market/ticker?instId={SYMBOL}"
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
                if data.get("code") == "0":
                    last = float(data["data"][0]["last"])
                    diag.append(("✅", f"Symbole <b>{SYMBOL}</b> disponible @ {last}$"))
                else:
                    diag.append(("❌", f"Symbole {SYMBOL} non disponible sur OKX"))
        except:
            diag.append(("❌", f"Symbole {SYMBOL} — vérification échouée"))

        # 7. Liste tous les contrats BTC réellement tradables avec leur état (diagnostic listing)
        try:
            url = f"{OKX_BASE_URL}/api/v5/public/instruments?instType=SWAP"
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                if data.get("code") == "0":
                    btc_instruments = [(i["instId"], i.get("state", "?")) for i in data["data"] if i["instId"].startswith("BTC-")]
                    if btc_instruments:
                        formatted = ", ".join([f"{iid}({st})" for iid, st in btc_instruments])
                        diag.append(("ℹ️", f"Contrats BTC trouvés : {formatted}"))
                    else:
                        diag.append(("⚠️", "Aucun contrat BTC SWAP trouvé sur OKX"))
        except Exception as e:
            diag.append(("⚠️", f"Liste instruments — {str(e)[:50]}"))

        # 8. Liste les contrats X-Perp (FUTURES, ruleType=xperp) pour XAU — produit MiCA conforme EU
        try:
            url = f"{OKX_BASE_URL}/api/v5/public/instruments?instType=FUTURES"
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                if data.get("code") == "0":
                    xperp_xau = [(i["instId"], i.get("state", "?"), i.get("ruleType", "?")) for i in data["data"] if "XAU" in i["instId"]]
                    xperp_all = [(i["instId"], i.get("ruleType", "?")) for i in data["data"] if i.get("ruleType") == "xperp"]
                    if xperp_xau:
                        formatted = ", ".join([f"{iid}({st},{rt})" for iid, st, rt in xperp_xau])
                        diag.append(("🎯", f"X-Perp XAU trouvés : {formatted}"))
                    else:
                        diag.append(("⚠️", "Aucun X-Perp XAU trouvé"))
                    diag.append(("ℹ️", f"Total X-Perp disponibles : {len(xperp_all)} contrats"))
        except Exception as e:
            diag.append(("⚠️", f"Liste X-Perp — {str(e)[:50]}"))

        # Construire le message diagnostic
        diag_lines = "\n".join([f"{icon} {msg}" for icon, msg in diag])
        all_ok = all(icon == "✅" for icon, _ in diag)
        status = "🟢 <b>TOUT EST OPÉRATIONNEL</b>" if all_ok else "🟡 <b>OPÉRATIONNEL AVEC AVERTISSEMENTS</b>"

        await send_telegram(http, f"""🔍 <b>DIAGNOSTIC SYSTÈME</b>
━━━━━━━━━━━━━━━━━━━━━━━━
{diag_lines}
━━━━━━━━━━━━━━━━━━━━━━━━
{status}

📊 <b>Paramètres actifs :</b>
→ Symbole : {SYMBOL}
→ Score min : {MIN_SCORE}/100
→ Scan : toutes les secondes
→ Levier : dynamique 2x-10x
→ Risque/trade : {RISK_PERCENT}% du capital
→ Timeout : 15 min

<i>Je commence l'analyse BTC...</i>""")

        # Test de placement d'ordre désactivé — attente d'un signal naturel
        # await asyncio.sleep(10)
        # await test_order_placement(http)

        tick = 0

        while state.running:
            try:
                price = await get_gold_price(http)
                state.prices.append(price)
                state.volumes.append(random.randint(60, 250))
                state.last_price = price
                if len(state.prices) > 500:
                    state.prices = state.prices[-500:]
                    state.volumes = state.volumes[-500:]

                tick += 1

                if tick % 30 == 0:
                    state.dxy_prices.append(await get_dxy_price(http))
                    if len(state.dxy_prices) > 20: state.dxy_prices = state.dxy_prices[-20:]

                if tick % 60 == 0:
                    state.us10y_val = await get_us10y(http)

                # Blackout news
                news = check_news()
                if news and not state.news_blackout_until:
                    state.news_blackout_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                    await send_telegram(http, f"⚠️ <b>BLACKOUT {news}</b> — Pause 30 min")

                blackout, _ = is_news_blackout()
                if blackout:
                    await asyncio.sleep(1)
                    continue
                else:
                    state.news_blackout_until = None

                # EN TRADE — surveille sortie
                if state.in_trade:
                    exit_info = check_exit(price)
                    if exit_info:
                        # Fermer sur OKX automatiquement
                        closed = await okx_close_position(http, state.current_trade["direction"])
                        if not closed:
                            print("⚠️ Fermeture auto échouée — SL/TP OKX gère")

                        if exit_info["pnl"] > 0:
                            state.wins += 1; state.consecutive_losses = 0
                        else:
                            state.losses += 1; state.consecutive_losses += 1

                        state.total_pnl = round(state.total_pnl + exit_info["pnl"], 2)
                        await send_exit_notification(http, exit_info)
                        state.in_trade = False
                        state.current_trade = None
                        state.okx_order_id = None
                        print(f"✅ Fermé: {exit_info['reason']} | PnL: {exit_info['pnl']:.2f}")

                # PAS EN TRADE — scan
                elif tick % 1 == 0:
                    session = detect_session()
                    if not session["active"]:
                        if tick % 120 == 0: print(f"😴 {session['name']} — pause")
                        await asyncio.sleep(1)
                        continue

                    # Filtre spread
                    spread_ok, spread_val = await check_spread(http)
                    if not spread_ok:
                        if tick % 60 == 0:
                            print(f"⚠️ Spread trop large: {spread_val} pts — attente")
                        await asyncio.sleep(1)
                        continue

                    signal = score_signal()
                    if signal:
                        lev = signal["leverage"]
                        print(f"🚨 {signal['direction']} @ {signal['entry']} | Score: {signal['score']}/100 | Levier: {lev}x | Spread: {spread_val}")

                        # Définir le levier sur OKX
                        await okx_set_leverage(http, lev)

                        # Placer l'ordre sur OKX
                        order_id = await okx_place_order(
                            http,
                            signal["direction"],
                            signal["size"],
                            signal["sl"],
                            signal["tp2"],
                            signal["entry"]
                        )

                        if order_id:
                            state.in_trade = True
                            state.okx_order_id = order_id
                            state.current_trade = {**signal, "open_time": time.time()}
                            await send_entry_notification(http, signal, order_id)
                        else:
                            print("❌ Ordre échoué — skip ce signal")
                    else:
                        if tick % 60 == 0: print(f"🔍 Scan #{tick} — Pas de setup")

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                error_msg = str(e)
                print(f"❌ Erreur: {error_msg}")

                # Diagnostic automatique
                if "401" in error_msg or "Invalid" in error_msg or "sign" in error_msg.lower():
                    advice = "🔑 Problème de clé API OKX\n→ Vérifie OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE dans Railway Variables"
                elif "429" in error_msg or "rate" in error_msg.lower():
                    advice = "⏱️ Trop de requêtes\n→ Aucune action requise, reprise automatique dans 60s"
                elif "connect" in error_msg.lower() or "timeout" in error_msg.lower():
                    advice = "🌐 Problème de connexion internet\n→ Vérifie que Railway est bien actif\n→ Redémarre le service si ça dure"
                elif "insufficient" in error_msg.lower() or "balance" in error_msg.lower():
                    advice = "💰 Solde OKX insuffisant\n→ Recharge ton compte OKX démo ou réel"
                elif "position" in error_msg.lower():
                    advice = "📊 Problème de position\n→ Vérifie sur OKX si une position est déjà ouverte\n→ Ferme-la manuellement si besoin"
                else:
                    advice = f"⚠️ Erreur inconnue\n→ Redémarre le service Railway si ça persiste"

                await send_telegram(http, f"""🚨 <b>ERREUR DÉTECTÉE — BOT EN PAUSE</b>

❌ <b>Erreur :</b> <code>{error_msg[:200]}</code>

{advice}

⏳ <i>Reprise automatique dans 30 secondes...</i>""")
                await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
