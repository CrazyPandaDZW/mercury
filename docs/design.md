# Mercury — 自建气象预报 + Polymarket 自动交易系统

## 1. 项目目标

替代 polywx.xyz，自建城市最高温多模型集合预报系统，服务于 Polymarket 天气温度市场交易。

**核心能力：**
- 逐时温度曲线预报（0-23h），覆盖 40 城
- 多模型集合（ECMWF/GFS/JMA 等），每模型输出独立成员
- METAR 实时修正，每天多次更新
- 历史准确率追踪，偏差方向感知

## 2. 系统架构

```
┌──────────────────────────────────────────────────────┐
│                    Atlas Forecast                      │
├──────────┬──────────┬──────────┬──────────────────────┤
│ 数据采集  │ 预报引擎  │ 实时修正  │ 输出 & 验证          │
├──────────┼──────────┼──────────┼──────────────────────┤
│Open-Meteo│ 集合聚合  │METAR拉取 │ API 端点             │
│ (多模型)  │ 偏差校准  │温度比对  │ 历史准确率 DB         │
│          │          │预报修正  │ 偏差方向追踪          │
│IEM ASOS  │ 时间分解  │          │                      │
│ (实测)   │ (逐时)   │          │                      │
└──────────┴──────────┴──────────┴──────────────────────┘
```

**数据流：**
```
Open-Meteo  ─→ 原始集合预报 ─→ 偏差校准 ─→ 逐时温度曲线
                                          │
METAR 观测  ─→ 实时修正 ─────────────────┘
                                          │
                                    最终预报输出
                                          │
ASOS 实测   ─→ 准确率追踪 ←──────────────┘
```

## 3. 数据源

### 3.1 预报数据：Open-Meteo API
- 免费，无需 API Key
- 支持模型：`ecmwf_ifs04`、`gfs_seamless`、`jma_seamless`、`icon_seamless`、`gem_seamless`
- 每个模型返回逐时温度（0-23h 或更长）
- 更新频率：每 6 小时（00/06/12/18 UTC）
- 端点：`https://api.open-meteo.com/v1/forecast`

### 3.2 实测数据：IEM ASOS
- NOAA 旗下 Iowa Environmental Mesonet
- 免费，全球机场 METAR 数据
- 每个城市对应一个 ICAO 机场站
- 端点：`https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py`

### 3.3 城市覆盖（40城）

| 区域 | 城市 | ICAO | polywx ID |
|:---|:---|:---|:---|
| 🇨🇳 东亚 | beijing | ZBAA | beijing-zbaa |
| | shanghai | ZSSS | shanghai-zspd |
| | tokyo | RJTT | tokyo-rjtt |
| | seoul | RKSS | seoul-rksi |
| | taipei | RCSS | taipei-rcss |
| | hong-kong | VHHH | hong-kong-vhhh |
| | chongqing | ZUCK | chongqing-zuck |
| | qingdao | ZSQD | qingdao-zsqd |
| | wuhan | ZHHH | wuhan-zhhh |
| 🇺🇸 美国 | atlanta | KATL | atlanta-katl |
| | austin | KAUS | austin-kaus |
| | chicago | KORD | chicago-kord |
| | dallas | KDFW | dallas-kdal |
| | denver | KDEN | denver-kden |
| | houston | KHOU | houston-khou |
| | los-angeles | KLAX | los-angeles-klax |
| | miami | KMIA | miami-kmia |
| | nyc | KJFK | nyc-klga |
| | san-francisco | KSFO | san-francisco-ksfo |
| | seattle | KSEA | seattle-ksea |
| 🇪🇺 欧洲 | amsterdam | EHAM | amsterdam-eham |
| | ankara | LTAC | ankara-ltac |
| | helsinki | EFHK | helsinki-efhk |
| | istanbul | LTFM | istanbul-ltfm |
| | london | EGLL | london-eglc |
| | madrid | LEMD | madrid-lemd |
| | moscow | UUWW | moscow-uuww |
| | munich | EDDM | munich-eddm |
| | paris | LFPB | paris-lfpb |
| | tel-aviv | LLBG | tel-aviv-llbg |
| | warsaw | EPWA | warsaw-epwa |
| 🌴 热带 | jakarta | WIII | — |
| | karachi | OPKC | — |
| | kuala-lumpur | WMKK | — |
| | manila | RPLL | — |
| | singapore | WSSS | — |
| 🌍 南半球 | cape-town | FACT | — |
| | mexico-city | MMMX | — |
| | panama-city | MPTO | — |

## 4. 预报引擎

### 4.1 集合预报聚合

每个模型返回一条逐时温度曲线。对 N 个模型做集合聚合：

```
对每个城市、每个小时 h：
  T_ensemble[h] = mean(T_ecmwf[h], T_gfs[h], T_jma[h], ...)
  T_std[h]     = std(T_ecmwf[h], T_gfs[h], T_jma[h], ...)
  
日最高温：
  T_max_ensemble = max(T_ensemble[0..23])
  T_max_range    = [T_max_ensemble - T_std, T_max_ensemble + T_std]
```

**与 polywx 的区别：** polywx 返回每个模型的独立最高温预测（离散值），我们返回均值+不确定性（连续分布）。

### 4.2 偏差校准

根据历史表现，对每个城市独立校准：

```
对城市 C，收集过去 N 天的预报 vs 实测：
  bias[C] = mean(forecast[C][i] - actual[C][i])

校准后预报：
  T_calibrated = T_ensemble - bias[C]
```

初期使用全局 bias（如北京 -0.5°C），积累 30 天数据后切换到城市专属校准。

