import os, time, hmac, hashlib, secrets
from urllib.parse import urlencode
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
BASE_URL = os.getenv('BINANCE_TR_BASE_URL', 'https://www.binance.tr').rstrip('/')
MARKET_BASE_URL = 'https://api.binance.me'
API_KEY = os.getenv('BINANCE_TR_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_TR_API_SECRET', '').strip()
REAL_TRADING = os.getenv('JARVIS_REAL_TRADING', 'false').lower() == 'true'
RECV_WINDOW = int(os.getenv('BINANCE_RECV_WINDOW', '5000'))


def signed_request(method, path, params):
    if not API_KEY or not API_SECRET:
        raise RuntimeError('API credentials are not configured')
    params = dict(params)
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = RECV_WINDOW
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = {'X-MBX-APIKEY': API_KEY}
    url = f'{BASE_URL}{path}?{qs}&signature={sig}'
    r = requests.request(method, url, headers=headers, timeout=15)
    try: data = r.json()
    except Exception: data = {'raw': r.text}
    return r.status_code, data


def public_request(path, params=None):
    r = requests.get(f'{MARKET_BASE_URL}{path}', params=params or {}, timeout=15)
    try: data = r.json()
    except Exception: data = {'raw': r.text}
    return r.status_code, data

@app.get('/')
def home():
    return render_template('index.html', configured=bool(API_KEY and API_SECRET), real=REAL_TRADING, base=BASE_URL)

@app.get('/api/status')
def status():
    return jsonify({
        'configured': bool(API_KEY and API_SECRET),
        'real_trading': REAL_TRADING,
        'base_url': BASE_URL,
        'order_endpoint': '/open/v1/orders',
        'warning': 'REAL TRADING ACTIVE' if REAL_TRADING else 'PAPER/LOCKED'
    })

@app.post('/api/config')
def save_config():
    """Local JARVIS configuration. Stores API credentials in .env for this device."""
    global API_KEY, API_SECRET, REAL_TRADING
    if request.remote_addr not in ('127.0.0.1','::1'): return jsonify({'error':'Local device only'}), 403
    body=request.get_json(force=True) or {}
    new_key=str(body.get('apiKey', API_KEY) or '').strip()
    new_secret=str(body.get('apiSecret', API_SECRET) or '').strip()
    enable=body.get('enableReal', REAL_TRADING)
    if ('apiKey' in body or 'apiSecret' in body) and (not new_key or not new_secret):
        return jsonify({'error':'API Key ve API Secret birlikte gerekli'}), 400
    API_KEY=new_key
    API_SECRET=new_secret
    REAL_TRADING=bool(enable) and bool(API_KEY and API_SECRET)
    env_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(env_path,'w',encoding='utf-8') as f:
            f.write('BINANCE_TR_BASE_URL='+BASE_URL+'\n')
            f.write('BINANCE_TR_API_KEY='+API_KEY+'\n')
            f.write('BINANCE_TR_API_SECRET='+API_SECRET+'\n')
            f.write('JARVIS_REAL_TRADING='+('true' if REAL_TRADING else 'false')+'\n')
            f.write('BINANCE_RECV_WINDOW='+str(RECV_WINDOW)+'\n')
    except Exception as e:
        return jsonify({'error':'Ayar kaydedilemedi: '+str(e)}),500
    return jsonify({'ok':True,'configured':bool(API_KEY and API_SECRET),'real_trading':REAL_TRADING,'base_url':BASE_URL,'warning':'REAL TRADING ACTIVE' if REAL_TRADING else 'PAPER/LOCKED'})

@app.get('/api/ticker')
def ticker():
    symbol = request.args.get('symbol', 'SAND_TRY').upper()
    code, data = public_request('/api/v3/ticker/24hr', {'symbol': symbol.replace('_', '')})
    return jsonify(data), code

@app.get('/api/balance')
def balance():
    if not (API_KEY and API_SECRET):
        return jsonify({'ok':True,'mode':'ANALYSIS','analysis_mode':True,'data':[],'message':'API anahtarı yok; gerçek hesap bakiyesi gösterilemiyor.'})
    code, data = signed_request('GET', '/open/v1/account', {})
    return jsonify(data), code

@app.post('/api/order')
def order():
    if not REAL_TRADING:
        return jsonify({'error':'Real trading is locked. Set JARVIS_REAL_TRADING=true after configuring the API.'}), 403
    if not (API_KEY and API_SECRET):
        return jsonify({'error':'API credentials missing'}), 400
    body = request.get_json(force=True) or {}
    # Explicit server-side confirmation prevents accidental browser calls.
    if body.get('confirm') != 'EXECUTE_REAL_ORDER':
        return jsonify({'error':'Explicit real-order confirmation required'}), 400
    symbol = str(body.get('symbol','')).upper().strip()
    side = str(body.get('side','')).upper().strip()
    order_type = str(body.get('type','LIMIT')).upper().strip()
    quantity = body.get('quantity')
    price = body.get('price')
    if not symbol or side not in ('BUY','SELL') or order_type not in ('LIMIT','MARKET') or quantity is None:
        return jsonify({'error':'Invalid order fields'}), 400
    # Binance TR numeric enums: side 0/1 and type 1=LIMIT, 2=MARKET per official docs.
    params = {
        'symbol': symbol,
        'side': 0 if side == 'BUY' else 1,
        'type': 1 if order_type == 'LIMIT' else 2,
        'quantity': str(quantity),
    }
    if order_type == 'LIMIT':
        if price is None: return jsonify({'error':'LIMIT order requires price'}), 400
        params['price'] = str(price)
        params['timeInForce'] = int(body.get('timeInForce', 1))
    else:
        if body.get('quoteOrderQty') is not None:
            params.pop('quantity', None)
            params['quoteOrderQty'] = str(body['quoteOrderQty'])
    code, data = signed_request('POST', '/open/v1/orders', params)
    return jsonify(data), code

@app.post('/api/order/cancel')
def cancel_order():
    if not REAL_TRADING: return jsonify({'error':'Real trading is locked'}), 403
    if not (API_KEY and API_SECRET): return jsonify({'error':'API credentials missing'}), 400
    body = request.get_json(force=True) or {}
    if body.get('confirm') != 'CANCEL_REAL_ORDER': return jsonify({'error':'Explicit cancel confirmation required'}), 400
    params = {}
    if body.get('orderId') is not None: params['orderId'] = body['orderId']
    elif body.get('clientId'): params['clientId'] = body['clientId']
    else: return jsonify({'error':'orderId or clientId required'}), 400
    code, data = signed_request('POST', '/open/v1/orders/cancel', params)
    return jsonify(data), code

@app.get('/api/order')
def order_detail():
    if not (API_KEY and API_SECRET): return jsonify({'error':'API credentials missing'}), 400
    params = {}
    if request.args.get('orderId'): params['orderId'] = request.args['orderId']
    elif request.args.get('clientId'): params['clientId'] = request.args['clientId']
    else: return jsonify({'error':'orderId or clientId required'}), 400
    code, data = signed_request('GET', '/open/v1/orders/detail', params)
    return jsonify(data), code

@app.get('/api/orders')
def orders():
    if not (API_KEY and API_SECRET):
        return jsonify({'ok':True,'mode':'ANALYSIS','analysis_mode':True,'data':[],'message':'API anahtarı yok; gerçek açık emirler gösterilemiyor.'})
    symbol = request.args.get('symbol','').upper().strip()
    if not symbol: return jsonify({'error':'symbol required'}), 400
    params = {'symbol': symbol, 'type': int(request.args.get('type','-1')), 'limit': int(request.args.get('limit','50'))}
    code, data = signed_request('GET', '/open/v1/orders', params)
    return jsonify(data), code


