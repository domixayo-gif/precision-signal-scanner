import os, re, csv, json, time, math, logging, threading, subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import iqoptionapi.constants as OP_code
from iqoptionapi.stable_api import IQ_Option

# NORMAL FOREX + OTC FOREX + NORMAL CRYPTO + OTC CRYPTO.
# MOMENTUM 10 + MOMENTUM 20 + 12 candle patterns + a separate closed follow-up candle.
# PRACTICE ACCOUNT ONLY.
IQ_EMAIL=os.getenv('IQ_EMAIL','').strip(); IQ_PASSWORD=os.getenv('IQ_PASSWORD','').strip()
TELEGRAM_TOKEN=os.getenv('TELEGRAM_TOKEN','').strip(); TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','').strip()
GITHUB_TOKEN=os.getenv('GITHUB_TOKEN','').strip(); GITHUB_REPOSITORY=os.getenv('GITHUB_REPOSITORY','').strip()
GITHUB_REF_NAME=os.getenv('GITHUB_REF_NAME','main').strip(); GITHUB_WORKFLOW_FILE='scanner.yml'
BALANCE_MODE='PRACTICE'; AUTO_TRADE=True; STAKE=1.50; EXPIRY_MINUTES=1; CANDLE_SECONDS=60
MOMENTUM_PERIOD=10; MOMENTUM_CONFIRM_PERIOD=20; MOMENTUM_LOOKBACK=50
EXTREME_PERCENTILE=0.08; MIN_TURN_DISTANCE=0.06
SESSION_PROFIT_TARGET=10.0; SESSION_LOSS_LIMIT=4.0
MAX_RESOLVED_TRADES=20; MAX_CONSECUTIVE_LOSSES=5
SCAN_INTERVAL=5; ASSET_REFRESH_SECONDS=900; HEARTBEAT_SECONDS=300
RECONNECT_SECONDS=30; CANDLE_REQUEST_TIMEOUT=10; ORDER_RESULT_RETRY_SECONDS=5
REJECTED_ASSET_COOLDOWN_SECONDS=600; CANDLE_REQUEST_COUNT=MOMENTUM_LOOKBACK+MOMENTUM_CONFIRM_PERIOD+12
DIAGNOSTIC_LOG_SECONDS=900; LOG_FILE='momentum_signal_log.csv'; STATE_FILE='momentum_state.json'
LAGOS=ZoneInfo('Africa/Lagos')
CURRENCIES={'USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','HKD','SGD','SEK','NOK','DKK','PLN','CZK','HUF','TRY','ZAR','MXN','BRL','INR','THB','AED','SAR','ILS','KRW','TWD','RUB','RON','BGN','ISK','PHP','IDR','MYR'}
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
SESSION_ID='{}-{}'.format(os.getenv('GITHUB_RUN_ID',str(int(time.time()))),os.getenv('GITHUB_RUN_ATTEMPT','1'))
IQ=None; ASSETS=[]; ASSET_REFRESH_AT=0; REJECTED_ASSETS={}; STOP_EVENT=threading.Event(); TRADE_LOCK=threading.Lock(); STATE_LOCK=threading.RLock(); DIAG_LOCK=threading.Lock(); NEXT_WORKFLOW_REQUESTED=False
DIAG_KEYS=['assets_scanned','candle_data_ok','empty_candles','insufficient_candles','signals_checked','patterns_found','followup_confirmed','followup_rejected','m10_extreme_pass','m10_extreme_rejected','m10_turn_pass','m10_turn_rejected','m20_trend_pass','m20_trend_rejected','setups_found','duplicate_signals','order_rejected']
DIAG={k:0 for k in DIAG_KEYS}; DIAG_LAST_ASSET_LOG={}

def now_lagos(): return datetime.now(LAGOS)
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        r=requests.post('https://api.telegram.org/bot{}/sendMessage'.format(TELEGRAM_TOKEN),data={'chat_id':TELEGRAM_CHAT_ID,'text':message,'parse_mode':'HTML','disable_web_page_preview':True},timeout=10); r.raise_for_status()
    except Exception as e: logging.warning('Telegram send failed: %s',e)
def fresh_state():
    return {'date':now_lagos().date().isoformat(),'session_id':SESSION_ID,'profit':0.0,'wins':0,'losses':0,'resolved':0,'consecutive_losses':0,'pending_order_id':None,'pending_asset':None,'pending_direction':None,'pending_started':None,'last_signals':{},'stop_reason':''}