### 4.3 实时 METAR 修正

每天北京时间 08:00-16:00，每 30 分钟拉取一次 METAR：

```
已知：当前实测温度 T_now，当前时间 h_now
预报：峰值温度 T_max_forecast，峰值时间 h_peak

如果 h_now < h_peak：
  heating_remaining = (T_max_forecast - T_now) / (h_peak - h_now)
  调整后：T_max = T_now + heating_remaining * (h_peak - h_now)

如果 h_now >= h_peak：
  T_max = max(T_now, 已过去时段的最高温)
```

**简单版：** 如果当前实测 > 预报峰值，直接上调预报；如果当前实测远低于预报轨迹，下调预报。

### 4.4 更新频率

| 时间(UTC) | 北京时间 | 动作 |
|:---|:---|:---|
| 00:00 | 08:00 | Open-Meteo 最新集合预报 + 偏差校准 → 生成日预报 |
| 03:00 | 11:00 | METAR 修正（关键窗口前） |
| 04:00 | 12:00 | METAR 修正（交易入场） |
| 05:00 | 13:00 | METAR 修正 |
| 06:00 | 14:00 | METAR 修正（交易窗口尾） |
| 12:00 | 20:00 | 次日预报预生成 |

## 5. 输出格式

### 5.1 API 端点

```
GET /api/forecast?city=beijing&date=2026-06-01

{
  "city": "beijing",
  "date": "2026-06-01",
  "generated_at": "2026-06-01T00:15:00Z",
  "last_metar_update": "2026-06-01T04:00:00Z",
  "t_max_ensemble": 33.5,
  "t_max_range": [32.0, 35.0],
  "t_max_calibrated": 34.0,
  "bias_correction": -0.5,
  "hourly": [
    {"hour": 0, "t_ensemble": 22.1, "t_range": [21, 23]},
    ...
    {"hour": 15, "t_ensemble": 34.0, "t_range": [33, 35]},
    ...
  ],
  "models": {
    "ecmwf_ifs04": {"t_max": 34.1, "bias": -0.3},
    "gfs_seamless": {"t_max": 33.0, "bias": -0.7},
    "jma_seamless": {"t_max": 33.4, "bias": -0.5}
  }
}
```

### 5.2 本地缓存

- `data/forecasts/{city}/{date}.json` — 逐时温度曲线
- `data/models/{city}_bias.json` — 偏差校准参数
- `data/metar/{station}_{date}.json` — METAR 观测记录
- `data/accuracy/{city}.json` — 历史准确率

## 6. 准确率追踪

### 6.1 每日结算

每天北京时间 20:00（当天 ASOS 数据完整后）自动结算：

```
对每个城市：
  actual_max = ASOS 当天最高温
  forecast_max = 当天 08:00 时的预报
  diff = forecast_max - actual_max

更新：
  - 滑动 30 天 MAE
  - 累计 bias
  - ±1°C / ±2°C 命中率
  - 低估/高估 比例
```

### 6.2 基准线（来自 polywx 回测）

| 城市 | polywx MAE | 目标 MAE |
|:---|:--:|:--:|
| beijing | 0.5°C | ≤0.5°C |
| ankara | 0.2°C | ≤0.3°C |
| amsterdam | 0.3°C | ≤0.4°C |

## 7. 与 polywx 的差异

| 维度 | polywx | Atlas |
|:---|:---|:---|
| 可靠性 | ❌ 502 挂了 | ✅ 自主可控 |
| 模型 | 未知黑盒 | ECMWF+GFS+JMA 等，透明 |
| 修正时机 | 不可见 | 日志化，可审计 |
| 偏差认知 | 靠回测发现 | 内置偏差追踪 |
| 更新频率 | 不确定 | 定时+METAR 驱动 |
| 成本 | 免费，但有风险 | 免费（Open-Meteo无API Key） |
| 输出格式 | 逐时温度 | 逐时温度+不确定性+模型明细 |

## 8. 实施路线

### Phase 1 — 数据管道（1-2天）
- [ ] `scripts/fetch_openmeteo.py` — 拉取多模型集合预报
- [ ] `scripts/fetch_metar.py` — 拉取 IEM ASOS 实测
- [ ] 先跑北京，验证数据格式

### Phase 2 — 预报引擎（2-3天）
- [ ] `scripts/engine.py` — 集合聚合 + 偏差校准
- [ ] 生成初始的 30 天历史预报（用 Open-Meteo 历史数据回填）
- [ ] 与 polywx 准确率对比

### Phase 3 — 实时修正（1-2天）
- [ ] METAR 实时拉取
- [ ] 温度轨迹比对 + 预报修正
- [ ] 修正日志

### Phase 4 — 上线运行（1天）
- [ ] cron 定时调度
- [ ] API 端点
- [ ] 飞书通知
- [ ] 准确率追踪

## 9. 文件结构

```
atlas-forecast/
├── README.md
├── docs/
│   └── design.md           ← 本文档
├── scripts/
│   ├── fetch_openmeteo.py   # 拉取集合预报
│   ├── fetch_metar.py       # 拉取 METAR 实测
│   ├── engine.py            # 预报引擎（聚合+校准）
│   ├── correct.py           # 实时修正
│   ├── evaluate.py          # 准确率追踪
│   └── serve.py             # API 端点（可选）
├── data/
│   ├── forecasts/           # {city}/{date}.json
│   ├── metar/               # {station}_{date}.json
│   ├── models/              # {city}_bias.json
│   └── accuracy/            # {city}.json
├── notebooks/               # Jupyter 分析
└── config/
    └── cities.yaml          # 城市配置
```