@app.get('/api/markets')
def markets():
    """
    JARVIS GERÇEK 309 COIN ANALİZ MOTORU

    Public veriler:
    - 24s fiyat değişimi
    - 24s hacim
    - bid / ask
    - spread
    - quote volume

    Gerçek emir göndermez.
    """

    try:
        # =================================================
        # 1) BINANCE TR GERÇEK TRY SEMBOLLERİ
        # =================================================

        # Binance TR'nin /open/v1/common/symbols endpointi Render
        # bulut IP'lerinde 451 döndürebildiği için sembol listesini
        # public 24s ticker verisinden çıkarıyoruz.
        ticker_resp = requests.get(
            f"{MARKET_BASE_URL}/api/v3/ticker/24hr",
            timeout=20
        )
        ticker_resp.raise_for_status()
        ticker_data = ticker_resp.json()
        if not isinstance(ticker_data, list):
            ticker_data = []

        ticker_map = {}
        try_symbols = []

        for item in ticker_data:
            if not isinstance(item, dict):
                continue

            raw_symbol = str(item.get("symbol", "")).strip().upper()
            if not raw_symbol:
                continue

            ticker_map[raw_symbol] = item

            # Binance/market ticker sembolü BTC_TRY veya BTCTRY olabilir.
            if raw_symbol.endswith("_TRY"):
                tr_symbol = raw_symbol
            elif raw_symbol.endswith("TRY"):
                tr_symbol = raw_symbol[:-3] + "_TRY"
            else:
                continue

            try_symbols.append(tr_symbol)

        try_symbols = sorted(set(try_symbols))

        # =================================================
        # 2) TOPLU PUBLIC TICKER
        # =================================================

        # ticker_map yukarıda oluşturuldu; aynı ticker isteğini tekrar etmiyoruz.

        result = {}
        opportunities = []

        # =================================================
        # 3) JARVIS SKOR MOTORU
        # =================================================

        for tr_symbol in try_symbols:

            api_symbol = tr_symbol.replace("_", "")
            item = ticker_map.get(api_symbol)

            if not item:
                result[tr_symbol] = {
                    "symbol": tr_symbol,
                    "error": "Ticker bulunamadı"
                }
                continue

            try:
                price = float(item.get("lastPrice", 0) or 0)
                change = float(
                    item.get("priceChangePercent", 0) or 0
                )
                volume = float(
                    item.get("volume", 0) or 0
                )
                quote_volume = float(
                    item.get("quoteVolume", 0) or 0
                )
                bid = float(
                    item.get("bidPrice", 0) or 0
                )
                ask = float(
                    item.get("askPrice", 0) or 0
                )
            except Exception:
                price = change = volume = quote_volume = 0
                bid = ask = 0

            # ---------------------------------------------
            # MOMENTUM SKORU
            # -50 / +50
            # ---------------------------------------------

            momentum = max(-50, min(50, change * 5))

            # ---------------------------------------------
            # HACİM SKORU
            # Güçlü hacim hareketi ekstra puan getirir.
            # ---------------------------------------------

            volume_score = 0

            if quote_volume > 0:

                if quote_volume >= 100_000_000:
                    volume_score = 20

                elif quote_volume >= 50_000_000:
                    volume_score = 15

                elif quote_volume >= 10_000_000:
                    volume_score = 10

                elif quote_volume >= 1_000_000:
                    volume_score = 5

            # ---------------------------------------------
            # SPREAD
            # ---------------------------------------------

            spread_pct = 0

            if bid > 0 and ask > 0 and ask >= bid:
                spread_pct = ((ask - bid) / bid) * 100

            spread_score = 0

            if spread_pct <= 0.05:
                spread_score = 10

            elif spread_pct <= 0.15:
                spread_score = 6

            elif spread_pct <= 0.50:
                spread_score = 2

            else:
                spread_score = -5

            # ---------------------------------------------
            # YÖN
            # ---------------------------------------------

            direction_score = 0

            if change > 0:
                direction_score = 10

            elif change < 0:
                direction_score = -10

            # ---------------------------------------------
            # HAM SKOR
            # ---------------------------------------------

            raw_score = (
                momentum
                + volume_score
                + spread_score
                + direction_score
            )

            # ---------------------------------------------
            # 0-100 NORMALİZASYON
            # ---------------------------------------------

            score = round(
                max(0, min(100, 50 + raw_score)),
                1
            )

            # ---------------------------------------------
            # SİNYAL
            # ---------------------------------------------

            if score >= 70 and change > 0:
                signal = "AL"

            elif score <= 30 and change < 0:
                signal = "SAT"

            else:
                signal = "BEKLE"

            record = {
                "symbol": tr_symbol,
                "lastPrice": item.get("lastPrice", "0"),
                "priceChange": item.get("priceChange", "0"),
                "priceChangePercent": item.get(
                    "priceChangePercent", "0"
                ),
                "weightedAvgPrice": item.get(
                    "weightedAvgPrice", "0"
                ),
                "prevClosePrice": item.get(
                    "prevClosePrice", "0"
                ),
                "lastQty": item.get("lastQty", "0"),
                "bidPrice": item.get("bidPrice", "0"),
                "askPrice": item.get("askPrice", "0"),
                "bidQty": item.get("bidQty", "0"),
                "askQty": item.get("askQty", "0"),
                "volume": item.get("volume", "0"),
                "quoteVolume": item.get(
                    "quoteVolume", "0"
                ),
                "openPrice": item.get("openPrice", "0"),
                "highPrice": item.get("highPrice", "0"),
                "lowPrice": item.get("lowPrice", "0"),
                "openTime": item.get("openTime"),
                "closeTime": item.get("closeTime"),
                "count": item.get("count", 0),

                # JARVIS ANALİZ
                "jarvisScore": score,
                "jarvisSignal": signal,
                "momentumScore": round(momentum, 1),
                "volumeScore": volume_score,
                "spreadPercent": round(spread_pct, 4),
                "spreadScore": spread_score,
                "directionScore": direction_score
            }

            result[tr_symbol] = record

            opportunities.append(record)

        # =================================================
        # 4) JARVIS RELATIVE PERCENTILE SCORING
        # =================================================

        import math

        def pct_rank(values, value):
            if not values:
                return 50.0

            ordered = sorted(values)
            n = len(ordered)

            if n <= 1:
                return 50.0

            below = sum(1 for v in ordered if v < value)
            equal = sum(1 for v in ordered if v == value)

            return max(
                0.0,
                min(
                    100.0,
                    ((below + equal / 2.0) / n) * 100.0
                )
            )

        change_values = []
        volume_values = []
        spread_values = []

        for x in opportunities:

            try:
                change = float(
                    x.get("priceChangePercent", 0) or 0
                )
            except Exception:
                change = 0.0

            try:
                volume = float(
                    x.get("quoteVolume", 0) or 0
                )
            except Exception:
                volume = 0.0

            try:
                bid = float(
                    x.get("bidPrice", 0) or 0
                )
            except Exception:
                bid = 0.0

            try:
                ask = float(
                    x.get("askPrice", 0) or 0
                )
            except Exception:
                ask = 0.0

            if bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
                spread = ((ask - bid) / mid) * 100.0
            else:
                spread = 1.0

            spread = max(0.0, min(10.0, spread))

            x["_jarvis_change"] = change
            x["_jarvis_volume"] = max(0.0, volume)
            x["_jarvis_spread"] = spread

            change_values.append(change)
            volume_values.append(math.log1p(max(0.0, volume)))
            spread_values.append(-spread)

        # Her coinun 309 coin içindeki göreli konumu
        for x in opportunities:

            change = x["_jarvis_change"]
            volume = x["_jarvis_volume"]
            spread = x["_jarvis_spread"]

            momentum_rank = pct_rank(
                change_values,
                change
            )

            volume_rank = pct_rank(
                volume_values,
                math.log1p(volume)
            )

            spread_quality = pct_rank(
                spread_values,
                -spread
            )

            bull_score = (
                momentum_rank * 0.60
                + volume_rank * 0.25
                + spread_quality * 0.15
            )

            bear_score = (
                (100.0 - momentum_rank) * 0.60
                + volume_rank * 0.25
                + spread_quality * 0.15
            )

            if change > 0 and bull_score >= 70:
                signal = "AL"
                score = bull_score

            elif change < 0 and bear_score >= 70:
                signal = "SAT"
                score = bear_score

            else:
                signal = "BEKLE"
                score = 50.0 + (
                    abs(momentum_rank - 50.0) * 0.20
                )

            score = max(0.0, min(100.0, score))

            x["jarvisScore"] = round(score, 1)
            x["jarvisSignal"] = signal
            x["momentumScore"] = round(momentum_rank, 1)
            x["volumeScore"] = round(volume_rank, 1)
            x["spreadPercent"] = round(spread, 4)
            x["spreadScore"] = round(spread_quality, 1)
            x["directionScore"] = round(momentum_rank, 1)

            # V10 hızlı radar açıklaması
            reasons = []

            if change > 1.0:
                reasons.append(f"24s momentum güçlü (+{change:.2f}%)")
            elif change < -1.0:
                reasons.append(f"24s momentum negatif ({change:.2f}%)")
            else:
                reasons.append(f"24s hareket sınırlı ({change:+.2f}%)")

            if volume_rank >= 80:
                reasons.append("24s hacim göreli olarak yüksek")
            elif volume_rank <= 20:
                reasons.append("24s hacim göreli olarak düşük")

            if spread_quality >= 80:
                reasons.append("spread kalitesi güçlü")
            elif spread_quality <= 20:
                reasons.append("spread zayıf")

            if signal == "AL":
                reasons.append("göreli momentum AL bölgesinde")
            elif signal == "SAT":
                reasons.append("göreli momentum SAT bölgesinde")
            else:
                reasons.append("net teyit yok → BEKLE")

            x["jarvisReasons"] = reasons

            x.pop("_jarvis_change", None)
            x.pop("_jarvis_volume", None)
            x.pop("_jarvis_spread", None)

        # =================================================
        # 5) EN GÜÇLÜ FIRSATLARI SIRALA
        # =================================================

        buy_opportunities = sorted(
            [
                x for x in opportunities
                if x["jarvisSignal"] == "AL"
            ],
            key=lambda x: float(
                x.get("jarvisScore", 0)
            ),
            reverse=True
        )[:10]

        sell_opportunities = sorted(
            [
                x for x in opportunities
                if x["jarvisSignal"] == "SAT"
            ],
            key=lambda x: float(
                x.get("jarvisScore", 0)
            ),
            reverse=True
        )[:10]

        wait_opportunities = sorted(
            [
                x for x in opportunities
                if x["jarvisSignal"] == "BEKLE"
            ],
            key=lambda x: float(
                x.get("jarvisScore", 0)
            ),
            reverse=True
        )[:10]

        # =================================================
        # 6) META
        # =================================================

        result["_META"] = {
            "exchange": "BINANCE TR",
            "quoteAsset": "TRY",
            "totalSymbols": len(try_symbols),
            "tickerMatched": len(opportunities),
            "scanMode": "REAL_PUBLIC_MARKET_SCAN",

            "jarvisEngine": "RELATIVE_PERCENTILE_V10",

            "buyCount": len([
                x for x in opportunities
                if x["jarvisSignal"] == "AL"
            ]),

            "sellCount": len([
                x for x in opportunities
                if x["jarvisSignal"] == "SAT"
            ]),

            "waitCount": len([
                x for x in opportunities
                if x["jarvisSignal"] == "BEKLE"
            ]),

            "topBuy": buy_opportunities,
            "topSell": sell_opportunities,
            "topWait": wait_opportunities,

            "v10": {
                "enabled": True,
                "detailEndpoint": "/api/analysis/<symbol>",
                "detailCacheSeconds": 4,
                "realTrading": False,
                "features": [
                    "1m",
                    "5m",
                    "orderBook",
                    "largeTrades",
                    "jarvisReasons"
                ]
            }
        }

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "_META": {
                "exchange": "BINANCE TR",
                "quoteAsset": "TRY",
                "totalSymbols": 0,
                "tickerMatched": 0,
                "scanMode": "ERROR"
            },
            "_ERROR": str(e)
        }), 500


