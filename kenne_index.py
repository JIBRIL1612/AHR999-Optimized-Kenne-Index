"""
Kenne Index 动态定投分析
用法: python3 kenne_index.py

依赖: pandas, numpy, scipy
"""

import json
import pandas as pd
import numpy as np
from scipy import stats
from scipy.stats import gmean
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ─── 币种配置（硬编码值作为回退，运行时会用实际数据重拟合覆盖）─────────────────

@dataclass
class CoinConfig:
    symbol:     str
    slope:      float    # log10(price) ~ log10(days) 幂律斜率
    intercept:  float
    genesis:    str      # 创世日期
    buy_thresh: float    # 极低估阈值
    dca_thresh: float    # 定投上沿
    knife_7d:   float    # 7日跌幅阈值
    knife_14d:  float    # 14日跌幅阈值
    bounce_min: float    # 企稳反弹幅度
    r2:         float    # 幂律拟合 R²
    data_years: float    # 数据年限


COIN_CONFIG = {
    'BTC': CoinConfig(
        symbol='BTC', slope=4.7777, intercept=-13.1486,
        genesis='2009-01-03',
        buy_thresh=0.45, dca_thresh=1.20,
        knife_7d=-0.10, knife_14d=-0.18, bounce_min=0.05,
        r2=0.78, data_years=15,
    ),
    'ETH': CoinConfig(
        symbol='ETH', slope=1.9872, intercept=-3.5997,
        genesis='2015-07-30',
        buy_thresh=0.45, dca_thresh=1.20,
        knife_7d=-0.10, knife_14d=-0.18, bounce_min=0.05,
        r2=0.58, data_years=10,
    ),
    'SOL': CoinConfig(
        symbol='SOL', slope=1.4446, intercept=-2.5934,
        genesis='2020-03-16',
        buy_thresh=0.45, dca_thresh=1.50,
        knife_7d=-0.13, knife_14d=-0.22, bounce_min=0.07,
        r2=0.53, data_years=5.5,
    ),
}

MODEL_PARAMS_FILE = 'model_params.json'
MIN_DAYS_FOR_REFIT = 365


# ─── 幂律重拟合 ───────────────────────────────────────────────────────────────

def _refit(df_d: pd.DataFrame, cfg: CoinConfig) -> tuple[float, float, float]:
    df_valid = df_d[df_d['days'] > 0].dropna(subset=['Close'])
    if len(df_valid) < MIN_DAYS_FOR_REFIT:
        return cfg.slope, cfg.intercept, cfg.r2

    x = np.log10(df_valid['days'].values)
    y = np.log10(df_valid['Close'].values)
    slope, intercept, r, _, _ = stats.linregress(x, y)
    return slope, intercept, r ** 2


