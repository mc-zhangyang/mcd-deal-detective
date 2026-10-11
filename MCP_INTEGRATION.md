# MCP 集成说明

## 服务

- MCP Server：麦当劳中国官方 MCP，`https://mcp.mcd.cn`，Streamable HTTP。
- 鉴权：`Authorization: Bearer ${MCD_MCP_TOKEN}`，由 WorkBuddy 连接器保存，示例见 [mcp-config.example.json](mcp-config.example.json)。本项目代码不读取、不保存 Token。
- 限流：每个 Token 每分钟 600 次。麦侦探一次完整计算通常只需约 15～40 次只读调用（门店 1、菜单 1、套餐详情 10～30、计价约 10）。

## 分工

| 角色 | 负责 |
|---|---|
| WorkBuddy（按 SKILL.md） | 理解需求、调用 MCP、整理菜单数据为 JSON、展示方案、确认后下单 |
| `scripts/optimizer.py plan` | 按菜单价与套餐差价在本地精确搜索，选出约 10 个候选，生成可直接提交的计价参数 |
| 官方 `calculate-price` | 给出每个候选的真实整单实付（含随单购卡费、优惠） |
| `scripts/optimizer.py pick` | 校验官方回显，按实付排序，给出最多 3 个方案和展示文字 |

## 使用的工具

| 工具 | 用途 |
|---|---|
| `query-nearby-stores` | 按城市+关键词找到门店，取得 `storeCode` |
| `query-meals` | 全店菜单：code、现价、原价、优惠类型、随单购条件 |
| `query-meal-detail` | 套餐/双人餐/随心配的每轮选项、差价、默认特调 |
| `available-coupons` / `auto-bind-coupons` | 查看并领取麦麦省免费券 |
| `query-store-coupons` | 本店本单可用的券及适用商品 |
| `calculate-price` | 候选方案官方计价；下单前复核价格，并取得 `takeWayList` |
| `create-order` | 用户确认后创建订单，返回支付链接 |
| `order-list` / `query-order` | 下单异常时核查是否已生成订单；支付后查状态 |
| `cancel-order` | 取消重复生成的未支付订单 |

## 调用链

```text
门店/位置 + 必须餐品
  → query-nearby-stores → query-meals → query-meal-detail（相关套餐）
  → available-coupons →（有则）auto-bind-coupons → query-store-coupons
  → optimizer plan（本地精确搜索，约 10 个候选）
  → calculate-price × 候选数
  → optimizer pick（最多 3 个方案）→ 用户选择
  → calculate-price 复核 → create-order（一次）→ 支付链接 → query-order
```

## 参数要点

- 到店自取：`beType=1, orderType=1`，不传 `beCode`；得来速：`beType=5, orderType=1`，传门店的 `beCode`。
- 套餐请求：`items[].roundList[] = {round: 轮次id字符串, comboItemList: [{code, quantity, modification?}]}`，每套数量乘以 `items[].quantity`；同一套餐不同选法拆成多行。
- 特调：选项 `supportModify=true` 时按详情默认值传 `modification.values[]`，选中项用 `selectedKey`，同组含 `unselectedKey` 的未选项用 `unselectedKey`。
- 随单购：菜单 `discountType` 为“随单购…”时，现价需在请求里带 `withOrder`（菜单 `specId` 对应请求 `membershipSpecId`）；不带则按原价。计价回显会多出一行卡商品，其小计计入整单 `data.price`。
- 用券：`items[]` 上带 `couponId` 与 `couponCode`，一张券码一行。
- 金额：计价返回的 `price / subtotal` 为整数分；整单实付取 `data.price`。
- 下单：到店必须带 `takeWayCode`（计价返回的 `takeWayList[].code`，如 `eat-in` 堂食、`take-in-store` 外带）。

## 真实调用记录（2026-10-11，只读）

| 门店 | 必须餐品 | 官方整单实付 |
|---|---|---|
| 西安开元餐厅 1960622 | 3双层吉士汉堡＋2小杯玉米杯 | 全单点 ¥95.00；两份人气经典随心配（汉堡＋玉米杯）＋1个单点汉堡 ¥52.80 |
| 西安开元餐厅 1960622 | 3龙焰鸡腿堡＋2奥利奥麦旋风 | 全单点 ¥103.00（9 种套餐/单点组合全部计价，单点最低；龙焰四件套选奥利奥麦旋风、不开卡时每份 ¥58） |
| 西安开元餐厅 1960622 | 3麦香鸡＋2奥利奥麦旋风 | 全单点 ¥80.50；每多用一份随心配（麦香鸡＋玉米杯）少 ¥2.60、多一份玉米杯 |
| 西安长和卜蜂中心 1960649 | 2汉堡＋几块麦乐鸡＋2块鸡翅＋1薯条 | 全明星双人分享餐八件套：不开卡 ¥114.50；随单开麦金卡体验卡（15天）¥59.80（套餐 ¥49.90＋卡 ¥9.90），多送中薯条×1、可乐中杯×2 |

这些记录验证了：套餐详情差价、随心配组合、随单购卡回显与卡费、按套数拆行的计价参数均与官方一致；菜单现价在不开卡时不可直接享受（八件套 ¥49.90 → 不开卡 ¥114.50）。当时测试账户与门店均无可用优惠券，用券计价与真实下单、支付链路尚未在真实订单上走通。

## 业务价值与安全

- 用户只说“想吃什么”，就能拿到这家店当下最省的点法，并清楚看到多送了什么、要不要开卡。
- 只使用官方菜单、官方优惠与官方计价；不利用漏洞，不跨品牌比较。
- 只读查询与领取免费券可自动进行；创建订单必须由用户确认，异常时先核查再处理，不自动重复下单。