# =========================================================
# JARVIS V10 INTELLIGENCE ENGINE
# Seçilen coin için derin analiz:
# 1m / 5m / order book / büyük işlemler
# Gerçek emir göndermez.
# =========================================================

import time as _jarvis_time

JARVIS_DETAIL_CACHE = {}
JARVIS_DETAIL_CACHE_TTL = 4.0


def _jarvis_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _jarvis_get_json(url, params=None, timeout=8):
    response = requests.get(
        url,
        params=params or {},
        timeout=timeout
    )
    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and "data" in data:
        return data.get("data")

    return data


def _jarvis_kline_metrics(rows):
    if not isinstance(rows, list) or len(rows) < 2:
        return {
            "changePercent": 0.0,
            "volume": 0.0,
            "quoteVolume": 0.0,
            "takerBuyQuote": 0.0,
            "bars": 0
        }

    first = rows[0]
    last = rows[-1]

    open_price = _jarvis_float(first[1] if len(first) > 1 else 0)
    close_price = _jarvis_float(last[4] if len(last) > 4 else 0)

    if open_price > 0:
        change = ((close_price - open_price) / open_price) * 100.0
    else:
        change = 0.0

    volume = sum(
        _jarvis_float(row[5] if len(row) > 5 else 0)
        for row in rows
    )

    quote_volume = sum(
        _jarvis_float(row[7] if len(row) > 7 else 0)
        for row in rows
    )

    taker_buy_quote = sum(
        _jarvis_float(row[10] if len(row) > 10 else 0)
        for row in rows
    )

    return {
        "changePercent": round(change, 4),
        "volume": round(volume, 8),
        "quoteVolume": round(quote_volume, 2),
        "takerBuyQuote": round(taker_buy_quote, 2),
        "bars": len(rows)
    }




# =========================================================
# JARVIS RISK / WICK ENGINE V1
# Flash-crash / wick / P&L / SL / TP / trailing altyapısı.
# GERÇEK EMİR GÖNDERMEZ.
# =========================================================

JARVIS_RISK_CONFIG = {
    "stopLossPercent": 3.0,
    "takeProfit1Percent": 3.0,
    "takeProfit2Percent": 5.0,
    "trailingActivationPercent": 2.0,
    "trailingDistancePercent": 1.2,
    "newBuyBlockRisk": 70,
    "tightenStopRisk": 80,
    "criticalRisk": 90,
}

JARVIS_PAPER_POSITIONS = {}


def _jarvis_median(values):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _jarvis_wick_risk(rows, book_pressure=0.0, whale_flow=0.0):
    """Son 1m mumlardan flash-crash/wick riskini 0-100 hesaplar."""
    if not isinstance(rows, list) or len(rows) < 3:
        return {
            "riskScore": 0,
            "riskLevel": "VERİ YETERSİZ",
            "state": "INSUFFICIENT_DATA",
            "wickPercent": 0.0,
            "rangePercent": 0.0,
            "dropPercent": 0.0,
            "recoveryPercent": 0.0,
            "volumeAnomaly": 1.0,
            "reasons": ["Flash-crash hesabı için yeterli 1DK mum yok."],
            "blockNewBuy": False,
            "tightenStop": False,
            "exitCandidate": False,
        }

    candles = []
    for row in rows:
        try:
            o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4]); v = float(row[5])
            if o > 0 and h >= l:
                candles.append((o, h, l, c, v))
        except Exception:
            continue

    if len(candles) < 3:
        return {
            "riskScore": 0, "riskLevel": "VERİ YETERSİZ", "state": "INSUFFICIENT_DATA",
            "wickPercent": 0.0, "rangePercent": 0.0, "dropPercent": 0.0,
            "recoveryPercent": 0.0, "volumeAnomaly": 1.0, "reasons": ["Geçerli mum bulunamadı."],
            "blockNewBuy": False, "tightenStop": False, "exitCandidate": False,
        }

    cur = candles[-1]
    prev = candles[-2]
    o, h, l, c, v = cur
    po, ph, pl, pc, pv = prev
    rng = max(0.0, h - l)
    lower_wick = max(0.0, min(o, c) - l)
    upper_wick = max(0.0, h - max(o, c))
    wick_pct = (lower_wick / rng * 100.0) if rng > 0 else 0.0
    range_pct = (rng / o * 100.0) if o > 0 else 0.0
    drop_pct = ((pc - l) / pc * 100.0) if pc > 0 else 0.0
    recovery_pct = ((c - l) / rng * 100.0) if rng > 0 else 0.0

    recent_volumes = [x[4] for x in candles[:-1][-6:]]
    median_volume = _jarvis_median(recent_volumes)
    volume_anomaly = (v / median_volume) if median_volume > 0 else 1.0

    # Sustained downside: son üç kapanışın ikisi negatif ve son kapanış prev'den aşağı.
    downside_bars = 0
    for a, b in zip(candles[-4:-1], candles[-3:]):
        if b[3] < a[3]:
            downside_bars += 1
    sustained_down = downside_bars >= 2 and c < pc

    score = 0.0
    reasons = []

    # Büyük range + ani düşüş
    if drop_pct >= 1.0:
        score += 22
        reasons.append(f"Ani düşüş {drop_pct:.2f}%")
    elif drop_pct >= 0.5:
        score += 12
        reasons.append(f"Hızlı düşüş {drop_pct:.2f}%")

    if range_pct >= 3.0:
        score += 18
        reasons.append(f"1DK mum aralığı çok geniş ({range_pct:.2f}%)")
    elif range_pct >= 1.5:
        score += 10
        reasons.append(f"1DK mum aralığı yüksek ({range_pct:.2f}%)")

    if wick_pct >= 65:
        score += 15
        reasons.append(f"Uzun alt iğne ({wick_pct:.1f}%)")
    elif wick_pct >= 45:
        score += 8
        reasons.append(f"Belirgin alt iğne ({wick_pct:.1f}%)")

    if volume_anomaly >= 3.0:
        score += 18
        reasons.append(f"Hacim anomalisi {volume_anomaly:.1f}x")
    elif volume_anomaly >= 2.0:
        score += 10
        reasons.append(f"Hacim artışı {volume_anomaly:.1f}x")

    if book_pressure <= -20:
        score += 15
        reasons.append(f"Order book satış baskısı {book_pressure:+.1f}%")
    elif book_pressure <= -10:
        score += 8
        reasons.append(f"Order book zayıf {book_pressure:+.1f}%")

    if whale_flow < -250000:
        score += 12
        reasons.append(f"Büyük para net satış {whale_flow:,.0f} TL")
    elif whale_flow < 0:
        score += 5
        reasons.append("Büyük para akışı satış yönlü")

    # Hızlı toparlanma transient wick riskini düşürür; kalıcı düşüşü artırır.
    if wick_pct >= 45 and recovery_pct >= 70 and c >= po:
        score -= 18
        reasons.append(f"İğne güçlü toparlandı ({recovery_pct:.1f}%)")
        state = "WICK_RECOVERY"
    elif sustained_down:
        score += 15
        reasons.append("Düşüş birden fazla mumda devam ediyor")
        state = "SUSTAINED_DROP"
    elif drop_pct >= 1.0 and recovery_pct < 45:
        score += 10
        reasons.append("İğne sonrası toparlanma zayıf")
        state = "FLASH_DROP"
    elif wick_pct >= 45:
        state = "WICK_WARNING"
    else:
        state = "NORMAL"

    score = max(0, min(100, int(round(score))))
    if score >= 86:
        level = "KRİTİK"
    elif score >= 71:
        level = "ÇOK YÜKSEK"
    elif score >= 51:
        level = "YÜKSEK"
    elif score >= 26:
        level = "ORTA"
    else:
        level = "DÜŞÜK"

    # Tek bir transient wick ile otomatik çıkış üretme.
    exit_candidate = score >= 90 and (
        state == "SUSTAINED_DROP" or
        (state == "FLASH_DROP" and recovery_pct < 45)
    )

    return {
        "riskScore": score,
        "riskLevel": level,
        "state": state,
        "wickPercent": round(wick_pct, 2),
        "upperWickPercent": round((upper_wick / rng * 100.0) if rng > 0 else 0.0, 2),
        "rangePercent": round(range_pct, 2),
        "dropPercent": round(drop_pct, 2),
        "recoveryPercent": round(recovery_pct, 2),
        "volumeAnomaly": round(volume_anomaly, 2),
        "bookPressure": round(book_pressure, 2),
        "whaleFlowTRY": round(whale_flow, 2),
        "reasons": reasons or ["Ani risk sinyali yok."],
        "blockNewBuy": score >= JARVIS_RISK_CONFIG["newBuyBlockRisk"],
        "tightenStop": score >= JARVIS_RISK_CONFIG["tightenStopRisk"],
        "exitCandidate": exit_candidate,
    }


