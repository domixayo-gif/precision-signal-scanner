import os, time, json, math, logging, threading, traceback, requests, queue, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from iqoptionapi.stable_api import IQ_Option

# MOMENTUM 10 — PRACTICE ONLY. Manual scans NEVER place trades.
BOT_NAME = "Momentum 10 IQ Option Scanner"
AUTO_TRADING_ALLOWED = True
STAKE, EXPIRY_MINUTES, CANDLE_SECONDS, LOOKBACK_CANDLES = 1.0, 1, 60, 50
# Original strategy filters unchanged
MOMENTUM_PERIOD, EXTREME_PERCENTILE, MIN_TURN_DISTANCE = 10, 0.08, 0.06
DAILY_PROFIT_TARGET, DAILY_MAX_LOSS = 5.0, -3.0
MAX_RESOLVED_TRADES, MAX_CONSECUTIVE_LOSSES = 20, 5
SCAN_INTERVAL_SECONDS, REQUEST_PAUSE_SECONDS, RECONNECT_SECONDS = 60, 0.12, 30
HEARTBEAT_SECONDS, RESULT_TIMEOUT_SECONDS = 300, EXPIRY_MINUTES * 60 + 45
# IQ Option candle calls can be slow; allow a longer wait before abandoning a batch.
MAX_SYMBOLS_PER_SCAN, CANDLE_REQUEST_TIMEOUT, RESULT_CHECK_TIMEOUT = 40, 15, 8
TOP_SIGNALS, PROGRESS_EVERY, MAX_SIGNAL_AGE_SECONDS = 3, 10, 180
STATE_FILE = "momentum_state.json"
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
GITHUB_TOKEN, GITHUB_REPOSITORY = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try: IQ_Option._IQ_Option__get_digital_open = lambda self: None
except Exception: logging.warning("Could not disable digital-open discovery.")

IQ, RUNNING, AUTO_MODE = None, True, False
STATE_LOCK, SAVE_LOCK = threading.RLock(), threading.Lock()
CLIENT_LOCKS_GUARD = threading.Lock()
CLIENT_LOCKS = {}
RESULT_LOOKUP_LOCK = threading.Lock()
RESULT_LOOKUPS_IN_FLIGHT = set()
TELEGRAM_OFFSET, OPEN_TRADES, LAST_SIGNAL_BY_ASSET = 0, {}, {}
MANUAL_SCAN_REQUESTED, SCAN_IN_PROGRESS = False, False
SCAN_RESULTS_QUEUE = queue.Queue()
LAST_HEARTBEAT = LAST_SCAN_STARTED = LAST_SCAN_FINISHED = 0
LAST_SCAN_CANDIDATES = LAST_SCAN_SCANNED = LAST_SCAN_TOTAL = LAST_ASSET_COUNT = ASSET_CURSOR = 0
LAST_SCAN_TIMED_OUT, LAST_SCAN_ERROR, RECONCILE_IN_PROGRESS = False, "", False
DEFAULT_STATE = {"date":"", "daily_pl":0.0, "resolved":0, "wins":0, "losses":0,
 "consecutive_losses":0, "signals":0, "executed":0, "blocked":0, "history":[],
 "open_trades":{}, "last_signal_candles":{}, "last_update_id":0, "auto_enabled":False, "stop_reason":"",
 "tracked_signals":{}, "last_scan_rejections":{}, "last_scan":"Never"}
STATE = dict(DEFAULT_STATE)


def utc_now(): return datetime.now(timezone.utc)
def money(v): return f"${float(v):.2f}"


def client_api_lock(client):
    """Give each IQ client its own lock so concurrent calls cannot corrupt replies."""
    key=id(client)
    with CLIENT_LOCKS_GUARD:
        lock=CLIENT_LOCKS.get(key)
        if lock is None:
            lock=threading.RLock(); CLIENT_LOCKS[key]=lock
        return lock
