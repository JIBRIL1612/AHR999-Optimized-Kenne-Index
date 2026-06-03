"""
Kenne Index x OKX 自动定投 (HTML邮件版本)
"""

import os, sys, json, hmac, base64, hashlib, time, logging, argparse
import datetime, csv, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent))
from kenne_index import analyze as kenne_analyze


# ─── 配置 ─────────────────────────────────────────────────────────────────────
CFG = {
    'API_KEY':        os.environ.get('OKX_API_KEY',        'YOUR_API_KEY'),
    'API_SECRET':     os.environ.get('OKX_API_SECRET',     'YOUR_API_SECRET'),
    'API_PASSPHRASE': os.environ.get('OKX_API_PASSPHRASE', 'YOUR_PASSPHRASE'),
    'SIMULATED': os.environ.get('SIMULATED', 'true').lower() != 'false',
    'BUDGET_MODE':        os.environ.get('BUDGET_MODE', 'MONTHLY').strip().upper(),
    'BUDGET_AMOUNT':      float(os.environ.get('BUDGET_AMOUNT', '700')),
    'RUN_INTERVAL_DAYS':  int(os.environ.get('RUN_INTERVAL_DAYS', '7')),
    'DATA_FILES': {
        'BTC': 'btc_4h_data_2018_to_2025.csv',
        'ETH': 'eth_4h_data_2017_to_2025.csv',
        'SOL': 'sol_4h_data_2020_to_2025.csv',
    },
    'INST_ID': {
        'BTC': 'BTC-USDT',
        'ETH': 'ETH-USDT',
        'SOL': 'SOL-USDT',
    },
    'MAX_WEIGHT':     {'BTC': 0.60, 'ETH': 0.50, 'SOL': 0.50},
    'MIN_ORDER_USDT': 5.0,
    'LOG_FILE':       'dca_log.json',
    'SMTP_HOST':     os.environ.get('SMTP_HOST',     'smtp.gmail.com'),
    'SMTP_PORT':     int(os.environ.get('SMTP_PORT', '587')),
    'SMTP_USER':     os.environ.get('SMTP_USER',     'your@gmail.com'),
    'SMTP_PASSWORD': os.environ.get('SMTP_PASSWORD', 'your_app_password'),
    'EMAIL_TO':      os.environ.get('EMAIL_TO',      'your@gmail.com'),
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('dca')


# ─── OKX 客户端 (保持不变) ─────────────────────────────────────────────────────
class OKXClient:
    BASE = 'https://www.okx.com'

    def __init__(self, key, secret, passphrase, simulated):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.simulated = simulated
        self.sess = requests.Session()
        self.sess.headers['Content-Type'] = 'application/json'

    @staticmethod
    def _ts():
        n = datetime.datetime.utcnow()
        return n.strftime('%Y-%m-%dT%H:%M:%S.') + f'{n.microsecond//1000:03d}Z'

    def _sign(self, ts, method, path, body=''):
        return base64.b64encode(
            hmac.new(self.secret.encode(), (ts + method + path + body).encode(),
                     hashlib.sha256).digest()
        ).decode()

    def _auth(self, method, path, body=''):
        ts = self._ts()
        h  = {'OK-ACCESS-KEY': self.key, 'OK-ACCESS-SIGN': self._sign(ts, method, path, body),
               'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': self.passphrase}
        if self.simulated:
            h['x-simulated-trading'] = '1'
        return h

    def _get(self, path, params=None, auth=False):
        qs   = ('?' + '&'.join(f'{k}={v}' for k, v in params.items())) if params else ''
        full = path + qs
        kw   = {'headers': self._auth('GET', full)} if auth else {}
        r    = self.sess.get(self.BASE + full, timeout=15, **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        s = json.dumps(body)
        r = self.sess.post(self.BASE + path, headers=self._auth('POST', path, s),
                           data=s, timeout=15)
        r.raise_for_status()
        return r.json()

    def candles(self, inst_id, bar='4H', before=None, after=None, limit=100):
        p = {'instId': inst_id, 'bar': bar, 'limit': str(limit)}
        if before is not None: p['before'] = str(before)
        if after  is not None: p['after']  = str(after)
        try:
            resp = self._get('/api/v5/market/history-candles', p)
        except Exception:
            resp = self._get('/api/v5/market/candles', p)
        if resp.get('code') != '0':
            raise RuntimeError(f"candles error: {resp.get('msg', resp)}")
        return resp.get('data', [])

    def balance(self, ccy='USDT'):
        resp = self._get('/api/v5/account/balance', {'ccy': ccy}, auth=True)
        if resp.get('code') != '0':
            raise RuntimeError(f"balance error: {resp}")
        for d in resp['data'][0]['details']:
            if d['ccy'] == ccy:
                return float(d['availBal'])
        return 0.0

    def buy_market_usdt(self, inst_id, usdt):
        return self._post('/api/v5/trade/order', {
            'instId': inst_id, 'tdMode': 'cash', 'side': 'buy',
            'ordType': 'market', 'sz': f'{usdt:.4f}', 'tgtCcy': 'quote_ccy',
        })


# ─── 数据更新 (保持不变) ───────────────────────────────────────────────────────
BAR_MS = 4 * 3600 * 1000


def _candle_to_row(c):
    ts  = int(c[0])
    odt = datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)
    cdt = datetime.datetime.fromtimestamp((ts + BAR_MS - 1) / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)
    return [
        odt.strftime('%Y-%m-%d %H:%M:%S.%f'),
        c[1], c[2], c[3], c[4], c[5],
        cdt.strftime('%Y-%m-%d %H:%M:%S') + '.999000',
        c[7], '0', '0', '0', '0',
    ]


class DataUpdater:
    def __init__(self, client):
        self.client = client

    def _last_ts_ms(self, path):
        lines = path.read_text(encoding='utf-8').strip().splitlines()
        if len(lines) < 2:
            return None
        for line in reversed(lines[1:]):
            parts = line.split(',')
            if len(parts) < 7:
                continue
            ot = parts[0].strip()
            ct = parts[6].strip()
            if not ot or not ct:
                continue
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
                try:
                    dt = datetime.datetime.strptime(ot, fmt)
                    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                except ValueError:
                    continue
        return None

    def _clean_tail(self, path):
        lines   = path.read_text(encoding='utf-8').splitlines()
        cleaned = [lines[0]] + [
            l for l in lines[1:]
            if len(l.split(',')) > 6
            and l.split(',')[0].strip()
            and l.split(',')[6].strip()
        ]
        removed = len(lines) - len(cleaned)
        if removed:
            path.write_text('\n'.join(cleaned) + '\n', encoding='utf-8')
        return removed

    def _fetch_after(self, inst_id, since_ms):
        collected, cursor = [], since_ms
        for _ in range(20):
            try:
                batch = self.client.candles(inst_id, bar='4H', before=cursor, limit=100)
            except Exception as e:
                log.error(f'candles fetch error: {e}')
                break
            if not batch:
                break
            closed = [c for c in batch if c[8] == '1']
            if closed:
                collected.extend(closed)
            newest = int(batch[0][0])
            if newest <= cursor:
                break
            cursor = newest
            if len(batch) < 100:
                break
            time.sleep(0.15)

        seen, unique = set(), []
        for c in collected:
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)
        unique.sort(key=lambda c: int(c[0]))
        return unique

    def update(self, symbol, csv_file):
        path = Path(csv_file)
        if not path.exists():
            log.warning(f'[{symbol}] CSV not found: {csv_file}')
            return 0

        removed = self._clean_tail(path)
        if removed:
            log.info(f'[{symbol}] removed {removed} incomplete tail rows')

        last_ms = self._last_ts_ms(path)
        if last_ms is None:
            log.warning(f'[{symbol}] no valid data in CSV')
            return 0

        gap     = (int(time.time() * 1000) - last_ms) // BAR_MS
        last_dt = datetime.datetime.fromtimestamp(
            last_ms / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)
        log.info(f'[{symbol}] local last: {last_dt.strftime("%Y-%m-%d %H:%M")} UTC  gap: ~{gap} bars')

        if gap < 1:
            log.info(f'[{symbol}] up to date')
            return 0

        new = [c for c in self._fetch_after(CFG['INST_ID'][symbol], since_ms=last_ms)
               if int(c[0]) > last_ms]

        if not new:
            log.info(f'[{symbol}] current bar not yet closed, no new data')
            return 0

        with open(path, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerows(_candle_to_row(c) for c in new)

        t0 = datetime.datetime.fromtimestamp(
            int(new[0][0]) / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)
        t1 = datetime.datetime.fromtimestamp(
            int(new[-1][0]) / 1000, tz=datetime.timezone.utc).replace(tzinfo=None)
        log.info(f'[{symbol}] +{len(new)} bars  '
                 f'{t0.strftime("%Y-%m-%d %H:%M")} -> {t1.strftime("%Y-%m-%d %H:%M")} UTC')
        return len(new)


# ─── 预算追踪 (保持不变) ───────────────────────────────────────────────────────
@dataclass
class Record:
    ts:       str
    symbol:   str
    inst_id:  str
    usdt:     float
    kenne_index: float
    mult:     float
    momentum: str
    order_id: str
    status:   str
    note:     str = ''


class Budget:
    def __init__(self):
        self.path         = Path(CFG['LOG_FILE'])
        self.mode         = CFG['BUDGET_MODE']
        self.amount       = CFG['BUDGET_AMOUNT']
        self.interval     = CFG['RUN_INTERVAL_DAYS']
        self.recs         = json.loads(self.path.read_text()) if self.path.exists() else []
        self._runs_per_month = 30.0 / self.interval

    def _save(self):
        self.path.write_text(json.dumps(self.recs, indent=2, ensure_ascii=False))

    def _month_str(self):
        return datetime.date.today().strftime('%Y-%m')

    def spent_this_month(self):
        m = self._month_str()
        return sum(
            r['usdt'] for r in self.recs
            if r.get('ts', '').startswith(m)
            and r.get('status') in ('filled', 'dry_run')
        )

    def monthly_remaining(self):
        return max(0.0, self.amount - self.spent_this_month())

    def this_run_amount(self):
        if self.mode == 'FIXED':
            return self.amount
        target_per_run = self.amount / self._runs_per_month
        remaining      = self.monthly_remaining()
        if remaining <= 0:
            return 0.0
        today  = datetime.date.today()
        next_m = datetime.date(
            today.year + (today.month == 12),
            today.month % 12 + 1, 1
        )
        days_left = max(1, (next_m - today).days)
        runs_left = max(1, round(days_left / self.interval))
        return min(target_per_run, remaining / runs_left)

    def add(self, r):
        self.recs.append(asdict(r))
        self._save()

    def summary_str(self):
        if self.mode == 'FIXED':
            return (f'mode=FIXED  per_run=${self.amount:.0f}'
                    f'  this_month_spent=${self.spent_this_month():.2f}')
        return (f'mode=MONTHLY  budget=${self.amount:.0f}/mo'
                f'  spent=${self.spent_this_month():.2f}'
                f'  remaining=${self.monthly_remaining():.2f}'
                f'  interval={self.interval}d')


# ─── 资金分配 (保持不变) ───────────────────────────────────────────────────────
def allocate(signals, budget_usdt):
    active = [s for s in signals if s['final_mult'] > 0]
    if not active or budget_usdt <= 0:
        return []

    norm = {s['symbol']: s['final_mult'] for s in active}
    for _ in range(3):
        total = sum(norm.values())
        norm  = {k: v / total for k, v in norm.items()}
        cap   = {k: min(v, CFG['MAX_WEIGHT'].get(k, 1.0)) for k, v in norm.items()}
        if sum(cap.values()) == 0:
            break
        norm = cap

    total = sum(norm.values())
    norm  = {k: v / total for k, v in norm.items()}
    return [{**s,
             'usdt_amount': round(norm.get(s['symbol'], 0) * budget_usdt, 2),
             'weight':      round(norm.get(s['symbol'], 0), 4)}
            for s in active]


# ─── 主流程 (保持不变) ─────────────────────────────────────────────────────────
def _make_client():
    return OKXClient(CFG['API_KEY'], CFG['API_SECRET'],
                     CFG['API_PASSPHRASE'], CFG['SIMULATED'])


def run_update():
    updater = DataUpdater(_make_client())
    total   = 0
    for sym, f in CFG['DATA_FILES'].items():
        try:
            total += updater.update(sym, f)
        except Exception as e:
            log.error(f'[{sym}] update failed: {e}')
    log.info(f'update done, {total} new bars total')


def run_dca(dry_run=False):
    log.info(f'--- Kenne Index DCA {"[dry-run]" if dry_run else "[live]"} ---')

    client  = _make_client()
    updater = DataUpdater(client)
    budget  = Budget()

    log.info('[1/4] updating market data')
    total_new = 0
    for sym, f in CFG['DATA_FILES'].items():
        try:
            total_new += updater.update(sym, f)
        except Exception as e:
            log.error(f'[{sym}] update failed, using local data: {e}')
    log.info(f'{total_new} new bars added' if total_new else 'data up to date')

    log.info('[2/4] budget check')
    run_amount = budget.this_run_amount()
    log.info(budget.summary_str() + f'  this_run=${run_amount:.2f}')
    if run_amount < CFG['MIN_ORDER_USDT']:
        log.info('budget for this run is below minimum order amount, skipping')
        return

    log.info('[3/4] calculating signals')
    signals = []
    for sym, f in CFG['DATA_FILES'].items():
        try:
            r = kenne_analyze(f, sym)
            if r:
                signals.append(r)
                log.info(f'  {sym}: kenne={r["kenne_index"]:.4f}  '
                         f'momentum={r["momentum"]}  mult={r["final_mult"]:.2f}x')
        except Exception as e:
            log.error(f'[{sym}] analysis failed: {e}')
    if not signals:
        log.error('all analysis failed, abort')
        return

    log.info('[4/4] allocating and ordering')
    allocs = allocate(signals, run_amount)
    if not allocs:
        log.info('all assets in hold zone, no orders this run')
        return

    for a in allocs:
        log.info(f'  plan: {a["symbol"]} ${a["usdt_amount"]:.2f}'
                 f'  weight={a["weight"]:.1%}  mult={a["final_mult"]:.2f}x')
    log.info(f'  total: ${sum(a["usdt_amount"] for a in allocs):.2f}')

    if not dry_run:
        try:
            avail = client.balance('USDT')
            log.info(f'OKX USDT balance: ${avail:.2f}')
            total = sum(a['usdt_amount'] for a in allocs)
            if avail < total * 0.95:
                ratio  = avail * 0.95 / total
                allocs = [{**a, 'usdt_amount': round(a['usdt_amount'] * ratio, 2)}
                          for a in allocs]
                allocs = [a for a in allocs if a['usdt_amount'] >= CFG['MIN_ORDER_USDT']]
                log.warning(f'insufficient balance, scaled to ${sum(a["usdt_amount"] for a in allocs):.2f}')
        except Exception as e:
            log.error(f'balance check failed: {e}')
            if not CFG['SIMULATED']:
                return

    for a in allocs:
        sym     = a['symbol']
        inst_id = CFG['INST_ID'][sym]
        usdt    = a['usdt_amount']
        ts      = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

        if usdt < CFG['MIN_ORDER_USDT']:
            log.info(f'  {sym}: ${usdt:.2f} below minimum, skip')
            budget.add(Record(ts, sym, inst_id, usdt, a['kenne_index'],
                              a['final_mult'], a['momentum'], '', 'skipped', 'below minimum'))
            continue

        if dry_run:
            log.info(f'  {sym}: [dry-run] would buy ${usdt:.2f} USDT')
            budget.add(Record(ts, sym, inst_id, usdt, a['kenne_index'],
                              a['final_mult'], a['momentum'], 'DRY_RUN', 'dry_run'))
            continue

        try:
            resp = client.buy_market_usdt(inst_id, usdt)
            if resp.get('code') == '0':
                oid = resp['data'][0]['ordId']
                log.info(f'  {sym}: filled  order={oid}  ${usdt:.2f} USDT')
                budget.add(Record(ts, sym, inst_id, usdt, a['kenne_index'],
                                  a['final_mult'], a['momentum'], oid, 'filled'))
            else:
                err = resp.get('data', [{}])[0].get('sMsg', str(resp))
                log.error(f'  {sym}: order failed: {err}')
                budget.add(Record(ts, sym, inst_id, usdt, a['kenne_index'],
                                  a['final_mult'], a['momentum'], '', 'failed', err))
        except Exception as e:
            log.error(f'  {sym}: exception: {e}')
            budget.add(Record(ts, sym, inst_id, usdt, a['kenne_index'],
                              a['final_mult'], a['momentum'], '', 'failed', str(e)))
        time.sleep(0.3)

    log.info(budget.summary_str())
    log.info('--- done ---')


# ─── 守护进程 (保持不变) ───────────────────────────────────────────────────────
def _next_run_time():
    now      = datetime.datetime.now()
    interval = datetime.timedelta(days=CFG['RUN_INTERVAL_DAYS'])
    target   = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += interval
    return target


def run_daemon(dry_run=False):
    log.info(f'daemon started, interval={CFG["RUN_INTERVAL_DAYS"]}d, next run at 09:00 (Ctrl+C to stop)')
    while True:
        t    = _next_run_time()
        secs = (t - datetime.datetime.now()).total_seconds()
        log.info(f'next run: {t.strftime("%Y-%m-%d %H:%M")} ({secs/3600:.1f}h from now)')
        time.sleep(secs)
        try:
            run_dca(dry_run=dry_run)
        except Exception as e:
            log.error(f'run_dca exception: {e}')
        time.sleep(60)


# ─── 历史记录 (保持不变) ───────────────────────────────────────────────────────
def show_history(month=None):
    recs = json.loads(Path(CFG['LOG_FILE']).read_text()) \
           if Path(CFG['LOG_FILE']).exists() else []
    if month:
        recs = [r for r in recs if r.get('ts', '').startswith(month)]
    if not recs:
        print('no records')
        return

    status_map = {'filled': 'OK', 'dry_run': 'DRY', 'failed': 'ERR', 'skipped': 'SKP'}
    total = 0.0
    print(f'\n{"date":<20} {"sym":<4} {"usdt":>8} {"kenne":>8} {"mult":>6} status')
    print('-' * 58)
    for r in recs:
        u  = r.get('usdt', 0)
        if r['status'] in ('filled', 'dry_run'):
            total += u
        st = status_map.get(r['status'], '???')
        print(f'  {r.get("ts",""):<18} {r["symbol"]:<4} '
              f'{u:>8.2f} {r.get("kenne_index",0):>8.4f} '
              f'{r.get("mult",0):>5.2f}x  {st}')
    print(f'{"-"*58}\n  total: ${total:.2f}')


# ─── HTML 邮件通知 (全新版本) ──────────────────────────────────────────────────
def _send_email(subject, body_html, body_text=None):
    """发送 HTML 邮件（支持纯文本备选）"""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = CFG['SMTP_USER']
    msg['To']      = CFG['EMAIL_TO']
    
    if body_text:
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    host, port = CFG['SMTP_HOST'], CFG['SMTP_PORT']
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(CFG['SMTP_USER'], CFG['SMTP_PASSWORD'])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.login(CFG['SMTP_USER'], CFG['SMTP_PASSWORD'])
                s.send_message(msg)
        log.info(f'email sent -> {CFG["EMAIL_TO"]}')
    except Exception as e:
        log.error(f'email failed: {e}')


def _calc_score(kenne):
    """Kenne Index 评分: 0.2=100分, 0.45=50分, 0.8=0分"""
    if kenne <= 0.2:
        return 100
    elif kenne >= 0.8:
        return 0
    else:
        return int(100 - (kenne - 0.2) / (0.8 - 0.2) * 100)


def _build_report(signals, allocs, budget):
    """构建 HTML 信号报告"""
    date = datetime.date.today().strftime('%Y-%m-%d')
    data_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    
    # 检查是否有参数更新
    refitted_symbols = [s['symbol'] for s in signals if s.get('refitted')]
    param_alert = ""
    if refitted_symbols:
        param_alert = f"""
        <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;margin:12px 0;">
            <div style="display:flex;align-items:center;gap:8px;">
                <span style="font-size:16px;">⚠️</span>
                <span style="font-size:14px;font-weight:600;color:#92400e;">幂律参数已更新</span>
            </div>
            <div style="font-size:12px;color:#a16207;margin-top:4px;">
                {' / '.join(refitted_symbols)} 的参数已自动重拟合，新参数已保存至 model_params.json
            </div>
        </div>
        """
    
    avg_kenne = sum(s['kenne_index'] for s in signals) / len(signals) if signals else 0
    overall_score = _calc_score(avg_kenne)
    
    score_color = '#22c55e' if overall_score >= 70 else '#f59e0b' if overall_score >= 40 else '#ef4444'
    score_text = '强力买入' if overall_score >= 70 else '可以买入' if overall_score >= 40 else '观望等待'
    
    if budget.mode == 'FIXED':
        budget_desc = f'每次固定 ${budget.amount:.0f} USDT'
    else:
        interval_label = {1: '日投', 7: '周投', 14: '双周投', 30: '月投'}.get(
            budget.interval, f'每{budget.interval}天投')
        budget_desc = f'${budget.amount:.0f}/月 · {interval_label}'
    
    # 趋势中文映射
    momentum_cn = {
        'STABLE': '趋势平稳',
        'SHARP_DROP': '短期急跌',
        'KNIFE_CATCH': '飞刀危险',
        'BOUNCE': '反弹回升',
    }
    
    # 信号卡片
    signal_cards = []
    for s in signals:
        ahr = s['kenne_index']
        prc = s.get('price', 0)
        score = _calc_score(ahr)
        
        # 确定区间和颜色
        # 彩虹条颜色分布: 红(0-16%)→橙(16-33%)→黄(33-50%)→蓝(50-66%)→浅绿(66-83%)→深绿(83-100%)
        # 对应区间: ≥1.2→0.8-1.2→0.6-0.8→0.45-0.6→0.35-0.45→0.25-0.35→<0.25
        if ahr < 0.25:
            zone = '极低估'
            zone_color = '#059669'
            zone_bg = '#6ee7b7'
            bar_color = '#059669'
            # <0.25 对应 83%-100%，线性插值
            bar_width = 83 + (ahr / 0.25) * 17
        elif ahr < 0.35:
            zone = '低估'
            zone_color = '#10b981'
            zone_bg = '#a7f3d0'
            bar_color = '#10b981'
            # 0.25-0.35 对应 66%-83%
            bar_width = 66 + ((ahr - 0.25) / 0.10) * 17
        elif ahr < 0.45:
            zone = '偏低'
            zone_color = '#22c55e'
            zone_bg = '#d1fae5'
            bar_color = '#22c55e'
            # 0.35-0.45 对应 50%-66%
            bar_width = 50 + ((ahr - 0.35) / 0.10) * 16
        elif ahr < 0.6:
            zone = '合理'
            zone_color = '#3b82f6'
            zone_bg = '#dbeafe'
            bar_color = '#3b82f6'
            # 0.45-0.6 对应 33%-50%
            bar_width = 33 + ((ahr - 0.45) / 0.15) * 17
        elif ahr < 0.8:
            zone = '偏贵'
            zone_color = '#f59e0b'
            zone_bg = '#fef3c7'
            bar_color = '#f59e0b'
            # 0.6-0.8 对应 16%-33%
            bar_width = 16 + ((ahr - 0.6) / 0.20) * 17
        elif ahr < 1.2:
            zone = '极贵'
            zone_color = '#ef4444'
            zone_bg = '#fee2e2'
            bar_color = '#ef4444'
            # 0.8-1.2 对应 0%-16%
            bar_width = ((ahr - 0.8) / 0.40) * 16
        else:
            zone = '停止'
            zone_color = '#dc2626'
            zone_bg = '#fee2e2'
            bar_color = '#dc2626'
            bar_width = 0
        
        score_bg = '#22c55e' if score >= 70 else '#f59e0b' if score >= 40 else '#ef4444'
        momentum_text = momentum_cn.get(s['momentum'], s['momentum'])
        
        signal_cards.append(f"""
        <div style="background:#f8fafc;border-radius:12px;padding:16px;margin:12px 0;border-left:4px solid {zone_color};">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span style="font-size:20px;font-weight:bold;color:#1f2937;">{s['symbol']}</span>
                <span style="background:{score_bg};color:white;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:bold;">{score}分</span>
            </div>
            <div style="font-size:14px;color:#6b7280;margin-bottom:8px;">价格 <span style="font-size:18px;font-weight:bold;color:#1f2937;">${prc:,.2f}</span> USDT</div>
            
            <!-- Kenne值进度条 -->
            <div style="background:#e5e7eb;border-radius:6px;height:24px;position:relative;overflow:hidden;margin-bottom:12px;">
                <div style="background:linear-gradient(90deg,#059669 0%,#10b981 20%,#22c55e 40%,#3b82f6 60%,#f59e0b 80%,#ef4444 100%);width:100%;height:100%;"></div>
                <div style="position:absolute;top:0;left:{bar_width}%;transform:translateX(-50%);width:3px;height:100%;background:#1f2937;box-shadow:0 0 4px rgba(0,0,0,0.5);"></div>
                <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:11px;font-weight:bold;color:white;text-shadow:0 1px 2px rgba(0,0,0,0.5);">Kenne {ahr:.4f}</div>
            </div>
            
            <div style="display:flex;gap:12px;margin-top:8px;flex-wrap:wrap;">
                <span style="background:{zone_bg};color:{zone_color};padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600;">{zone}</span>
                <span style="color:#6b7280;font-size:13px;">{momentum_text}</span>
                <span style="color:#3b82f6;font-weight:600;font-size:13px;">建议 {s['final_mult']:.2f}x</span>
            </div>
        </div>
        """)
    
    # 分配详情
    alloc_rows = []
    run_amount = budget.this_run_amount()
    if allocs:
        for a in allocs:
            alloc_rows.append(f"""
            <tr>
                <td style="padding:12px;border-bottom:1px solid #e5e7eb;font-weight:600;">{a['symbol']}</td>
                <td style="padding:12px;border-bottom:1px solid #e5e7eb;text-align:right;">${a['usdt_amount']:.2f}</td>
                <td style="padding:12px;border-bottom:1px solid #e5e7eb;text-align:center;">{a['weight']:.0%}</td>
                <td style="padding:12px;border-bottom:1px solid #e5e7eb;text-align:center;color:#3b82f6;font-weight:600;">{a['final_mult']:.2f}x</td>
            </tr>
            """)
        total = sum(a['usdt_amount'] for a in allocs)
        alloc_rows.append(f"""
        <tr style="background:#f9fafb;font-weight:bold;">
            <td style="padding:12px;border-top:2px solid #3b82f6;">合计</td>
            <td style="padding:12px;border-top:2px solid #3b82f6;text-align:right;color:#3b82f6;">${total:.2f}</td>
            <td style="padding:12px;border-top:2px solid #3b82f6;"></td>
            <td style="padding:12px;border-top:2px solid #3b82f6;"></td>
        </tr>
        """)
    else:
        alloc_rows = ['<tr><td colspan="4" style="padding:20px;text-align:center;color:#6b7280;">当前无买入信号，本次停止定投</td></tr>']
    
    # 预算信息 (仅MONTHLY模式显示)
    if budget.mode == 'MONTHLY':
        spent = budget.spent_this_month()
        remaining = budget.monthly_remaining()
        budget_html = f"""
        <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);border-radius:12px;padding:16px;color:white;margin:16px 0;">
            <div style="font-size:12px;opacity:0.9;margin-bottom:4px;">月度预算</div>
            <div style="font-size:24px;font-weight:bold;">${budget.amount:.0f}</div>
            <div style="margin-top:12px;display:flex;justify-content:space-between;font-size:14px;">
                <span>已花 ${spent:.2f}</span>
                <span>剩余 ${remaining:.2f}</span>
            </div>
        </div>
        """
    else:
        budget_html = ""
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kenne Index 定投信号</title>
</head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f3f4f6;">
    <div style="max-width:480px;margin:0 auto;background:white;min-height:100vh;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#1e3a8a 0%,#3b82f6 100%);padding:24px 20px;color:white;">
            <div style="font-size:12px;opacity:0.8;margin-bottom:4px;">{date}</div>
            <div style="font-size:22px;font-weight:bold;">Kenne Index</div>
            <div style="font-size:14px;opacity:0.9;margin-top:4px;">定投信号</div>
        </div>
        
        <!-- Data Time -->
        <div style="padding:12px 20px;background:#fef3c7;border-bottom:1px solid #fde68a;">
            <div style="font-size:12px;color:#92400e;text-align:center;">
                &#128202; 数据截止时间: {data_time} UTC+8
            </div>
        </div>
        
        <!-- Overall Score -->
        <div style="padding:20px;background:white;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                <div>
                    <div style="font-size:14px;color:#6b7280;margin-bottom:4px;">策略: {budget_desc}</div>
                    <div style="font-size:18px;font-weight:bold;color:#1f2937;">综合评分</div>
                </div>
                <div style="text-align:center;">
                    <div style="width:64px;height:64px;border-radius:50%;background:{score_color};display:flex;align-items:center;justify-content:center;color:white;font-size:20px;font-weight:bold;box-shadow:0 4px 12px rgba(0,0,0,0.15);">{overall_score}</div>
                    <div style="font-size:12px;color:{score_color};margin-top:6px;font-weight:600;">{score_text}</div>
                </div>
            </div>
            
            {param_alert}
            
            <!-- 幂律参数 -->
            <div style="background:#f0f9ff;border-radius:12px;padding:16px;margin:16px 0;border-left:3px solid #0ea5e9;">
                <div style="font-weight:600;margin-bottom:12px;color:#0369a1;">📐 幂律参数 (Power Law)</div>
                <div style="font-size:13px;color:#64748b;margin-bottom:8px;">价格 = 10^(slope × log₁₀(天数) + intercept)</div>
                <table style="width:100%;font-size:13px;border-collapse:collapse;">
                    <thead>
                        <tr style="color:#64748b;">
                            <th style="padding:8px 4px;text-align:left;font-weight:500;">币种</th>
                            <th style="padding:8px 4px;text-align:center;font-weight:500;">Slope</th>
                            <th style="padding:8px 4px;text-align:center;font-weight:500;">R²</th>
                            <th style="padding:8px 4px;text-align:center;font-weight:500;">数据年限</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(
                            '<tr style="border-top:1px solid #e2e8f0;">'
                            f'<td style="padding:8px 4px;font-weight:600;">{s["symbol"]}</td>'
                            f'<td style="padding:8px 4px;text-align:center;color:{"#0ea5e9" if s.get("refitted") else "#64748b"}">'
                            f'{s["slope"]:.4f} {"⚡" if s.get("refitted") else ""}'
                            '</td>'
                            f'<td style="padding:8px 4px;text-align:center;color:{"#ef4444" if s["r2"] < 0.65 else "#22c55e"}">'
                            f'{s["r2"]:.2f}'
                            '</td>'
                            f'<td style="padding:8px 4px;text-align:center;color:#64748b;">{s.get("data_years", "-")}年</td>'
                            '</tr>'
                            for s in signals
                        )}
                    </tbody>
                </table>
                <div style="font-size:11px;color:#94a3b8;margin-top:8px;">
                    💡 R² > 0.65 表示拟合可信；⚡ 标记表示参数今日已更新
                </div>
            </div>
            
            <!-- Kenne Reference - 7档区间 -->
            <div style="background:#f8fafc;border-radius:12px;padding:16px;margin:16px 0;">
                <div style="font-weight:600;margin-bottom:12px;color:#374151;">Kenne Index 参考区间 (F方案)</div>
                <div style="display:flex;align-items:center;margin-bottom:12px;">
                    <div style="flex:1;height:12px;background:linear-gradient(90deg,#dc2626 0%,#ef4444 16%,#f59e0b 33%,#3b82f6 50%,#22c55e 66%,#10b981 83%,#059669 100%);border-radius:6px;"></div>
                </div>
                <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;font-size:12px;">
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#fee2e2;border-radius:6px;">
                        <span style="color:#991b1b;font-weight:500;">🛑 ≥1.2 停止</span>
                        <span style="color:#dc2626;font-weight:bold;">0x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#ffedd5;border-radius:6px;">
                        <span style="color:#9a3412;font-weight:500;">📉 0.8-1.2 极贵</span>
                        <span style="color:#f97316;font-weight:bold;">0.3x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#fef3c7;border-radius:6px;">
                        <span style="color:#92400e;font-weight:500;">📊 0.6-0.8 偏贵</span>
                        <span style="color:#f59e0b;font-weight:bold;">0.6x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#dbeafe;border-radius:6px;">
                        <span style="color:#1e40af;font-weight:500;">🎯 0.45-0.6 合理</span>
                        <span style="color:#3b82f6;font-weight:bold;">1x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#d1fae5;border-radius:6px;">
                        <span style="color:#065f46;font-weight:500;">💚 0.35-0.45 偏低</span>
                        <span style="color:#10b981;font-weight:bold;">1.5x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#a7f3d0;border-radius:6px;">
                        <span style="color:#047857;font-weight:500;">✅ 0.25-0.35 低估</span>
                        <span style="color:#059669;font-weight:bold;">2x</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;padding:6px 10px;background:#6ee7b7;border-radius:6px;grid-column:span 2;">
                        <span style="color:#064e3b;font-weight:500;">🚀 <0.25 极低估</span>
                        <span style="color:#047857;font-weight:bold;">3x</span>
                    </div>
                </div>
            </div>
            
            <!-- Signals -->
            <div style="margin-top:20px;">
                <div style="font-size:16px;font-weight:bold;color:#1f2937;margin-bottom:12px;">当前信号</div>
                {''.join(signal_cards)}
            </div>
            
            <!-- Allocation -->
            <div style="margin-top:24px;">
                <div style="font-size:16px;font-weight:bold;color:#1f2937;margin-bottom:12px;">本次分配建议 (${run_amount:.2f} USDT)</div>
                <table style="width:100%;border-collapse:collapse;font-size:14px;">
                    <thead>
                        <tr style="background:#f3f4f6;">
                            <th style="padding:12px;text-align:left;border-radius:8px 0 0 8px;">币种</th>
                            <th style="padding:12px;text-align:right;">金额</th>
                            <th style="padding:12px;text-align:center;">权重</th>
                            <th style="padding:12px;text-align:center;border-radius:0 8px 8px 0;">倍数</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(alloc_rows)}
                    </tbody>
                </table>
            </div>
            
            {budget_html}
        </div>
        
        <!-- Footer -->
        <div style="padding:20px;background:#f9fafb;border-top:1px solid #e5e7eb;text-align:center;">
            <div style="font-size:12px;color:#9ca3af;line-height:1.6;">
                本邮件由 Kenne Index 定投系统自动生成<br>
                仅供参考，不构成投资建议
            </div>
        </div>
    </div>
