# Mercury — 自建多模型集合气温预报系统

> 基于 Open-Meteo 多模型集合预报 + NOAA METAR 实测校准，对标 polywx v3（MAE ~0.29°C）。
> 长期目标：Polymarket 天气市场自动交易。当前阶段：建立预报精度基线 + 纸面交易回测。

---

## 当前状态 (2026-06-01)

### ✅ 已实现

| 模块 | 状态 | 说明 |
|:---|:---:|:---|
| 多模型预报拉取 | ✅ | 6模型 (ECMWF+GFS+JMA+ICON+GEM+UKMO)，每天2次，4天范围 |
| METAR 实测采集 | ✅ | 40城 NOAA ASOS，每30分钟增量追加 |
| 时间衰减加权 RMSE | ✅ | 半衰4h，窗口24h，动态融合模型权重 |
| 自有偏差校准 | ✅ | 前7天误差均值，≥2天样本启用 |
| L1 融合曲线 | ✅ | 逐时加权融合，过去冻结+未来动态重算 |
| 预测演变快照 | ✅ | snapshots.jsonl，每有新METAR自动记录 |
| 前端可视化 | ✅ | city.html：L1曲线图、模型权重、METAR时间线 |
| 外网访问 | ✅ | localhost.run SSH 隧道 |
| launchd 自动调度 | ✅ | 3个定时任务：预报拉取、METAR采集、引擎运行 |

### 🔶 进行中

| 模块 | 状态 | 说明 |
|:---|:---:|:---|
| 纸面交易策略 V1 | 📋 设计完成 | [策略文档](docs/trading_strategy_v1.md) |
| 三层交易体系 | 📋 待实现 | 开盘单(MAE权重) + 盘中修正(跨桶) + 收盘单 |
| 预测-时间曲线分析 | 📋 基础设施就绪 | snapshots.jsonl 积累中 |

### ⬜ 待做

| 模块 | 说明 |
|:---|:---|
| 信号生成脚本 | 读取引擎输出+CLOB → 生成 trade 记录 |
| 结算脚本 | D+1 读取 METAR t_max → 判定输赢 |
| 盘中修正触发 | 跨桶自动生成修正 trade |
| CLB 数据接入 | Polymarket CLOB 订单簿拉取 |
| MAE 种子自动化 | 每天结算后自动更新模型权重 |

---

## 快速开始

```bash
# 拉取预报数据
python3 scripts/fetch_openmeteo.py --all

# 拉取实测数据
python3 scripts/fetch_metar.py --all --merge

# 运行预报引擎
python3 scripts/engine.py --all --date 2026-06-01 --deb --save

# 启动前端
python3 scripts/serve.py --port 8080
```

## 技术栈

- **语言**: Python 3.13
- **数据源**: Open-Meteo API, NOAA IEM ASOS
- **调度**: macOS launchd
- **前端**: 纯 HTML/CSS/JS (Chart.js), SVG 图表
- **部署**: localhost.run SSH 隧道 (外网)
- **配置**: YAML (cities, models)
- **存储**: JSON 文件

## 城市覆盖

40 个全球主要城市，覆盖亚/欧/北美/南美/非洲/大洋洲。

详见 `config/cities.yaml`。

## 目录结构

```
mercury/
├── config/          # cities.yaml 城市+模型配置
├── scripts/         # Python 脚本 (fetch/engine/serve)
├── frontend/        # 前端页面
├── data/
│   ├── forecasts/   # 多模型预报快照
│   ├── metar/       # METAR 实测数据
│   ├── engine/      # 引擎输出+预测快照
│   └── models/      # MAE 种子权重
├── docs/            # 文档
│   ├── PIPELINE.md          # 数据处理流程
│   └── trading_strategy_v1.md  # 交易策略
└── logs/            # 日志
```

## 文档

- [数据处理流程 (PIPELINE)](docs/PIPELINE.md)
- [纸面交易策略 V1](docs/trading_strategy_v1.md)

## 许可证

私有项目。
