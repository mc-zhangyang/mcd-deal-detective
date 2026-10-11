#!/usr/bin/env python3
"""麦侦探求解器：先按菜单价在本地精确搜索点法，再用官方计价复核排序。

纯 Python 3.9+ 标准库；不联网、不读取凭证、不下单。

    plan  --input request.json --output plan.json    生成少量需要官方计价的候选
    pick  --input quotes.json  --output picked.json  按官方实付给出最多 3 个方案
"""
import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from html import escape
from collections import Counter
from decimal import Decimal, InvalidOperation

NONE_CTX = "none"
BIG = 10 ** 7
TIE = 1000
DEFAULT_MAX_QUOTES = 10
DEFAULT_KEEP = 80
DEFAULT_NODE_LIMIT = 200000
UPGRADE_DIVISOR = 5  # 多花的钱 <= 所加餐品单点原价合计的 1/5
MAX_RECOMMENDATIONS = 3
DEFAULT_CARD_VALUE = 500  # 随单开的卡算作多得价值时按 ¥5 计（不超过卡费）
EFFORT_FIELDS = ("menu_items", "details_checked", "coupons_claimed", "store_coupons")
MODES = {1: "到店自取", 5: "得来速", 2: "麦乐送", 6: "团餐"}


class InputError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise InputError(message)


# ---------------------------------------------------------------- 金额与基础

def yuan_to_cents(value, field):
    need(value is not None and not isinstance(value, bool) and isinstance(value, (int, float, str)), field + " 缺少金额")
    text = str(value).strip().replace("¥", "").replace("￥", "").replace(" ", "")
    try:
        amount = Decimal(text) * 100
    except InvalidOperation:
        raise InputError(field + " 金额无法识别：" + str(value))
    need(amount.is_finite() and amount == amount.to_integral_value(), field + " 金额精度超过分：" + str(value))
    return int(amount)


def diff_to_cents(value, field):
    """解析套餐选项差价，如 "+ ¥0"、"- ¥1"、"+ ¥1.5"。"""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return yuan_to_cents(value, field)
    need(isinstance(value, str), field + " 差价格式无法识别")
    text = value.replace(" ", "").replace("¥", "").replace("￥", "")
    match = re.fullmatch(r"([+-]?)(\d+(?:\.\d{1,2})?)", text)
    need(match is not None, field + " 差价格式无法识别：" + value)
    cents = yuan_to_cents(match.group(2), field)
    return -cents if match.group(1) == "-" else cents


