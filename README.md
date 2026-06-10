# 量化交易风控平台

简化版交易风控与委托执行系统。

## 功能

- FastAPI 单体服务，SQLite 持久化。
- 登录认证，默认用户：
  - `trader_a / password_a`
  - `trader_b / password_b`
- 下单/撤单指令双人复核，提交人不能审核自己的指令。
- 审核通过后执行风控，风控拒绝不会发送到模拟交易所。
- 下单前风控覆盖委托限频、未完结订单数、每分钟成交额、价格区间/价格上限。
- 合约覆盖 IF、IC、IM 当前可交易合约：当月、下月、随后两个季月。
- 模拟交易所异步产生拒单、排队、部分成交、完全成交、撤单回报。
- 模拟交易所提供直连 HTTP/WS 接口：`POST /orders`、`POST /cancel`、`WS /exchange/ws`。
- 订单状态机支持乱序和重复回报，所有回报先落 `order_events`。
- 前端支持下单、撤单、复核、订单实时刷新和仓位展示。

## 模拟交易所节奏

`app/exchange.py` 中可调整撮合速度：

- 首次成交延迟：`FIRST_TRADE_DELAY_RANGE = (3.0, 5.0)`
- 后续成交延迟：`NEXT_TRADE_DELAY_RANGE = (2.0, 5.0)`
- 每轮撮合成交概率：`TRADE_PROBABILITY = 0.35`
- 单次全成概率：`FULL_FILL_PROBABILITY = 0.15`

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000`。

## 测试

```powershell
pytest
```

## 演示全部测试场景

登录后点击页面里的“载入全部测试场景”，系统会清空业务数据并生成覆盖以下情况的演示数据：

- 新订单未确认、拒单、排队、部分成交、全部成交、撤单
- 重复回报、乱序回报、超量回报裁剪
- 下单风控拒绝：价格、限频、每日笔数、未完结订单数、成交额
- 撤单风控拒绝：订单不存在、未确认、终态、重复撤单

对应接口：

- `POST /demo/scenarios`
- `GET /instructions`

## 关键接口

- `POST /auth/login`
- `GET /instruments`
- `POST /instructions/orders`
- `POST /instructions/cancel`
- `GET /instructions/pending`
- `GET /instructions`
- `POST /instructions/{id}/review`
- `POST /demo/scenarios`
- `POST /orders`：模拟交易所直连下单入口
- `POST /cancel`：模拟交易所直连撤单入口
- `GET /orders`
- `GET /orders/{order_id}`
- `GET /positions`
- `WS /ws`
- `WS /exchange/ws`：模拟交易所原始回报推送

## 恢复策略

订单、指令、成交和原始回报都保存在 `quant_risk.db`。服务重启后直接从 SQLite 读取最新订单、指令和仓位；未完成的模拟撮合任务不会跨进程恢复，但已落库状态保持一致，后续撤单和查询仍基于持久化订单状态。
