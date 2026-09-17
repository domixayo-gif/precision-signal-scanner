#!/usr/bin/env python3
"""PRECISION SCANNER V3.9 - complete replacement.
Bybit public candles -> 5M trend + 1M entry -> filters -> score -> Telegram.
Research/test framework only; no profit guarantee and no trade execution.
"""
from __future__ import annotations
import base64,json,math,os,sys,time
from datetime import datetime,timezone
from typing import Any,Optional
import requests

VERSION='V3.9'
ENDPOINTS=['https://api.bybit.com','https://api.bytick.com']
SYMBOLS={'EURUSD':'EURUSDUSDT','GBPUSD':'GBPUSDUSDT','USDJPY':'USDJPYUSDT'}
OTC_ENABLED=False
TF5='5'; TF1='1'; EXPIRY=5
MIN_SCORE=85; BORDERLINE=80; MIN_DOM=3; MIN_ADX=18; MIN_CANDLE=.50
CALL_RSI=(43,68); PUT_RSI=(32,57); LOCK=300; LIMIT=220; TIMEOUT=20; RETRIES=3
TRACKER_FILE='tracker.json'; MAX_ITEMS=1000
TG=os.getenv('TELEGRAM_TOKEN','').strip(); CHAT=os.getenv('TELEGRAM_CHAT_ID','').strip()
GH=os.getenv('GITHUB_TOKEN','').strip(); REPO=os.getenv('GITHUB_REPOSITORY','').strip(); ACTIONS=os.getenv('GITHUB_ACTIONS','').lower()=='true'
S=requests.Session(); S.headers.update({'User-Agent':f'PrecisionScanner/{VERSION}','Accept':'application/json'})
WORKING=None

def now(): return datetime.now(timezone.utc).isoformat()
def unix(): return int(time.time())
def f(x):
    try:
        y=float(x); return y if math.isfinite(y) else None
    except: return None
def price(x):
    if x is None:return 'N/A'
    return f'{x:.3f}' if abs(x)>=100 else f'{x:.5f}' if abs(x)>=1 else f'{x:.8f}'
def default():
    return {'version':VERSION,'signals':[],'borderline':[],'metadata':{'last_scan':None,'last_signal':None,'scan_count':0,'signal_locks':{},'processed_keys':[],'delivery_failed_keys':[],'telegram_offset':None,'working_bybit_endpoint':None}}

def load():
    try:
        with open(TRACKER_FILE,encoding='utf8') as h:d=json.load(h)
        b=default(); b.update({k:d[k] for k in ('signals','borderline') if k in d}); b['metadata'].update(d.get('metadata',{})); return b
    except:return default()
T=load()
def save_local():
    with open(TRACKER_FILE,'w',encoding='utf8') as h: json.dump(T,h,indent=2,ensure_ascii=False)
def gh_save():
    save_local()
    if not ACTIONS or not GH or not REPO:return False
    u=f'https://api.github.com/repos/{REPO}/contents/{TRACKER_FILE}'; hd={'Authorization':f'Bearer {GH}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':f'PrecisionScanner/{VERSION}'}
    try:
        r=S.get(u,headers=hd,timeout=TIMEOUT); sha=r.json().get('sha') if r.status_code==200 else None
        if r.status_code not in (200,404): print('[TRACKER] GET',r.status_code,r.text[:300]); return False
        raw=json.dumps(T,indent=2,ensure_ascii=False).encode(); p={'message':f'Update tracker {VERSION}','content':base64.b64encode(raw).decode()};
        if sha:p['sha']=sha
        r=S.put(u,headers=hd,json=p,timeout=TIMEOUT); print('[TRACKER] GitHub save',r.status_code); return r.status_code in (200,201)
    except Exception as e: print('[TRACKER] save error',e); return False

def tg(method,**kw):
    if not TG:return None
    try:return S.post(f'https://api.telegram.org/bot{TG}/{method}',timeout=TIMEOUT+5,**kw)
    except Exception as e: print('[TELEGRAM]',e); return None
