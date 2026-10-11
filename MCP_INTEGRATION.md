# MCP 集成说明

本文档说明本项目使用的麦当劳 MCP Server、调用的 Tool、调用流程与业务价值。

- **MCP Server：** 麦当劳中国 MCP（`https://mcp.mcd.cn`）
- **传输协议：** Streamable HTTP
- **鉴权方式：** `Authorization: Bearer ${MCD_MCP_TOKEN}`
- **限流：** 600 次 / 分钟 / Token（超限返回 `429`）
- **配置示例：** [`mcp-config.example.json`](./mcp-config.example.json)

---

## 1. 使用的 Tool

本项目按「查询 → 核对 → 试算 → 下单」四个环节调用以下 Tool：

| MCP Tool | 在本项目中的作用 | 调用时机 |
|---|---|---|
| `query-nearby-stores` | 取得用户附近的候选门店 | 用户未指定门店时，作为流程起点 |
| `query-meals` | 取得门店当前可售卖的餐品清单 | 需要选品或核对餐品是否供应时 |
| `query-meal-detail` | 取得套餐组成与可替换选项 | 需要确认套餐内容时 |
| `query-my-coupons` | 取得账户下全部可用优惠券 | 试算前汇总可使用的券 |
| `query-store-coupons` | 取得指定门店可用优惠券 | 已确定门店后，补充门店维度可用券 |
| `available-coupons` | 取得麦麦省当前可领取的券 | 用户券不足时，提示是否有可领券 |
| `calculate-price` | 计算商品金额、配送费、优惠金额与应付总价 | 输出最终价格前，做真实试算 |
| `create-order` | 创建订单并返回订单详情与支付链接 | 用户确认方案后下单 |
| `query-order` | 查询订单状态与内容 | 用户查看订单进度时 |
| `now-time-info` | 取得当前日期与时间 | 需要判断营业时段、活动有效期时 |

> 上表为当前基线，随功能迭代同步更新。

---

## 2. 调用流程

### 场景 A：查询与试算（只读）

```text
用户输入
  → Step 1. query-nearby-stores   # 确定候选门店
  → Step 2. query-meals           # 取得该门店可售餐品
  → Step 3. query-my-coupons      # 汇总账户可用券
            query-store-coupons   # 汇总门店可用券
  → Step 4. calculate-price       # 试算优惠金额与应付总价
  → 输出：餐品清单 + 可用券 + 实付金额
```

### 场景 B：下单（有副作用，需用户确认）

```text
用户确认方案
  → Step 1. calculate-price       # 复核最终价格
  → Step 2. create-order          # 创建订单，取得支付链接
  → Step 3. query-order           # 回查订单状态
  → 输出：订单详情 + 支付链接
```

### 异常处理

| 情况 | 处理方式 |
|---|---|
| `401` Token 无效 / 过期 | 提示用户重新申请并更新 Token |
| `429` 触发限流 | 降低调用频率、合并请求后重试 |
| 门店无可用餐品 / 券 | 回退到次优门店，或如实告知用户 |
| 关键参数缺失（地址、门店等） | 先向用户确认，不臆造参数 |
| 用户未确认即触发下单 | 拒绝执行，先复述方案并等待确认 |

---

## 3. 业务价值

- **对用户：** 把分散在多个入口的门店、菜单、优惠券、价格信息汇总到一次对话中，减少逐项查看与人工比对的成本。
- **数据可靠性：** 价格、库存、券状态一律取自 MCP 实时返回，避免大模型凭既有知识给出失真的价格信息。
- **可复用性：** 调用流程以 Skill 形式沉淀，同一套流程可直接迁移到其他支持 MCP 的 AI 客户端。

---

## 4. 附：麦当劳 MCP 全量 Tool 清单

供开发时选型参考（来源：[M-China/mcd-mcp-server](https://github.com/M-China/mcd-mcp-server)，按业务域归类）。

### 门店与地址

| Tool | 说明 |
|---|---|
| `query-nearby-stores` | 查询用户附近可用的麦当劳餐厅 |
| `query-store-coupons` | 查询当前门店下可用的优惠券列表 |
| `delivery-query-addresses` | 获取用户可配送地址列表 |
| `delivery-create-address` | 新增配送地址 |
| `delivery-query-stores` | 查询可配送的门店列表 |
| `query-meal-assistance` | 查询助餐服务（企业团餐场景） |

### 菜单与营养

| Tool | 说明 |
|---|---|
| `query-meals` | 查询当前门店可售卖的餐品列表（分类、餐品编码、标签等） |
| `query-meal-detail` | 按餐品编码查询餐品详情、套餐组成与可替换选项 |
| `list-nutrition-foods` | 获取常见餐品的营养成分数据（能量、蛋白质、脂肪、碳水、钠、钙等） |

### 价格与订单

| Tool | 说明 |
|---|---|
| `calculate-price` | 根据选购列表（可含优惠券）计算金额、配送费、优惠金额与应付总价 |
| `create-order` | 创建订单，返回订单详情与支付链接 |
| `cancel-order` | 取消订单 |
| `query-order` | 查询订单状态、内容、配送信息 |
| `order-list` | 查询近期到店 / 外送历史订单 |

### 优惠券与活动

| Tool | 说明 |
|---|---|
| `available-coupons` | 查询当前可领取的麦麦省优惠券列表 |
| `auto-bind-coupons` | 一键领取麦麦省全部当前可用优惠券 |
| `query-my-coupons` | 查询账户下所有可用优惠券 |
| `campaign-calendar` | 查询当月营销活动日历（进行中 / 往期 / 未来） |

### 积分与麦麦商城

| Tool | 说明 |
|---|---|
| `query-my-account` | 查询积分账户（可用、累计、冻结、即将过期积分） |
| `mall-points-products` | 查询麦麦商城可积分兑换或现金购买的商品 |
| `mall-product-detail` | 查询商城商品详情 |
| `mall-create-order` | 积分兑换商品下单 |
| `mall-order-list` | 查询麦麦商城近一年订单 |
| `mall-order-detail` | 查询麦麦商城订单详情 |

### 积分抽奖

| Tool | 说明 |
|---|---|
| `query-lottery-info` | 查看积分抽奖活动信息（状态、奖品、消耗规则） |
| `draw-lottery` | 执行一次积分抽奖 |
| `query-my-prizes` | 查看我在抽奖活动中获得的奖品记录 |

### 主题活动（派对 / 品鉴会）

| Tool | 说明 |
|---|---|
| `query-party-city` | 查询主题活动可参与城市列表 |
| `query-party-store` | 查询指定城市下可参与的门店 |
| `query-partystore-date` | 查询指定门店可预约日期 |
| `query-partystore-session` | 查询指定门店、日期下可预约场次 |
| `party-order-create` | 创建主题活动订单 |

### 基础

| Tool | 说明 |
|---|---|
| `now-time-info` | 获取当前时间信息（供 LLM 判断日期 / 时段） |