def safe_float(v, default=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception: return default


def telegram_send(message, keyboard=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("Telegram unavailable: %s", message); return False
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":str(message)[:4000],"disable_web_page_preview":True}
    if keyboard: payload["reply_markup"]={"keyboard":[[{"text":"START AUTO"},{"text":"STOP AUTO"}],
      [{"text":"SCAN MARKET"},{"text":"STATUS"}]],"resize_keyboard":True}
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=12)
        r.raise_for_status(); return True
    except Exception as e: logging.warning("Telegram send failed: %s",e); return False


def save_state():
    with STATE_LOCK:
        STATE["open_trades"]={str(k):dict(v) for k,v in OPEN_TRADES.items()}
        STATE["last_signal_candles"]=dict(LAST_SIGNAL_BY_ASSET)
        snap=dict(STATE); snap["history"]=list(STATE.get("history",[]))
        snap["open_trades"]=dict(STATE["open_trades"]); snap["last_signal_candles"]=dict(STATE["last_signal_candles"])
    try:
        content=json.dumps(snap,indent=2).encode()
        with open(STATE_FILE,"wb") as f: f.write(content)
    except Exception as e: logging.warning("Local state save failed: %s",e); return
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return
    with SAVE_LOCK:
        try:
            import base64
            url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
            headers={"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
            old=requests.get(url,headers=headers,params={"ref":GITHUB_REF_NAME},timeout=8)
            body={"message":"Update Momentum 10 scanner state","branch":GITHUB_REF_NAME,"content":base64.b64encode(content).decode()}
            if old.status_code==200: body["sha"]=old.json().get("sha")
            elif old.status_code!=404: logging.warning("GitHub state lookup failed: %s",old.status_code); return
            res=requests.put(url,headers=headers,json=body,timeout=12)
            if res.status_code not in (200,201): logging.warning("GitHub state update failed: %s %s",res.status_code,res.text[:180])
        except Exception as e: logging.warning("GitHub state save failed: %s",e)


def load_state():
    global STATE,OPEN_TRADES,LAST_SIGNAL_BY_ASSET,TELEGRAM_OFFSET
    try:
        with open(STATE_FILE,encoding="utf-8") as f:
            d=json.load(f)
            if isinstance(d,dict): STATE.update(d)
    except FileNotFoundError: pass
    except Exception as e: logging.warning("State load failed: %s",e)
    for k,v in DEFAULT_STATE.items():
        if k not in STATE: STATE[k]=v.copy() if isinstance(v,(dict,list)) else v
    if not isinstance(STATE.get("history"),list): STATE["history"]=[]
    if not isinstance(STATE.get("open_trades"),dict): STATE["open_trades"]={}
    if not isinstance(STATE.get("last_signal_candles"),dict): STATE["last_signal_candles"]={}
    if not isinstance(STATE.get("tracked_signals"),dict): STATE["tracked_signals"]={}
    if not isinstance(STATE.get("last_scan_rejections"),dict): STATE["last_scan_rejections"]={}
    pending=STATE.get("pending_order_id"); settled={str(x) for x in STATE.get("settled_order_ids",[])}
    if pending and str(pending) not in settled and str(pending) not in STATE["open_trades"]:
        started=time.time()
        try: started=datetime.fromisoformat(str(STATE.get("pending_started")).replace("Z","+00:00")).timestamp()
        except Exception: pass
        STATE["open_trades"][str(pending)]={"signal_id":f"RECOVERED-{pending}","asset":str(STATE.get("pending_asset") or "Unknown"),
          "direction":str(STATE.get("pending_direction") or "unknown"),"stake":STAKE,"score":0,"started":started,"recovered":True}
    OPEN_TRADES={str(k):dict(v) for k,v in STATE["open_trades"].items() if isinstance(v,dict)}
    LAST_SIGNAL_BY_ASSET={str(k):str(v) for k,v in STATE["last_signal_candles"].items()}
    try: TELEGRAM_OFFSET=int(STATE.get("last_update_id",0))+1
    except Exception: TELEGRAM_OFFSET=0
    today=utc_now().strftime("%Y-%m-%d")
    if STATE.get("date")!=today:
        STATE.update({"date":today,"daily_pl":0.0,"resolved":0,"wins":0,"losses":0,"consecutive_losses":0,
          "signals":0,"executed":0,"blocked":0,"history":[],"stop_reason":""})
    # Never restore AUTO while any broker order remains unresolved.
    if OPEN_TRADES or STATE.get("unknown_order_request"):
        STATE["auto_enabled"]=False
        STATE["stop_reason"]="Unresolved broker order(s); reconcile and verify before AUTO"
        logging.error("AUTO forced OFF at startup: %d unresolved trade(s); unknown order request=%s.",len(OPEN_TRADES),bool(STATE.get("unknown_order_request")))
    save_state()


def daily_limit_reason():
    with STATE_LOCK:
        if safe_float(STATE.get("daily_pl"))>=DAILY_PROFIT_TARGET: return "Daily profit target reached."
        if safe_float(STATE.get("daily_pl"))<=DAILY_MAX_LOSS: return "Daily loss limit reached."
        if int(STATE.get("resolved",0))>=MAX_RESOLVED_TRADES: return "Maximum resolved trades reached."
        if int(STATE.get("consecutive_losses",0))>=MAX_CONSECUTIVE_LOSSES: return "Maximum consecutive losses reached."
    return ""


def set_auto(enabled,reason=""):
    global AUTO_MODE
    with STATE_LOCK: AUTO_MODE=bool(enabled); STATE["auto_enabled"]=bool(enabled); STATE["stop_reason"]=reason or ""
    save_state()


def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD: raise RuntimeError("Missing IQ_EMAIL or IQ_PASSWORD secret.")
    logging.info("Connecting to IQ Option..."); client=IQ_Option(IQ_EMAIL,IQ_PASSWORD); result=client.connect()
    if result is False: raise RuntimeError("IQ Option connection failed.")
    for _ in range(15):
        try:
            if client.check_connect(): break
        except Exception: pass
        time.sleep(1)
    else: raise RuntimeError("IQ Option connection timed out.")
    client.change_balance("PRACTICE"); time.sleep(2)
    try: mode=str(client.get_balance_mode()).upper()
    except Exception: mode=""
    if "PRACTICE" not in mode: raise RuntimeError("Safety stop: PRACTICE account mode could not be verified.")
    IQ=client
    try: bal=safe_float(client.get_balance())
    except Exception: bal=0.0
    logging.info("Connected. Practice balance: %s",money(bal))
    telegram_send(f"🟢 MOMENTUM 10 CONNECTED\nAccount: PRACTICE ONLY\nBalance: {money(bal)}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nExpiry: 1 minute | Stake: $1\nManual scans never place trades.")


def ensure_connection():
    global IQ
    try:
        if IQ is not None and IQ.check_connect() and "PRACTICE" in str(IQ.get_balance_mode()).upper(): return True
    except Exception: pass
    IQ=None
    try: connect_iq(); return True
    except Exception as e: logging.error("Reconnect failed: %s",e); return False


def practice_mode_verified():
    client=IQ
    if client is None: return False
    lock=client_api_lock(client)
    if not lock.acquire(timeout=1): return False
    try: return bool(client.check_connect() and "PRACTICE" in str(client.get_balance_mode()).upper())
    except Exception: return False
    finally: lock.release()


def get_asset_names():
    client=IQ
    if client is None: return []
    q=queue.Queue(maxsize=1)
    def worker():
        try:
            with client_api_lock(client): m=client.get_all_ACTIVES_OPCODE()
            q.put(("ok",m))
        except Exception as e: q.put(("error",str(e)))
    threading.Thread(target=worker,daemon=True,name="asset-discovery").start()
    try: status,m=q.get(timeout=CANDLE_REQUEST_TIMEOUT)
    except queue.Empty:
        logging.error("ASSET DISCOVERY TIMEOUT: exceeded %ss; reconnect required.",CANDLE_REQUEST_TIMEOUT)
        if IQ is client: globals()["IQ"]=None
        return []
    if status!="ok": logging.warning("Asset discovery failed: %s",m); return []
    return sorted({s.strip() for s in m if isinstance(s,str) and s.strip()}) if isinstance(m,dict) else []


def fetch_candles(asset):
    global IQ
    client=IQ
    if client is None: return None
    q=queue.Queue(maxsize=1)
    def worker():
        try:
            with client_api_lock(client): candles=client.get_candles(asset,CANDLE_SECONDS,LOOKBACK_CANDLES,int(time.time()))
            q.put(("ok",candles))
        except Exception as e: q.put(("error",str(e)))
    threading.Thread(target=worker,daemon=True).start()
    try: status,candles=q.get(timeout=CANDLE_REQUEST_TIMEOUT)
    except queue.Empty:
        logging.error("CANDLE TIMEOUT: %s exceeded %ss; reconnect required.",asset,CANDLE_REQUEST_TIMEOUT)
        if IQ is client: IQ=None
        return None
    if status!="ok" or not isinstance(candles,list): return []
    if len(candles)<MOMENTUM_PERIOD+3: return []
    cleaned=[]
    for c in sorted(candles,key=lambda x:x.get("from",0)):
        close=safe_float(c.get("close"),float("nan")); op=safe_float(c.get("open"),float("nan"))
        high=safe_float(c.get("max"),float("nan")); low=safe_float(c.get("min"),float("nan"))
        if not all(math.isfinite(v) for v in (close,op,high,low)) or close<=0 or high<low: continue
        cleaned.append({"open":op,"close":close,"high":high,"low":low,"from":c.get("from",0)})
    # Keep the live candle for manual advance warnings. Automatic trading still
    # evaluates only fully closed candles in scan_market().
    return cleaned


def calculate_signal(asset,candles):
    if len(candles)<MOMENTUM_PERIOD+3: return None,"insufficient candles"
    closes=[c["close"] for c in candles]; opens=[c["open"] for c in candles]
    highs=[c["high"] for c in candles]; lows=[c["low"] for c in candles]
    current,previous=closes[-1],closes[-2]; old=closes[-1-MOMENTUM_PERIOD]
    if old<=0: return None,"invalid reference price"
    momentum=(current-old)/old
    ranges=[highs[i]-lows[i] for i in range(max(0,len(candles)-14),len(candles))]
    avg=sum(ranges)/len(ranges) if ranges else 0.0
    if avg<=0: return None,"no usable candle range"
    last=highs[-1]-lows[-1]
    if last<=0: return None,"flat latest candle"
    body=abs(current-opens[-1])/last; upper=(highs[-1]-current)/last; lower=(current-lows[-1])/last
    if momentum<0:
        if current<=opens[-1]: return None,"no bullish reversal after downward momentum"
        turn,direction=lower,"call"
    elif momentum>0:
        if current>=opens[-1]: return None,"no bearish reversal after upward momentum"
        turn,direction=upper,"put"
    else: return None,"zero momentum"
    if turn<MIN_TURN_DISTANCE: return None,"reversal distance too small"
    normalized=abs(current-old)/avg
    if normalized<EXTREME_PERCENTILE: return None,"momentum not extreme enough"
    if direction=="call" and current<=previous: return None,"bullish reversal lacks close confirmation"
    if direction=="put" and current>=previous: return None,"bearish reversal lacks close confirmation"
    score=50+(10 if normalized>=0.5 else 0)+(10 if normalized>=1.0 else 0)+(10 if body>=0.4 else 0)+(10 if body>=0.65 else 0)+(5 if turn>=0.3 else 0)+(5 if turn>=0.5 else 0)
    try: candle_time=int(candles[-1].get("from") or time.time())
    except Exception: candle_time=int(time.time())
    return {"asset":asset,"direction":direction,"score":min(score,100),"momentum":momentum,
      "normalized_momentum":normalized,"body_ratio":body,"turn_distance":turn,"price":current,
      "candle_time":candle_time,"time":utc_now().strftime("%H:%M:%S UTC")},""


def lagos_time(timestamp):
    try:
        text=datetime.fromtimestamp(float(timestamp), ZoneInfo("Africa/Lagos")).strftime("%I:%M %p")
    except Exception:
        text=datetime.fromtimestamp(float(timestamp)).strftime("%I:%M %p")
    return text.lstrip("0") + " WAT"


def display_asset(asset):
    name=str(asset or "").strip()
    otc=bool(re.search(r"(?:[-_\s]?OTC)$",name,re.IGNORECASE))
    base=re.sub(r"(?:[-_\s]?OTC)$","",name,flags=re.IGNORECASE).strip()
    if len(base)==6 and base.isalpha():
        base=base[:3].upper()+"/"+base[3:].upper()
    else:
        base=base.replace("_"," ").replace("-"," ").strip()
    return base+(" OTC" if otc else "")


def send_manual_scan_results(results,scanned,total,rejected,timed_out=False):
    # Manual scans remain report-only; this does not alter the strategy or filters.
    now=time.time()
    live=[x for x in results if x.get("entry_time",0)>now]
    if not live:
        top=sorted(rejected.items(),key=lambda item:item[1],reverse=True)[:3]
        reasons="; ".join(f"{name}: {count}" for name,count in top) or "No valid candles or scan result."
        status="\nScan timed out; reconnect is required." if timed_out else ""
        telegram_send(f"NO TRADE\nScanned: {scanned}/{total}\nTop rejections: {reasons}{status}")
        return
    signal=live[0]
    direction="BUY" if str(signal.get("direction","")).lower()=="call" else "SELL"
    sid=f"M10-SCAN-{signal.get('asset','UNKNOWN')}-{str(signal.get('direction','')).upper()}-{int(signal.get('candle_time',now))}"
    with STATE_LOCK:
        tracked=STATE.setdefault("tracked_signals",{})
        tracked[sid]={"signal_id":sid,"asset":signal.get("asset","Unknown"),"direction":signal.get("direction","unknown"),"score":signal.get("score",0),"status":"PENDING","created_at":utc_now().isoformat(),"entry_time":int(signal.get("entry_time",now))}
        if len(tracked)>200:
            ordered=sorted(tracked.items(),key=lambda item:item[1].get("created_at",""))
            for old_id,_ in ordered[:len(tracked)-200]: tracked.pop(old_id,None)
    save_state()
    telegram_send(f"{display_asset(signal.get('asset'))} — {direction}\nSignal ID: {sid}\nEntry: {lagos_time(signal['entry_time'])}\nScore: {signal.get('score',0)}/100\nManual scan only — no trade placed.")


def scan_market(manual=False,auto_eligible=False):
    global SCAN_IN_PROGRESS,ASSET_CURSOR,LAST_ASSET_COUNT,LAST_SCAN_FINISHED,LAST_SCAN_CANDIDATES
    global LAST_SCAN_SCANNED,LAST_SCAN_TOTAL,LAST_SCAN_TIMED_OUT,LAST_SCAN_ERROR
    results=[]; rejected={}; scanned=attempted=total=0; timed_out=False; error_text=""; start=ASSET_CURSOR
    try:
        if not ensure_connection():
            error_text="IQ Option disconnected; reconnect failed."
            return
        assets=get_asset_names(); total=len(assets); LAST_ASSET_COUNT=total
        if not assets:
            error_text="IQ Option returned zero symbols."
            logging.warning(error_text); return
        start=ASSET_CURSOR%total; batch=[assets[(start+i)%total] for i in range(min(MAX_SYMBOLS_PER_SCAN,total))]
        logging.info("Scanning batch: %d symbols; total=%d; start=%d; auto_eligible=%s",len(batch),total,start,auto_eligible)
        for i,asset in enumerate(batch,1):
            if not RUNNING: break
            if IQ is None: timed_out=True; rejected["connection reset after timeout"]=1; break
            attempted+=1; candles=fetch_candles(asset)
            if candles is None:
                timed_out=True; rejected["candle request timeout/reconnect"]=rejected.get("candle request timeout/reconnect",0)+1; break
            scanned+=1
            if len(candles)<MOMENTUM_PERIOD+4:
                rejected["no candles/unavailable"]=rejected.get("no candles/unavailable",0)+1
            elif manual:
                # Advance-warning scan evaluates the still-forming candle. This is
                # informational only; it never enters a trade.
                signal,reason=calculate_signal(asset,candles)
                if signal:
                    signal["preview"]=True
                    signal["entry_time"]=int(signal.get("candle_time",0))+CANDLE_SECONDS
                    if signal["entry_time"]-time.time() >= 8:
                        results.append(signal)
                    else:
                        rejected["potential setup too close to candle close"]=rejected.get("potential setup too close to candle close",0)+1
                else: rejected[reason]=rejected.get(reason,0)+1
            else:
                # Automatic mode uses only closed candles, preserving the original rules.
                signal,reason=calculate_signal(asset,candles[:-1])
                if signal: results.append(signal)
                else: rejected[reason]=rejected.get(reason,0)+1
            if i%PROGRESS_EVERY==0: logging.info("Batch progress: %d/%d candidates=%d",i,len(batch),len(results))
            time.sleep(REQUEST_PAUSE_SECONDS)
    except Exception as e:
        error_text=str(e); logging.error("Market scan failed: %s",e); logging.debug(traceback.format_exc())
    finally:
        if total: ASSET_CURSOR=(start+max(attempted,1))%total
        results.sort(key=lambda s:s["score"],reverse=True)
        LAST_SCAN_FINISHED=time.time(); LAST_SCAN_CANDIDATES=len(results); LAST_SCAN_SCANNED=scanned; LAST_SCAN_TOTAL=total
        LAST_SCAN_TIMED_OUT=timed_out; LAST_SCAN_ERROR=error_text
        with STATE_LOCK:
            STATE["last_scan"]=utc_now().isoformat(); STATE["last_scan_count"]=scanned; STATE["last_scan_setups"]=len(results)
            STATE["last_scan_rejections"]=dict(sorted(rejected.items(),key=lambda item:item[1],reverse=True)[:8])
        logging.info("Batch finished: scanned=%d/%d attempted=%d candidates=%d timeout=%s error=%s rejections=%s",scanned,total,attempted,len(results),timed_out,error_text or "none",rejected)
        if not manual and not results:
            top=sorted(rejected.items(),key=lambda item:item[1],reverse=True)[:3]
            logging.info("NO TRADE — top rejection reasons: %s", "; ".join(f"{name}={count}" for name,count in top) or "none")
        if manual: send_manual_scan_results(results,scanned,total,rejected,timed_out)
        SCAN_RESULTS_QUEUE.put({"manual":manual,"auto_eligible":auto_eligible,"finished_at":time.time(),"results":results,
          "scanned":scanned,"total":total,"timed_out":timed_out,"error":error_text})
        with STATE_LOCK: SCAN_IN_PROGRESS=False


def start_scan(manual=False):
    global SCAN_IN_PROGRESS,LAST_SCAN_STARTED
    with STATE_LOCK:
        if SCAN_IN_PROGRESS: return False
        SCAN_IN_PROGRESS=True; LAST_SCAN_STARTED=time.time(); auto_eligible=bool(AUTO_MODE and not manual)
    threading.Thread(target=scan_market,kwargs={"manual":manual,"auto_eligible":auto_eligible},daemon=True,name="market-scan").start()
    return True


def execute_signal(signal):
    if not AUTO_TRADING_ALLOWED or not AUTO_MODE: return False
    reason=daily_limit_reason()
    if reason: set_auto(False,reason); telegram_send("🛑 AUTO STOPPED\n"+reason); return False
    if not practice_mode_verified(): set_auto(False,"PRACTICE mode could not be verified"); telegram_send("🚨 Safety stop: PRACTICE mode not verified."); return False
    asset=str(signal.get("asset","")); direction=str(signal.get("direction","")).lower()
    try: candle_time=int(signal.get("candle_time") or time.time())
    except Exception: return False
    age=time.time()-candle_time
    if age>MAX_SIGNAL_AGE_SECONDS or age < -5: logging.info("Skipping stale signal: %s age=%.1f",asset,age); return False
    if not asset or direction not in ("call","put") or LAST_SIGNAL_BY_ASSET.get(asset)==str(candle_time): return False
    sid=f"M10-{asset}-{direction.upper()}-{candle_time}"; q=queue.Queue(maxsize=1)
    def order_worker(client=IQ):
        try:
            if not practice_mode_verified(): q.put(("safety","PRACTICE verification failed"))
            else:
                with client_api_lock(client): response=client.buy(STAKE,asset,direction,EXPIRY_MINUTES)
                q.put(("result",response))
        except Exception as e: q.put(("error",str(e)))
    threading.Thread(target=order_worker,daemon=True,name="place-order").start()
    try: status,response=q.get(timeout=12)
    except queue.Empty:
        with STATE_LOCK:
            STATE["blocked"]+=1
            STATE["unknown_order_request"]={"signal_id":sid,"asset":asset,"direction":direction,"stake":STAKE,"requested_at":utc_now().isoformat(),"status":"UNKNOWN — verify in IQ Option history"}
        save_state()
        set_auto(False,"Order request timed out; broker outcome unknown")
        logging.error("Order request timed out for %s; AUTO stopped to prevent duplicate orders.",asset)
        telegram_send(f"⚠️ AUTO STOPPED — ORDER STATUS UNKNOWN\n{asset} {direction.upper()}\nSignal: {sid}\nCheck IQ Option history before restarting. No automatic retry was made."); return False
    if status=="safety": set_auto(False,response); telegram_send("🚨 SAFETY STOP: "+response); return False
    if status!="result" or not isinstance(response,tuple) or len(response)<2:
        with STATE_LOCK: STATE["blocked"]+=1
        save_state(); logging.warning("Order failed for %s: %r",asset,response); return False
    success,order_id=bool(response[0]),response[1]
    if not success or order_id is None or str(order_id)=="":
        with STATE_LOCK: STATE["blocked"]+=1
        save_state(); logging.warning("Order rejected: %s; response=%r",asset,response); return False
    key=str(order_id)
    with STATE_LOCK:
        OPEN_TRADES[key]={"signal_id":sid,"asset":asset,"direction":direction,"stake":STAKE,"score":signal.get("score",0),"started":time.time(),"candle_time":candle_time}
        STATE["pending_order_id"]=key; STATE["pending_asset"]=asset; STATE["pending_direction"]=direction; STATE["pending_started"]=utc_now().isoformat()
        LAST_SIGNAL_BY_ASSET[asset]=str(candle_time); STATE["executed"]+=1; STATE["signals"]+=1
    save_state()
    telegram_send(f"🟡 PRACTICE TRADE ACCEPTED\nSignal ID: {sid}\nOrder ID: {key}\nAsset: {asset}\nDirection: {direction.upper()}\nStake: {money(STAKE)}\nExpiry: 1 minute\nScore: {signal.get('score',0)}/100\nScore is not win probability.")
    logging.info("Practice order accepted: %s id=%s",asset,key); return True


def reconcile_trades():
    global RECONCILE_IN_PROGRESS
    if RECONCILE_IN_PROGRESS or not OPEN_TRADES or not ensure_connection(): return
    RECONCILE_IN_PROGRESS=True
    try:
        for oid in list(OPEN_TRADES):
            trade=OPEN_TRADES.get(oid)
            if not trade or time.time()-safe_float(trade.get("started"),time.time())<60: continue
            with RESULT_LOOKUP_LOCK:
                if str(oid) in RESULT_LOOKUPS_IN_FLIGHT:
                    logging.info("Result lookup still running for %s; skipping duplicate request.",oid); continue
                RESULT_LOOKUPS_IN_FLIGHT.add(str(oid))
            q=queue.Queue(maxsize=1); client=IQ
            def worker(client=client,order=oid,result_queue=q):
                try:
                    with client_api_lock(client): result=client.check_win_v4(int(order))
                    result_queue.put(("ok",result))
                except Exception as e: result_queue.put(("error",str(e)))
                finally:
                    with RESULT_LOOKUP_LOCK: RESULT_LOOKUPS_IN_FLIGHT.discard(str(order))
            threading.Thread(target=worker,daemon=True,name="trade-result").start()
            try: status,result=q.get(timeout=RESULT_CHECK_TIMEOUT)
            except queue.Empty:
                logging.warning("Result lookup timed out for %s; keeping one lookup in flight and reconnecting with a fresh client.",oid)
                if IQ is client: globals()["IQ"]=None
                continue
            if status!="ok" or result is None:
                if status!="ok": logging.warning("Result check failed for %s: %s",oid,result)
                continue
            result=safe_float(result,float("nan"))
            if not math.isfinite(result): continue
            with STATE_LOCK:
                if oid not in OPEN_TRADES: continue
                OPEN_TRADES.pop(oid,None); STATE["resolved"]+=1; STATE["daily_pl"]=safe_float(STATE.get("daily_pl"))+result
                settled_list=[str(x) for x in STATE.get("settled_order_ids",[]) if str(x)!=str(oid)]
                settled_list.append(str(oid)); STATE["settled_order_ids"]=settled_list[-200:]
                if str(STATE.get("pending_order_id",""))==str(oid):
                    STATE["pending_order_id"]=None; STATE["pending_asset"]=None; STATE["pending_direction"]=None; STATE["pending_started"]=None
                if result>0: STATE["wins"]+=1; STATE["consecutive_losses"]=0; outcome="WIN"
                elif result<0: STATE["losses"]+=1; STATE["consecutive_losses"]+=1; outcome="LOSS"
                else: outcome="BREAKEVEN"
                STATE["history"].append({"signal_id":trade.get("signal_id",oid),"order_id":str(oid),"asset":trade.get("asset","Unknown"),
                  "direction":trade.get("direction","unknown"),"outcome":outcome,"net":result,"time":utc_now().isoformat()})
                STATE["history"]=STATE["history"][-200:]; pl=STATE["daily_pl"]; resolved=STATE["resolved"]
            save_state(); telegram_send(f"📊 TRADE RESULT: {outcome}\nSignal ID: {trade.get('signal_id',oid)}\nOrder ID: {oid}\nAsset: {trade.get('asset','Unknown')}\nDirection: {str(trade.get('direction','unknown')).upper()}\nNet result: {money(result)}\nDaily P/L: {money(pl)}\nResolved: {resolved}/{MAX_RESOLVED_TRADES}")
            reason=daily_limit_reason()
            if reason: set_auto(False,reason); telegram_send("🛑 AUTO MODE OFF\n"+reason)
    finally: RECONCILE_IN_PROGRESS=False


def status_message():
    connected=practice_ok=False; balance="Unavailable"
    try:
        connected=bool(IQ is not None and IQ.check_connect())
        if connected:
            practice_ok="PRACTICE" in str(IQ.get_balance_mode()).upper()
            balance=money(IQ.get_balance()) if practice_ok else "Blocked: practice mode not verified"
    except Exception: connected=practice_ok=False
    with STATE_LOCK:
        age=f"{int(time.time()-LAST_SCAN_FINISHED)}s ago" if LAST_SCAN_FINISHED else "Never completed"
        return ("📊 MOMENTUM 10 STATUS\n" f"Connection: {'CONNECTED' if connected else 'DISCONNECTED'}\nPractice verification: {'OK' if practice_ok else 'NOT VERIFIED'}\nBalance: {balance}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nDaily P/L: {money(STATE['daily_pl'])}\nWins: {STATE['wins']} | Losses: {STATE['losses']}\nResolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\nOpen trades tracked: {len(OPEN_TRADES)}\nSignals accepted: {STATE['signals']}\nOrders accepted: {STATE['executed']}\nOrders blocked/rejected: {STATE['blocked']}\nConsecutive losses: {STATE['consecutive_losses']}\nDiscovered symbols: {LAST_ASSET_COUNT}\nLast scan: {age}\nLast batch: {LAST_SCAN_SCANNED}/{LAST_SCAN_TOTAL}\nLast candidates: {LAST_SCAN_CANDIDATES}\nLast timeout: {'YES' if LAST_SCAN_TIMED_OUT else 'NO'}\nLast scan error: {LAST_SCAN_ERROR or 'None'}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\nDaily stop: {daily_limit_reason() or 'Not reached'}\nAuto stop reason: {STATE.get('stop_reason') or 'None'}")


def process_telegram_update(update):
    global MANUAL_SCAN_REQUESTED
    msg=update.get("message") or update.get("callback_query",{}).get("message")
    if not msg: return
    chat=str(msg.get("chat",{}).get("id",""))
    if TELEGRAM_CHAT_ID and chat!=str(TELEGRAM_CHAT_ID): return
    text=(msg.get("text") or update.get("callback_query",{}).get("data") or "").strip()
    if not text: return
    cmd=text.upper(); logging.info("Telegram command received: %s",cmd)
    if cmd in ("/START","START AUTO"):
        if not AUTO_TRADING_ALLOWED: telegram_send("AUTO refused: automatic trading is disabled.")
        elif OPEN_TRADES or STATE.get("unknown_order_request"):
            telegram_send(f"AUTO refused: {len(OPEN_TRADES)} unresolved broker trade(s) or an unknown order request remains. Reconcile and verify IQ Option history first.")
        elif daily_limit_reason(): telegram_send("Cannot start AUTO.\n"+daily_limit_reason())
        elif not ensure_connection() or not practice_mode_verified(): telegram_send("AUTO refused: connection or PRACTICE verification failed.")
        else: set_auto(True,""); telegram_send("🟢 AUTO MODE ON\nPRACTICE verified. $1 stake / 1-minute expiry.\nSCAN MARKET remains report-only.")
    elif cmd in ("/STOP","STOP AUTO"):
        set_auto(False,"Stopped manually"); telegram_send("🔴 AUTO MODE OFF. No new automatic trades will be started.")
    elif cmd in ("SCAN MARKET","/SCAN"):
        MANUAL_SCAN_REQUESTED=True
    elif cmd in ("STATUS","/STATUS"): telegram_send(status_message())
    elif cmd.startswith("/WIN ") or cmd.startswith("/LOSS "):
        parts=text.split(maxsplit=1)
        if len(parts)<2 or not parts[1].strip(): telegram_send("Usage: /win SIGNAL-ID or /loss SIGNAL-ID"); return
        sid=parts[1].strip(); outcome="WIN" if cmd.startswith("/WIN ") else "LOSS"
        with STATE_LOCK:
            tracked=STATE.setdefault("tracked_signals",{}); item=tracked.get(sid)
            if not item: telegram_send(f"Signal ID not found: {sid}"); return
            if item.get("status") in ("WIN","LOSS"): telegram_send(f"{sid} already recorded as {item.get('status')}."); return
            item["status"]=outcome; item["resolved_at"]=utc_now().isoformat(); item["result_source"]="manual"
        save_state(); telegram_send(f"✅ Signal result saved\nSignal ID: {sid}\nOutcome: {outcome}\nThis manual signal tracker does not change broker P/L.")
    elif cmd.startswith("/STATS"):
        with STATE_LOCK:
            total=STATE["wins"]+STATE["losses"]; rate=STATE["wins"]/total*100 if total else 0
            tracked=STATE.get("tracked_signals",{}); tracked_wins=sum(1 for v in tracked.values() if v.get("status")=="WIN"); tracked_losses=sum(1 for v in tracked.values() if v.get("status")=="LOSS"); tracked_pending=sum(1 for v in tracked.values() if v.get("status")=="PENDING")
            tracked_total=tracked_wins+tracked_losses; tracked_rate=tracked_wins/tracked_total*100 if tracked_total else 0
            out=f"📈 MOMENTUM 10 STATS\nBroker W/L: {STATE['wins']}/{STATE['losses']}\nBroker win rate: {rate:.2f}%\nDaily P/L: {money(STATE['daily_pl'])}\nResolved broker trades: {STATE['resolved']}\nOpen broker trades: {len(OPEN_TRADES)}\n\nManual signal tracker W/L: {tracked_wins}/{tracked_losses}\nManual signal win rate: {tracked_rate:.2f}%\nPending tracked signals: {tracked_pending}"
        telegram_send(out)
    elif cmd.startswith("/RECONCILE"):
        threading.Thread(target=reconcile_trades,daemon=True).start(); telegram_send("Reconciliation requested. Check STATUS or /stats.")


def telegram_listener():
    global TELEGRAM_OFFSET
    if not TELEGRAM_TOKEN: logging.warning("TELEGRAM_TOKEN missing; listener disabled."); return
    base=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    while RUNNING:
        try:
            r=requests.get(base+"/getUpdates",params={"offset":TELEGRAM_OFFSET,"timeout":20},timeout=25); r.raise_for_status(); data=r.json()
            if not data.get("ok"): time.sleep(3); continue
            for upd in data.get("result",[]):
                TELEGRAM_OFFSET=int(upd.get("update_id",0))+1
                try: process_telegram_update(upd)
                except Exception: logging.exception("Telegram command handler failed"); telegram_send("⚠️ Command handler error. Check Actions log.")
                with STATE_LOCK: STATE["last_update_id"]=TELEGRAM_OFFSET-1
                save_state()
        except Exception as e: logging.warning("Telegram listener error: %s",e); time.sleep(5)


def heartbeat():
    global LAST_HEARTBEAT
    if time.time()-LAST_HEARTBEAT<HEARTBEAT_SECONDS: return
    LAST_HEARTBEAT=time.time()
    with STATE_LOCK:
        msg=("💓 MOMENTUM 10 HEARTBEAT\nAccount: PRACTICE ONLY\n" f"Connection: {'CONNECTED' if practice_mode_verified() else 'NOT VERIFIED'}\nAUTO MODE: {'ON' if AUTO_MODE else 'OFF'}\nDaily P/L: {money(STATE['daily_pl'])}\nResolved: {STATE['resolved']}/{MAX_RESOLVED_TRADES}\nW/L: {STATE['wins']}/{STATE['losses']}\nOpen trades tracked: {len(OPEN_TRADES)}\nOrders accepted: {STATE['executed']}\nOrders blocked/rejected: {STATE['blocked']}\nManual tracker W/L: {sum(1 for v in STATE.get('tracked_signals',{}).values() if v.get('status')=='WIN')}/{sum(1 for v in STATE.get('tracked_signals',{}).values() if v.get('status')=='LOSS')}\nScan in progress: {'YES' if SCAN_IN_PROGRESS else 'NO'}\nLast candidates: {LAST_SCAN_CANDIDATES}\nTop rejection reasons: {'; '.join(f"{k}: {v}" for k,v in list(STATE.get("last_scan_rejections",{}).items())[:3]) or "No rejection data yet"}\nNo qualifying setup means NO TRADE.")
    telegram_send(msg)


def main():
    global AUTO_MODE,MANUAL_SCAN_REQUESTED
    load_state(); restore_auto=bool(STATE.get("auto_enabled",False)); AUTO_MODE=False
    telegram_send("🚀 MOMENTUM 10 STARTING\nChecking saved AUTO preference and PRACTICE mode.\nManual SCAN MARKET gives advance warnings only and never places trades.")
    threading.Thread(target=telegram_listener,daemon=True,name="telegram-listener").start()
    next_reconcile=0
    while RUNNING:
        try:
            if IQ is None and not ensure_connection():
                if AUTO_MODE: set_auto(False,"Connection lost; waiting for reconnect")
                heartbeat(); time.sleep(RECONNECT_SECONDS); continue
            if restore_auto:
                restore_auto=False; reason=daily_limit_reason()
                if reason: set_auto(False,reason); telegram_send("🛑 Saved AUTO not restored.\n"+reason)
                elif practice_mode_verified(): set_auto(True,""); telegram_send("🟢 Saved AUTO preference restored after PRACTICE verification.")
                else: set_auto(False,"PRACTICE verification failed at startup")
            # Never run potentially slow broker result checks on this control loop.
            if OPEN_TRADES and not RECONCILE_IN_PROGRESS and time.time()>=next_reconcile:
                threading.Thread(target=reconcile_trades,daemon=True,name="reconcile").start(); next_reconcile=time.time()+10
            while True:
                try: done=SCAN_RESULTS_QUEUE.get_nowait()
                except queue.Empty: break
                if not done.get("manual") and done.get("auto_eligible") and AUTO_MODE and not done.get("timed_out") and not done.get("error") and not daily_limit_reason():
                    for signal in done.get("results",[]):
                        if not AUTO_MODE or daily_limit_reason(): break
                        if execute_signal(signal): break
            if MANUAL_SCAN_REQUESTED and not SCAN_IN_PROGRESS:
                MANUAL_SCAN_REQUESTED=False
                if not start_scan(manual=True): MANUAL_SCAN_REQUESTED=True
            elif not SCAN_IN_PROGRESS and time.time()-LAST_SCAN_STARTED>=SCAN_INTERVAL_SECONDS: start_scan(manual=False)
            heartbeat(); time.sleep(0.5)
        except KeyboardInterrupt: break
        except Exception as e: logging.error("Main loop error: %s",e); logging.debug(traceback.format_exc()); time.sleep(2)
    set_auto(False,"Scanner stopped"); telegram_send("Scanner stopped. AUTO MODE OFF.")

if __name__=="__main__": main()
