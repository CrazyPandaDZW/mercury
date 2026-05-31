# Mercury

水星 — 自建多模型集合气温预报 + Polymarket 自动交易系统。

**"水银温度计"** 测量气温，**"墨丘利"** 掌管商业交易。一石二鸟。

## 核心能力

- 🌡️ **气温预报**: 多模型集合（ECMWF/GFS/JMA/ICON/GEM）逐时温度预测
- 📊 **实时修正**: METAR 观测驱动，每 30 分钟更新
- 💰 **自动交易**: 基于预报 + CLOB 订单簿的 Polymarket 策略执行
- 📈 **准确率追踪**: 滑动窗口 MAE/bias，偏差方向感知

## 快速开始

```bash
# 1. 拉取北京多模型预报
python3 scripts/fetch_openmeteo.py --city beijing

# 2. 生成集合预报
python3 scripts/engine.py --city beijing --date 2026-06-01

# 3. 查看预报
cat data/forecasts/beijing/2026-06-01.json
```

## 状态

- [ ] Phase 1: 数据管道（预报 + 实测）
- [ ] Phase 2: 预报引擎（集合聚合 + 偏差校准）
- [ ] Phase 3: 实时修正（METAR 驱动）
- [ ] Phase 4: 交易引擎（策略 + CLOB 交互）
- [ ] Phase 5: 上线运行（cron + 通知）

## 城市覆盖

40 城，详见 [设计文档](docs/design.md)。

## 数据源

- 预报：Open-Meteo API（免费，多模型）
- 实测：IEM ASOS（NOAA METAR 数据）
- 交易：Polymarket CLOB API