def _jarvis_position_state(entry_price, current_price, quantity, peak_price=None, stop_percent=None):
    entry = _jarvis_float(entry_price)
    current = _jarvis_float(current_price)
    qty = max(0.0, _jarvis_float(quantity))
    if entry <= 0 or current <= 0 or qty <= 0:
        return {"error": "entry_price, current_price ve quantity pozitif olmalı"}

    cost = entry * qty
    value = current * qty
    pnl = value - cost
    pnl_pct = ((current - entry) / entry) * 100.0
    peak = max(entry, _jarvis_float(peak_price, current))
    stop_pct = _jarvis_float(stop_percent, JARVIS_RISK_CONFIG["stopLossPercent"])
    base_stop = entry * (1.0 - stop_pct / 100.0)

    trailing_active = pnl_pct >= JARVIS_RISK_CONFIG["trailingActivationPercent"]
    trailing_stop = peak * (1.0 - JARVIS_RISK_CONFIG["trailingDistancePercent"] / 100.0)
    effective_stop = max(base_stop, trailing_stop if trailing_active else 0.0)

    tp1 = entry * (1.0 + JARVIS_RISK_CONFIG["takeProfit1Percent"] / 100.0)
    tp2 = entry * (1.0 + JARVIS_RISK_CONFIG["takeProfit2Percent"] / 100.0)

    if current <= effective_stop:
        exit_reason = "TRAILING_STOP" if trailing_active and trailing_stop >= base_stop else "STOP_LOSS"
    elif current >= tp2:
        exit_reason = "TAKE_PROFIT_2"
    elif current >= tp1:
        exit_reason = "TAKE_PROFIT_1"
    else:
        exit_reason = "HOLD"

    return {
        "entryPrice": round(entry, 10),
        "currentPrice": round(current, 10),
        "quantity": round(qty, 10),
        "costTRY": round(cost, 2),
        "valueTRY": round(value, 2),
        "unrealizedPnlTRY": round(pnl, 2),
        "unrealizedPnlPercent": round(pnl_pct, 3),
        "peakPrice": round(peak, 10),
        "stopLossPrice": round(base_stop, 10),
        "takeProfit1Price": round(tp1, 10),
        "takeProfit2Price": round(tp2, 10),
        "trailingActive": trailing_active,
        "trailingStopPrice": round(trailing_stop, 10) if trailing_active else None,
        "effectiveStopPrice": round(effective_stop, 10),
        "exitReason": exit_reason,
    }


def _jarvis_risk_comment(risk, position=None):
    if risk.get("riskScore", 0) >= 90:
        return "KRİTİK RİSK — yeni AL engellendi; mevcut pozisyon için çıkış teyidi aranıyor."
    if risk.get("riskScore", 0) >= 80:
        return "ÇOK YÜKSEK RİSK — yeni AL yok; stop sıkılaştırılıyor."
    if risk.get("riskScore", 0) >= 70:
        return "YÜKSEK RİSK — JARVIS yeni AL açmıyor."
    if risk.get("state") == "WICK_RECOVERY":
        return "WICK TOPARLANMASI — sert iğne geri alındı; tek başına çıkış sinyali değil."
    if position and position.get("exitReason") not in (None, "HOLD"):
        return f"POZİSYON ÇIKIŞI — {position['exitReason']} tetiklendi."
    return "Risk normal — olağan stop/take-profit kuralları izleniyor."


