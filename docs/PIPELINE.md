# Mercury 数据处理流程

> 自建多模型集合气温预报系统。终极目标：预报精度对标 polywx v3（MAE ~0.29°C）。

---

## 架构总览

```
┌─────────────────┐    ┌─────────────────┐
│  Open-Meteo API  │    │  NOAA IEM ASOS   │
│  多模型集合预报    │    │  机场 METAR 实测   │
└────────┬────────┘    └────────┬────────┘
         │                      │
    ┌────▼────┐            ┌────▼────┐
    │ fetch_   │            │ fetch_   │
    │ opm      │  launchd   │ metar    │  launchd
    │ 每天2次   │  0:00/12:00│ 每30分钟  │
    └────┬────┘            └────┬────┘
         │                      │
    data/forecasts/         data/metar/
    {city}/{date}/          {city}/{date}.json
    latest.json             (增量追加)
         │                      │
         └──────────┬───────────┘
                    │
              ┌─────▼──────┐
              │   engine.py │  launchd
              │   每天2次    │  0:05/12:05
              └─────┬──────┘
                    │
              data/engine/
              {city}/{date}.json
              {city}/{date}_snapshots.jsonl  ← 预测演变
                    │
              ┌─────▼──────┐
              │   serve.py  │  前端 + API
              │  端口 8080   │  动态L1重算
              └─────────────┘
```

---

## 数据源

### 1. 多模型集合预报（Open-Meteo）

| 项目 | 说明 |
|:---|:---|
| **API** | `api.open-meteo.com/v1/forecast` |
| **模型** | ECMWF IFS, GFS, JMA, ICON, GEM, UKMO (共6个) |
| **频率** | 每天 2 次 (00:00 + 12:00 UTC) |
| **天数** | 当天 + 未来 3 天 (共 4 天) |
| **数据** | 逐时温度 + 云量，每个模型独立曲线 |
| **脚本** | `scripts/fetch_openmeteo.py --all` |
| **输出** | `data/forecasts/{city}/{target_date}/latest.json` |

### 2. 机场实测（NOAA IEM ASOS）

| 项目 | 说明 |
|:---|:---|
| **API** | `mesonet.agron.iastate.edu/cgi-bin/request/asos.py` |
| **频率** | 每 30 分钟增量拉取 |
| **数据** | 温度(°C)、露点、风速、气压 |
| **模式** | `--merge` 增量追加，按 `time_utc` 去重 |
| **脚本** | `scripts/fetch_metar.py --all --merge` |
| **输出** | `data/metar/{city}/{date}.json` |

**代理注意**：两个抓取脚本均绕过系统代理直连（Open-Meteo 防 TLS Reset，NOAA IEM 防 MITM SSL）。

---

## 预报引擎（engine.py）

### 权重计算：时间衰减加权 RMSE

每天引擎运行时，用当天+前一天的 METAR 实测数据计算各模型权重：

```
w_i ∝ 1/RMSE_i² × e^(-λ·Δt)
λ = ln(2) / 4h    (半衰期 4 小时)
窗口 = 24h         (最大回溯)
```

- 最近的 METAR 点权重高，昨天的低权重平滑过渡
- 至少 3 个 METAR 点才能计算实时权重
- 不足时 fallback 到 MAE 种子权重

### 偏差校准

优先使用自有历史误差均值（前 7 天，≥2 天样本），不足时不校准（宁可不偏不用错偏）。

### 输出

| 字段 | 说明 |
|:---|:---|
| `t_calibrated` | 偏差校准后的预测最高温 |
| `t_std` | 加权标准差 |
| `buckets_normal` | 正态分布离散桶概率 |
| `l1_curve` | L1 融合曲线（冻结快照，逐时温度） |
| `snapshot_hour` | 引擎运行时的当地小时 |

---

## L1 融合曲线与冻结机制

L1 曲线 = 各模型逐时温度按权重融合的单一预测曲线。

### 冻结规则

```
h < snapshot_hour → 🔒 冻结（不随后续 METAR 变化）
h ≥ snapshot_hour → 🔄 动态（每次新 METAR 触发重算）
```

**触发时机：**
1. **METAR 写入时**：`fetch_metar.py` 保存新数据后自动调用 `snapshot_prediction()`，桶变化或距上次≥30分钟时记录到 `snapshots.jsonl`
2. **前端请求时**：`serve.py` 每次请求动态合并冻结+新权重，同步记录快照

### snapshots.jsonl

每行一个 JSON，记录预测演变：

```json
{"time": "2026-06-01T14:35:22+08:00", "t_predicted": 31.2, "bucket": 31,
 "metar_count": 12, "current_hour": 14, "live_weights": {...}, "live_rmse": {...}}
```

事后可画出「预测最高温-时间曲线」，与结算实际值对比，验证各时间点预测精度。

---

## 前端服务（serve.py）

| 项目 | 说明 |
|:---|:---|
| **端口** | 8080 |
| **API** | `/api/forecast?city=beijing`, `/api/summary`, `/api/metar`, `/api/cities` |
| **页面** | `frontend/city.html` — 单城详情（L1曲线图、模型权重、METAR时间线） |

### 动态 L1 重算

用户每次刷新前端，serve.py 用最新 METAR 重算未来时段 L1，过去时段保持冻结。不需要等引擎定时跑。

---

## 调度系统（launchd）

| 任务 | Label | 频率 | 脚本 |
|:---|:---|:---|:---|
| 预报拉取 | `com.mercury.forecast-pull` | 0:00, 12:00 | `fetch_openmeteo.py --all` |
| METAR 采集 | `com.mercury.metar-fetch` | 每 30 分钟 | `fetch_metar.py --all --merge --quiet` |
| 引擎运行 | `com.mercury.engine` | 0:05, 12:05 | `engine_auto.py` |

配置文件在 `~/Library/LaunchAgents/com.mercury.*.plist`。

---

## 数据目录结构

```
data/
├── forecasts/{city}/{target_date}/
│   ├── {generated_date}.json     # 某天生成的预报快照
│   └── latest.json               # 最新快照
├── metar/{city}/{target_date}.json           # METAR 实测（增量追加）
├── engine/{city}/
│   ├── {target_date}.json                    # 引擎输出（每天覆盖）
│   └── {target_date}_snapshots.jsonl          # 预测演变快照（追加）
└── models/
    └── mae_seed.json                          # 历史 MAE 种子权重
```

---

## 流程时序图

```
D-1 日
  23:30  METAR 拉取（最后一波数据）
  23:59  当天 METAR 文件最终化

D 日
  00:00  fetch_openmeteo --all     ← 新一天模型预报
  00:05  engine --all --deb        ← 初始预测（MAE种子权重）
  06:00+ METAR 陆续进来            ← 每次写入触发 snapshot_prediction
  12:00  fetch_openmeteo --all     ← 第二次模型更新
  12:05  engine --all --deb        ← 有半天 METAR，live_weights 激活
  15:00+ METAR 基本停更（多数机场黄昏后不发报）
  18:00  前端查看预测趋于稳定

D+1 日
  00:05  engine --all --deb        ← 结算：forecast_error = t_calibrated - METAR t_max
  00:06+ MAE 种子更新              ← 新一天的误差写入 mae_seed.json
```
