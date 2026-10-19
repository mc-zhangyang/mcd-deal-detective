# MCP 集成说明

## 服务

- MCP Server：麦当劳中国官方 MCP，`https://mcp.mcd.cn`，Streamable HTTP。
- 鉴权：`Authorization: Bearer ${MCD_MCP_TOKEN}`，由 WorkBuddy 连接器保存，示例见 [mcp-config.example.json](mcp-config.example.json)。本项目代码不读取、不保存 Token。
- 限流：每个 Token 每分钟 600 次。麦侦探一次完整计算通常只需约 15～40 次调用（门店/地址 1～3、菜单 1、套餐详情 10～30、计价约 10），全部是查询与计价，不下单。

## 分工

| 角色 | 负责 |
|---|---|
| WorkBuddy（按 SKILL.md） | 理解需求并问清就餐方式、调用 MCP、整理菜单数据为 JSON、展示方案与点法 |
| `scripts/optimizer.py plan` | 按菜单价与套餐差价在本地精确搜索，选出约 10 个候选，生成可直接提交的计价参数 |
| 官方 `calculate-price` | 给出每个候选的真实整单实付（含随单购卡费、优惠、配送/打包费） |
| `scripts/optimizer.py pick` | 校验官方回显，按实付排序，给出最多 3 个方案和方案卡片（按套餐/单点/用券/随单开卡分块） |

## 使用的工具

| 工具 | 用途 |
|---|---|
| `query-nearby-stores` | 到店取餐：按用户本次给的城市+位置/店名找门店（`searchType=2`），或列出收藏餐厅（`searchType=1`），取得 `storeCode` |
| `delivery-query-addresses` / `delivery-create-address` | 麦乐送：列出已保存地址请用户确认送到哪个；要送新地址时按用户提供的信息新增 |
| `delivery-query-stores` | 麦乐送：按地址取能配送的门店 `storeCode / beCode` |
| `query-meals` | 全店菜单：code、现价、原价、优惠类型、随单购条件 |
| `query-meal-detail` | 套餐/双人餐/随心配的每轮选项、差价、默认特调 |
| `available-coupons` / `auto-bind-coupons` | 查看并领取麦麦省免费券 |
| `query-store-coupons` | 本店本单可用的券及适用商品 |
| `calculate-price` | 候选方案官方计价（整单实付、卡费、配送费、打包费） |

## 调用链

```text
就餐方式（麦乐送/到店取餐，先问清只算一种）+ 位置或地址 + 必须餐品
  → 到店取餐：问清本次位置 → query-nearby-stores → 自动选最近营业门店（可换）
    麦乐送：delivery-query-addresses → 用户确认地址 →（新地址）delivery-create-address → delivery-query-stores
  → query-meals → query-meal-detail（相关套餐）
  → available-coupons →（有则）auto-bind-coupons → query-store-coupons
  → optimizer plan（本地精确搜索，约 10 个候选）
  → calculate-price × 候选数
  → optimizer pick（最多 3 个方案卡片）→ 展示，用户自行在 App/小程序下单
```

## 参数要点

