# optimizer.py 接口

纯 Python 3.9+ 标准库，不联网、不读凭证、不下单。所有输入输出都是 UTF-8 JSON。

```bash
python3 -B "<SKILL_ROOT>/scripts/optimizer.py" plan --input "<RUN>/request.json" --output "<RUN>/plan.json"
python3 -B "<SKILL_ROOT>/scripts/optimizer.py" pick --input "<RUN>/quotes.json"  --output "<RUN>/picked.json"
```

成功时标准输出一行摘要 JSON，退出码 0；输入有误时标准错误输出 `{"error": "..."}`，退出码 2。

## request.json（plan 输入）

```json
{
  "context": {"storeCode": "1960649", "orderType": 1, "beType": 1, "needTableware": false},
  "demands": [
    {"id": "burger", "label": "汉堡", "quantity": 2, "codes": ["1440", "1406"]},
    {"id": "nugget", "label": "麦乐鸡", "quantity": 1, "codes": ["1401", "515106"]}
  ],
  "forbidden_codes": [],
  "strict": false,
  "products": [
    {"code": "1440", "name": "麦辣鸡腿汉堡", "currentPrice": "22.5", "originalPrice": "22.5"},
    {"code": "9900011056", "name": "全明星双人分享餐八件套", "currentPrice": "49.9", "originalPrice": "114.5",
     "discountType": "随单购麦金卡优惠", "withOrder": {"membershipCode": "106", "specId": "880"},
     "rounds": [
       {"id": 1, "minQuantity": 1, "choices": [
         {"code": "1440", "name": "麦辣鸡腿汉堡", "diffPrice": "+ ¥0", "maxQuantity": -1, "isDefault": 1, "modification": {"items": ["…详情原样…"]}}
       ]},
       {"id": 6, "minQuantity": 2, "choices": [
         {"code": "3050", "name": "可乐中杯", "diffPrice": "+ ¥0", "maxQuantity": -1, "isDefault": 1}
       ]}
     ]},
    {"code": "COUPON_PRODUCT", "name": "券商品名", "price": "9.9", "food_code": "1440",
     "coupon": {"couponId": "…", "couponCodes": ["…", "…"], "title": "券名"}}
  ],
  "reference_prices": {"3050": "9.5"},
  "max_quotes": 10,
  "store_name": "麦当劳西安长和卜蜂中心餐厅",
  "effort": {"menu_items": 131, "details_checked": 5, "coupons_claimed": 0, "store_coupons": 0}
}
```

| 字段 | 说明 |
|---|---|
| `context` | 计价/下单公共参数：`storeCode / orderType / beType`，按需 `beCode / reservationDate / needTableware`。不放 `items / withOrder`。 |
| `demands` | 必须餐品。`quantity` 为至少要的份数；`codes` 为可接受的真实餐品 code（类别需求列多个）。 |
| `forbidden_codes` | 明确不要的餐品 code，任何方案都不会出现。 |
| `strict` | `true` 表示用户要求“只要这些”，不允许任何附带。默认 `false`。 |
| `products` | 单品、套餐、券商品。字段名与官方菜单/详情一致；`rounds` 只保留必选轮次（`minQuantity≥1`），可选加购轮次会被忽略。`maxQuantity`（商品级，可选）为限购份数。 |
| 券商品 | `price` 为单独官方计价得到的券后价；`coupon.couponCodes` 的数量即可用张数；单品券写 `food_code`（它对应的普通餐品 code），套餐券写 `rounds`。 |
| `reference_prices` | 套餐里出现、但 `products` 里没有同 code 单品的食品的单点价，用于升级判断（没有原价的多出餐品不计价值）。只有一轮且每份选 1 个的商品（如选杯型），其选项单点价会自动推算。仍缺的，用同一轮里有真实单点价的选项按差价推一步：价(X)≈价(Y)+差价(X)−差价(Y)，多处可推取最低，不用推算值再推；推算结果在 plan 输出的 `estimated_prices`。 |
| `max_quotes` | 最多交给官方计价的候选数，默认 10（1～30）。 |
| `card_value` | 随单开的卡算作多得价值时的金额（元），默认 5，不超过实际卡费。 |
| `store_name` | 门店名，显示在方案标题。 |
| `effort` | 本次实际查询量（非负整数，均可省略）：`menu_items` 菜单商品数、`details_checked` 查看详情的套餐数（省略时按 `products` 里的套餐数）、`coupons_claimed` 领到的免费券数、`store_coupons` 本单可用券数。用于“侦查过程”说明。 |

价格规则：`discountType` 以“随单购”开头的商品，不开卡时按 `originalPrice`，随单开对应卡时按 `currentPrice`；其它商品一律按 `currentPrice`（已经是会员时菜单现价即可享）。