def send(text):
    if not TG or not CHAT: print('[TELEGRAM] credentials missing'); return False
    r=tg('sendMessage',json={'chat_id':CHAT,'text':text[:3900],'disable_web_page_preview':True}); ok=bool(r and r.status_code==200)
    if not ok: print('[TELEGRAM] send failed',r.status_code if r else 'exception',r.text[:300] if r else '')
    return ok
def updates():
    if not TG:return []
    p={'timeout':1,'limit':20}; off=T['metadata'].get('telegram_offset')
    if off is not None:p['offset']=off
    r=tg('getUpdates',params=p)
    if not r or r.status_code!=200:return []
    try:return r.json().get('result',[])
    except:return []

def ema(a,n):
    if len(a)<n:return [None]*len(a)
    z=[None]*len(a); prev=sum(a[:n])/n; z[n-1]=prev; k=2/(n+1)
    for i in range(n,len(a)):prev=(a[i]-prev)*k+prev; z[i]=prev
    return z
def rsi(a,n=14):
    z=[None]*len(a)
    if len(a)<=n:return z
    g=sum(max(a[i]-a[i-1],0) for i in range(1,n+1))/n; l=sum(max(a[i-1]-a[i],0) for i in range(1,n+1))/n
    def q(g,l):return 100 if l==0 else 100-100/(1+g/l)
    z[n]=q(g,l)
    for i in range(n+1,len(a)):
        d=a[i]-a[i-1]; g=((g*(n-1))+max(d,0))/n; l=((l*(n-1))+max(-d,0))/n; z[i]=q(g,l)
    return z
def atr(c,n=14):
    if len(c)<=n:return [None]*len(c)
    tr=[0]
    for i in range(1,len(c)):tr.append(max(c[i]['h']-c[i]['l'],abs(c[i]['h']-c[i-1]['c']),abs(c[i]['l']-c[i-1]['c'])))
    z=[None]*len(c); p=sum(tr[1:n+1])/n; z[n]=p
    for i in range(n+1,len(c)):p=((p*(n-1))+tr[i])/n; z[i]=p
    return z
def macd(a):
    e12,e26=ema(a,12),ema(a,26); m=[None]*len(a); idx=[]; vals=[]
    for i in range(len(a)):
        if e12[i] is not None and e26[i] is not None:m[i]=e12[i]-e26[i]; idx.append(i); vals.append(m[i])
    se=ema(vals,9); sig=[None]*len(a); hist=[None]*len(a)
    for j,i in enumerate(idx):sig[i]=se[j]; hist[i]=m[i]-sig[i] if sig[i] is not None else None
    return hist
def adx(c,n=14):
    L=len(c); tr=[0]*L; pd=[0]*L; md=[0]*L
    for i in range(1,L):
        tr[i]=max(c[i]['h']-c[i]['l'],abs(c[i]['h']-c[i-1]['c']),abs(c[i]['l']-c[i-1]['c']))
        u=c[i]['h']-c[i-1]['h']; d=c[i-1]['l']-c[i]['l']; pd[i]=u if u>d and u>0 else 0; md[i]=d if d>u and d>0 else 0
    z=[None]*L; plus=[None]*L; minus=[None]*L; dx=[None]*L
    if L<=2*n:return z,plus,minus
    at=sum(tr[1:n+1])/n; pp=sum(pd[1:n+1])/n; mm=sum(md[1:n+1])/n
    for i in range(n,L):
        if i>n:at=((at*(n-1))+tr[i])/n; pp=((pp*(n-1))+pd[i])/n; mm=((mm*(n-1))+md[i])/n
        if at:
            plus[i]=100*pp/at; minus[i]=100*mm/at; den=plus[i]+minus[i]
            if den:dx[i]=100*abs(plus[i]-minus[i])/den
    valid=[x for x in dx if x is not None]
    if len(valid)<n:return z,plus,minus
    # first n valid DX values seed ADX
    count=0; start=None; seed=0
    for i in range(n,L):
        if dx[i] is not None:
            seed+=dx[i]; count+=1
            if count==n:start=i; break
    if start is None:return z,plus,minus
    p=seed/n; z[start]=p
    for i in range(start+1,L):
        if dx[i] is not None:p=((p*(n-1))+dx[i])/n; z[i]=p
    return z,plus,minus