def load_state():
    s=fresh_state()
    try:
        with open(STATE_FILE,encoding='utf-8') as f: old=json.load(f)
        if not isinstance(old,dict): return s
    except (OSError,ValueError,TypeError): return s
    # Daily P/L and risk counters persist across chained workflow runs, reset only on a new Lagos date.
    if old.get('date')==s['date']:
        for k in ('profit','wins','losses','resolved','consecutive_losses','last_signals','stop_reason'):
            if k in old: s[k]=old[k]
    for k in ('pending_order_id','pending_asset','pending_direction','pending_started'): s[k]=old.get(k)
    if not isinstance(s.get('last_signals'),dict): s['last_signals']={}
    return s
STATE=load_state()
def save_state(persist=False):
    try:
        with STATE_LOCK:
            STATE['date']=now_lagos().date().isoformat(); STATE['session_id']=SESSION_ID
            with open(STATE_FILE+'.tmp','w',encoding='utf-8') as f: json.dump(STATE,f,indent=2)
            os.replace(STATE_FILE+'.tmp',STATE_FILE)
        if persist: persist_state_to_github()
    except Exception as e: logging.warning('State save failed: %s',e)
def persist_state_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.warning('State not committed: GITHUB_TOKEN/repository unavailable'); return
    try:
        subprocess.run(['git','config','user.name','github-actions[bot]'],check=True,capture_output=True,text=True)
        subprocess.run(['git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com'],check=True,capture_output=True,text=True)
        files=[STATE_FILE]+([LOG_FILE] if os.path.exists(LOG_FILE) else [])
        subprocess.run(['git','add']+files,check=True,capture_output=True,text=True)
        if subprocess.run(['git','diff','--cached','--quiet'],capture_output=True).returncode==0: return
        subprocess.run(['git','commit','-m','Persist scanner state and trade log'],check=True,capture_output=True,text=True)
        r=subprocess.run(['git','push','origin','HEAD'],capture_output=True,text=True)
        if r.returncode: logging.warning('State push failed: %s',r.stderr.strip())
    except Exception as e: logging.warning('Git state persistence failed: %s',e)
def log_trade(asset,direction,m10,status,profit='',order_id=''):
    fields=['timestamp_lagos','session_id','asset','direction','momentum10','status','profit','order_id','session_profit']
    try:
        exists=os.path.exists(LOG_FILE)
        with open(LOG_FILE,'a',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields)
            if not exists or os.path.getsize(LOG_FILE)==0: w.writeheader()
            w.writerow({'timestamp_lagos':now_lagos().isoformat(timespec='seconds'),'session_id':SESSION_ID,'asset':asset,'direction':direction,'momentum10':'' if m10 is None else round(float(m10),5),'status':status,'profit':profit,'order_id':order_id,'session_profit':round(float(STATE.get('profit',0)),2)})
    except Exception as e: logging.warning('CSV logging error: %s',e)
def inc(k,n=1):
    with DIAG_LOCK:
        if k in DIAG: DIAG[k]+=n
def diag_notice(asset,msg):
    if time.time()-DIAG_LAST_ASSET_LOG.get(asset,0)>=DIAGNOSTIC_LOG_SECONDS:
        DIAG_LAST_ASSET_LOG[asset]=time.time(); logging.warning('CANDLE DIAGNOSTIC %s: %s',asset,msg)
def format_stats(): return 'Daily P/L: ${:+.2f}\nWins: {}\nLosses: {}\nResolved trades: {}\nConsecutive losses: {}'.format(STATE['profit'],STATE['wins'],STATE['losses'],STATE['resolved'],STATE['consecutive_losses'])
def risk_stop_reason():
    p=round(float(STATE.get('profit',0)),2)
    if p>=SESSION_PROFIT_TARGET: return 'Daily take-profit reached (+${:.2f})'.format(SESSION_PROFIT_TARGET)
    if p<=-SESSION_LOSS_LIMIT: return 'Daily loss limit reached (-${:.2f})'.format(SESSION_LOSS_LIMIT)
    if int(STATE.get('resolved',0))>=MAX_RESOLVED_TRADES: return 'Daily resolved-trade limit reached ({})'.format(MAX_RESOLVED_TRADES)
    if int(STATE.get('consecutive_losses',0))>=MAX_CONSECUTIVE_LOSSES: return 'Consecutive-loss limit reached ({})'.format(MAX_CONSECUTIVE_LOSSES)
    return None
