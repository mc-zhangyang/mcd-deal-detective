# optimizer.py 接口

纯 Python 3.9+ 标准库，不联网、不读凭证、不下单，只算到方案展示。所有输入输出都是 UTF-8 JSON。

```bash
python3 -B "<SKILL_ROOT>/scripts/optimizer.py" plan "<RUN>"   # 读 request.json + products_*.json，写 plan.json
python3 -B "<SKILL_ROOT>/scripts/optimizer.py" pick "<RUN>"   # 读 plan.json + quotes.json / quotes_*.json，写 picked.json
```

也可以用 `--input 文件 --output 文件` 指定。

**运行目录里的片段文件**（可多个，程序自动合并）：
- `products_*.json`：`{"products": [...], "singles": [...], "reference_prices": {...}, "demand_codes": {需求id: [code]}, "demand_pieces": {需求id: {code: 块数}}}`。同一 sid 的商品内容完全一致时去重，内容冲突则列出双方路径报错，不默丢价格、轮次或限购。`demand_codes` 只按需求 id 映射 code 数组；`demand_pieces` 仅补按块数需求，非块数需求的空映射忽略。映射错误汇总后一次返回。
- `quotes_*.json`：计价结果数组（或 `{"results": [...]}`），按 `id` 合并，后读到的覆盖先读到的；`plan_file` 省略时取同目录 `plan.json`。

**简写**：
- `singles`：`[code, 名称, 现价]` 或 `[code, 名称, 现价, 原价]`，等同于 `products` 里的单品对象。
- 套餐选项 `choices[]`：`[code, 名称]`、`[code, 名称, 差价]`、`[code, 名称, 差价, 1]`、`[code, 名称, 差价, 1, 上限]`（第四项默认标记写数字 `0/1`，兼容字符串 `"0"/"1"` 和布尔值；缺省/None/空为 false，其他值拒绝；第五项是官方返回的 `maxQuantity`，这一轮该选项最多选几份，省略表示不限；差价写 `0`、`"+1.5"`、`"-1"` 或 `"+ ¥1.5"`）。
- `demands[]`：`id` 可省略（自动为 d1、d2…），`label` 也可写成 `name`。