@app.post('/api/jarvis/chat')
def jarvis_chat():
    data = request.get_json(silent=True) or {}
    command = str(data.get("command", "")).strip()

    if not command:
        return jsonify({
            "error": "Komut boş",
            "answer": "JARVIS burada. Ne analiz etmemi istiyorsun?"
        }), 400

    text = command.upper()
    aliases = {
        "BITCOIN": "BTC_TRY",
        "BTC": "BTC_TRY",
        "ETHEREUM": "ETH_TRY",
        "ETH": "ETH_TRY",
        "SOLANA": "SOL_TRY",
        "SOL": "SOL_TRY",
        "SAND": "SAND_TRY",
        "DOGE": "DOGE_TRY",
        "DOGECOIN": "DOGE_TRY",
        "XRP": "XRP_TRY",
        "RIPPLE": "XRP_TRY",
        "ADA": "ADA_TRY",
        "CARDANO": "ADA_TRY",
        "AVAX": "AVAX_TRY",
        "AVALANCHE": "AVAX_TRY",
    }

    selected = None
    for name, symbol in aliases.items():
        if name in text:
            selected = symbol
            break

    # -------------------------------------------------
    # GENEL PİYASA KOMUTU
    # Coin adı seçili olsa bile önce genel piyasa niyeti
    # kontrol edilir. Böylece "ADA piyasada ne oluyor?"
    # gibi komutlar yanlışlıkla sadece ADA analizine düşmez.
    # -------------------------------------------------

    market_words = [
        "PIYASA",
        "PİYASA",
        "GENEL DURUM",
        "GENEL PIYASA",
        "GENEL PİYASA",
        "NELER OLUYOR",
        "NE OLUYOR",
        "PİYASADA",
        "PIYASADA",
        "PİYASAYI TARA",
        "PIYASAYI TARA",
        "PİYASAYI ANALİZ",
        "PIYASAYI ANALIZ",
        "FIRSATLAR",
        "FIRSAT VAR MI",
        "COINLER",
        "COINLERDE",
        "TÜM COINLER",
        "TUM COINLER",
        "PİYASA RADARI",
        "PIYASA RADARI",
        "MARKET",
        "MARKET DURUMU"
    ]

    explicit_coin_words = [
        "ANALİZ ET",
        "ANALIZ ET",
        "İNCELE",
        "INCELE",
        "YORUMLA",
        "FİYATINI SÖYLE",
        "FIYATINI SOYLE",
        "FİYAT",
        "FIYAT",
        "KAÇ",
        "KAC",
        "ALINIR MI",
        "SATILIR MI",
        "BEKLEMELI MI",
        "BEKLEMELİ Mİ",
        "NE DURUMDA"
    ]

    is_market_query = any(
        word in text for word in market_words
    )

    is_explicit_coin_query = any(
        word in text for word in explicit_coin_words
    )

    # Coin adı var ama kullanıcı açıkça piyasa soruyorsa
    # genel piyasa motoru kazanır.
    if selected and is_market_query and not is_explicit_coin_query:
        selected = None

    # Coin özel analiz
    if selected:
        try:
            r = requests.get(
                "http://127.0.0.1:5000/api/analysis/" + selected,
                timeout=12
            )
            detail = r.json()

            signal = detail.get("signal", "BEKLE")
            score = float(detail.get("score", 0))
            confirm = detail.get("confirmationPercent", 0)
            intelligence = detail.get("intelligence") or {}

            change1 = float(
                intelligence.get("change1m",
                detail.get("change1m", 0)) or 0
            )
            change5 = float(
                intelligence.get("change5m",
                detail.get("change5m", 0)) or 0
            )

            risk = intelligence.get("riskLevel", "BELİRSİZ")
            direction = intelligence.get(
                "directionState", "YATAY"
            )

            answer = (
                f"{selected.replace('_TRY','')}/TRY için JARVIS kararı "
                f"{signal}. Skor {score:.1f}/100. "
                f"Teyit %{confirm}. "
                f"1 dakikalık değişim {change1:+.2f}%, "
                f"5 dakikalık değişim {change5:+.2f}%. "
                f"Yön durumu {direction}. "
                f"Risk seviyesi {risk}."
            )

            return jsonify({
                "ok": True,
                "type": "coin",
                "symbol": selected,
                "signal": signal,
                "score": round(score, 1),
                "confirmationPercent": confirm,
                "answer": answer,
                "raw": detail
            })

        except Exception as e:
            return jsonify({
                "ok": False,
                "answer": "JARVIS coin analizine ulaşamadı: " + str(e)
            }), 500

    # Genel piyasa analizi
    try:
        r = requests.get(
            "http://127.0.0.1:5000/api/markets",
            timeout=20
        )
        market = r.json()

        rows = []

        for symbol, item in market.items():
            if str(symbol).startswith("_"):
                continue
            if not isinstance(item, dict):
                continue
            if item.get("error"):
                continue

            try:
                score = float(item.get("jarvisScore", 0))
            except Exception:
                score = 0

            signal = item.get("jarvisSignal", "BEKLE")

            try:
                change = float(
                    item.get("priceChangePercent", 0)
                )
            except Exception:
                change = 0

            rows.append({
                "symbol": symbol,
                "signal": signal,
                "score": score,
                "change": change
            })

        buys = sorted(
            [x for x in rows if x["signal"] == "AL"],
            key=lambda x: x["score"],
            reverse=True
        )

        sells = sorted(
            [x for x in rows if x["signal"] == "SAT"],
            key=lambda x: x["score"],
            reverse=True
        )

        total = len(rows)

        if buys and sells:
            market_state = "Piyasada güçlü ayrışma var."
        elif buys:
            market_state = "Piyasada AL tarafı daha baskın."
        elif sells:
            market_state = "Piyasada SAT tarafı daha baskın."
        else:
            market_state = "Piyasada net yön oluşmamış."

        buy_text = ", ".join(
            f"{x['symbol'].replace('_TRY','')} {x['score']:.1f}"
            for x in buys[:3]
        ) or "yok"

        sell_text = ", ".join(
            f"{x['symbol'].replace('_TRY','')} {x['score']:.1f}"
            for x in sells[:3]
        ) or "yok"

        answer = (
            f"JARVIS piyasa taraması tamamlandı. "
            f"{total} coin değerlendirildi. "
            f"{market_state} "
            f"En güçlü AL adayları: {buy_text}. "
            f"En güçlü SAT adayları: {sell_text}. "
            f"Gerçek emir gönderilmiyor; sistem analiz modunda."
        )

        return jsonify({
            "ok": True,
            "type": "market",
            "total": total,
            "buyCount": len(buys),
            "sellCount": len(sells),
            "answer": answer,
            "topBuy": buys[:5],
            "topSell": sells[:5]
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "answer": "JARVIS piyasa taramasına ulaşamadı: " + str(e)
        }), 500