## plan.json（plan 输出）

- `quotes[]`：需要官方计价的候选，每项有 `id`、`roles`、`context`（`none` 或随单购条件名）、`estimate`（菜单估价，不含卡费）、`display`、`minimal`（每份购买都不可少）、`payload`（可直接用于 `calculate-price` 和 `create-order`）。
- `roles`：`baseline` 全单点、`best` 该情况估价最低、`upgrade` 同一开卡情况内的加钱升级候选（跨开卡情况的升级由 pick 用官方价判断，各情况最低价已作为 `best` 计价）、`flavor` 换口味、`lean` 少附带、`next` 其它接近最优的组合。
- `notes`：需要转告用户的提示；`search`：各情况的搜索节点数与是否完整。

搜索方法：对每种开卡情况，枚举套餐购买组合（分支定界，按套餐固定成本剪枝），每个组合用最小费用流求“套餐选项＋单点补齐”的最优分配；同价优先套餐默认选项。估价以菜单现价/原价加选项差价计算。

## quotes.json（pick 输入）

```json
{
  "plan_file": "/绝对路径/plan.json",
  "results": [
    {"id": "q1", "price": 8750, "productList": [
      {"productCode": "1440", "productName": "麦辣鸡腿汉堡", "quantity": 2, "subtotal": 4500}
    ]},
    {"id": "q2", "price": 5980, "productList": [
      {"productCode": "9900011056", "productName": "全明星双人分享餐八件套", "quantity": 1, "subtotal": 4990},
      {"productCode": "521234", "productName": "麦金卡体验卡（15天）", "quantity": 1, "subtotal": 990}
    ]},
    {"id": "q3", "error": "官方返回的错误信息"}
  ]
}
```

`price` 填官方 `data.price`（整数分，已含随单购卡费，不要另加），`productList` 照抄官方回显。校验规则：下单商品的数量必须与回显一致；回显里多出的商品只在带 `withOrder` 的方案中视为随单购卡（计入卡名与卡费），否则该报价作废；卡费超过整单实付也作废。实付与方案差价一律用官方价（含真实卡费），`card_value` 只用于升级的价值判断。

## picked.json（pick 输出）

- `recommendations[]`（最多 3 个）：
  - `label` 特点、`price` 官方实付、`summary` 一句话点法（如“人气经典随心配＋4 件单品”）；
  - `items` 整单全部餐品，`demanded=true` 为满足需求的、`false` 为多送的；
  - `notes` 备注（卡费、升级多得、换了什么口味、“同样这些单点要¥X，省¥Y”）。“同样这些”指方案里全部餐品（含多送的）按单点原价的合计；用了推算价时写“约要”；仍有餐品推不出单点价时只算已知部分，写“至少要¥X（某某未计入），省¥Y以上”；完全算不出才改为和全单点基准比；
  - `card`（随单开卡名与费用，或 null）、`upgrade`、`display`（每份套餐的具体选法）、`allocation`、`extras`、`payload`（下单参数）。
- `baseline`：全单点官方价。
- `effort`：侦查过程一句话；`conclusion`：结论一句话。
- `title`：标题行（门店与取餐方式）。
- `cards_html`：方案卡片 HTML 片段（只用内联样式和宿主 CSS 变量，无脚本），直接作为 WorkBuddy 内联可视化组件的 `widget_code`。每张卡：特点标签、实付、一句话点法、餐品（点名的加粗，附带灰色）、备注；第一张高亮。
- `footer`：图例、侦查过程、结论、选择提示。
- `table_markdown`：卡片的 Markdown 对比表版本；`message` = `title`＋`table_markdown`＋`footer`，供不支持内联组件的客户端使用。
- `excluded`：计价失败或回显不一致的候选及原因；`unquoted`：没有提交结果的候选。

推荐规则：

1. **最低实付**：官方整单实付最低（同价时附带少、不开卡、餐品原价更高的优先）。
2. **多花¥x升级**：比最低实付多出附带餐品、每份购买都不可少，且 `5 × 多花金额 ≤ 多得价值`。多花金额是官方整单差价（含卡费）；多得价值 = 整单餐品单点原价合计之差（多出的加、少掉的减，换更贵口味也计入）＋ 新开卡的价值（`card_value`，默认 ¥5，不超过卡费）。可以跨开卡情况。有多个时选“多得价值减多花金额”最大的。
3. 其余名额依次给：**不开卡**（最低实付需要开卡时）、**换口味**（有类别需求时，必须餐品用了不同口味的最便宜方案）、**少附带**（附带更少的最便宜方案）。没有就不凑数。