</body>
</html>"""
    
    # 纯文本备选
    text_lines = [
        f'Kenne Index 定投信号 {date}',
        f'数据时间: {data_time}',
        f'综合评分: {overall_score}分 ({score_text})',
        f'策略: {budget_desc}',
        '',
        '当前信号:'
    ]
    for s in signals:
        score = _calc_score(s['kenne_index'])
        text_lines.append(f"  {s['symbol']}: ${s.get('price', 0):,.2f} | Kenne {s['kenne_index']:.4f} | 评分{score}分 | 建议{s['final_mult']:.2f}x")
    
    text_lines.extend(['', f'本次分配: ${run_amount:.2f} USDT'])
    if allocs:
        for a in allocs:
            text_lines.append(f"  {a['symbol']}: ${a['usdt_amount']:.2f} ({a['weight']:.0%}) x{a['final_mult']:.2f}")
    
    if budget.mode == 'MONTHLY':
        text_lines.append(f"\n月预算: ${budget.amount:.0f} | 已花: ${budget.spent_this_month():.2f} | 剩余: ${budget.monthly_remaining():.2f}")
    
    return html, '\n'.join(text_lines)


def run_notify():
    """更新数据 -> 计算信号 -> 发送 HTML 邮件"""
    log.info('--- Kenne Index notify ---')

    client  = _make_client()
    updater = DataUpdater(client)
    budget  = Budget()

    log.info('[1/3] updating market data')
    for sym, f in CFG['DATA_FILES'].items():
        try:
            updater.update(sym, f)
        except Exception as e:
            log.error(f'[{sym}] update failed, using local data: {e}')

    log.info('[2/3] calculating signals')
    signals = []
    for sym, f in CFG['DATA_FILES'].items():
        try:
            r = kenne_analyze(f, sym)
            if r:
                signals.append(r)
                log.info(f'  {sym}: kenne={r["kenne_index"]:.4f}  '
                         f'momentum={r["momentum"]}  mult={r["final_mult"]:.2f}x')
        except Exception as e:
            log.error(f'[{sym}] analysis failed: {e}')

    if not signals:
        log.error('all analysis failed')
        return

    allocs = allocate(signals, budget.this_run_amount())

    log.info('[3/3] sending email')
    log.info(budget.summary_str())
    subject = f'Kenne Index 定投信号 {datetime.date.today().strftime("%Y-%m-%d")}'
    html_body, text_body = _build_report(signals, allocs, budget)
    _send_email(subject, html_body, text_body)
    log.info('--- done ---')


def run_notify_daemon():
    """守护进程：按 RUN_INTERVAL_DAYS 间隔自动发送信号邮件。"""
    log.info(f'notify daemon started, interval={CFG["RUN_INTERVAL_DAYS"]}d (Ctrl+C to stop)')
    while True:
        t    = _next_run_time()
        secs = (t - datetime.datetime.now()).total_seconds()
        log.info(f'next notify: {t.strftime("%Y-%m-%d %H:%M")} ({secs/3600:.1f}h from now)')
        time.sleep(secs)
        try:
            run_notify()
        except Exception as e:
            log.error(f'run_notify exception: {e}')
        time.sleep(60)


# ─── 入口 ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description='Kenne Index x OKX auto DCA')
    p.add_argument('--update',        action='store_true', help='update CSV data only')
    p.add_argument('--notify',        action='store_true', help='send signal email (no trade)')
    p.add_argument('--dry-run',       action='store_true', help='simulate without ordering')
    p.add_argument('--daemon',        action='store_true', help='run on interval, real orders')
    p.add_argument('--notify-daemon', action='store_true', help='run on interval, email only')
    p.add_argument('--history',       nargs='?', const='', metavar='YYYY-MM', help='show history')
    a = p.parse_args()

    if   a.history is not None: show_history(month=a.history or None)
    elif a.update:               run_update()
    elif a.notify:               run_notify()
    elif a.notify_daemon:        run_notify_daemon()
    elif a.daemon:               run_daemon(dry_run=a.dry_run)
    else:                        run_dca(dry_run=a.dry_run)


if __name__ == '__main__':
    main()