@app.get('/api/analysis/<path:symbol>')
def jarvis_v10_analysis(symbol):

    symbol = str(symbol or "").strip().upper()

    if not symbol:
        return jsonify({"error": "Symbol gerekli"}), 400

    if "_" not in symbol:
        return jsonify({"error": "Geçersiz symbol"}), 400

    now = _jarvis_time.time()

    cached = JARVIS_DETAIL_CACHE.get(symbol)

    if cached and (now - cached["time"]) < JARVIS_DETAIL_CACHE_TTL:
        payload = dict(cached["data"])
        payload["cache"] = True
        return jsonify(payload)

    api_symbol = symbol.replace("_", "")

    try:

        # -------------------------------------------------
        # 1) 1 DAKİKA
        # -------------------------------------------------

        klines_1m = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v1/klines",
            {
                "symbol": api_symbol,
                "interval": "1m",
                "limit": 10
            }
        )

        # -------------------------------------------------
        # 2) 5 DAKİKA
        # -------------------------------------------------

        klines_5m = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v1/klines",
            {
                "symbol": api_symbol,
                "interval": "5m",
                "limit": 6
            }
        )

        # -------------------------------------------------
        # 3) ORDER BOOK
        # -------------------------------------------------

        depth = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/depth",
            {
                "symbol": api_symbol,
                "limit": 20
            }
        )

        if not isinstance(depth, dict):
            depth = {}

        bids = depth.get("bids") or []
        asks = depth.get("asks") or []

        bid_value = sum(
            _jarvis_float(row[0]) * _jarvis_float(row[1])
            for row in bids
            if isinstance(row, (list, tuple)) and len(row) >= 2
        )

        ask_value = sum(
            _jarvis_float(row[0]) * _jarvis_float(row[1])
            for row in asks
            if isinstance(row, (list, tuple)) and len(row) >= 2
        )

        book_total = bid_value + ask_value

        if book_total > 0:
            book_pressure = (
                (bid_value - ask_value) / book_total
            ) * 100.0
        else:
            book_pressure = 0.0

        # -------------------------------------------------
        # 4) SON İŞLEMLER
        # -------------------------------------------------

        trades = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/trades",
            {
                "symbol": api_symbol,
                "limit": 100
            }
        )

        if not isinstance(trades, list):
            trades = []

        large_buy = 0.0
        large_sell = 0.0
        large_count = 0

        total_buy = 0.0
        total_sell = 0.0

        # TRY piyasası için pratik büyük işlem eşiği.
        # Bu "balinanın kim olduğunu" göstermez;
        # sadece olağandışı büyük işlem heuristiğidir.
        whale_threshold = 250000.0

        for trade in trades:

            if not isinstance(trade, dict):
                continue

            price = _jarvis_float(
                trade.get("price", 0)
            )

            qty = _jarvis_float(
                trade.get("qty", 0)
            )

            value = price * qty

            if value <= 0:
                continue

            is_buyer_maker = bool(
                trade.get("isBuyerMaker", False)
            )

            if is_buyer_maker:
                total_sell += value
            else:
                total_buy += value

            if value >= whale_threshold:

                large_count += 1

                if is_buyer_maker:
                    large_sell += value
                else:
                    large_buy += value

        # -------------------------------------------------
        # 5) 1m / 5m METRİKLER
        # -------------------------------------------------

        metric_1m = _jarvis_kline_metrics(klines_1m)
        metric_5m = _jarvis_kline_metrics(klines_5m)

        # -------------------------------------------------
        # 6) V10 SKORU
        # -------------------------------------------------

        score = 50.0

        reasons = []

        change_1m = metric_1m["changePercent"]
        change_5m = metric_5m["changePercent"]

        score += max(-15.0, min(15.0, change_1m * 5.0))
        score += max(-15.0, min(15.0, change_5m * 3.0))

        pressure_bonus = max(
            -15.0,
            min(15.0, book_pressure * 0.20)
        )

        score += pressure_bonus

        whale_flow = large_buy - large_sell

        # -------------------------------------------------
        # 6B) FLASH CRASH / WICK RİSKİ
        # -------------------------------------------------
        risk_engine = _jarvis_wick_risk(
            klines_1m,
            book_pressure=book_pressure,
            whale_flow=whale_flow
        )

        if whale_flow > 0:
            score += min(15.0, whale_flow / 1000000.0 * 5.0)
        elif whale_flow < 0:
            score += max(-15.0, whale_flow / 1000000.0 * 5.0)

        # -------------------------------------------------
        # 7A) JARVIS YORUM MOTORU
        # -------------------------------------------------

        if change_1m > 0 and change_5m > 0:
            direction_state = "YUKSELIS_TEYIT"
        elif change_1m < 0 and change_5m < 0:
            direction_state = "DUSUS_TEYIT"
        elif change_1m > 0 and change_5m < 0:
            direction_state = "KISA_VADE_ZITLIK"
        elif change_1m < 0 and change_5m > 0:
            direction_state = "TOPARLANMA_IHTIMALI"
        else:
            direction_state = "YATAY"

        if book_pressure >= 15:
            book_state = "BUY_PRESSURE"
        elif book_pressure <= -15:
            book_state = "SELL_PRESSURE"
        else:
            book_state = "BALANCED"

        if whale_flow > 0:
            money_state = "BUY_PRESSURE"
        elif whale_flow < 0:
            money_state = "SELL_PRESSURE"
        else:
            money_state = "NO_CLEAR_FLOW"

        confirmations = 0

        if change_1m > 0 and change_5m > 0:
            confirmations += 1
        if change_1m < 0 and change_5m < 0:
            confirmations += 1
        if book_pressure >= 15 or book_pressure <= -15:
            confirmations += 1
        if whale_flow != 0:
            confirmations += 1

        if confirmations >= 3:
            confirmation_text = "Güçlü teyit"
        elif confirmations == 2:
            confirmation_text = "Kısmi teyit"
        elif confirmations == 1:
            confirmation_text = "Zayıf teyit"
        else:
            confirmation_text = "Teyit yok"

        if abs(change_1m) >= 1.5 or abs(change_5m) >= 3.0:
            risk_level = "YÜKSEK"
        elif abs(change_1m) >= 0.75 or abs(change_5m) >= 1.5:
            risk_level = "ORTA"
        else:
            risk_level = "DÜŞÜK"

        if direction_state == "YUKSELIS_TEYIT" and book_state == "BUY_PRESSURE":
            comment = "AL — 1DK ve 5DK yükselişi teyit ediyor; emir defteri alış baskılı."
        elif direction_state == "DUSUS_TEYIT" and book_state == "SELL_PRESSURE":
            comment = "SAT — 1DK ve 5DK düşüşü teyit ediyor; emir defteri satış baskılı."
        elif direction_state == "KISA_VADE_ZITLIK":
            comment = "BEKLE — 1DK ve 5DK farklı yön gösteriyor; net trend teyidi yok."
        elif direction_state == "TOPARLANMA_IHTIMALI":
            comment = "BEKLE — 1DK toparlanıyor ancak 5DK trendi henüz teyit etmiyor."
        elif book_state == "BUY_PRESSURE" and money_state != "BUY_PRESSURE":
            comment = "BEKLE — emir defteri alış baskılı fakat büyük para akışı teyit etmiyor."
        elif book_state == "SELL_PRESSURE" and money_state != "SELL_PRESSURE":
            comment = "BEKLE — emir defteri satış baskılı fakat düşüş teyidi zayıf."
        else:
            comment = "BEKLE — mevcut veriler güçlü AL veya SAT teyidi oluşturmuyor."

        # -------------------------------------------------
        # 7) NEDENLER
        # -------------------------------------------------

        if change_1m >= 0.30:
            reasons.append(
                f"1DK momentum güçlü (+{change_1m:.2f}%)"
            )
        elif change_1m <= -0.30:
            reasons.append(
                f"1DK momentum zayıf ({change_1m:.2f}%)"
            )
        else:
            reasons.append(
                f"1DK hareket sınırlı ({change_1m:+.2f}%)"
            )

        if change_5m >= 0.50:
            reasons.append(
                f"5DK trend pozitif (+{change_5m:.2f}%)"
            )
        elif change_5m <= -0.50:
            reasons.append(
                f"5DK trend negatif ({change_5m:.2f}%)"
            )

        if book_pressure >= 15:
            reasons.append(
                f"Emir defteri alış baskılı ({book_pressure:+.1f}%)"
            )
        elif book_pressure <= -15:
            reasons.append(
                f"Emir defteri satış baskılı ({book_pressure:+.1f}%)"
            )
        else:
            reasons.append(
                f"Emir defteri dengeli ({book_pressure:+.1f}%)"
            )

        if large_buy > large_sell and large_buy > 0:
            reasons.append(
                f"Büyük işlemlerde alış üstün "
                f"({large_buy:,.0f} TL)"
            )
        elif large_sell > large_buy and large_sell > 0:
            reasons.append(
                f"Büyük işlemlerde satış üstün "
                f"({large_sell:,.0f} TL)"
            )
        else:
            reasons.append("Büyük işlem akışı belirgin değil")

        score = max(0.0, min(100.0, score))

        if score >= 70:
            signal = "AL"
        elif score <= 30:
            signal = "SAT"
        else:
            signal = "BEKLE"

        # -------------------------------------------------
        # 8) TEK JARVIS KARARI
        # Radar motoru ana karar kaynağıdır.
        # V10 yalnızca derin teyit bilgisi üretir.
        # Böylece radar ve V10 farklı AL/SAT/BEKLE vermez.
        # -------------------------------------------------

        canonical_signal = signal
        canonical_score = score

        try:
            radar_response = requests.get(
                "http://127.0.0.1:5000/api/markets",
                params={"symbols": symbol},
                timeout=8
            )

            if radar_response.ok:
                radar_data = radar_response.json()
                radar_item = radar_data.get(symbol, {})

                if not radar_item.get("error"):
                    canonical_signal = radar_item.get(
                        "jarvisSignal",
                        canonical_signal
                    )

                    canonical_score = float(
                        radar_item.get(
                            "jarvisScore",
                            canonical_score
                        )
                    )
        except Exception:
            pass

        canonical_score = max(
            0.0,
            min(100.0, canonical_score)
        )

        signal = canonical_signal
        score = canonical_score

        # Derin analiz artık ana kararı değiştirmez.
        # Sadece kararın ne kadar teyit edildiğini açıklar.
        if signal == "AL":
            if confirmation_text == "Güçlü teyit":
                comment = (
                    "AL — JARVIS ana radar kararı AL; "
                    "derin analiz güçlü teyit veriyor."
                )
            elif confirmation_text == "Kısmi teyit":
                comment = (
                    "AL — JARVIS ana radar kararı AL; "
                    "derin analiz kısmi teyit veriyor."
                )
            else:
                comment = (
                    "AL — JARVIS ana radar kararı AL; "
                    "kısa vadeli derin teyit henüz güçlü değil."
                )

        elif signal == "SAT":
            if confirmation_text == "Güçlü teyit":
                comment = (
                    "SAT — JARVIS ana radar kararı SAT; "
                    "derin analiz güçlü teyit veriyor."
                )
            elif confirmation_text == "Kısmi teyit":
                comment = (
                    "SAT — JARVIS ana radar kararı SAT; "
                    "derin analiz kısmi teyit veriyor."
                )
            else:
                comment = (
                    "SAT — JARVIS ana radar kararı SAT; "
                    "kısa vadeli derin teyit henüz güçlü değil."
                )

        else:
            comment = (
                "BEKLE — JARVIS ana radarında güçlü AL/SAT "
                "kararı oluşmadı."
            )

        # -------------------------------------------------
        # TEK JARVIS KARAR + TEYİT YÜZDESİ
        # -------------------------------------------------

        confirmation_percent = min(
            100,
            int(round((confirmations / 3.0) * 100))
        )

        # Büyük para akışı varsa ek teyit sağlar.
        if whale_flow != 0:
            confirmation_percent = min(
                100,
                confirmation_percent + 15
            )

        payload = {
            "symbol": symbol,
            "engine": "JARVIS_V10_INTELLIGENCE",

            "signal": signal,
            "score": round(score, 1),
            "decisionEngine": "UNIFIED_RADAR_V10",
            "canonicalSignal": signal,
            "canonicalScore": round(score, 1),
            "confirmationPercent": confirmation_percent,
            "risk": risk_engine,
            "riskComment": _jarvis_risk_comment(risk_engine),

            "timeframes": {
                "1m": metric_1m,
                "5m": metric_5m
            },

            "orderBook": {
                "bidValueTRY": round(bid_value, 2),
                "askValueTRY": round(ask_value, 2),
                "pressurePercent": round(book_pressure, 2),
                "bidLevels": len(bids),
                "askLevels": len(asks)
            },

            "largeTrades": {
                "thresholdTRY": whale_threshold,
                "count": large_count,
                "largeBuyTRY": round(large_buy, 2),
                "largeSellTRY": round(large_sell, 2),
                "netFlowTRY": round(whale_flow, 2),
                "totalBuyTRY": round(total_buy, 2),
                "totalSellTRY": round(total_sell, 2)
            },

            "reasons": reasons,

            "jarvisComment": comment,

            "intelligence": {
                "direction": direction_state,
                "confirmationPercent": confirmation_percent,
                "moneyFlow": money_state,
                "orderBook": book_state,
                "confirmations": confirmations,
                "confirmationText": confirmation_text,
                "riskLevel": risk_level,
                "flashRiskLevel": risk_engine["riskLevel"],
                "flashRiskScore": risk_engine["riskScore"],
                "flashState": risk_engine["state"]
            },

            "generatedAt": int(_jarvis_time.time() * 1000),
            "cache": False
        }

        JARVIS_DETAIL_CACHE[symbol] = {
            "time": now,
            "data": payload
        }

        return jsonify(payload)

    except Exception as e:

        return jsonify({
            "symbol": symbol,
            "engine": "JARVIS_V10_INTELLIGENCE",
            "signal": "BEKLE",
            "score": 50.0,
            "error": str(e)
        }), 502