- 到店取餐：`beType=1, orderType=1`，不传 `beCode`（堂食/外带在下单页选，价格相同）；麦乐送：`beType=2, orderType=2`，传 `delivery-query-stores` 返回的 `beCode`。菜单、详情、门店券、计价都要用同一组参数，麦乐送的菜单与价格不能沿用到店取餐。
- 套餐请求：`items[].roundList[] = {round: 轮次id字符串, comboItemList: [{code, quantity, modification?}]}`，每套数量乘以 `items[].quantity`；同一套餐不同选法拆成多行。
- 特调：选项 `supportModify=true` 时按详情默认值传 `modification.values[]`，选中项用 `selectedKey`，同组含 `unselectedKey` 的未选项用 `unselectedKey`。
- 随单购：菜单 `discountType` 为“随单购…”时，现价需在请求里带 `withOrder`（菜单 `specId` 对应请求 `membershipSpecId`）；不带则按原价。计价回显会多出一行卡商品，其小计计入整单 `data.price`。
- 随单购卡种由门店决定：同一账号、同一 `membershipSpecId`，长和卜蜂 1960649 回显“麦金卡体验卡（15天）”¥9.90，钟鼓楼 1960602 回显“麦金卡月卡（30天）”¥19。卡名与卡费一律以该门店计价回显为准。
- 套餐选项上限：`query-meal-detail` 返回 `data.rounds[].choices[].maxQuantity`（`-1` 为不限）。一轮 `minQuantity>1` 不代表同一个选项能选多份——例如长和卜蜂 1960649 的「BFF四宫格小食盘」第 1 轮要选 3 份，但「麦乐鸡-5块」`maxQuantity=1`，写成 `quantity=3` 会整单报错“麦乐鸡-5块最多可选1份”，整单作废。片段文件必须把这个值原样抄下来。
- 用券：`items[]` 上带 `couponId` 与 `couponCode`，一张券码一行。券商品的券后价只能靠带券 `calculate-price` 拿（不带券时 `productList` 为空白、`price` 为 0）——**判据是 `productList` 里有没有这个券商品，不是 price 是否为 0**：`productList` 有它、`price=0` 就是 0 元券（长和卜蜂／钟鼓楼「25块麦乐鸡」「新地买一送一」均为 ¥0，与小程序显示一致）；`productList` 为空才是不可用。
- **多券同单**：同一个 payload 里带两个不同 `couponId`，`calculate-price` 返回 `code 600022“暂不支持多张券使用”`（在钟鼓楼 1960602 用多组券、加不加第三件商品、带不带 `roundList` 共 4 次均复现）。但**小程序里两张券可以一起下单、下单页显示 ¥0**——这只是计价接口算不了，不是真的不能买。所以这条报错**直接忽略**：`pick` 遇到多券候选被 600022 拒绝时，按各券券后价合计出价（本地估价，与小程序一致），方案照常给出，备注写“按各券券后价合计；以下单页为准”。
- **券后价 ¥0 多为外购券**：卡包里的券带券 `calculate-price` 返回 `price=0` 且 `productList` 里有它，说明钱是在抖音/快手/团购等渠道付过的，麦当劳这单只收 ¥0（开元 1960622：「25块麦乐鸡」「新地买一送一」两张都是 ¥0，而同店「王牌小食拼」券是 ¥15.90）。这不是免费券，**要问用户买券花了多少**才能算真实成本（`coupon.paid`）；接口拿不到这个金额。**只有券后价 ¥0 的券才问**：券后价 > ¥0 的券（如上面 ¥15.90 的「王牌小食拼」）都是领的或活动送的，直接按官方券后价算，不存在买券成本。
- 券商品可能是套组：`query-meal-detail` 显示「25块麦乐鸡」= 5×麦乐鸡5块（`minQuantity=5`）、「新地买一送一」= 2 个新地（`minQuantity=2`，可选朱古力/草莓），计价时带上 `roundList` 更贴近真实下单。
- **`items[].productCode` 必须是菜单/券列表里的真实商品编码**，不能用自己起的内部名（如 `COUPON_1`）。用内部名时 `calculate-price` 整单返回 `code 600012“促销规则不支持该商品”`——注意它和「券不可用」是同一个码，**不是券的问题，是编码不对**（长和卜蜂 1960649 麦乐送：同一张「25块麦乐鸡」券，`productCode=9900008604` 单券计价 ¥0 成功；换成 `COUPON_1` 后三个候选全部 600012）。带券计价时 `productCode`＋`couponId`＋`couponCode` 三者缺一不可。
- 到店取餐在门店打烊时段会返回 `code 60003“门店暂停营业，请重新选择门店”`，需要传 `reservationDate`（`yyyy-MM-dd HH:mm`）按下一营业时段计价。
- 金额：计价返回的 `price / subtotal / deliveryPrice / packingPrice / tablewarePrice` 为整数分；整单实付取 `data.price`（已含卡费和各项费用）。

## 真实调用记录（只读）

| 门店 | 必须餐品 | 官方整单实付 |
|---|---|---|
| 西安开元餐厅 1960622 | 3双层吉士汉堡＋2小杯玉米杯 | 全单点 ¥95.00；两份人气经典随心配（汉堡＋玉米杯）＋1个单点汉堡 ¥52.80 |
| 西安开元餐厅 1960622 | 3龙焰鸡腿堡＋2奥利奥麦旋风 | 全单点 ¥103.00（9 种套餐/单点组合全部计价，单点最低；龙焰四件套选奥利奥麦旋风、不开卡时每份 ¥58） |
| 西安开元餐厅 1960622 | 3麦香鸡＋2奥利奥麦旋风 | 全单点 ¥80.50；每多用一份随心配（麦香鸡＋玉米杯）少 ¥2.60、多一份玉米杯 |
| 西安长和卜蜂中心 1960649 | 2汉堡＋几块麦乐鸡＋2块鸡翅＋1薯条 | 全明星双人分享餐八件套：不开卡 ¥114.50；随单开麦金卡体验卡（15天）¥59.80（套餐 ¥49.90＋卡 ¥9.90），多送中薯条×1、可乐中杯×2 |
| 西安钟鼓楼餐厅 1960602 | 2辣汉堡＋2不辣汉堡＋1份炸鸡＋2杯饮料 | 不开卡最低 ¥84.30（精选超值随心配×2＋单点）；八件套＋随心配×2 随单开麦金卡月卡（30天）¥96.70（含卡 ¥19），多得鸡翅、麦乐鸡、薯条×2、可乐×2 |

这些记录验证了：套餐详情差价、随心配组合、随单购卡回显与卡费、按套数拆行的计价参数均与官方一致；菜单现价在不开卡时不可直接享受（八件套 ¥49.90 → 不开卡 ¥114.50）。这些调用发生在账户无可用券、门店仅有一张与需求无关的券的情况下，用券计价与麦乐送计价暂无真实样例。

## 业务价值与安全

- 用户只说“想吃什么”，就能拿到这家店当下最省的点法，并清楚看到多送了什么、要不要开卡。
- 只使用官方菜单、官方优惠与官方计价；不利用漏洞，不跨品牌比较。
- 只做查询、计价、领取免费券和（用户提供信息后）新增送餐地址；不创建订单、不付款，用户按方案自行下单。