成功时退出码 0，并在标准输出直接打印下一步要用的内容：`plan` 打印每个候选一行 `qN {payload}`；`pick` 打印 `===== TITLE =====`、`===== EFFORT =====`、`===== LEGEND =====`（无方案时省略）、`===== CARDS_HTML =====`、`===== FOOTER =====` 五段（以 `===== END =====` 结束）。调用方不必再读输出文件。输入有误时汇总可独立校验的分组错误，标准错误输出 `{"error": "..."}`，退出码 2；不创建或覆盖 plan.json，按已列出的字段一次修复后重试。

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
| `context` | 计价公共参数：`storeCode / orderType / beType`，按需 `beCode / reservationDate / needTableware`。到店取餐 `orderType=1, beType=1`，不带 `beCode`；麦乐送 `orderType=2, beType=2`，必须带配送门店的 `beCode`。不放 `items / withOrder`。 |
| `dining` | 就餐方式：`到店取餐` / `麦乐送`（与小程序一致），显示在标题；必须与 `context` 一致（到店取餐↔orderType 1/beType 1，麦乐送↔orderType 2/beType 2）。 |
| `address` | 麦乐送的送餐地址简称，显示在标题。 |
| `demands` | 必须餐品。`quantity` 为至少要的份数；`codes` 为可接受的真实餐品 code（类别需求列多个）。 |
| `demands[].pieces` | 可选，按块数计的需求（如麦乐鸡）：`{code: 一份的块数}`，须覆盖该需求全部 `codes`；此时 `quantity` 是至少要的总块数。整份餐品的加权覆盖 DP 同时支持按份数与按块数的需求，同一份五块装贡献 5 块但不可拆成五份；多个需求共享同一 code 时改用子集（Hall 条件）记帐，保证同一份餐品只顶一个需求——但共享 code 若落在块数需求里（整份不可拆）仍回退数量搜索。DP 状态预算耗尽时懒生成按规格的极小覆盖组合并回退数量搜索；预算未搜完会标注。 |
| `demands[].about` | 可选，`true` 表示用户说的是“左右/大概”：下限取 `quantity` 的 80%（向上取整，至少 1），份数和块数需求都适用。 |
| `forbidden_codes` | 明确不要的餐品 code，任何方案都不会出现。 |
| `strict` | `true` 表示用户要求“只要这些”，不允许任何附带。默认 `false`。 |
| `products` | 单品、套餐、券商品。字段名与官方菜单/详情一致；`rounds` 只保留必选轮次（`minQuantity≥1`），可选加购轮次会被忽略。`maxQuantity`（商品级，可选）为限购份数。 |
| 券商品 | `price` 为单独官方计价得到的券后价；`coupon.couponCodes` 的数量即可用张数；单品券写 `food_code`（它对应的普通餐品 code），套餐券写 `rounds`。`coupon.paid` 是买这张券已经花掉的钱，默认 0，程序按「券后价 + paid」比较方案，`pick` 再把它加回官方报价。**只有券后价为 ¥0 的外购券（抖音/快手之类渠道买的）才需要写**；券后价 > ¥0 的券都是送的，没有买券成本，不要写。 |
| `unused_coupons` | 卡包里有但本单选不到的券，`[{"title", "reason"}]`（只给字符串即标题）。不参与计算，只在 `pick` 的 footer 里列出。 |
| `reference_prices` | 套餐里出现、但 `products` 里没有同 code 单品的食品的单点价，用于升级判断（没有原价的多出餐品不计价值）。只有一轮且每份选 1 个的商品（如选杯型），其选项单点价会自动推算。仍缺的，用同一轮里有真实单点价的选项按差价推一步：价(X)≈价(Y)+差价(X)−差价(Y)，多处可推取最低，不用推算值再推；推算结果在 plan 输出的 `estimated_prices`。 |
| `expected_product_codes / expected_single_codes` | 可选真实商品 code 清单：前者为全部分组待查商品，后者为菜单中所有符合需求的可购买单品（含带 rounds、写在 products 的规格商品）。plan 核对合并后的顶层商品 code，一次列出全部缺失；券 food_code 或套餐 choices 不能代替商品已采集。清单从菜单生成、不按已写文件反填，不证明菜单分类本身无遗漏；未提供清单时照常求解。 |
| `max_quotes` | 最多交给官方计价的候选数，默认 10（1～30）。 |
| `card_value` | 随单开的卡算作多得价值时的金额（元），默认 5，不超过实际卡费。 |
| `store_name` | 门店名，显示在方案标题。 |
| `store_note` | 门店说明，显示在标题下一行（如“离钟楼 152m，最近的营业门店；想换店直接说”）。 |
| `effort` | 本次实际查询量（非负整数，均可省略）：`menu_items` 菜单商品数（省略时写“翻了本店全部菜单”）、`details_checked` 查看详情的套餐数（省略时按 `products` 里的套餐数）、`coupons_claimed` 领到的免费券数、`store_coupons` 本单可用券数。用于“侦查过程”说明。 |

套餐选项只需 `code`、`name`；默认项写 `"isDefault": 1`，差价不为 0 时写 `diffPrice`（如 `"- ¥1"`、`"+ ¥1.5"`），`maxQuantity` 省略即不限，`supportModify=true` 的选项带 `modification`。必选轮次保留全部选项的精简字段。有限容量时非需求选项也是合法补齐所必需，不能只留默认/负差价项；商品级限购同样需要传递。

价格规则：`discountType` 以“随单购”开头的商品，不开卡时按 `originalPrice`，随单开对应卡时按 `currentPrice`；其它商品一律按 `currentPrice`（已经是会员时菜单现价即可享）。