@app.get('/api/risk/<path:symbol>')
def jarvis_risk_analysis(symbol):
    """Bağımsız Flash Crash/Wick risk endpoint'i. Gerçek emir göndermez."""
    symbol = str(symbol or '').strip().upper()
    if not symbol or '_' not in symbol:
        return jsonify({"error": "Geçersiz symbol"}), 400
    api_symbol = symbol.replace('_', '')
    try:
        klines = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v1/klines",
            {"symbol": api_symbol, "interval": "1m", "limit": 10}
        )
        depth = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/depth",
            {"symbol": api_symbol, "limit": 20}
        )
        bids = depth.get('bids') or [] if isinstance(depth, dict) else []
        asks = depth.get('asks') or [] if isinstance(depth, dict) else []
        bid_value = sum(_jarvis_float(x[0])*_jarvis_float(x[1]) for x in bids if isinstance(x,(list,tuple)) and len(x)>=2)
        ask_value = sum(_jarvis_float(x[0])*_jarvis_float(x[1]) for x in asks if isinstance(x,(list,tuple)) and len(x)>=2)
        total = bid_value + ask_value
        pressure = ((bid_value-ask_value)/total*100.0) if total else 0.0
        trades = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/trades",
            {"symbol": api_symbol, "limit": 100}
        )
        large_buy = large_sell = 0.0
        for tr in trades if isinstance(trades,list) else []:
            value = _jarvis_float(tr.get('price')) * _jarvis_float(tr.get('qty'))
            if value >= 250000:
                if bool(tr.get('isBuyerMaker',False)):
                    large_sell += value
                else:
                    large_buy += value
        risk = _jarvis_wick_risk(klines, pressure, large_buy-large_sell)
        risk['symbol'] = symbol
        risk['comment'] = _jarvis_risk_comment(risk)
        risk['generatedAt'] = int(_jarvis_time.time()*1000)
        return jsonify(risk)
    except Exception as e:
        return jsonify({"symbol": symbol, "error": str(e)}), 502


@app.post('/api/paper/position')
def jarvis_paper_position():
    """Kağıt pozisyonu açar/günceller. Gerçek emir göndermez."""
    body = request.get_json(silent=True) or {}
    symbol = str(body.get('symbol','')).strip().upper()
    entry = _jarvis_float(body.get('entryPrice'))
    qty = _jarvis_float(body.get('quantity'))
    if not symbol or entry <= 0 or qty <= 0:
        return jsonify({"error":"symbol, entryPrice ve quantity gerekli"}), 400
    JARVIS_PAPER_POSITIONS[symbol] = {
        "symbol": symbol,
        "entryPrice": entry,
        "quantity": qty,
        "peakPrice": entry,
        "createdAt": int(_jarvis_time.time()*1000)
    }
    return jsonify({"ok":True,"mode":"PAPER","position":JARVIS_PAPER_POSITIONS[symbol]})


@app.get('/api/paper/position/<path:symbol>')
def jarvis_paper_position_state(symbol):
    symbol = str(symbol or '').strip().upper()
    position = JARVIS_PAPER_POSITIONS.get(symbol)
    if not position:
        return jsonify({"ok":True,"mode":"PAPER","position":None})
    try:
        ticker = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/ticker/price",
            {"symbol": symbol.replace('_','')}
        )
        current = _jarvis_float(ticker.get('price') if isinstance(ticker,dict) else 0)
        position['peakPrice'] = max(position.get('peakPrice',position['entryPrice']), current)
        state = _jarvis_position_state(position['entryPrice'], current, position['quantity'], position['peakPrice'])
        risk = None
        try:
            rr = requests.get(f"http://127.0.0.1:5000/api/risk/{symbol}", timeout=8)
            if rr.ok: risk = rr.json()
        except Exception:
            pass
        return jsonify({"ok":True,"mode":"PAPER","position":position,"state":state,"risk":risk})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 502


@app.delete('/api/paper/position/<path:symbol>')
def jarvis_paper_close(symbol):
    symbol = str(symbol or '').strip().upper()
    existed = JARVIS_PAPER_POSITIONS.pop(symbol, None)
    return jsonify({"ok":True,"mode":"PAPER","closed":bool(existed),"position":existed})



# =========================================================
# JARVIS PAPER AUTO TRADER V1
# Tamamen SANAL portföy. Binance'e emir göndermez.
# Amaç: JARVIS karar motorunu canlı piyasa verisiyle test etmek.
# =========================================================
import threading as _paper_threading
import json as _paper_json

JARVIS_PAPER_CONFIG = {
    "enabled": True,
    "startingTRY": 100000.0,
    "maxPositions": 5,
    "positionPercent": 10.0,
    "minBuyScore": 85.0,
    "minSellScore": 80.0,
    "maxRiskScore": 70.0,
    "cooldownSeconds": 180,
    "scanSeconds": 15,
}

JARVIS_PAPER_ACCOUNT = {
    "cashTRY": float(JARVIS_PAPER_CONFIG["startingTRY"]),
    "equityTRY": float(JARVIS_PAPER_CONFIG["startingTRY"]),
    "realizedPnLTRY": 0.0,
    "unrealizedPnLTRY": 0.0,
    "tradeCount": 0,
    "winCount": 0,
    "lossCount": 0,
    "startedAt": int(_jarvis_time.time()*1000),
}
JARVIS_PAPER_TRADES = []
JARVIS_PAPER_COOLDOWN = {}
JARVIS_PAPER_AUTO_STARTED = False
JARVIS_PAPER_LOCK = _paper_threading.RLock()


def _paper_now():
    return int(_jarvis_time.time()*1000)


def _paper_price(symbol):
    try:
        data = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/ticker/price",
            {"symbol": str(symbol).replace('_','').upper()},
            timeout=6
        )
        return _jarvis_float(data.get('price') if isinstance(data,dict) else 0)
    except Exception:
        return 0.0


def _paper_risk(symbol):
    try:
        api_symbol = str(symbol).upper().replace('_','')
        klines = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v1/klines",
            {"symbol":api_symbol,"interval":"1m","limit":10}, timeout=7
        )
        depth = _jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/depth",
            {"symbol":api_symbol,"limit":20}, timeout=7
        )
        bids = depth.get('bids') or [] if isinstance(depth,dict) else []
        asks = depth.get('asks') or [] if isinstance(depth,dict) else []
        bv=sum(_jarvis_float(x[0])*_jarvis_float(x[1]) for x in bids if isinstance(x,(list,tuple)) and len(x)>=2)
        av=sum(_jarvis_float(x[0])*_jarvis_float(x[1]) for x in asks if isinstance(x,(list,tuple)) and len(x)>=2)
        total=bv+av
        pressure=((bv-av)/total*100.0) if total else 0.0
        trades=_jarvis_get_json(
            f"{MARKET_BASE_URL}/api/v3/trades",
            {"symbol":api_symbol,"limit":100}, timeout=7
        )
        lb=ls=0.0
        for tr in trades if isinstance(trades,list) else []:
            value=_jarvis_float(tr.get('price'))*_jarvis_float(tr.get('qty'))
            if value>=250000:
                if bool(tr.get('isBuyerMaker',False)): ls+=value
                else: lb+=value
        return _jarvis_wick_risk(klines,pressure,lb-ls)
    except Exception:
        return {"score":0,"riskScore":0,"riskLevel":"DÜŞÜK","blockNewBuy":False,"exitCandidate":False}


