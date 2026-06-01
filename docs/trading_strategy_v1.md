# Mercury 纸面交易策略 V1

> 2026-06-01 | 基于模型预测 + CLOB 订单簿的离散桶纸面交易

---

## 1. 核心假设

我们的多模型融合预测（live_rmse 加权 + 自有偏差校准）是准的。
纸面阶段不砍仓位，每一笔都是独立数据点，事后结算时按"预测时间点"分组分析。

---

## 2. 预测值的日内演变

```
D日 00:00  初始预测（MAE种子权重/A方案，无当天METAR）
D日 06:00  陆续有METAR进来 → live_weights激活（B方案）
D日 12:00  半天数据，权重趋于稳定
D日 18:00  METAR基本停更
D日 24:00  结算
```

引擎每次跑完输出 `t_calibrated`，对应当前时刻的最佳预测。

---

## 3. 三层交易体系

### 3.1 开盘单（D日 00:00–02:00）

| 维度 | 内容 |
|:---|:---|
| 权重来源 | MAE种子静态权重（A方案）或前一天同时段 live_weights（B方案） |
| 信号 | t_calibrated → 离散桶概率 normal → 取 top3 桶 → edge vs CLOB |
| 仓位 | 50%（半仓） |
| 目的 | 最"干净"的预测，验证模型在无当天数据时的精度 |

### 3.2 盘中修正（METAR ≥ 6小时，D日 06:00–15:00）

| 维度 | 内容 |
|:---|:---|
| 权重来源 | live_rmse_weighted（当天METAR时间衰减加权） |
| 触发 | 🔴 **round(new_cal) != round(old_cal)** — 跨桶 |
| 不触发 | 桶未变，即使温度漂移 0.9°C 也视为噪音 |
| 行动 | 旧桶仓位保留 + 新桶开仓 |
| 仓位 | 追加 30%（总 80%） |

**为什么跨桶判断而不是温度阈值？**

| 案例 | ΔT | 旧桶 | 新桶 | 是否触发 |
|:---|:---:|:---:|:---:|:---:|
| 36.4→37.0 | 0.6°C | 36 | 37 | ✅ 触发 |
| 36.4→36.8 | 0.4°C | 36 | 37 | ✅ 触发 |
| 35.6→36.2 | 0.6°C | 36 | 36 | ❌ 不触发 |
| 34.1→33.5 | 0.6°C | 34 | 34 | ❌ 不触发 |

Polymarket 交易的是离散桶合约，桶变了 = 完全不同的标的物。

### 3.3 收盘单（D日 15:00–18:00）

| 维度 | 内容 |
|:---|:---|
| 权重来源 | live_rmse_weighted（几乎完整一天 METAR） |
| 信号 | edge > 降低后的阈值，仅捡漏 |
| 仓位 | 20% |
| 目的 | 预测高度确定时的低风险收尾 |

---

## 4. 不砍仓位原则

```
当天同一城市可能出现 2-3 个桶的仓位，全部保留到结算。
事后按 layer+时间戳分组分析胜率。
```

三层独立：
- 开盘单 → 检验"纯模型预测"的精度
- 盘中修正 → 检验"METAR 修正"的边际价值（是否比开盘预测更准？）
- 收盘单 → 检验"几乎确定"时的剩余 edge

---

## 5. 信号生成流程

### 5.1 输入

- `t_calibrated`：偏差校准后的预测最高温
- `t_std`：加权标准差
- `buckets_normal`：正态分布离散桶概率
- CLOB 订单簿：各桶的 yes_ask / no_ask

### 5.2 Edge 计算

```
edge = max(prob - market_implied_prob, 0)

其中：
  prob = bucket_prob_normal(t_calibrated, t_std, target_bucket)
  market_implied_prob = yes_ask_price（做 LONG）或 (1 - no_ask_price)（做 SHORT）
```

仅 SHORT 时用 `1 - no_ask_price` 作为市场隐含概率。

### 5.3 入场规则

| 层级 | min_edge | 仓位比例 |
|:---|:---:|:---:|
| 开盘 | 0.05 | 50% |
| 盘中修正 | 0.03 | 30%（新增） |
| 收盘 | 0.02 | 20%（新增） |

P3v2 经验：edge 阈值应随确定性提高而降低。收盘时预测几乎确定，少量 edge 就够了。

### 5.4 Top3 桶选择

每次信号取 `probs_normal` 中概率最高的 3 个桶（跳过概率 <0.05 的），分别计算 edge。同一天同一层的不同桶是独立交易。

---

## 6. 回测记录格式

```json
{
  "trade_id": "bj_2026-06-01_L1_0630_001",
  "city": "beijing",
  "layer": "mid_day",
  "entry_time": "2026-06-01T06:30:00+08",
  "prediction": {
    "t_calibrated": 31.2,
    "t_std": 1.8,
    "blend_method": "live_rmse_weighted",
    "bias_correction": 0.3,
    "bias_source": "own-track (n=5d)",
    "metar_hours_available": 6,
    "metar_count": 12
  },
  "model_detail": {
    "icon_seamless": {"t_max": 30.5, "weight": 0.28, "live_rmse": 1.2},
    "ecmwf_ifs":    {"t_max": 31.8, "weight": 0.22, "live_rmse": 1.5}
  },
  "order": {
    "target_bucket": 31,
    "side": "LONG",
    "fill_price": 0.48,
    "shares": 100,
    "cost": 48.0
  },
  "settlement": {
    "actual_tmax": 31.5,
    "settled_bucket": 31,
    "pnl": 52.0,
    "result": "WIN"
  }
}
```

---

## 7. 待验证的开放问题

| 问题 | 纸面阶段方案 | 实盘决策依据 |
|:---|:---|:---|
| Q1: 开盘权重来源 | A/B 方案都跑，比对效果 | 选效果好的 |
| Q2: 只加不减 | ✅ 采用，每组独立分析 | 根据纸面数据决定 |
| Q3: 预测变化阈值 | round(t) 跨桶 | 同上 |
| Q4: 开盘/收盘时间窗口 | 暂定 0-2h / 15-18h | 多城时区不同需适配 |
| Q5: 跨桶修正时机 | METAR ≥6h 每小时检查 | 根据数据验证最佳窗口 |

---

## 8. 实施计划

1. **引擎定时跑**（已有）：0:05, 12:05 → 输出 t_calibrated + l1_curve
2. **新增：信号生成脚本**：读取引擎输出 + CLOB → 输出 trade 记录
3. **新增：结算脚本**：D+1 读取 METAR t_max → 判定输赢 → 写 settlement
4. **新增：定时检查**：遇到跨桶变化 → 生成修正 trade 记录

第一阶段只做 step 1+2+3，step 4（盘中修正）在积累了足够开盘数据后再加入。