def _load_saved_params() -> dict:
    p = Path(MODEL_PARAMS_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_params(all_params: dict):
    try:
        Path(MODEL_PARAMS_FILE).write_text(
            json.dumps(all_params, indent=2, ensure_ascii=False)
        )
    except Exception as e:
        print(f"  [warn] 参数保存失败: {e}")


# ─── 信号计算 ─────────────────────────────────────────────────────────────────


def _base_mult(ahr, cfg):
    """
    F方案：连续倍数曲线 - Kenne越低，买得越多
    """
    if ahr >= cfg.dca_thresh:      # >= 1.2
        return 0.0                  # 停止
    elif ahr >= 0.8:               # 0.8 ~ 1.2
        return 0.3                  # 观望
    elif ahr >= 0.6:               # 0.6 ~ 0.8
        return 0.6                  # 轻仓
    elif ahr >= cfg.buy_thresh:    # 0.45 ~ 0.6
        return 1.0                  # 正常定投
    elif ahr >= 0.35:              # 0.35 ~ 0.45
        return 1.5                  # 加仓
    elif ahr >= 0.25:              # 0.25 ~ 0.35
        return 2.0                  # 重仓
    else:                           # < 0.25
        return 3.0                  # 极重仓（历史罕见）


def _score(ahr, cfg):
    """
    买入评分（纯Kenne驱动，0-100分）
    """
    if ahr < cfg.buy_thresh:
        ratio = max(0, (cfg.buy_thresh - ahr) / (cfg.buy_thresh - 0.2))
        return int(50 + 50 * min(1, ratio))
    elif ahr <= cfg.dca_thresh:
        ratio = 1 - (ahr - cfg.buy_thresh) / (cfg.dca_thresh - cfg.buy_thresh)
        return int(10 + 40 * ratio)
    else:
        return max(0, 10 - int((ahr - cfg.dca_thresh) * 20))


def _momentum_state(ret_3d, ret_7d, ret_14d, bounce_7, cfg):
    """
    趋势状态检测（仅用于风险提示，不强制调整倍数）
    """
    is_sharp   = ret_3d < -0.07
    is_knife   = (ret_7d < cfg.knife_7d) or (ret_14d < cfg.knife_14d)
    has_bounce = bounce_7 >= cfg.bounce_min
    if   is_sharp:                    return 'SHARP_DROP'
    elif is_knife and not has_bounce: return 'FALLING'
    elif is_knife and has_bounce:     return 'STABILIZING'
    else:                             return 'STABLE'


# ─── 主分析函数 ───────────────────────────────────────────────────────────────

def analyze(file_path: str, symbol: str,
            saved_params: Optional[dict] = None) -> Optional[dict]:
    symbol = symbol.upper()
    if symbol not in COIN_CONFIG:
        print(f"不支持的币种: {symbol}")
        return None

    cfg = COIN_CONFIG[symbol]

    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print(f"文件未找到: {file_path}")
        return None

    df['Open time'] = pd.to_datetime(df['Open time'], format='mixed')
    df.set_index('Open time', inplace=True)
    df_d = df.resample('D').agg({'Close': 'last', 'Low': 'min', 'High': 'max'}).dropna()

    genesis = pd.to_datetime(cfg.genesis)
    df_d['days'] = (df_d.index - genesis).days
    df_d = df_d[df_d['days'] > 0]

    slope, intercept, r2 = _refit(df_d, cfg)
    data_years = round(len(df_d) / 365, 1)
    refitted   = (abs(slope - cfg.slope) > 1e-6)

    if refitted:
        print(f"  [{symbol}] 参数已更新  slope: {cfg.slope} → {slope:.4f}"
              f"  intercept: {cfg.intercept} → {intercept:.4f}"
              f"  R²: {cfg.r2:.4f} → {r2:.4f}")
    else:
        print(f"  [{symbol}] 参数无变化  slope={slope:.4f}  R²={r2:.4f}")

    if saved_params is not None:
        saved_params[symbol] = {
            'slope': round(slope, 6), 'intercept': round(intercept, 6),
            'r2': round(r2, 4), 'data_years': data_years,
            'updated_at': pd.Timestamp.now().strftime('%Y-%m-%d'),
        }

    df_d['valuation'] = 10 ** (slope * np.log10(df_d['days']) + intercept)
    df_d['cost_200']  = df_d['Close'].rolling(200).apply(gmean, raw=True)
    df_d['kenne_index'] = (df_d['Close'] / df_d['cost_200']) * (df_d['Close'] / df_d['valuation'])

    df_d['ret_3d']   = df_d['Close'].pct_change(3)
    df_d['ret_7d']   = df_d['Close'].pct_change(7)
    df_d['ret_14d']  = df_d['Close'].pct_change(14)
    df_d['low7']     = df_d['Low'].rolling(7).min()
    df_d['bounce_7'] = (df_d['Close'] - df_d['low7']) / df_d['low7']

    df_d = df_d.dropna()
    if df_d.empty:
        print(f"{symbol}: 数据不足")
        return None

    row  = df_d.iloc[-1]
    ahr  = row['kenne_index']

    momentum   = _momentum_state(row['ret_3d'], row['ret_7d'], row['ret_14d'], row['bounce_7'], cfg)
    base_m     = _base_mult(ahr, cfg)
    score      = _score(ahr, cfg)
    pct_rank   = (df_d['kenne_index'] < ahr).mean() * 100
    pct        = df_d['kenne_index'].quantile([0.05, 0.25, 0.50, 0.75]).to_dict()

    if   ahr < cfg.buy_thresh:  zone = f'极低估区 (< {cfg.buy_thresh})'
    elif ahr <= cfg.dca_thresh: zone = f'定投区 ({cfg.buy_thresh}-{cfg.dca_thresh})'
    else:                       zone = f'观望区 (> {cfg.dca_thresh})'

    mom_desc = {
        'STABLE':      f'平稳  7d={row["ret_7d"]:+.1%}',
        'STABILIZING': f'趋稳  7d={row["ret_7d"]:+.1%}  已从低点反弹 {row["bounce_7"]:.1%}',
        'FALLING':     f'急跌  7d={row["ret_7d"]:+.1%}  14d={row["ret_14d"]:+.1%}',
        'SHARP_DROP':  f'急跌  3d={row["ret_3d"]:+.1%}  短期急跌',
    }.get(momentum, '未知')

    if base_m == 0.0:
        action = f'停止定投  Kenne={ahr:.3f}'
    else:
        action = f'建议投入 {base_m:.1f}x (${30*base_m:.0f})  评分{score}分'

    W = 52
    print(f"\n{'─'*W}")
    print(f"  {symbol}  {row.name.strftime('%Y-%m-%d')}")
    print(f"{'─'*W}")
    print(f"  价格    {row['Close']:>14,.2f}  USDT")
    print(f"  200日线 {row['cost_200']:>14,.2f}  USDT")
    print(f"  幂律估值 {row['valuation']:>13,.2f}  USDT  (slope={slope:.4f})")
    print(f"  Kenne Index  {ahr:.4f}   历史 {pct_rank:.0f}% 分位")
    print(f"  区间    {zone}")
    print(f"  动量    {momentum:<12}  {mom_desc}")
    print(f"  评分    {score}/100")
    print(f"  建议    {action}")
    if r2 < 0.65:
        print(f"  注意    R²={r2:.4f} 幂律拟合可信度较低")
    print(f"{'─'*W}")

    return {
        'symbol':     symbol,
        'kenne_index': ahr,
        'momentum':   momentum,
        'base_mult':  base_m,
        'final_mult': base_m,
        'score':      score,
        'price':      row['Close'],
        'r2':         r2,
        'slope':      slope,
        'intercept':  intercept,
        'refitted':   refitted,
        'data_years': data_years,
    }


TASKS = [
    ('btc_4h_data_2018_to_2025.csv', 'BTC'),
    ('eth_4h_data_2017_to_2025.csv', 'ETH'),
    ('sol_4h_data_2020_to_2025.csv', 'SOL'),
]

if __name__ == '__main__':
    saved_params = _load_saved_params()
    results = []

    for fp, sym in TASKS:
        r = analyze(fp, sym, saved_params=saved_params)
        if r:
            results.append(r)

    if saved_params:
        _save_params(saved_params)
        print(f"\n拟合参数已保存至 {MODEL_PARAMS_FILE}")

    if len(results) > 1:
        print(f"\n{'─'*40}")
        print("排名（评分高 = 当前机会更优）")
        print(f"{'─'*40}")
        for r in sorted(results, key=lambda x: x['score'], reverse=True):
            mult = f"{r['final_mult']:.2f}x" if r['final_mult'] > 0 else '停止'
            print(f"  {r['symbol']:<4}  评分 {r['score']:>3}/100  执行 {mult:<6}")
        print(f"{'─'*40}")