def _paper_scan():
    try:
        r=requests.get('http://127.0.0.1:5000/api/markets',timeout=35,headers={'Cache-Control':'no-cache'})
        if not r.ok: return None
        return r.json()
    except Exception:
        return None


def _paper_buy(symbol, score, risk_score, reason):
    symbol=str(symbol).upper()
    with JARVIS_PAPER_LOCK:
        if symbol in JARVIS_PAPER_POSITIONS: return False
        if len(JARVIS_PAPER_POSITIONS)>=JARVIS_PAPER_CONFIG['maxPositions']: return False
        now=_paper_now()
        if now-JARVIS_PAPER_COOLDOWN.get(symbol,0)<JARVIS_PAPER_CONFIG['cooldownSeconds']*1000: return False
        price=_paper_price(symbol)
        if price<=0: return False
        allocation=min(JARVIS_PAPER_ACCOUNT['cashTRY'],JARVIS_PAPER_ACCOUNT['cashTRY']*JARVIS_PAPER_CONFIG['positionPercent']/100.0)
        if allocation<10: return False
        qty=allocation/price
        JARVIS_PAPER_POSITIONS[symbol]={
            'symbol':symbol,'entryPrice':price,'quantity':qty,'peakPrice':price,
            'entryScore':round(float(score),1),'entryRisk':round(float(risk_score),1),
            'createdAt':now,'reason':reason,'mode':'PAPER_AUTO'
        }
        JARVIS_PAPER_ACCOUNT['cashTRY']-=allocation
        JARVIS_PAPER_ACCOUNT['tradeCount']+=1
        trade={'time':now,'side':'BUY','symbol':symbol,'price':price,'quantity':qty,'valueTRY':allocation,'score':score,'riskScore':risk_score,'reason':reason,'mode':'PAPER_AUTO'}
        JARVIS_PAPER_TRADES.append(trade)
        JARVIS_PAPER_COOLDOWN[symbol]=now
        return True


def _paper_sell(symbol, reason, score=0, risk_score=0):
    symbol=str(symbol).upper()
    with JARVIS_PAPER_LOCK:
        pos=JARVIS_PAPER_POSITIONS.get(symbol)
        if not pos: return False
        price=_paper_price(symbol)
        if price<=0: return False
        entry=pos['entryPrice']; qty=pos['quantity']; value=price*qty
        pnl=(price-entry)*qty
        pnl_pct=((price-entry)/entry*100.0) if entry else 0.0
        JARVIS_PAPER_ACCOUNT['cashTRY']+=value
        JARVIS_PAPER_ACCOUNT['realizedPnLTRY']+=pnl
        if pnl>=0: JARVIS_PAPER_ACCOUNT['winCount']+=1
        else: JARVIS_PAPER_ACCOUNT['lossCount']+=1
        JARVIS_PAPER_TRADES.append({'time':_paper_now(),'side':'SELL','symbol':symbol,'price':price,'quantity':qty,'valueTRY':value,'pnlTRY':pnl,'pnlPercent':pnl_pct,'score':score,'riskScore':risk_score,'reason':reason,'mode':'PAPER_AUTO'})
        JARVIS_PAPER_COOLDOWN[symbol]=_paper_now()
        del JARVIS_PAPER_POSITIONS[symbol]
        return True


def _paper_manage_positions():
    with JARVIS_PAPER_LOCK:
        symbols=list(JARVIS_PAPER_POSITIONS.keys())
    unreal=0.0
    for symbol in symbols:
        with JARVIS_PAPER_LOCK:
            pos=JARVIS_PAPER_POSITIONS.get(symbol)
        if not pos: continue
        price=_paper_price(symbol)
        if price<=0: continue
        pos['peakPrice']=max(pos.get('peakPrice',pos['entryPrice']),price)
        state=_jarvis_position_state(pos['entryPrice'],price,pos['quantity'],pos['peakPrice'])
        risk=_paper_risk(symbol)
        unreal+=(price-pos['entryPrice'])*pos['quantity']
        should_exit=False; reason=''
        if state.get('exitReason') not in (None,'HOLD'):
            should_exit=True; reason=state.get('exitReason')
        elif risk.get('exitCandidate'):
            should_exit=True; reason='RISK_EXIT'
        else:
            # JARVIS risk engine can tighten the exit without immediately forcing one.
            if risk.get('tightenStop') and price < pos['peakPrice']:
                trailing=(pos['peakPrice']*(1.0-JARVIS_RISK_CONFIG['trailingDistancePercent']/100.0))
                if price<=trailing:
                    should_exit=True; reason='RISK_TIGHTENED_TRAIL'
        if should_exit:
            _paper_sell(symbol,reason,0,float(risk.get('score',risk.get('riskScore',0))))
    with JARVIS_PAPER_LOCK:
        JARVIS_PAPER_ACCOUNT['unrealizedPnLTRY']=unreal
        JARVIS_PAPER_ACCOUNT['equityTRY']=JARVIS_PAPER_ACCOUNT['cashTRY']+sum(_paper_price(s)*p['quantity'] for s,p in JARVIS_PAPER_POSITIONS.items())


def _paper_auto_loop():
    global JARVIS_PAPER_AUTO_STARTED
    JARVIS_PAPER_AUTO_STARTED=True
    while True:
        try:
            _paper_manage_positions()
            data=_paper_scan()
            if isinstance(data,dict):
                meta=data.get('_META') or {}
                buys=meta.get('topBuy') or []
                sells=meta.get('topSell') or []
                for item in buys[:5]:
                    symbol=str(item.get('symbol','')).upper()
                    score=_jarvis_float(item.get('jarvisScore'))
                    change=_jarvis_float(item.get('_jarvis_change',item.get('priceChangePercent',0)))
                    if not symbol or score<JARVIS_PAPER_CONFIG['minBuyScore'] or change<=0: continue
                    risk=_paper_risk(symbol)
                    rs=_jarvis_float(risk.get('score',risk.get('riskScore',0)))
                    if rs>JARVIS_PAPER_CONFIG['maxRiskScore'] or risk.get('blockNewBuy'): continue
                    _paper_buy(symbol,score,rs,'UNIFIED_RADAR_AL')
                for item in sells[:10]:
                    symbol=str(item.get('symbol','')).upper()
                    score=_jarvis_float(item.get('jarvisScore'))
                    if symbol in JARVIS_PAPER_POSITIONS and score>=JARVIS_PAPER_CONFIG['minSellScore']:
                        _paper_sell(symbol,'UNIFIED_RADAR_SAT',score,0)
        except Exception as exc:
            print('JARVIS PAPER AUTO:',exc)
        _jarvis_time.sleep(JARVIS_PAPER_CONFIG['scanSeconds'])


@app.get('/api/paper/account')
def jarvis_paper_account():
    with JARVIS_PAPER_LOCK:
        return jsonify({
            'ok':True,'mode':'PAPER_AUTO','enabled':JARVIS_PAPER_CONFIG['enabled'],
            'config':JARVIS_PAPER_CONFIG,'account':JARVIS_PAPER_ACCOUNT,
            'positions':list(JARVIS_PAPER_POSITIONS.values()),
            'trades':JARVIS_PAPER_TRADES[-100:]
        })


@app.get('/api/paper/trades')
def jarvis_paper_trades():
    with JARVIS_PAPER_LOCK:
        return jsonify({'ok':True,'mode':'PAPER_AUTO','trades':JARVIS_PAPER_TRADES[-200:]})


@app.post('/api/paper/reset')
def jarvis_paper_reset():
    with JARVIS_PAPER_LOCK:
        JARVIS_PAPER_POSITIONS.clear()
        JARVIS_PAPER_TRADES.clear()
        JARVIS_PAPER_COOLDOWN.clear()
        JARVIS_PAPER_ACCOUNT.update({'cashTRY':float(JARVIS_PAPER_CONFIG['startingTRY']),'equityTRY':float(JARVIS_PAPER_CONFIG['startingTRY']),'realizedPnLTRY':0.0,'unrealizedPnLTRY':0.0,'tradeCount':0,'winCount':0,'lossCount':0,'startedAt':_paper_now()})
    return jsonify({'ok':True,'mode':'PAPER_AUTO','account':JARVIS_PAPER_ACCOUNT})


if JARVIS_PAPER_CONFIG['enabled']:
    _paper_thread=_paper_threading.Thread(target=_paper_auto_loop,daemon=True,name='JARVIS-PAPER-AUTO')
    _paper_thread.start()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT','5000')), debug=False)