## plan.json（plan 输出）

- `quotes[]`：需要官方计价的候选，每项有 `id`、`roles`、`context`（`none` 或随单购条件名）、`estimate`（菜单估价，不含卡费）、`prepaid`（买这些券已经花掉的钱）、`display`、`minimal`（每份购买都不可少）、`payload`（可直接用于 `calculate-price`）。
- `roles`：`baseline` 全单点、`best` 该情况估价最低、`upgrade` 同一开卡情况内的加钱升级候选（跨开卡情况的升级由 pick 用官方价判断，各情况最低价已作为 `best` 计价）、`flavor` 换口味、`lean` 少附带、`next` 其它接近最优的组合。
- `notes`：需要转告用户的提示；仅备选未完备不进入提示。`search` 保留各情况的 `engine`、最低金额 `nodes/complete`、备选 `candidate_nodes/candidates_complete` 及保证范围。
- `search[].dp.minimum/candidate`：内部阶段统计，包括 `config_nodes/transition_nodes`、配置降级次数与原因、完整/降级缓存命中及未缓存跳过数量；仅描述加权 DP，数量回退另计 `quantity_nodes/candidate_quantity_nodes`；各阶段 DP 两类节点加对应数量回退节点等于总 nodes/candidate_nodes。最低金额/备选两阶段分开统计。`complete` 只指给定菜单模型最低金额，不代表全部官方价格或备选已验证。
- `verification`：`local_minimum_complete/candidates_complete/quote_candidates_truncated/scope`。核价名额优先本地最低、不开卡最低和单点基准（名额足够才全部保留）；固定少量核价不能证明官方全局最低。

搜索方法：用加权覆盖 DP 统一处理餐品份数/块数、完整套餐配置、券库存、商品限购和开卡条件；需求共享 code 时按子集记帐（同一份餐品只顶一个需求），先每状态一个标签求最低金额，再在独立预算中保留多条购买路径生成备选。商品配置与覆盖转移缓存复用，余下最大供给用于可达性剪枝，非负购买与有界负价费用下界用于安全支配。共享 code 落在块数需求（整份不可拆）或 DP 状态预算耗尽时回退套餐数量搜索，费用流同时优化所有必选槽位、需求与附带出口，不依赖先选需求再补齐或大负数奖励；一份 `node_limit` 预算在每个变体间按实际用量共享回收，堆里已无待展开项时命中预算仍算搜完。价格仍为菜单现价/原价加选项差价，外购券成本仅加一次。备选属于有限标签候选，不声称枚举了全部口味/升级；仅备选达预算不提示、不追加预算重跑，最低金额未完成等风险仍提示。

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

`price` 填官方 `data.price`（整数分，已含随单购卡费和配送/打包/餐具费，不要另加），`productList` 照抄官方回显；返回 `deliveryPrice / packingPrice / tablewarePrice` 就照抄（整数分，包括 0；缺字段不能当成 0）。券的官方报价不含买券的钱（`coupon.paid`），`pick` 会自动把它加回实付，所以 `price` 照抄官方返回的即可。校验规则：下单商品的数量必须与回显一致；回显里多出的商品只在带 `withOrder` 的方案中视为随单购卡（计入卡名与卡费），否则该报价作废；卡费或各项费用合计超过整单实付也作废。“同样这些单点要多少”只和餐品部分比（实付减去配送/打包/餐具费），因为单点也要付这些费。实付与方案差价一律用官方价（含真实卡费），`card_value` 只用于升级的价值判断。

## picked.json（pick 输出）