def candles(rows):
    out=[]
    for r in rows:
        if len(r)<5:continue
        ts,o,h,l,c=map(f,r[:5])
        if None in (ts,o,h,l,c) or h<l:continue
        out.append({'t':ts,'o':o,'h':h,'l':l,'c':c})
    return list(reversed(out))
def fetch(sym,interval):
    global WORKING
    eps=([WORKING] if WORKING else [])+[e for e in ENDPOINTS if e!=WORKING]
    last='unknown'
    for ep in eps:
        for attempt in range(1,RETRIES+1):
            try:
                r=S.get(ep+'/v5/market/kline',params={'category':'linear','symbol':sym,'interval':interval,'limit':LIMIT},timeout=TIMEOUT)
                if r.status_code!=200:
                    last=f'HTTP {r.status_code}: {r.text[:250]}'; print(f'[BYBIT] {sym} {interval} {last}')
                    if r.status_code in (400,401,403,404):break
                    time.sleep(attempt*2);continue
                p=r.json()
                if p.get('retCode')!=0:
                    last=f"retCode={p.get('retCode')} {p.get('retMsg')}"; print('[BYBIT]',last); break
                rows=p.get('result',{}).get('list',[])
                if not rows: last='empty kline'; break
                WORKING=ep; T['metadata']['working_bybit_endpoint']=ep; print(f'[BYBIT] OK {sym} {interval}: {len(rows)} candles via {ep}'); return rows
            except Exception as e:
                last=str(e); print(f'[BYBIT] {sym} {interval} exception {attempt}/{RETRIES}: {e}'); time.sleep(min(attempt*2,5))
    raise RuntimeError(f'Unable to fetch {sym} {interval}: {last}')

def trend(c):
    a=[x['c'] for x in c]; e9,e21,e50=ema(a,9)[-1],ema(a,21)[-1],ema(a,50)[-1]
    if None in (e9,e21,e50):return 'NEUTRAL'
    if e9>e21>e50 or (e9>e21 and a[-1]>e21):return 'BULLISH'
    if e9<e21<e50 or (e9<e21 and a[-1]<e21):return 'BEARISH'
    return 'NEUTRAL'