def money(cents):
    return "¥%d.%02d" % (cents // 100, cents % 100) if cents >= 0 else "-" + money(-cents)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


def code_of(value, field):
    need(isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip(), field + " 缺少 code")
    return str(value).strip()


def card_condition(with_order, field):
    """菜单 withOrder -> (条件键, 计价请求用 withOrder)。"""
    need(isinstance(with_order, dict), field + " 是随单购优惠价，但缺少 withOrder")
    if with_order.get("membershipCode"):
        spec = with_order.get("membershipSpecId", with_order.get("specId"))
        need(spec not in (None, ""), field + " withOrder 缺少 specId")
        request = {"membershipCode": str(with_order["membershipCode"]), "membershipSpecId": str(spec)}
        return "会员" + request["membershipCode"] + "/" + request["membershipSpecId"], request
    if with_order.get("cardId"):
        card_type = with_order.get("cardType")
        need(type(card_type) is int, field + " withOrder.cardType 必须为整数")
        request = {"cardId": str(with_order["cardId"]), "cardType": card_type}
        return "卡" + request["cardId"] + "/" + str(card_type), request
    raise InputError(field + " withOrder 需要 membershipCode+specId 或 cardId+cardType")


def default_modification(entry, field):
    """把详情里的默认特调转换成计价/下单参数格式；没有特调返回 None。"""
    modification = entry.get("modification")
    if not modification:
        return None
    need(isinstance(modification, dict) and isinstance(modification.get("items", []), list), field + " modification 格式不对")
    values = []
    for group in modification.get("items", []):
        entries = group.get("values") or []
        has_unselected = any(v.get("unselectedKey") for v in entries)
        for v in entries:
            quantity = v.get("selectedQuantity", 0) or 0
            need(type(quantity) is int and quantity >= 0, field + " selectedQuantity 必须为非负整数")
            if quantity > 0:
                need(bool(v.get("selectedKey")), field + " 默认特调缺 selectedKey")
                values.append({"code": str(v["code"]), "key": v["selectedKey"], "quantity": quantity})
            elif has_unselected and v.get("unselectedKey"):
                values.append({"code": str(v["code"]), "key": v["unselectedKey"], "quantity": 1})
    return {"values": values} if values else None


# ---------------------------------------------------------------- 请求解析

class Request:
    def __init__(self, data):
        need(isinstance(data, dict), "request 必须是 JSON 对象")
        context = data.get("context")
        need(isinstance(context, dict), "缺少 context")
        for key in ("storeCode", "orderType", "beType"):
            need(key in context, "context 缺少 " + key)
        need("items" not in context and "withOrder" not in context, "context 不放 items/withOrder")
        self.context = dict(context)
        self.strict = data.get("strict", False)
        need(type(self.strict) is bool, "strict 必须为 true/false")
        self.forbidden = {code_of(c, "forbidden_codes") for c in data.get("forbidden_codes", [])}
        self.max_quotes = data.get("max_quotes", DEFAULT_MAX_QUOTES)
        need(type(self.max_quotes) is int and 1 <= self.max_quotes <= 30, "max_quotes 取 1~30")
        self.node_limit = data.get("node_limit", DEFAULT_NODE_LIMIT)
        need(type(self.node_limit) is int and self.node_limit >= 1, "node_limit 必须为正整数")
        self.card_value = yuan_to_cents(data.get("card_value", DEFAULT_CARD_VALUE / 100), "card_value")
        need(self.card_value >= 0, "card_value 不能为负")
        self.store_name = data.get("store_name")
        need(self.store_name is None or isinstance(self.store_name, str), "store_name 必须为字符串")
        effort = data.get("effort") or {}
        need(isinstance(effort, dict), "effort 必须是对象")
        for key, value in effort.items():
            need(key in EFFORT_FIELDS, "effort 不支持字段 " + str(key))
            need(type(value) is int and value >= 0, "effort." + key + " 必须为非负整数")
        self.effort = dict(effort)

        self.demands = []
        seen = set()
        for i, raw in enumerate(data.get("demands") or []):
            field = "demands[%d]" % i
            need(isinstance(raw, dict), field + " 必须是对象")
            demand_id = code_of(raw.get("id"), field + ".id")
            need(demand_id not in seen, "重复的 demand id：" + demand_id)
            seen.add(demand_id)
            quantity = raw.get("quantity")
            need(type(quantity) is int and quantity >= 1, field + ".quantity 必须为正整数")
            codes = [code_of(c, field + ".codes") for c in raw.get("codes") or []]
            need(codes and len(set(codes)) == len(codes), field + ".codes 需要不重复的真实餐品 code")
            need(not self.forbidden.intersection(codes), field + " 的 codes 与 forbidden_codes 冲突")
            self.demands.append({"id": demand_id, "label": str(raw.get("label") or demand_id), "quantity": quantity, "codes": codes})
        need(bool(self.demands), "demands 不能为空")
        self.total_units = sum(d["quantity"] for d in self.demands)
        need(self.total_units <= 30, "必须餐品合计超过 30 份，请拆开计算")

        self.names = {}
        self.sources = []
        self.card_requests = {}
        sids = set()
        for i, raw in enumerate(data.get("products") or []):
            source = self._source(raw, "products[%d]" % i)
            need(source["sid"] not in sids, "重复的 product：" + source["sid"])
            sids.add(source["sid"])
            self.sources.append(source)
        need(bool(self.sources), "products 不能为空")

        self.ref_prices = {}
        for source in self.sources:
            if not source["rounds"] and not source["coupon"]:
                food = source["food"]
                price = source["original"]
                if food not in self.ref_prices or price < self.ref_prices[food]:
                    self.ref_prices[food] = price
        for source in self.sources:  # 只有一轮且选 1 份的商品（如选杯型/口味），选项原价=商品原价+差价
            if not source["coupon"] and len(source["rounds"]) == 1 and source["rounds"][0]["units"] == 1:
                for choice in source["rounds"][0]["choices"]:
                    self.ref_prices.setdefault(choice["code"], source["original"] + choice["diff"])
        for code, value in (data.get("reference_prices") or {}).items():
            self.ref_prices[str(code)] = yuan_to_cents(value, "reference_prices." + str(code))
        self.estimated_prices = self._infer_missing_prices()
        self.contexts = [NONE_CTX] + sorted(self.card_requests)

    def _infer_missing_prices(self):
        """兜底：套餐里出现但没有单点价的餐品，用同一轮里有真实单点价的选项推算：
        价(X) ≈ 价(Y) + 差价(X) − 差价(Y)，多处可推时取最低（保守，不高估多得的价值）。
        只从真实单点价推一步，不用推算值再推（同一餐品在不同套餐里差价不同，连环推会越推越离谱）。
        推算值单独记录，展示时标“约”；推不出的餐品保持未知。"""
        given = dict(self.ref_prices)  # 菜单单品与明确给出的单点价，不被推算覆盖
        estimated = {}
        for source in self.sources:
            for rnd in source["rounds"]:
                known = [ch for ch in rnd["choices"] if ch["code"] in given]
                for ch in rnd["choices"]:
                    if ch["code"] in given or not known:
                        continue
                    guess = min(given[k["code"]] + ch["diff"] - k["diff"] for k in known)
                    if guess > 0 and guess < estimated.get(ch["code"], guess + 1):
                        estimated[ch["code"]] = guess
        self.ref_prices.update(estimated)
        return estimated

    def _source(self, raw, field):
        need(isinstance(raw, dict), field + " 必须是对象")
        code = code_of(raw.get("code"), field + ".code")
        name = str(raw.get("name") or code)
        coupon = raw.get("coupon")
        limit = raw.get("maxQuantity")
        need(limit is None or (type(limit) is int and limit >= 0), field + ".maxQuantity 必须为非负整数")
        card_key = None
        if coupon is not None:
            need(isinstance(coupon, dict) and coupon.get("couponId"), field + ".coupon 缺少 couponId")
            codes = [str(c) for c in coupon.get("couponCodes") or []]
            need(codes and len(set(codes)) == len(codes), field + ".coupon.couponCodes 需要不重复的券码")
            coupon = {"couponId": str(coupon["couponId"]), "couponCodes": codes, "title": str(coupon.get("title") or name)}
            current = original = yuan_to_cents(raw.get("price"), field + ".price（券后官方单独计价）")
            limit = len(codes) if limit is None else min(limit, len(codes))
            sid = code + "#券" + coupon["couponId"]
        else:
            current = yuan_to_cents(raw.get("currentPrice"), field + ".currentPrice")
            original = yuan_to_cents(raw.get("originalPrice", raw.get("currentPrice")), field + ".originalPrice")
            if str(raw.get("discountType") or "").startswith("随单购"):
                card_key, request = card_condition(raw.get("withOrder"), field)
                need(self.card_requests.get(card_key, request) == request, field + " 随单购条件冲突")
                self.card_requests[card_key] = request
            sid = code
        self.names.setdefault(code, name)
        rounds = []
        for j, raw_round in enumerate(raw.get("rounds") or []):
            parsed = self._round(raw_round, "%s.rounds[%d]" % (field, j))
            if parsed is not None:
                rounds.append(parsed)
        food = code_of(raw.get("food_code", code), field + ".food_code") if not rounds else None
        if food:
            self.names.setdefault(food, name)
        return {"sid": sid, "code": code, "name": name, "food": food, "rounds": rounds, "coupon": coupon,
                "card_key": card_key, "current": current, "original": original, "limit": limit,
                "modification": default_modification(raw, field) if not rounds else None}

    def _round(self, raw, field):
        need(isinstance(raw, dict), field + " 必须是对象")
        round_id = raw.get("id", raw.get("round"))
        need(round_id is not None, field + " 缺少 id")
        units = raw.get("minQuantity", raw.get("min"))
        need(type(units) is int and units >= 0, field + ".minQuantity 必须为非负整数")
        if units == 0:
            return None  # 可选加购轮次不参与搜索，也不传
        choices = []
        seen = set()
        for k, choice in enumerate(raw.get("choices") or []):
            cf = "%s.choices[%d]" % (field, k)
            code = code_of(choice.get("code"), cf + ".code")
            need(code not in seen, field + " 内重复选项 " + code)
            seen.add(code)
            cap = choice.get("maxQuantity", -1)
            need(type(cap) is int and cap >= -1, cf + ".maxQuantity 必须为 -1 或非负整数")
            cap = units if cap == -1 else min(cap, units)
            name = str(choice.get("name") or code)
            self.names[code] = name  # 详情里的选项名带规格（如“麦乐鸡5块”“中薯条”），优先使用
            choices.append({"code": code, "name": name, "diff": diff_to_cents(choice.get("diffPrice"), cf + ".diffPrice"),
                            "cap": cap, "default": choice.get("isDefault") == 1, "index": k,
                            "modification": default_modification(choice, cf)})
        need(bool(choices), field + " 没有选项")
        return {"id": str(round_id), "units": units, "choices": choices}

    def price(self, source, ctx):
        if source["card_key"] is None:
            return source["current"]
        return source["current"] if source["card_key"] == ctx else source["original"]

    def name(self, code):
        return self.names.get(code, code)


# ---------------------------------------------------------------- 最小费用流

class MinCostFlow:
    def __init__(self):
        self.graph = []

    def node(self):
        self.graph.append([])
        return len(self.graph) - 1

    def edge(self, u, v, cap, cost):
        self.graph[u].append([v, cap, cost, len(self.graph[v])])
        self.graph[v].append([u, 0, -cost, len(self.graph[u]) - 1])
        return u, len(self.graph[u]) - 1

    def used(self, ref):
        u, i = ref
        v, _, _, back = self.graph[u][i]
        return self.graph[v][back][1]

    def run(self, source, sink, limit):
        flow = cost = 0
        size = len(self.graph)
        while flow < limit:
            dist = [None] * size
            dist[source] = 0
            previous = [None] * size
            queue = [source]
            queued = [False] * size
            queued[source] = True
            head = 0
            while head < len(queue):  # SPFA，允许严格模式下的负费用边
                u = queue[head]
                head += 1
                queued[u] = False
                for i, (v, cap, c, _) in enumerate(self.graph[u]):
                    if cap > 0 and (dist[v] is None or dist[u] + c < dist[v]):
                        dist[v] = dist[u] + c
                        previous[v] = (u, i)
                        if not queued[v]:
                            queued[v] = True
                            queue.append(v)
            if dist[sink] is None:
                break
            push = limit - flow
            v = sink
            while v != source:
                u, i = previous[v]
                push = min(push, self.graph[u][i][1])
                v = u
            v = sink
            while v != source:
                u, i = previous[v]
                self.graph[u][i][1] -= push
                back = self.graph[u][i][3]
                self.graph[v][back][1] += push
                v = u
            flow += push
            cost += push * dist[sink]
        return flow, cost


def covers(demands, supply):
    """供给能否同时满足全部必须餐品（每份餐品只能满足一个需求）。"""
    flow = MinCostFlow()
    source, sink = flow.node(), flow.node()
    nodes = {}
    total = 0
    for d in demands:
        node = flow.node()
        flow.edge(source, node, d["quantity"], 0)
        total += d["quantity"]
        for code in d["codes"]:
            if supply.get(code, 0) > 0:
                if code not in nodes:
                    nodes[code] = flow.node()
                    flow.edge(nodes[code], sink, supply[code], 0)
                flow.edge(node, nodes[code], d["quantity"], 0)
    return flow.run(source, sink, total)[0] == total


# ---------------------------------------------------------------- 本地精确搜索

class Solver:
    def __init__(self, request):
        self.r = request

    def _demands(self, bans):
        result = []
        for d in self.r.demands:
            codes = [c for c in d["codes"] if (d["id"], c) not in bans]
            if not codes:
                return None
            result.append({**d, "codes": codes})
        return result

    def _combos(self, ctx, demands, notes):
        demand_codes = {c for d in demands for c in d["codes"]}
        combos = []
        for source in self.r.sources:
            if not source["rounds"]:
                continue
            info = []
            usable = True
            for rnd in source["rounds"]:
                allowed = [ch for ch in rnd["choices"] if ch["code"] not in self.r.forbidden]
                if self.r.strict:
                    allowed = [ch for ch in allowed if ch["code"] in demand_codes]
                if sum(ch["cap"] for ch in allowed) < rnd["units"]:
                    usable = False
                    break
                info.append({"round": rnd, "allowed": allowed, "min_diff": min(ch["diff"] for ch in allowed)})
            serves = {ch["code"] for item in info for ch in item["allowed"]} & demand_codes if usable else set()
            if not usable or not serves:
                if usable is False and notes is not None:
                    notes.add(source["name"] + "：必选内容含禁止或无法满足严格要求，未使用")
                continue
            coverable = sum(d["quantity"] for d in demands if serves.intersection(d["codes"]))
            bound = min(coverable, self.r.total_units, BIG if source["limit"] is None else source["limit"])
            if bound <= 0:
                continue
            fixed = self.r.price(source, ctx) + sum(item["round"]["units"] * item["min_diff"] for item in info)
            combos.append({"source": source, "info": info, "bound": bound, "fixed": fixed})
        combos.sort(key=lambda c: (c["fixed"], c["source"]["sid"]))
        return combos

    def _singles(self, ctx, demands):
        demand_codes = {c for d in demands for c in d["codes"]}
        result = {}
        for source in self.r.sources:
            if not source["rounds"] and source["food"] in demand_codes:
                if source["limit"] == 0:
                    continue
                result.setdefault(source["food"], []).append(source)
        return result

    def solve(self, ctx, bans=frozenset(), keep=DEFAULT_KEEP, notes=None):
        """返回 (按估价升序的方案列表, 搜索统计)。"""
        demands = self._demands(bans)
        stats = {"nodes": 0, "complete": True, "combos": 0}
        if demands is None:
            return [], stats
        combos = self._combos(ctx, demands, notes)
        singles = self._singles(ctx, demands)
        stats["combos"] = len(combos)
        best = []  # (cost, extra_count, key, plan)
        keys = set()
        stack = [((), 0, 0)]
        while stack:
            multiset, start, fixed = stack.pop()
            threshold = best[-1][0] if len(best) >= keep else None
            if threshold is not None and fixed > threshold:
                continue
            stats["nodes"] += 1
            if stats["nodes"] > self.r.node_limit:
                stats["complete"] = False
                break
            plan = self.evaluate(ctx, demands, combos, singles, Counter(multiset))
            if plan is not None and (ctx == NONE_CTX or plan["uses_card"]) and plan["key"] not in keys:
                keys.add(plan["key"])
                bisect.insort(best, (plan["cost"], plan["extra_count"], plan["key"], plan))
                if len(best) > keep:
                    keys.discard(best.pop()[2])
                threshold = best[-1][0] if len(best) >= keep else None
            if len(multiset) >= self.r.total_units:
                continue
            counts = Counter(multiset)
            for position in range(start, len(combos)):
                combo = combos[position]
                if threshold is not None and fixed + combo["fixed"] > threshold:
                    break  # combos 按固定成本升序，后面的更贵
                if counts[position] < combo["bound"]:
                    stack.append((multiset + (position,), position, fixed + combo["fixed"]))
        return [entry[3] for entry in best], stats

    def baseline(self):
        """全单点：不用任何套餐与券。"""
        demands = self._demands(frozenset())
        singles = {food: [s for s in items if not s["coupon"]] for food, items in self._singles(NONE_CTX, demands).items()}
        return self.evaluate(NONE_CTX, demands, [], singles, Counter())

    def evaluate(self, ctx, demands, combos, singles, counts):
        r = self.r
        n_units = r.total_units
        flow = MinCostFlow()
        source, sink = flow.node(), flow.node()
        code_nodes = {}
        demand_edges = []
        for d in demands:
            node = flow.node()
            flow.edge(source, node, d["quantity"], 0)
            for code in d["codes"]:
                if code not in code_nodes:
                    code_nodes[code] = flow.node()
                demand_edges.append((d["id"], code, flow.edge(node, code_nodes[code], d["quantity"], 0)))
        single_edges = []
        for food, items in singles.items():
            if food in code_nodes:
                for item in items:
                    cap = n_units if item["limit"] is None else min(item["limit"], n_units)
                    single_edges.append((item, flow.edge(code_nodes[food], sink, cap, r.price(item, ctx) * TIE)))
        round_edges = []  # (combo index, round index, choice, ref)
        round_refs = []
        for index, count in counts.items():
            combo = combos[index]
            for ri, item in enumerate(combo["info"]):
                node = flow.node()
                capacity = count * item["round"]["units"]
                round_refs.append((capacity, flow.edge(node, sink, capacity, 0)))
                for choice in item["allowed"]:
                    if choice["code"] in code_nodes:
                        # 费用按“分×TIE”计，同价时优先套餐默认选项（+1 只用于打破平局）
                        cost = (choice["diff"] - item["min_diff"]) * TIE + (0 if choice["default"] else 1) - (BIG * TIE if r.strict else 0)
                        ref = flow.edge(code_nodes[choice["code"]], node, count * choice["cap"], cost)
                        round_edges.append((index, ri, choice, ref))
        total, _ = flow.run(source, sink, n_units)
        if total < n_units:
            return None
        if r.strict and any(flow.used(ref) < capacity for capacity, ref in round_refs):
            return None

        allocation = {}
        for demand_id, code, ref in demand_edges:
            used = flow.used(ref)
            if used:
                allocation.setdefault(demand_id, {})[code] = used
        single_counts = Counter()
        for item, ref in single_edges:
            used = flow.used(ref)
            if used:
                single_counts[item["sid"]] += used
        assigned = {}
        served = Counter()
        for index, ri, choice, ref in round_edges:
            used = flow.used(ref)
            if used:
                assigned.setdefault((index, ri), Counter())[choice["code"]] += used
                served[index] += used
        if any(served[index] < count for index, count in counts.items()):
            return None  # 有一份套餐没承担任何必须餐品：去掉它只会更便宜

        cost = 0
        supply = Counter()
        lines = []
        uses_card = False
        by_sid = {s["sid"]: s for s in r.sources}
        for sid, quantity in sorted(single_counts.items()):
            item = by_sid[sid]
            cost += quantity * r.price(item, ctx)
            supply[item["food"]] += quantity
            uses_card = uses_card or (item["card_key"] == ctx and ctx != NONE_CTX)
            lines.append({"source": item, "quantity": quantity, "rounds": None})
        for index, count in sorted(counts.items()):
            combo = combos[index]
            item = combo["source"]
            cost += count * r.price(item, ctx)
            uses_card = uses_card or (item["card_key"] == ctx and ctx != NONE_CTX)
            per_round = []
            for ri, info in enumerate(combo["info"]):
                units = count * info["round"]["units"]
                taken = Counter(assigned.get((index, ri), {}))
                remaining = units - sum(taken.values())
                for choice in sorted(info["allowed"], key=lambda ch: (ch["diff"], not ch["default"], ch["index"])):
                    if remaining <= 0:
                        break
                    room = count * choice["cap"] - taken[choice["code"]]
                    add = min(room, remaining)
                    if add > 0:
                        taken[choice["code"]] += add
                        remaining -= add
                if remaining > 0:
                    return None
                choice_by_code = {ch["code"]: ch for ch in info["allowed"]}
                sequence = []
                for choice in sorted(info["allowed"], key=lambda ch: ch["index"]):
                    sequence.extend([choice] * taken[choice["code"]])
                    cost += taken[choice["code"]] * choice["diff"]
                    supply[choice["code"]] += taken[choice["code"]]
                instances = [Counter() for _ in range(count)]
                for position, choice in enumerate(sequence):  # 轮转分配，保证每套份数与选项上限
                    instances[position % count][choice["code"]] += 1
                per_round.append([(info["round"]["id"], dict(inst), choice_by_code) for inst in instances])
            configs = Counter()
            for k in range(count):
                configs[tuple((rid, tuple(sorted(inst.items()))) for rid, inst, _ in (per_round[ri][k] for ri in range(len(per_round))))] += 1
            lookup = {per_round[ri][0][0]: per_round[ri][0][2] for ri in range(len(per_round))}
            for config, quantity in sorted(configs.items()):
                lines.append({"source": item, "quantity": quantity, "rounds": [(rid, list(items), lookup[rid]) for rid, items in config]})
        used = Counter()
        for codes in allocation.values():
            used.update(codes)
        extras = supply - used
        payload = self.payload(ctx, lines)
        core = sorted((d, c, n) for d, codes in allocation.items() for c, n in codes.items())
        return {"ctx": ctx, "cost": cost, "payload": payload, "key": digest(payload), "allocation": allocation,
                "supply": {c: n for c, n in supply.items() if n}, "extras": dict(extras), "extra_count": sum(extras.values()),
                "uses_card": uses_card, "core": core, "display": self.display(lines), "lines": self.brief(lines),
                "_demands": demands, "_lines": lines}

    def is_minimal(self, plan):
        if "_minimal" not in plan:
            plan["_minimal"] = self.minimal(plan["_demands"], Counter(plan["supply"]), plan["_lines"])
        return plan["_minimal"]

    def minimal(self, demands, supply, lines):
        """每一份购买都不可少：去掉任意一份（一个单品或一套套餐）后就凑不齐必须餐品。"""
        for line in lines:
            if line["rounds"] is None:
                unit = Counter({line["source"]["food"]: 1})
            else:
                unit = Counter()
                for _, chosen, _ in line["rounds"]:
                    for code, quantity in chosen:
                        unit[code] += quantity
            if covers(demands, supply - unit):
                return False
        return True

    def brief(self, lines):
        result = []
        for line in lines:
            source = line["source"]
            if source["coupon"]:
                kind, name = "coupon", source["coupon"]["title"]
            elif line["rounds"] is not None:
                kind, name = "combo", source["name"]
            else:
                kind, name = "single", self.r.name(source["food"])
            result.append({"kind": kind, "name": name, "quantity": line["quantity"]})
        return result

    def payload(self, ctx, lines):
        items = []
        for line in lines:
            source = line["source"]
            base = {"productCode": source["code"]}
            if line["rounds"] is not None:
                round_list = []
                for rid, chosen, lookup in line["rounds"]:
                    combo_items = []
                    for code, quantity in chosen:
                        entry = {"code": code, "quantity": quantity}
                        if lookup[code]["modification"]:
                            entry["modification"] = lookup[code]["modification"]
                        combo_items.append(entry)
                    round_list.append({"round": rid, "comboItemList": combo_items})
                base["roundList"] = round_list
            elif source["modification"]:
                base["modification"] = source["modification"]
            if source["coupon"]:
                used = sum(1 for it in items if it.get("couponId") == source["coupon"]["couponId"])
                for k in range(line["quantity"]):
                    items.append({**base, "quantity": 1, "couponId": source["coupon"]["couponId"],
                                  "couponCode": source["coupon"]["couponCodes"][used + k]})
            else:
                items.append({**base, "quantity": line["quantity"]})
        items.sort(key=canonical)
        payload = dict(self.r.context)
        payload["items"] = items
        if ctx != NONE_CTX:
            payload["withOrder"] = dict(self.r.card_requests[ctx])
        return payload

    def display(self, lines):
        result = []
        for line in lines:
            source = line["source"]
            title = source["name"] if line["rounds"] is not None or source["coupon"] else self.r.name(source["food"])
            label = title + "×" + str(line["quantity"])
            if source["coupon"]:
                label += "（用券：" + source["coupon"]["title"] + "）"
            if line["rounds"]:
                parts = []
                for _, chosen, lookup in line["rounds"]:
                    for code, quantity in chosen:
                        parts.append(lookup[code]["name"] + ("×" + str(quantity) if quantity > 1 else ""))
                label += "（每份：" + "、".join(parts) + "）"
            result.append(label)
        return result


# ---------------------------------------------------------------- plan

def contains(bigger, smaller):
    return all(bigger.get(code, 0) >= quantity for code, quantity in smaller.items())


def added_items(upper, base):
    return {code: upper.get(code, 0) - base.get(code, 0) for code in upper if upper.get(code, 0) > base.get(code, 0)}


def value_gain(refs, upper, base):
    """整单餐品单点原价合计的增加额：多出的餐品加上、少掉的餐品减去。
    多出但不知道原价的餐品按 0 计（保守）；少掉的餐品不知道原价则无法比较，返回 None。"""
    gain = 0
    for code in set(upper) | set(base):
        diff = upper.get(code, 0) - base.get(code, 0)
        if diff == 0:
            continue
        if code not in refs:
            if diff > 0:
                continue
            return None
        gain += refs[code] * diff
    return gain


def extra_added(upper, base):
    """附带（必须餐品之外）比基准多出的部分。"""
    return dict(Counter(upper["extras"]) - Counter(base["extras"]))


def upgrade_ok(delta, gain, allow_negative=False):
    return gain is not None and gain > 0 and (allow_negative or delta >= 0) and UPGRADE_DIVISOR * delta <= gain


def plan(data):
    r = Request(data)
    solver = Solver(r)
    notes = set()
    results = {}
    search = {}
    for ctx in r.contexts:
        plans, stats = solver.solve(ctx, notes=notes)
        results[ctx] = plans
        search[ctx] = stats
    chosen = []  # (role, plan)

    def take(role, item):
        if item is None:
            return
        for existing in chosen:
            if existing[1]["key"] == item["key"]:
                if role not in existing[0]:
                    existing[0].append(role)
                return
        chosen.append(([role], item))

    take("baseline", solver.baseline())
    for ctx in r.contexts:
        take("best", results[ctx][0] if results[ctx] else None)
    category = [d for d in r.demands if len(d["codes"]) > 1]
    # 加钱升级候选：在同一开卡情况内和该情况最低价比（卡费相互抵消）；跨开卡情况的比较由 pick 用官方实付完成，
    # 各情况的最低价本身已作为 best 送去计价。只考虑“每份购买都不可少”的组合，避免凑一堆东西的方案。
    for ctx in r.contexts:
        if not results[ctx]:
            continue
        anchor = results[ctx][0]
        ups = []
        for item in results[ctx][1:]:
            if not extra_added(item, anchor) or not solver.is_minimal(item):
                continue
            gain = value_gain(r.ref_prices, item["supply"], anchor["supply"])
            delta = item["cost"] - anchor["cost"]
            if upgrade_ok(delta, gain):
                ups.append((-(gain - delta), delta, item["key"], item))
        for entry in sorted(ups)[:2]:
            take("upgrade", entry[3])
    for ctx in r.contexts:
        plans = results[ctx]
        if not plans:
            continue
        base = plans[0]
        if category:
            alternatives = []
            for demand_id, code, _ in sorted(base["core"], key=lambda x: -x[2]):
                if demand_id in {d["id"] for d in category}:
                    alt, _ = solver.solve(ctx, bans=frozenset({(demand_id, code)}), keep=1)
                    if alt and alt[0]["core"] != base["core"]:
                        alternatives.append(alt[0])
                if len(alternatives) >= 3:
                    break
            for alt in sorted(alternatives, key=lambda p: (p["cost"], p["key"]))[:2]:
                take("flavor", alt)
        leaner = [p for p in plans if p["extra_count"] < base["extra_count"]]
        take("lean", leaner[0] if leaner else None)
    def padded(item):  # 只是在更便宜方案上“多买一堆”的组合（不满足升级条件）不占计价名额
        return any(other["cost"] <= item["cost"] and other["key"] != item["key"] and contains(item["supply"], other["supply"])
                   for other in results[item["ctx"]])

    position = 1
    while len(chosen) < r.max_quotes and any(len(results[ctx]) > position for ctx in r.contexts):
        for ctx in r.contexts:
            if len(results[ctx]) > position and len(chosen) < r.max_quotes and not padded(results[ctx][position]):
                take("next", results[ctx][position])
        position += 1

    priority = {"baseline": 0, "best": 1, "upgrade": 2, "flavor": 3, "lean": 4, "next": 5}
    chosen.sort(key=lambda entry: (min(priority[x] for x in entry[0]), entry[1]["cost"]))
    chosen = chosen[:r.max_quotes]
    quotes = []
    for i, (roles, item) in enumerate(chosen, 1):
        quotes.append({"id": "q%d" % i, "roles": roles, "context": item["ctx"], "estimate_cents": item["cost"],
                       "estimate": money(item["cost"]), "display": item["display"], "lines": item["lines"], "payload": item["payload"],
                       "allocation": item["allocation"], "extras": item["extras"], "extra_count": item["extra_count"],
                       "supply": item["supply"], "core": item["core"], "minimal": solver.is_minimal(item)})
    if not quotes:
        notes.add("按现有菜单数据凑不齐必须餐品：检查 demands.codes 是否都在 products 里有单点或套餐来源")
    if not all(stats["complete"] for stats in search.values()):
        notes.add("组合太多，搜索在节点上限处停止：结果为已搜到的最优，可提高 node_limit")
    effort = dict(r.effort)
    effort.setdefault("details_checked", sum(1 for s in r.sources if s["rounds"]))
    effort["combos_usable"] = max(stats["combos"] for stats in search.values())
    effort["combinations"] = sum(stats["nodes"] for stats in search.values())
    effort["card_contexts"] = len(r.contexts) - 1
    store = {"name": r.store_name, "mode": MODES.get(r.context.get("beType"), "")}
    return {"schema": "mcd-deal-detective/plan@1", "store": store, "effort": effort, "demands": r.demands, "strict": r.strict,
            "forbidden_codes": sorted(r.forbidden), "contexts": {ctx: r.card_requests.get(ctx) for ctx in r.contexts},
            "names": r.names, "ref_prices": r.ref_prices, "estimated_prices": r.estimated_prices,
            "card_value_cents": r.card_value, "quotes": quotes, "search": search, "notes": sorted(notes),
            "next_step": "逐个用 quotes[].payload 调用官方 calculate-price，把 data.price 与 data.productList 记入 quotes.json 后运行 pick"}


# ---------------------------------------------------------------- pick

def aggregate(entries, field):
    result = Counter()
    for entry in entries:
        need(isinstance(entry, dict), field + " 每项必须是对象")
        quantity = entry.get("quantity")
        need(type(quantity) is int and quantity >= 1, field + " quantity 必须为正整数")
        result[code_of(entry.get("productCode"), field + ".productCode")] += quantity
    return result


def check_quote(quote, result):
    """校验官方回显；返回 (官方实付分, 随单购卡信息或 None)。"""
    price = result.get("price")
    need(type(price) is int and price >= 0, "price 必须是官方 data.price 的整数分")
    products = result.get("productList")
    need(isinstance(products, list) and products, "缺少官方 data.productList")
    ordered = aggregate(quote["payload"]["items"], "payload.items")
    echoed = aggregate(products, "productList")
    for code, quantity in ordered.items():
        need(echoed.get(code) == quantity, "回显数量不一致：%s 下单 %d 份，回显 %d 份" % (code, quantity, echoed.get(code, 0)))
    extra = [p for p in products if str(p.get("productCode")) not in ordered]
    if not extra:
        return price, None
    need("withOrder" in quote["payload"], "回显出现未下单的商品：" + "、".join(str(p.get("productName") or p.get("productCode")) for p in extra))
    fee = 0
    for p in extra:
        subtotal = p.get("subtotal")
        need(type(subtotal) is int and subtotal >= 0, "随单购卡回显缺少整数 subtotal")
        fee += subtotal
    # 实付一律取官方 data.price（已含卡费，不再另加）；卡费不可能超过整单实付
    need(fee <= price, "随单购卡费 %s 超过整单实付 %s，回显异常" % (money(fee), money(price)))
    name = "、".join(str(p.get("productName") or p.get("productCode")) for p in extra)
    return price, {"name": name, "fee_cents": fee, "fee": money(fee), "codes": [str(p.get("productCode")) for p in extra]}


def times(name, quantity, always=False):
    return name + ("×%d" % quantity if always or quantity > 1 else "")


def order_summary(entry):
    """一句话点法，如“全明星双人分享餐八件套＋随单开麦金卡体验卡（15天）”、“4 件单品直接点”。"""
    combos, coupons, singles = Counter(), Counter(), 0
    for line in entry["lines"]:
        if line["kind"] == "combo":
            combos[line["name"]] += line["quantity"]
        elif line["kind"] == "coupon":
            coupons[line["name"]] += line["quantity"]
        else:
            singles += line["quantity"]
    parts = [times(name, n) for name, n in combos.items()]
    parts += ["用券「%s」%s" % (name, "×%d" % n if n > 1 else "") for name, n in coupons.items()]
    if singles:
        parts.append(("%d 件单品" if parts else "%d 件单品直接点") % singles)
    if entry.get("card"):
        parts.append("随单开" + entry["card"]["name"])
    return "＋".join(parts)


def food_rows(entry, plan_data):
    """整单全部餐品：先列满足需求的（demanded=True），再列多送的。"""
    names = plan_data["names"]
    wanted = Counter()
    for d in plan_data["demands"]:
        for code in d["codes"]:
            wanted[code] += entry["allocation"].get(d["id"], {}).get(code, 0)
    rows = [{"code": c, "name": names.get(c, c), "quantity": n, "demanded": True} for c, n in wanted.items() if n]
    for code in entry["supply"]:
        if entry["extras"].get(code):
            rows.append({"code": code, "name": names.get(code, code), "quantity": entry["extras"][code], "demanded": False})
    return rows


def card_notes(entry, plan_data, baseline, first):
    notes = []
    if entry.get("card"):
        notes.append("含卡费" + entry["card"]["fee"])
    if entry.get("upgrade"):
        up = entry["upgrade"]
        notes.append("比方案1多花%s，多得%s，%s" % (up["delta"], up["added_text"], up["value_note"]))
    if entry["label"] == "换口味":
        for d in plan_data["demands"]:
            mine = entry["allocation"].get(d["id"], {})
            if len(d["codes"]) > 1 and mine != first["allocation"].get(d["id"], {}):
                notes.append("%s选%s" % (d["label"], "、".join(times(plan_data["names"].get(c, c), n, True) for c, n in sorted(mine.items()))))
    if baseline is not None and entry["id"] == baseline["id"]:
        notes.append("就是全单点")
        return notes
    # 和“同样这些东西全按单点买”比：同口味、同数量（含多送的），才是这个方案真正省了多少
    same, estimated, unknown = single_equivalent(entry, plan_data["ref_prices"], plan_data.get("estimated_prices") or {})
    if same > entry["price"]:
        if unknown:  # 还有餐品查不到单点价：只算已知部分，是下限
            names = "、".join(plan_data["names"].get(c, c) for c in unknown)
            notes.append("同样这些单点至少要%s（%s未计入），省%s以上" % (money(same), names, money(same - entry["price"])))
        else:
            prefix = "约" if estimated else ""
            notes.append("同样这些单点%s要%s，省%s" % (prefix, money(same), money(same - entry["price"])))
    elif baseline is not None and baseline["price"] > entry["price"]:
        notes.append("比只点你要的全单点省" + money(baseline["price"] - entry["price"]))
    return notes


def card_worth(entry, card_value):
    card = entry.get("card")
    return min(card["fee_cents"], card_value) if card else 0


def single_equivalent(entry, refs, estimated_prices):
    """方案里全部餐品按单点原价买要多少钱（不含卡）。
    返回 (已知部分合计, 是否用了推算价, 查不到单点价的餐品 code 列表)。"""
    supply = {code: n for code, n in entry["supply"].items() if n}
    total = sum(refs[code] * n for code, n in supply.items() if code in refs)
    estimated = any(code in estimated_prices for code in supply)
    unknown = sorted(code for code in supply if code not in refs)
    return total, estimated, unknown


def effort_text(plan_data, submitted, valid, card_names):
    effort = plan_data["effort"]
    parts = []
    if effort.get("menu_items"):
        parts.append("翻了本店 %d 款菜单" % effort["menu_items"])
    if effort.get("details_checked"):
        parts.append("拆开 %d 个套餐/随心配/双人餐看全部可选项" % effort["details_checked"])
    if "coupons_claimed" in effort or "store_coupons" in effort:
        claimed = effort.get("coupons_claimed", 0)
        usable = effort.get("store_coupons", 0)
        parts.append(("领了 %d 张免费券" % claimed if claimed else "没有可领的新券") + "，"
                     + ("本单可用券 %d 张都试过了" % usable if usable else "本店本单暂无可用券"))
    if effort.get("card_contexts"):
        parts.append("分别按「不开卡」和「随单开%s」算" % "、".join(card_names))
    if effort.get("combos_usable"):
        parts.append("本地精确比较了 %d 种套餐组合（每种都配出最省选法）" % effort["combinations"])
    else:
        parts.append("店里没有能装下这些餐品的套餐，只能单点")
    parts.append("挑出 %d 个候选向麦当劳官方核价（%d 个有效）" % (submitted, valid))
    return "；".join(parts)


def title_line(plan_data):
    store = plan_data.get("store") or {}
    return "**" + " · ".join(x for x in ("🕵️ 麦侦探", store.get("name"), store.get("mode")) if x) + "**"


def render_table(cards):
    """不支持卡片组件的客户端用的 Markdown 对比表。"""
    if not cards:
        return ""
    lines = ["| | " + " | ".join("**方案%d · %s**" % (c["no"], c["label"]) for c in cards) + " |",
             "|:--|" + ":--|" * len(cards),
             "| 实付 | " + " | ".join("**%s**" % c["price"] for c in cards) + " |",
             "| 点法 | " + " | ".join(c["summary"] for c in cards) + " |"]
    for i in range(max(len(c["items"]) for c in cards)):
        cells = []
        for c in cards:
            if i < len(c["items"]):
                item = c["items"][i]
                text = times(item["name"], item["quantity"], True)
                cells.append("**%s**" % text if item["demanded"] else text)
            else:
                cells.append("")
        lines.append("| %s | %s |" % ("餐品" if i == 0 else "", " | ".join(cells)))
    lines.append("| 备注 | " + " | ".join("；".join(c["notes"]) or "—" for c in cards) + " |")
    return "\n".join(lines)


def render_cards(cards, plan_data):
    """方案卡片 HTML 片段（WorkBuddy 内联组件），每张卡：特点标签、实付、点法、餐品（点名的加粗）、备注。"""
    if not cards:
        return ""
    store = (plan_data.get("store") or {}).get("name") or "麦当劳"
    summary = "%s %d 个点餐方案对比，最低实付 %s" % (store, len(cards), cards[0]["price"])
    parts = ['<div>', '<h2 class="sr-only">%s</h2>' % escape(summary),
             '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;">']
    for i, c in enumerate(cards):
        if i == 0:
            box = "border:2px solid var(--color-border-info);"
            badge = "background:var(--color-background-info);color:var(--color-text-info);"
        else:
            box = "border:0.5px solid var(--color-border-tertiary);"
            badge = "background:var(--color-background-secondary);color:var(--color-text-secondary);"
        rows = []
        for item in c["items"]:
            text = escape(times(item["name"], item["quantity"], True))
            if item["demanded"]:
                rows.append('<div style="font-weight:500;color:var(--color-text-primary);">%s</div>' % text)
            else:
                rows.append('<div style="color:var(--color-text-secondary);">%s</div>' % text)
        parts.append(
            '<div style="background:var(--color-background-primary);%sborder-radius:var(--border-radius-lg);padding:1rem 1.25rem;">' % box
            + '<div style="display:inline-block;%sfont-size:12px;padding:2px 8px;border-radius:var(--border-radius-md);margin-bottom:8px;">方案%d · %s</div>'
            % (badge, c["no"], escape(c["label"]))
            + '<div style="font-size:24px;font-weight:500;">%s</div>' % escape(c["price"])
            + '<div style="font-size:13px;color:var(--color-text-secondary);margin-top:4px;">%s</div>' % escape(c["summary"])
            + '<div style="font-size:13px;line-height:1.6;margin-top:12px;">%s</div>' % "".join(rows)
            + ('<div style="font-size:12px;line-height:1.5;color:var(--color-text-tertiary);margin-top:12px;padding-top:8px;'
               'border-top:0.5px solid var(--color-border-tertiary);">%s</div>' % "<br>".join(escape(n) for n in c["notes"]) if c["notes"] else "")
            + '</div>')
    parts.append('</div></div>')
    return "\n".join(parts)


def render_footer(cards, effort, conclusion):
    lines = []
    if cards:
        lines.append("加粗是你点名要的，不加粗是套餐附带多送的。")
        lines.append("")
    lines.append("🔍 **侦查过程**：" + effort + "。")
    if conclusion:
        lines.append("")
        lines.append("💡 **结论**：" + conclusion)
    lines.append("")
    if cards:
        lines.append("价格是麦当劳官方刚刚的试算，下单前会再核一次。回复方案编号即可，顺便告诉我堂食还是外带。")
    else:
        lines.append("这次没有拿到有效的官方报价，需要重新试算。")
    return "\n".join(lines)


def pick(data):
    need(isinstance(data, dict), "quotes 输入必须是 JSON 对象")
    path = data.get("plan_file")
    need(isinstance(path, str) and os.path.isabs(path), "plan_file 必须是 plan.json 的绝对路径")
    with open(path, encoding="utf-8") as handle:
        plan_data = json.load(handle)
    need(plan_data.get("schema") == "mcd-deal-detective/plan@1", "plan_file 不是 plan 命令的输出")
    quotes = {q["id"]: q for q in plan_data["quotes"]}
    results = {}
    for i, result in enumerate(data.get("results") or []):
        need(isinstance(result, dict) and result.get("id") in quotes, "results[%d].id 不在 plan 里" % i)
        need(result["id"] not in results, "重复的结果：" + result["id"])
        results[result["id"]] = result
    valid, excluded, unquoted = [], [], []
    for quote_id, quote in quotes.items():
        result = results.get(quote_id)
        if result is None:
            unquoted.append(quote_id)
            continue
        if result.get("error"):
            excluded.append({"id": quote_id, "reason": "官方计价失败：" + str(result["error"])})
            continue
        try:
            price, card = check_quote(quote, result)
        except InputError as error:
            excluded.append({"id": quote_id, "reason": str(error)})
            continue
        valid.append({**quote, "price": price, "card": card})
    refs = plan_data["ref_prices"]
    worth = lambda e: sum(refs.get(c, 0) * n for c, n in e["supply"].items())
    # 同价时：附带少的优先，再优先不开卡，再优先餐品原价更高的（同价拿到更值钱的口味）
    order = lambda e: (e["price"], e["extra_count"], e["context"] != NONE_CTX, -worth(e), e["id"])
    valid.sort(key=order)
    baseline = next((e for e in valid if "baseline" in e["roles"]), None)
    recommendations = []
    if valid:
        lowest = valid[0]
        recommendations.append({**lowest, "label": "最低实付"})
        refs = plan_data["ref_prices"]
        card_value = plan_data.get("card_value_cents", DEFAULT_CARD_VALUE)
        ups = []
        for entry in valid[1:]:
            added = extra_added(entry, lowest)
            if not added or not entry.get("minimal", True):
                continue
            gain = value_gain(refs, entry["supply"], lowest["supply"])
            if gain is not None:  # 随单开的卡也算多得的东西，按 card_value 计（默认 ¥5，不超过卡费）
                gain += card_worth(entry, card_value) - card_worth(lowest, card_value)
            delta = entry["price"] - lowest["price"]  # 官方整单差价，已含卡费
            if upgrade_ok(delta, gain):
                ups.append((-(gain - delta), delta, entry["id"], entry, added, gain))
        if ups:
            _, delta, _, entry, added, value = min(ups)
            text = "、".join(plan_data["names"].get(c, c) + "×%d" % n for c, n in sorted(added.items()))
            if entry["card"] and not lowest["card"]:
                text += "＋" + entry["card"]["name"]
                value_note = "原价约%s（卡按%s计）" % (money(value), money(card_worth(entry, card_value)))
            else:
                value_note = "原价约%s" % money(value)
            recommendations.append({**entry, "label": "多花%s升级" % money(delta),
                                    "upgrade": {"delta_cents": delta, "delta": money(delta), "added": added,
                                                "added_text": text, "added_value_cents": value, "added_value": money(value),
                                                "value_note": value_note}})
        picked = {e["id"] for e in recommendations}
        alternatives = []
        if lowest["context"] != NONE_CTX:
            alternatives.append(("不开卡", next((e for e in valid if e["context"] == NONE_CTX), None)))
        if any(len(d["codes"]) > 1 for d in plan_data["demands"]):
            cores = [e["core"] for e in recommendations]
            alternatives.append(("换口味", next((e for e in valid if e["core"] not in cores), None)))
        alternatives.append(("少附带", next((e for e in valid if e["extra_count"] < lowest["extra_count"]), None)))
        for label, entry in alternatives:
            if entry is not None and entry["id"] not in picked and len(recommendations) < MAX_RECOMMENDATIONS:
                recommendations.append({**entry, "label": label})
                picked.add(entry["id"])
    output = []
    for i, entry in enumerate(recommendations, 1):
        output.append({"no": i, "label": entry["label"], "quote_id": entry["id"], "price_cents": entry["price"],
                       "price": money(entry["price"]), "summary": order_summary(entry), "items": food_rows(entry, plan_data),
                       "notes": card_notes(entry, plan_data, baseline, recommendations[0]), "context": entry["context"],
                       "card": entry["card"], "upgrade": entry.get("upgrade"), "display": entry["display"],
                       "allocation": entry["allocation"], "extras": entry["extras"], "payload": entry["payload"]})
    card_names = []
    for ctx in plan_data["contexts"]:
        if ctx != NONE_CTX:
            named = next((e["card"]["name"] for e in valid if e["context"] == ctx and e["card"]), None)
            card_names.append(named or "卡")
    effort = effort_text(plan_data, len(results), len(valid), card_names)
    conclusion = None
    if recommendations and baseline is not None:
        saving = baseline["price"] - recommendations[0]["price"]
        if recommendations[0]["id"] == baseline["id"] or saving <= 0:
            conclusion = "这家店此刻直接单点就是最省的，套餐、优惠券和开卡都没能更便宜。"
        else:
            conclusion = "只点你要的、全按最便宜的单点买要 %s；最省的方案 %s，少花 **%s**。" % (
                money(baseline["price"]), money(recommendations[0]["price"]), money(saving))
    elif recommendations:
        conclusion = "全单点没拿到官方价，暂时算不出省了多少。"
    title = title_line(plan_data)
    table = render_table(output)
    cards_html = render_cards(output, plan_data)
    footer = render_footer(output, effort, conclusion)
    message = "\n\n".join(x for x in (title, table, footer) if x)
    return {"schema": "mcd-deal-detective/picked@1", "recommendations": output,
            "baseline": {"quote_id": baseline["id"], "price_cents": baseline["price"], "price": money(baseline["price"])} if baseline else None,
            "valid_count": len(valid), "excluded": excluded, "unquoted": sorted(unquoted), "effort": effort, "conclusion": conclusion,
            "title": title, "cards_html": cards_html, "table_markdown": table, "footer": footer, "message": message}


# ---------------------------------------------------------------- CLI

def reject_constant(value):
    raise InputError("JSON 不允许 NaN/Infinity")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, parse_constant=reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "pick"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        data = read_json(args.input)
        result = plan(data) if args.command == "plan" else pick(data)
    except (InputError, OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    summary = {"output": args.output}
    if args.command == "plan":
        summary.update(quotes=len(result["quotes"]), notes=result["notes"])
    else:
        summary.update(recommendations=len(result["recommendations"]), excluded=len(result["excluded"]), unquoted=result["unquoted"])
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