- `recommendations[]`（最多 3 个）：
  - `label` 特点、`price` 官方实付、`summary` 一句话点法（如“人气经典随心配＋4 件单品”）；
  - `items` 整单全部餐品，`demanded=true` 为满足需求的、`false` 为多送的；
  - `notes` 备注（多券同单的估价说明、买券花的钱、卡费、升级多得、换了什么口味、“同样这些单点要¥X，省¥Y”）。“同样这些”指方案里全部餐品（含多送的）按单点原价的合计；用了推算价时写“约要”；仍有餐品推不出单点价时只算已知部分，写“至少要¥X（某某未计入），省¥Y以上”；完全算不出才改为和全单点基准比；
  - `card`（随单开卡名与费用，或 null）、`fees`（配送费/打包费/餐具费，分）、`prepaid_cents`（买这些券已经花掉的钱，已计入 `price`）、`upgrade`、`display`（每份套餐的具体选法）、`allocation`、`extras`、`payload`（计价参数）。
- `baseline`：全单点官方价。
- `effort`：侦查过程一句话；`effort_line`：带“🔍 **侦查过程**”前缀的同一句，单独成段放在表格之前；`conclusion`：结论一句话。
- `title`：标题行（门店与取餐方式）＋门店/地址说明。
- `cards_html`：方案卡片 HTML 片段（只用内联样式和宿主 CSS 变量，无脚本），直接作为 WorkBuddy 内联可视化组件的 `widget_code`。每张卡：特点标签、实付、一句话点法、餐品（点名的加粗，附带灰色）、备注；第一张高亮。
- `legend`：图例一句（加粗/灰色是什么意思）。**属于表格，要紧贴表格输出**（内联组件渲染在调用位置，所以先写图例、紧接着调组件），不在 `footer` 里；无方案时为 null。
- `footer`：卡片**之后**的内容——结论、用不上的券、价格说明。不含下单引导、不含图例。
- `recommendations[].blocks`：按怎么买分块——`combo`（每个套餐一块，`title` 为套餐名×份数，`items` 为里面的餐品）、`coupon`（用券）、`single`（单点合成一块）、`card`（随单开卡，`title` 为卡名与卡费）；`items[].demanded` 为 true 表示满足点名需求（加粗），false 为附带（灰色）。卡片和 Markdown 表都按 blocks 渲染。
- `table_markdown`：卡片的 Markdown 对比表版本；`message` = `title`＋`effort_line`＋`legend`＋`table_markdown`＋`footer`（即展示顺序：地址说明 → 侦查过程 → 图例 → 方案表格 → 其余说明），供不支持内联组件的客户端使用。
- `excluded`：计价失败或回显不一致的候选及原因；`unquoted`：没有提交结果的候选。
- `recommendations[].estimated`：多券接口报 `600022` 时保留本地餐品估价（含外购券成本），附加费只参考同一开卡情况的成功报价；备注标注估价和“以下单页为准”，不作为 `excluded`。已知卡费、配送/打包/餐具费加一次，费用未知或同情况费用不一致时 `fees_complete=false`，作为估价备选展示，不参与最低实付及升级比较。
- `verification`：结果只在本次已取得完整价格的候选中比较；`official_global_minimum_proven=false`，并返回估价与缺费候选数量。最低金额搜索未完成、核价缺失、候选截断提示进入 footer；仅备选未完备不进入 footer。

推荐规则：

1. **最低实付**：官方整单实付最低（同价时附带少、不开卡、餐品原价更高的优先）。
2. **多花¥x升级**：比最低实付多出附带餐品、每份购买都不可少，且 `5 × 多花金额 ≤ 多得价值`。多花金额是官方整单差价（含卡费）；多得价值 = 整单餐品单点原价合计之差（多出的加、少掉的减，换更贵口味也计入）＋ 新开卡的价值（`card_value`，默认 ¥5，不超过卡费）。可以跨开卡情况。有多个时选“多得价值减多花金额”最大的。
3. 其余名额依次给：**不开卡**（最低实付需要开卡时）、**换口味**（有类别需求时，必须餐品用了不同口味的最便宜方案）、**少附带**（附带更少的最便宜方案）。没有就不凑数。