def request_next_workflow():
    global NEXT_WORKFLOW_REQUESTED
    if NEXT_WORKFLOW_REQUESTED: return
    NEXT_WORKFLOW_REQUESTED=True
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logging.error('Cannot dispatch next workflow: GITHUB_TOKEN/repository missing'); send_telegram('<b>Next workflow not triggered.</b> Check GITHUB_TOKEN permissions.'); return
    url='https://api.github.com/repos/{}/actions/workflows/{}/dispatches'.format(GITHUB_REPOSITORY,GITHUB_WORKFLOW_FILE)
    headers={'Authorization':'Bearer '+GITHUB_TOKEN,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    for attempt in range(3):
        try:
            r=requests.post(url,headers=headers,json={'ref':GITHUB_REF_NAME},timeout=15)
            if r.status_code in (201,204): send_telegram('<b>Next workflow queued</b> Daily risk totals preserved.'); return
            logging.warning('Workflow dispatch failed: %s %s',r.status_code,r.text[:200])
        except Exception as e: logging.warning('Workflow dispatch error: %s',e)
        time.sleep(3)
    send_telegram('<b>WARNING:</b> automatic workflow dispatch failed.')
def stop_if_limit_reached():
    reason=risk_stop_reason()
    if reason and not STOP_EVENT.is_set():
        STOP_EVENT.set(); STATE['stop_reason']=reason; save_state(True)
        logging.warning('%s | %s',reason,format_stats()); send_telegram('<b>SCANNER STOPPED</b>\n{}\n{}'.format(reason,format_stats()))
    return bool(reason)

CRYPTO_SYMBOLS={
    'BTC','ETH','XRP','LTC','BCH','ETC','EOS','ADA','DOT','SOL','DOGE','TRX',
    'DASH','ZEC','BNB','LINK','XLM','TON','SHIB','AVAX','UNI','ATOM','NEAR',
    'SAND','MANA','FIL','ICP','APT','ARB','OP','XMR','XTZ','VET','ALGO','AAVE',
    'FTM','MATIC','POL','HBAR','QNT','APE','EGLD','THETA','FLOW','CHZ','KAVA',
    'COMP','CRV','MKR','SNX','YFI','GRT','ENJ','RUNE','LDO','IMX','SEI','TIA',
    'WIF','PEPE','FET','INJ','KAS','SUI','TAO','ONDO','JUP','BONK','BTT','ZIL',
    'IOTA','NEO','WAVES','QTUM','BAT','ZRX','CAKE','MINA','ROSE','CELO','KSM',
    'DCR','DGB','ZEN','XEM','ONT','ICX','RVN','SC','ANKR','COTI','STX'
}
CRYPTO_QUOTES=set(CURRENCIES)|{'USDT','USDC','BTC','ETH','BNB','SOL','XRP','LTC'}

def classify_asset_name(value):
    # Return canonical ticker, market type and OTC flag; exclude unrelated assets.
    if not isinstance(value,str): return None,None,False
    raw=value.strip().upper().split('.')[-1].strip()
    if not raw: return None,None,False
    otc=bool(re.search(r'(^|[-_\s])OTC($|[-_\s])',raw)) or raw.endswith('OTC')
    letters=re.sub('[^A-Z0-9]','',raw)
    for suffix in ('OTC','BINARY','TURBO','DIGITAL'):
        if letters.endswith(suffix): letters=letters[:-len(suffix)]
    # Forex pairs: EURUSD, GBPJPY, and other supported three-letter currency pairs.
    forex_pair=letters[-6:] if len(letters)>=6 else ''
    if len(forex_pair)==6 and forex_pair[:3] in CURRENCIES and forex_pair[3:] in CURRENCIES:
        kind='otc_forex' if otc else 'normal_forex'
        return forex_pair+('-OTC' if otc else ''),kind,otc
    # Crypto pairs use a known crypto base and a supported fiat/crypto quote.
    for base in sorted(CRYPTO_SYMBOLS,key=len,reverse=True):
        if not letters.startswith(base): continue
        quote=letters[len(base):]
        if quote in CRYPTO_QUOTES and quote:
            kind='otc_crypto' if otc else 'normal_crypto'
            return base+quote+('-OTC' if otc else ''),kind,otc
    return None,None,otc

def normalize_forex_name(value):
    # Compatibility helper retained for older diagnostics.
    symbol,kind,otc=classify_asset_name(value)
    if symbol is None or kind not in ('normal_forex','otc_forex'): return None,otc
    return symbol,otc

def add_asset(found,name,active_id=None):
    symbol,kind,otc=classify_asset_name(name)
    if not symbol: return
    raw=str(name).strip().upper().split('.')[-1].strip()
    # Keep IQ Option's original ticker label where possible (especially -OTC).
    if not re.match(r'^[A-Z0-9]+(?:[-_ ]OTC)?$',raw):
        raw=symbol
    else:
        raw=re.sub(r'[-_ ]OTC$','-OTC',raw)
    resolved=None
    active_map=getattr(OP_code,'ACTIVES',{})
    candidates=(active_map.get(raw),active_map.get(symbol),active_map.get(str(name).strip()),active_id)
    for c in candidates:
        try:
            if c is not None: resolved=int(c); break
        except (TypeError,ValueError,OverflowError): pass
    item_name=raw or symbol
    found[item_name]={'name':item_name,'pair':symbol,'otc':otc,'market_type':kind,'id':resolved}
    if resolved is not None:
        try: OP_code.ACTIVES[item_name]=resolved
        except Exception: pass
        try: OP_code.ACTIVES[symbol]=resolved
        except Exception: pass
def walk_asset_tree(node,found,parent=None):
    if isinstance(node,dict):
        name=node.get('name') or node.get('active_name') or node.get('symbol'); aid=node.get('id') or node.get('active_id') or node.get('activeId')
        if name: add_asset(found,str(name),aid if aid is not None else parent)
        elif isinstance(parent,str): add_asset(found,parent,aid)
        for k,v in node.items(): walk_asset_tree(v,found,k)
    elif isinstance(node,list):
        for item in node: walk_asset_tree(item,found,parent)
def refresh_assets(force=False):
    global ASSETS,ASSET_REFRESH_AT
    if ASSETS and not force and time.time()-ASSET_REFRESH_AT<ASSET_REFRESH_SECONDS: return ASSETS
    found={}
    try:
        m=IQ.get_all_ACTIVES_OPCODE()
        if isinstance(m,dict):
            for n,i in m.items(): add_asset(found,str(n),i)
    except Exception as e: logging.warning('Active-code discovery failed: %s',e)
    for mn in ('get_all_init_v2','get_all_init'):
        try:
            fn=getattr(IQ,mn,None)
            if callable(fn): walk_asset_tree(fn(),found)
        except Exception as e: logging.warning('%s discovery failed: %s',mn,e)
    ASSETS=sorted(found.values(),key=lambda x:(x.get('market_type',''),x['name']))
    ASSET_REFRESH_AT=time.time()
    counts={k:sum(1 for x in ASSETS if x.get('market_type')==k) for k in ('normal_forex','otc_forex','normal_crypto','otc_crypto')}
    logging.info('MARKETS LOADED | normal forex=%s OTC forex=%s normal crypto=%s OTC crypto=%s total=%s',counts['normal_forex'],counts['otc_forex'],counts['normal_crypto'],counts['otc_crypto'],len(ASSETS))
    if ASSETS:
        send_telegram('<b>Markets refreshed</b>\nNormal forex: {}\nOTC forex: {}\nNormal crypto: {}\nOTC crypto: {}\nTotal: {}'.format(counts['normal_forex'],counts['otc_forex'],counts['normal_crypto'],counts['otc_crypto'],len(ASSETS)))
    return ASSETS
def disable_digital_open_worker(self): logging.info('Disabled unused digital watcher')
def safe_get_candles(self,active,interval,count,endtime):
    try:
        code=getattr(OP_code,'ACTIVES',{}).get(active)
        if code is None: return None
        self.api.candles.candles_data=None; self.api.getcandles(code,interval,count,endtime); deadline=time.time()+CANDLE_REQUEST_TIMEOUT
        while time.time()<deadline:
            data=getattr(self.api.candles,'candles_data',None)
            if data is not None: return data
            if not self.check_connect(): return None
            time.sleep(.1)
    except Exception as e: logging.warning('Candle request error %s: %s',active,e)
    return None
try:
    if hasattr(IQ_Option,'_IQ_Option__get_digital_open'): IQ_Option._IQ_Option__get_digital_open=disable_digital_open_worker
    IQ_Option.get_candles=safe_get_candles
except Exception as e: logging.warning('Candle patch failed: %s',e)
def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD: raise RuntimeError('Missing IQ_EMAIL or IQ_PASSWORD secret')
    client=IQ_Option(IQ_EMAIL,IQ_PASSWORD); last='unknown error'
    for attempt in range(1,6):
        try:
            result=client.connect(); ok=result[0] if isinstance(result,tuple) else bool(result)
            if not ok: raise ConnectionError(str(result[1] if isinstance(result,tuple) and len(result)>1 else result))
            client.change_balance(BALANCE_MODE); time.sleep(1)
            if not client.check_connect(): raise ConnectionError('Connection dropped after login')
            IQ=client; logging.info('Connected to IQ Option in PRACTICE mode')
            send_telegram('<b>Scanner connected</b>\nPRACTICE only\nStake: ${:.2f}\n{}'.format(STAKE,format_stats())); return
        except Exception as e:
            last=str(e); logging.warning('Connect attempt %s/5: %s',attempt,last)
            if attempt<5: time.sleep(RECONNECT_SECONDS)
    raise RuntimeError('Could not connect: '+last)
def momentum_series(closes,period):
    out=[]
    for i in range(period,len(closes)):
        old=closes[i-period]; out.append(0.0 if old<=0 else (closes[i]/old-1)*100.0)
    return out
def percentile_value(values,fraction):
    ordered=sorted(values)
    if not ordered: return 0.0
    return ordered[max(0,min(len(ordered)-1,int((len(ordered)-1)*fraction)))]
def get_closed_candles(asset):
    try: raw=IQ.get_candles(asset,CANDLE_SECONDS,CANDLE_REQUEST_COUNT,int(time.time()))
    except Exception as e: inc('empty_candles'); diag_notice(asset,'request failed: '+str(e)); return []
    if not isinstance(raw,list): inc('empty_candles'); diag_notice(asset,'API returned non-list'); return []
    try:
        raw.sort(key=lambda x:int(x.get('from',0))); now=int(time.time())
        closed=[c for c in raw if int(c.get('from',0))>0 and int(c.get('from',0))+CANDLE_SECONDS<=now]
    except Exception as e: inc('empty_candles'); diag_notice(asset,'bad candle data: '+str(e)); return []
    if not closed: inc('empty_candles'); diag_notice(asset,'zero fully closed candles'); return []
    inc('candle_data_ok'); need=MOMENTUM_LOOKBACK+MOMENTUM_CONFIRM_PERIOD+3
    if len(closed)<need: inc('insufficient_candles'); diag_notice(asset,'{} closed candles; need {}'.format(len(closed),need))
    return closed
def cv(c):
    try: return float(c['open']),float(c.get('max',c.get('high'))),float(c.get('min',c.get('low'))),float(c['close'])
    except (KeyError,TypeError,ValueError): return None
def bull_engulf(a,b):
    x,y=cv(a),cv(b)
    return bool(x and y and x[3]<x[0] and y[3]>y[0] and y[0]<=x[3] and y[3]>=x[0])
def bear_engulf(a,b):
    x,y=cv(a),cv(b)
    return bool(x and y and x[3]>x[0] and y[3]<y[0] and y[0]>=x[3] and y[3]<=x[0])
def hammer(c,inverted=False):
    p=cv(c)
    if not p: return False
    o,h,l,cl=p; span=h-l
    if span<=0: return False
    body=max(abs(cl-o),span*.03); upper=h-max(o,cl); lower=min(o,cl)-l
    if inverted: return upper>=body*2 and lower<=body*.8 and min(o,cl)<=l+span*.45
    return lower>=body*2 and upper<=body*.8 and max(o,cl)>=l+span*.55
def morning_star(a,b,c,bear=False):
    x,y,z=cv(a),cv(b),cv(c)
    if not x or not y or not z: return False
    body1=abs(x[3]-x[0]); body2=abs(y[3]-y[0]); body3=abs(z[3]-z[0]); mid=(x[0]+x[3])/2
    if bear: return x[3]>x[0] and body2<=body1*.6 and z[3]<z[0] and z[3]<=mid and body3>=body1*.35
    return x[3]<x[0] and body2<=body1*.6 and z[3]>z[0] and z[3]>=mid and body3>=body1*.35
def piercing(a,b):
    x,y=cv(a),cv(b)
    return bool(x and y and x[3]<x[0] and y[3]>y[0] and y[0]<=x[3] and y[3]>(x[0]+x[3])/2 and y[3]<x[0])
def dark_cloud(a,b):
    x,y=cv(a),cv(b)
    return bool(x and y and x[3]>x[0] and y[3]<y[0] and y[0]>=x[3] and y[3]<(x[0]+x[3])/2 and y[3]>x[0])
def three_soldiers(cs,bear=False):
    ps=[cv(c) for c in cs[-3:]]
    if len(ps)!=3 or any(not p for p in ps): return False
    if bear: return all(p[3]<p[0] for p in ps) and ps[1][3]<ps[0][3] and ps[2][3]<ps[1][3]
    return all(p[3]>p[0] for p in ps) and ps[1][3]>ps[0][3] and ps[2][3]>ps[1][3]
def detect_pattern(cs):
    # Pattern ends at cs[-2]; cs[-1] is a separate fully closed follow-up candle.
    if len(cs)<5: return None
    first,mid,pat=cs[-4],cs[-3],cs[-2]; prior=cs[-3]; p=cv(pat); ref=cv(cs[-5]); names=[]
    if bull_engulf(prior,pat): names.append(('call','Bullish Engulfing'))
    if bear_engulf(prior,pat): names.append(('put','Bearish Engulfing'))
    up=bool(p and ref and p[3]>ref[3]); down=bool(p and ref and p[3]<ref[3])
    if hammer(pat) and down: names.append(('call','Hammer'))
    if hammer(pat,True) and down: names.append(('call','Inverted Hammer'))
    if hammer(pat,True) and up: names.append(('put','Shooting Star'))
    if hammer(pat) and up: names.append(('put','Hanging Man'))
    if morning_star(first,mid,pat): names.append(('call','Morning Star'))
    if morning_star(first,mid,pat,True): names.append(('put','Evening Star'))
    if piercing(prior,pat): names.append(('call','Piercing Line'))
    if dark_cloud(prior,pat): names.append(('put','Dark Cloud Cover'))
    if three_soldiers(cs[-4:-1]): names.append(('call','Three White Soldiers'))
    if three_soldiers(cs[-4:-1],True): names.append(('put','Three Black Crows'))
    if not names: return None
    inc('patterns_found'); f=cv(cs[-1])
    if not f or not p: inc('followup_rejected'); return None
    for direction,name in names:
        if direction=='call' and f[3]>f[0] and f[3]>p[3]: inc('followup_confirmed'); return {'direction':direction,'pattern':name}
        if direction=='put' and f[3]<f[0] and f[3]<p[3]: inc('followup_confirmed'); return {'direction':direction,'pattern':name}
    inc('followup_rejected'); return None
def evaluate_signal(cs):
    if len(cs)<MOMENTUM_LOOKBACK+MOMENTUM_CONFIRM_PERIOD+3: return None
    pat=detect_pattern(cs)
    if not pat: return None
    try: closes=[float(c['close']) for c in cs[:-1]]; candle_time=int(cs[-2].get('from',0))
    except (KeyError,TypeError,ValueError): return None
    m10=momentum_series(closes,10); m20=momentum_series(closes,20)
    if len(m10)<MOMENTUM_LOOKBACK+2 or len(m20)<3: return None
    cur,prev,before=m10[-1],m10[-2],m10[-3]; hist=m10[-(MOMENTUM_LOOKBACK+1):-1]
    low=percentile_value(hist,EXTREME_PERCENTILE); high=percentile_value(hist,1-EXTREME_PERCENTILE); d=pat['direction']
    extreme=cur<=low if d=='call' else cur>=high
    if extreme: inc('m10_extreme_pass')
    else: inc('m10_extreme_rejected'); return None
    turn=abs(cur-prev)>=MIN_TURN_DISTANCE and ((cur>prev and prev<=before) if d=='call' else (cur<prev and prev>=before))
    if turn: inc('m10_turn_pass')
    else: inc('m10_turn_rejected'); return None
    trend=(m20[-1]>m20[-2]) if d=='call' else (m20[-1]<m20[-2])
    if trend: inc('m20_trend_pass')
    else: inc('m20_trend_rejected'); return None
    return {'direction':d,'momentum':round(cur,5),'momentum20':round(m20[-1],5),'candle_time':candle_time,'pattern':pat['pattern'],'confirmation':'M20 trend + {} + next fully closed candle'.format(pat['pattern'])}
def numeric_profit(v):
    if v is None or isinstance(v,bool): return None
    if isinstance(v,(int,float)): return float(v) if math.isfinite(float(v)) else None
    if isinstance(v,str):
        if v.strip().lower() in ('','false','none','null','pending','open','win','won','loss','lost','loose','draw','unknown'): return None
        try: return float(v)
        except (ValueError,OverflowError): return None
    if isinstance(v,dict):
        for k in ('profit','profit_amount','profitAmount','close_profit','closeProfit','net_profit','netProfit','win_amount','winAmount','result','data','betinfo','bet_info','option'):
            if k in v:
                x=numeric_profit(v[k])
                if x is not None: return x
    if isinstance(v,(list,tuple)):
        for x in v:
            if isinstance(x,dict):
                n=numeric_profit(x)
                if n is not None: return n
    return None
def query_order_profit(oid):
    for name in ('check_win_v4','check_win','get_betinfo','get_betinfo_v2'):
        fn=getattr(IQ,name,None)
        if not callable(fn): continue
        try: val=numeric_profit(fn(oid))
        except Exception as e: logging.debug('%s failed: %s',name,e); continue
        if val is not None: return val
    return None
def settle_result(asset,direction,broker_profit,m10=None,oid=''):
    if broker_profit is None: raise ValueError('Cannot settle without numeric broker result')
    net=-STAKE if float(broker_profit)==0 else float(broker_profit); win=net>0; label='WIN' if win else 'LOSS'
    with STATE_LOCK:
        STATE['profit']=round(float(STATE.get('profit',0))+net,2); STATE['resolved']=int(STATE.get('resolved',0))+1
        if win: STATE['wins']=int(STATE.get('wins',0))+1; STATE['consecutive_losses']=0
        else: STATE['losses']=int(STATE.get('losses',0))+1; STATE['consecutive_losses']=int(STATE.get('consecutive_losses',0))+1
        for k in ('pending_order_id','pending_asset','pending_direction','pending_started'): STATE[k]=None
    log_trade(asset,direction,m10,label,round(net,2),oid); save_state(True)
    logging.info('%s %s %s %+.2f | %s',label,asset,direction,net,format_stats())
    send_telegram('<b>Trade result: {}</b>\n{} {}\nBroker: ${:+.2f}\nAccounted: ${:+.2f}\n{}'.format(label,asset,direction.upper(),broker_profit,net,format_stats())); stop_if_limit_reached()
def wait_result(oid,asset,direction,m10):
    notice=0
    while not STOP_EVENT.is_set():
        value=query_order_profit(oid)
        if value is not None: settle_result(asset,direction,value,m10,oid); return
        if time.time()-notice>=60: logging.warning('Broker result unreadable for order %s; keeping pending',oid); notice=time.time()
        time.sleep(ORDER_RESULT_RETRY_SECONDS)
def recover_pending_order():
    oid=STATE.get('pending_order_id')
    if oid: logging.warning('Recovering pending order %s',oid); wait_result(oid,STATE.get('pending_asset') or 'UNKNOWN',STATE.get('pending_direction') or 'unknown',None)
def place_trade(item,sig):
    asset=item['name']; direction=sig['direction']
    with TRADE_LOCK:
        if STOP_EVENT.is_set() or stop_if_limit_reached() or STATE.get('pending_order_id'): return
        if item.get('id') is not None:
            try: OP_code.ACTIVES[asset]=int(item['id'])
            except Exception: pass
        try: result=IQ.buy(STAKE,asset,direction,EXPIRY_MINUTES)
        except Exception as e: log_trade(asset,direction,sig['momentum'],'ORDER_ERROR'); logging.warning('Order error %s: %s',asset,e); return
        ok,oid=(bool(result[0]),result[1] if len(result)>1 else None) if isinstance(result,tuple) else (bool(result),None)
        if not ok or oid is None:
            REJECTED_ASSETS[asset]=time.time()+REJECTED_ASSET_COOLDOWN_SECONDS; inc('order_rejected'); log_trade(asset,direction,sig['momentum'],'ORDER_REJECTED'); logging.warning('Order rejected: %r',result); return
        STATE['pending_order_id']=oid; STATE['pending_asset']=asset; STATE['pending_direction']=direction; STATE['pending_started']=now_lagos().isoformat(timespec='seconds'); save_state(True)
        log_trade(asset,direction,sig['momentum'],'OPENED','',oid)
        send_telegram('<b>Practice trade opened</b>\n{} {}\nStake: ${:.2f}\nM10: {:.5f} M20: {:.5f}\n{}\n{}'.format(asset,direction.upper(),STAKE,sig['momentum'],sig['momentum20'],sig['confirmation'],format_stats()))
    wait_result(oid,asset,direction,sig['momentum'])
def report_diagnostics():
    with DIAG_LOCK: s=dict(DIAG); [DIAG.__setitem__(k,0) for k in DIAG]
    logging.info('SCAN DIAGNOSTICS | scanned=%s candle_ok=%s empty=%s insufficient=%s evaluated=%s patterns=%s followup_ok=%s followup_rejected=%s m10_extreme_ok=%s m10_extreme_rejected=%s m10_turn_ok=%s m10_turn_rejected=%s m20_trend_ok=%s m20_trend_rejected=%s setups=%s duplicates=%s order_rejected=%s',*[s[k] for k in DIAG_KEYS])
def main():
    connect_iq(); refresh_assets(True)
    if not ASSETS: raise RuntimeError('No supported forex or crypto assets discovered')
    if stop_if_limit_reached(): return
    if STATE.get('pending_order_id'): recover_pending_order()
    if STOP_EVENT.is_set() or stop_if_limit_reached():
        return
    send_telegram('<b>Scanner started</b>\nPRACTICE only\nNormal/OTC forex + normal/OTC crypto; M10 extreme + turn; M20 trend; 12 patterns + separate closed confirmation candle\n{}'.format(format_stats()))
    heartbeat=time.time(); diagnostic=time.time()
    while not STOP_EVENT.is_set():
        if stop_if_limit_reached(): break
        try:
            if not IQ.check_connect(): raise ConnectionError('Connection lost')
        except Exception:
            logging.warning('Connection lost; reconnecting')
            try:
                r=IQ.connect(); ok=r[0] if isinstance(r,tuple) else bool(r)
                if not ok: raise ConnectionError('Reconnect rejected')
                IQ.change_balance(BALANCE_MODE); time.sleep(2)
                if not IQ.check_connect(): raise ConnectionError('Reconnect not ready')
                refresh_assets(True)
            except Exception as e: logging.warning('Reconnect failed: %s',e); time.sleep(RECONNECT_SECONDS); continue
        if time.time()-ASSET_REFRESH_AT>=ASSET_REFRESH_SECONDS: refresh_assets(True)
        if time.time()-heartbeat>=HEARTBEAT_SECONDS:
            logging.info('HEARTBEAT | %s | assets=%s | %s',now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'),len(ASSETS),format_stats()); send_telegram('<b>Scanner heartbeat</b>\n{}\n{}'.format(now_lagos().strftime('%Y-%m-%d %H:%M:%S WAT'),format_stats())); heartbeat=time.time()
        if time.time()-diagnostic>=DIAGNOSTIC_LOG_SECONDS: report_diagnostics(); diagnostic=time.time()
        for item in list(ASSETS):
            if STOP_EVENT.is_set() or stop_if_limit_reached(): break
            asset=item['name']
            try:
                inc('assets_scanned'); until=REJECTED_ASSETS.get(asset,0)
                if time.time()<until: continue
                if until: REJECTED_ASSETS.pop(asset,None)
                candles=get_closed_candles(asset)
                if len(candles)<MOMENTUM_LOOKBACK+MOMENTUM_CONFIRM_PERIOD+3: continue
                inc('signals_checked'); sig=evaluate_signal(candles)
                if not sig: continue
                inc('setups_found'); key=str(sig['candle_time']); last=STATE.setdefault('last_signals',{})
                if last.get(asset)==key: inc('duplicate_signals'); continue
                last[asset]=key; save_state(); log_trade(asset,sig['direction'],sig['momentum'],'SIGNAL')
                logging.info('CONFIRMED %s [%s] %s M10=%.5f M20=%.5f pattern=%s',asset,item.get('market_type','unknown'),sig['direction'],sig['momentum'],sig['momentum20'],sig['pattern'])
                send_telegram('<b>Confirmed setup</b>\n{} {}\nPattern: {}\nM10: {:.5f} M20: {:.5f}\n{}'.format(asset,sig['direction'].upper(),sig['pattern'],sig['momentum'],sig['momentum20'],sig['confirmation']))
                if AUTO_TRADE: place_trade(item,sig)
            except Exception as e: logging.warning('Scan error for %s: %s',asset,e)
            time.sleep(.15)
        time.sleep(SCAN_INTERVAL)
    reason=STATE.get('stop_reason') or risk_stop_reason() or 'Scanner stopped'; logging.info('Finished: %s | %s',reason,format_stats()); send_telegram('<b>Scanner finished</b>\n{}\n{}'.format(reason,format_stats()))
    # A reached daily risk limit stops scanning; do not auto-dispatch another run.
if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: STOP_EVENT.set(); save_state(True); logging.info('Stopped by keyboard interrupt')
    except Exception as e: logging.exception('Fatal scanner error'); send_telegram('<b>Scanner error</b>\n{}'.format(e)); save_state(True); raise