def structure(c,n=8):
    if len(c)<n+2:return 'NEUTRAL'
    a=c[-n:]; h1=max(x['h'] for x in a[:n//2]); h2=max(x['h'] for x in a[n//2:]); l1=min(x['l'] for x in a[:n//2]); l2=min(x['l'] for x in a[n//2:])
    if h2>h1 and l2>l1:return 'BULLISH'
    if h2<h1 and l2<l1:return 'BEARISH'
    return 'BULLISH' if a[-1]['c']>a[0]['c'] else 'BEARISH' if a[-1]['c']<a[0]['c'] else 'NEUTRAL'
def strength(x):
    r=x['h']-x['l']; return abs(x['c']-x['o'])/r if r>0 else 0
def cdir(x):return 'BULLISH' if x['c']>x['o'] else 'BEARISH' if x['c']<x['o'] else 'NEUTRAL'

def analyze(name,sym):
    print(f'[SCAN] {name} ({sym}) mode=NORMAL')
    c5,c1=candles(fetch(sym,TF5)),candles(fetch(sym,TF1))
    if len(c5)<60 or len(c1)<60:raise RuntimeError(f'{name}: insufficient candles')
    a5=[x['c'] for x in c5]; tr5=trend(c5); en1=trend(c1); st=structure(c5); rv=rsi(a5)[-1]; mh=macd(a5)[-1]; av,pdi,mdi=adx(c5); ax=av[-1]; pi=pdi[-1]; mi=mdi[-1]; ec=c1[-1]; cs=strength(ec)
    votes={'CALL':0,'PUT':0}
    for d,pts in ((tr5,4),(en1,4),(st,3)):
        if d=='BULLISH':votes['CALL']+=pts
        elif d=='BEARISH':votes['PUT']+=pts
    if pi is not None and mi is not None:
        if pi>mi:votes['CALL']+=2
        elif mi>pi:votes['PUT']+=2
    if mh is not None:
        if mh>0:votes['CALL']+=2
        elif mh<0:votes['PUT']+=2
    if rv is not None:
        if CALL_RSI[0]<=rv<=CALL_RSI[1]:votes['CALL']+=1
        if PUT_RSI[0]<=rv<=PUT_RSI[1]:votes['PUT']+=1
    if votes['CALL']==votes['PUT']: direction='NO TRADE';dom=0
    else:direction='CALL' if votes['CALL']>votes['PUT'] else 'PUT';dom=abs(votes['CALL']-votes['PUT'])
    blockers=[]; sc={k:0 for k in ('Trend','Structure','ADX/DMI','MACD','RSI','Entry','Pullback','Candle','Room','Extension')}
    if (direction=='CALL' and tr5=='BULLISH') or (direction=='PUT' and tr5=='BEARISH'):sc['Trend']=20
    else:blockers.append('5M trend mismatch')
    if (direction=='CALL' and st=='BULLISH') or (direction=='PUT' and st=='BEARISH'):sc['Structure']=10
    else:blockers.append('Structure not aligned')
    if ax is None:blockers.append('ADX unavailable')
    elif ax<MIN_ADX:blockers.append('ADX too low')
    else:
        sc['ADX/DMI']=10
        if pi is None or mi is None:blockers.append('DMI unavailable')
        elif not ((direction=='CALL' and pi>mi) or (direction=='PUT' and mi>pi)):blockers.append('DMI mismatch')
    if mh is None:blockers.append('MACD unavailable')
    elif (direction=='CALL' and mh>0) or (direction=='PUT' and mh<0):sc['MACD']=10
    else:blockers.append('MACD mismatch')
    if rv is None:blockers.append('RSI unavailable')
    elif (direction=='CALL' and CALL_RSI[0]<=rv<=CALL_RSI[1]) or (direction=='PUT' and PUT_RSI[0]<=rv<=PUT_RSI[1]):sc['RSI']=10
    else:blockers.append('RSI outside zone')
    if (direction=='CALL' and en1=='BULLISH') or (direction=='PUT' and en1=='BEARISH'):sc['Entry']=15
    else:blockers.append('1M entry mismatch')
    recent=c1[-5:]
    pb=(sum(cdir(x)=='BEARISH' for x in recent) if direction=='CALL' else sum(cdir(x)=='BULLISH' for x in recent))
    pull=10 if cdir(ec)==('BULLISH' if direction=='CALL' else 'BEARISH') and pb>=2 else 6 if cdir(ec)==('BULLISH' if direction=='CALL' else 'BEARISH') and pb>=1 else 2
    if pull>=6:sc['Pullback']=10
    else:blockers.append('No clean pullback')
    if cdir(ec)==('BULLISH' if direction=='CALL' else 'BEARISH') and cs>=MIN_CANDLE:sc['Candle']=5
    elif cdir(ec)!=('BULLISH' if direction=='CALL' else 'BEARISH'):blockers.append('Confirmation candle mismatch')
    else:blockers.append('Confirmation candle weak')
    recent20=c5[-21:-1]; cur=ec['c']; ranges=[x['h']-x['l'] for x in recent20 if x['h']>x['l']]; avg=sum(ranges)/len(ranges) if ranges else 0
    room=(max(x['h'] for x in recent20)-cur if direction=='CALL' else cur-min(x['l'] for x in recent20))/avg if avg else 0
    roomscore=5 if room>=2 else 4 if room>=1.2 else 3 if room>=.7 else 2 if room>=.4 else 1
    if roomscore>=3:sc['Room']=5
    else:blockers.append('Insufficient room')
    e21=ema(a5,21)[-1]; at=atr(c5)[-1]; ratio=abs(cur-e21)/at if e21 is not None and at else 99
    exscore=5 if ratio<=.75 else 4 if ratio<=1.1 else 3 if ratio<=1.5 else 2 if ratio<=2 else 1
    if exscore>=3:sc['Extension']=5
    else:blockers.append('Price too extended')
    if dom<MIN_DOM:blockers.append('Weak directional dominance')
    total=sum(sc.values()) if direction!='NO TRADE' else 0
    final=direction if direction in ('CALL','PUT') and total>=MIN_SCORE and not blockers else 'NO TRADE'
    return {'symbol':name,'bybit_symbol':sym,'mode':'NORMAL','signal':final,'candidate':direction,'score':total,'dominance':dom,'blockers':blockers,'trend5':tr5,'entry1':en1,'structure':st,'adx':ax,'plus_di':pi,'minus_di':mi,'rsi':rv,'macd_hist':mh,'price':cur,'entry_time_ms':int(ec['t']),'candle_strength':cs,'pullback_score':pull,'room_score':roomscore,'extension_score':exscore,'breakdown':sc}

def sid(r):return f"{r['symbol']}-{r['signal']}-{r['entry_time_ms']}"
def key(r):return f"{r['mode']}:{r['symbol']}:{r['signal']}:{r['entry_time_ms']}"
def locked(r):return unix()<int(T['metadata'].get('signal_locks',{}).get(r['mode']+':'+r['symbol'],0))
def process(r):
    if r['signal'] not in ('CALL','PUT'):return 'rejected',None
    if r['score']<MIN_SCORE or r['blockers']:return 'rejected',None
    k=key(r); id_=sid(r)
    if k in T['metadata'].get('processed_keys',[]) or any(x.get('signal_id')==id_ for x in T['signals']):return 'duplicate',id_
    if locked(r):T['metadata']['processed_keys'].append(k);return 'locked',id_
    rec={**r,'signal_id':id_,'created_at':now(),'created_at_unix':unix(),'result':'PENDING','reference_expiry_minutes':EXPIRY}
    T['signals'].append(rec);T['metadata']['last_signal']=now();T['metadata'].setdefault('processed_keys',[]).append(k);T['metadata'].setdefault('signal_locks',{})[r['mode']+':'+r['symbol']]=unix()+LOCK
    T['signals']=T['signals'][-MAX_ITEMS:];T['metadata']['processed_keys']=T['metadata']['processed_keys'][-MAX_ITEMS*2:];save_local()
    msg=(f"🧠 PRECISION SCANNER {VERSION}\n━━━━━━━━━━━━━━━━━━\n🟢 NEW QUALIFIED SIGNAL\n"
         f"💱 {r['symbol']} {'CALL / UP' if r['signal']=='CALL' else 'PUT / DOWN'}\n🎯 Score: {r['score']}/100\n⏱ Reference expiry: {EXPIRY} minutes\n💰 Price: {price(r['price'])}\n"
         f"📊 5M Trend: {r['trend5']}\n📈 1M Entry: {r['entry1']}\n🏗 Structure: {r['structure']}\n📐 ADX: {r['adx']:.1f}\n📉 RSI: {r['rsi']:.1f}\n↗️ +DI: {r['plus_di']:.1f}\n↘️ -DI: {r['minus_di']:.1f}\n〽️ MACD Hist: {r['macd_hist']:.5f}\n🕯 Candle: {r['candle_strength']:.2f}\n↩️ Pullback: {r['pullback_score']}/10\n🚪 Room: {r['room_score']}/5\n📏 Extension: {r['extension_score']}/5\n🆔 {id_}\n🕒 Created: {now()}\n\n⚠️ Test/research only. Score is setup quality, not win probability. No profit guarantee. Scanner does not place trades.")
    if send(msg):status='alert_sent'
    else:T['metadata'].setdefault('delivery_failed_keys',[]).append(k);status='delivery_failed'
    gh_save();return status,id_

def scan():
    T['metadata']['last_scan']=now();T['metadata']['scan_count']=int(T['metadata'].get('scan_count',0))+1
    s={'qualified':0,'borderline':0,'rejected':0,'errors':0,'new':0,'alerts':0,'duplicates':0,'locked':0}
    for n,sy in SYMBOLS.items():
        try:
            r=analyze(n,sy); st,id_=process(r)
            if st=='alert_sent':s['qualified']+=1;s['new']+=1;s['alerts']+=1
            elif st=='delivery_failed':s['qualified']+=1;s['new']+=1
            elif st=='duplicate':s['duplicates']+=1
            elif st=='locked':s['locked']+=1
            else:s['rejected']+=1
        except Exception as e:s['errors']+=1;print(f'[ERROR] {n}: {e}')
    save_local();gh_save();print('[SUMMARY]',s);return s

def stats():
    a=T['signals'];w=sum(x.get('result')=='WIN' for x in a);l=sum(x.get('result')=='LOSS' for x in a);d=w+l;wr=w/d*100 if d else 0
    return f'📊 PRECISION SCANNER {VERSION}\n\nRecorded: {len(a)}\nWIN: {w}\nLOSS: {l}\nPending: {len(a)-d}\nMeasured win rate: {wr:.1f}% ({d} decided)\n\nHistorical tracker data only.'
def commands():
    manual=False
    for u in updates():
        uid=u.get('update_id');
        if isinstance(uid,int):T['metadata']['telegram_offset']=uid+1
        m=u.get('message') or u.get('channel_post') or {}; text=str(m.get('text') or '').strip()
        if not text:continue
        p=text.split(maxsplit=1); cmd=p[0].split('@')[0].lower(); arg=p[1].strip() if len(p)>1 else ''
        if cmd=='/start' or cmd=='/help':send(f'🧠 PRECISION SCANNER {VERSION}\n\n/scan - run a scan now\n/stats - tracker statistics\n/win SIGNAL_ID - record WIN\n/loss SIGNAL_ID - record LOSS\n\n5M trend + 1M entry. NO TRADE is allowed. Test framework only.')
        elif cmd=='/stats':send(stats())
        elif cmd=='/scan':send('🔎 Manual scan requested. Running one scan now.');manual=True
        elif cmd in ('/win','/loss'):
            found=next((x for x in T['signals'] if x.get('signal_id')==arg),None)
            if found:found['result']='WIN' if cmd=='/win' else 'LOSS';found['result_updated_at']=now();save_local();gh_save();send(f"✅ Recorded {'WIN' if cmd=='/win' else 'LOSS'}\n{arg}")
            else:send('❌ Signal not found.')
    save_local();return manual

def once():
    print(f'=== PRECISION SCANNER {VERSION} ONE-SHOT ==='); manual=commands();s=scan()
    if manual:send(f"🔎 MANUAL SCAN COMPLETE\nQualified: {s['qualified']}\nBorderline: {s['borderline']}\nRejected: {s['rejected']}\nErrors: {s['errors']}\nNew: {s['new']}\nAlerts: {s['alerts']}\nDuplicates: {s['duplicates']}\nLocked: {s['locked']}")
def loop():
    print(f'=== PRECISION SCANNER {VERSION} LOOP / 300s ===')
    while True:
        try:manual=commands();s=scan();
        except KeyboardInterrupt:break
        except Exception as e:print('[LOOP ERROR]',e);manual=False;s=None
        if manual and s:send(f"🔎 MANUAL SCAN COMPLETE\nQualified: {s['qualified']}\nAlerts: {s['alerts']}\nErrors: {s['errors']}")
        time.sleep(300)

if __name__=='__main__':
    print('[START]',VERSION,'Bybit public market data','OTC:',OTC_ENABLED)
    if sys.argv[1:] and sys.argv[1].lower()=='--loop':loop()
    elif not sys.argv[1:] or sys.argv[1].lower()=='--once':once()
    else: print('Usage: python scanner.py --once | --loop');sys.exit(1)
