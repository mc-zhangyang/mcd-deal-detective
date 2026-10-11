# mcd-deal-detective

> 🏅 麦当劳程序员创意开发大赛（M-CODE）参赛项目
> 基于麦当劳中国 MCP 能力开发的 Skill。

一个运行在 AI 客户端中的 Skill，通过麦当劳中国 MCP 拉取实时数据，帮用户完成餐品查询、优惠核对与下单辅助，把分散在多个入口的信息汇总到一次对话里。

## 项目介绍

- **要解决的问题：** 麦当劳的餐品、价格、优惠券、活动、积分等信息分散在多个入口，人工逐个查看、比对的成本较高，且信息随时变动。
- **核心思路：** 将麦当劳 MCP 提供的多个 Tool 编排成固定流程，由 AI 在一次对话中按统一口径完成检索、比对与结论输出。
- **和「直接问 AI」的区别：** 直接询问大模型只能基于既有知识作答，价格、库存、券状态等实时信息容易失真；本 Skill 要求先通过 MCP 取得实时数据再作答。

### 能力一览

| 能力 | 说明 | 依赖的 MCP Tool |
|---|---|---|
| 门店与菜单查询 | 查询附近或指定门店的可售卖餐品、套餐组成与营养信息 | `query-nearby-stores`、`query-meals`、`query-meal-detail`、`list-nutrition-foods` |
| 优惠核对与价格试算 | 汇总账户优惠券与门店可用券，计算优惠金额与应付总价 | `query-my-coupons`、`query-store-coupons`、`available-coupons`、`calculate-price` |
| 订单与积分 | 查询历史订单、订单状态与积分账户信息 | `order-list`、`query-order`、`query-my-account` |

### 亮点

1. **实时数据优先** —— 结论全部来自 MCP 实时返回，不依赖模型记忆中的旧数据。
2. **多步流程自动化** —— 门店定位、菜单查询、优惠券核对、价格试算在一次对话内完成。
3. **流程与规则分离** —— Skill 定义调用流程，业务规则集中维护，便于调整与扩展。

## 目标用户

- 希望在同一个对话窗口中完成麦当劳餐品查询、优惠核对与下单辅助的顾客。
- 使用 WorkBuddy / Cursor / Cherry Studio / TRAE 等支持 MCP 的 AI 客户端的开发者。

## 安装方法

### 前置条件

1. 申请麦当劳中国 MCP Token：登录 [麦当劳 MCP 开放平台](https://open.mcd.cn/mcp) → 控制台 → 激活 → 复制 Token
   （图文步骤见 [mcd-mcp-server 使用指南](https://github.com/M-China/mcd-mcp-server)）
2. 一个支持 MCP 的客户端，例如 [WorkBuddy](https://www.workbuddy.cn/) / Cursor / Cherry Studio / TRAE / VSCode

### 第一步：接入 MCP Server

复制仓库根目录的 [`mcp-config.example.json`](./mcp-config.example.json)，填入你的 Token 后导入客户端：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer ${MCD_MCP_TOKEN}"
      }
    }
  }
}
```

> ⚠️ 请通过环境变量注入 Token，**不要**把真实 Token 提交到仓库。

### 第二步：安装 Skill

将 `SKILL.md` 放入客户端的 Skill 目录：

| 作用域 | 路径 |
|---|---|
| 用户级（所有项目可用） | `~/.workbuddy/skills/mcd-deal-detective/` |
| 项目级 | `{workspace}/.workbuddy/skills/mcd-deal-detective/` |

放置后在客户端启用，即可在对话中触发。

### 第三步：验证

在对话框中输入一句最简单的需求，确认 MCP 正常返回数据（例如「查一下我附近有哪些麦当劳门店」）。

## 使用示例

**示例 1**

> 用户输入：

```
查一下我附近有哪些麦当劳门店
```

> 输出：

```
以下为示意输出，实际结果以 MCP 实时返回为准。

附近共 3 家门店：
1. XX 餐厅（约 500m，营业中）
2. XX 甜品站（约 800m，营业中）
3. XX 餐厅（约 1.2km，已打烊）
```

**示例 2**

> 用户输入：

```
帮我算一下这两样东西加上我手上的券一共多少钱
```

> 输出：

```
以下为示意输出，实际结果以 MCP 实时返回为准。

商品原价：¥XX
可用优惠：-¥XX
配送费：¥XX
应付总价：¥XX
```

## 项目结构

```
.
├── README.md                  # 项目说明（本文件）
├── CONTEST_DECLARATION.md     # 参赛声明（官方文件，内容不可修改）
├── MCP_INTEGRATION.md         # MCP 接入与调用说明
├── mcp-config.example.json    # 脱敏后的 MCP 配置示例（仅环境变量占位符）
├── workbuddy.md               # WorkBuddy 开发上下文（联动奖励核验用）
└── SKILL.md                   # Skill 主体定义
```

## 注意事项

- 本项目为参赛作品，由参赛者独立开发，**非麦当劳官方产品**。
- 餐品、价格、优惠券、活动等信息以麦当劳官方渠道的实时结果为准，项目输出仅供参考。
- MCP Token 每分钟限 600 次请求（超限返回 `429`），请合理控制调用频率。

## 许可证

[MIT](./LICENSE)
