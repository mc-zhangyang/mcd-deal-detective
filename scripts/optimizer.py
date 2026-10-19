#!/usr/bin/env python3
"""麦侦探求解器：先按菜单价在本地精确搜索点法，再用官方计价复核排序。

纯 Python 3.9+ 标准库；不联网、不读取凭证、不下单（只算到方案展示）。

    plan  --input request.json --output plan.json    生成少量需要官方计价的候选
    pick  --input quotes.json  --output picked.json  按官方实付给出最多 3 个方案
"""
import argparse
import bisect
import glob
import hashlib
import heapq
import json
import os
import re
import sys
from html import escape
from collections import Counter
from decimal import Decimal, InvalidOperation

NONE_CTX = "none"
ALL_COUPONS = "*"  # 不限制券种：多张券同单的价格官方计价算不出来，按各券券后价合计
# 官方计价接口不支持多张券同单（小程序可以一起下单）：这条报错忽略，改用本地“各券券后价合计”
MULTI_COUPON_MARKS = ("多张券", "600022")
BIG = 10 ** 7
TIE = 1000
DEFAULT_MAX_QUOTES = 10
DEFAULT_KEEP = 80
DEFAULT_NODE_LIMIT = 200000
UPGRADE_DIVISOR = 5  # 多花的钱 <= 所加餐品单点原价合计的 1/5
MAX_RECOMMENDATIONS = 3
DEFAULT_CARD_VALUE = 500  # 随单开的卡算作多得价值时按 ¥5 计（不超过卡费）
EFFORT_FIELDS = ("menu_items", "details_checked", "coupons_claimed", "store_coupons")
MODES = {1: "到店取餐", 2: "麦乐送"}
DININGS = {"到店取餐": "到店取餐", "麦乐送": "麦乐送"}  # 与麦当劳小程序一致；堂食/外带在下单页选，价格相同
FEE_FIELDS = (("deliveryPrice", "配送费"), ("packingPrice", "打包费"), ("tablewarePrice", "餐具费"))


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

def default_flag(value, field):
    if value is None or (isinstance(value, str) and value == ""):
        return False
    need((isinstance(value, (bool, int, float)) and value in (0, 1)) or
         (isinstance(value, str) and value in ("0", "1")), field + " 必须为 0/1、true/false；None/空/缺省为 false")
    return value == 1 or value == "1"


def input_list(value, field):
    need(value is None or isinstance(value, list), field + " 必须是数组")
    return [] if value is None else value


def input_errors(errors):
    if errors:
        raise InputError("输入错误：\n" + "\n".join(errors))


def parsed_modification(raw, field):
    try:
        return default_modification(raw, field)
    except (AttributeError, KeyError, TypeError) as error:
        raise InputError(field + ".modification 格式不对：" + str(error)) from error


def compact_single(raw, field):
    """单品简写：[code, name, currentPrice] 或 [code, name, currentPrice, originalPrice]。"""
    if isinstance(raw, dict):
        return raw
    need(isinstance(raw, list) and len(raw) in (3, 4), field + " 简写须为 [code, name, 现价(, 原价)]")
    item = {"code": raw[0], "name": raw[1], "currentPrice": raw[2]}
    if len(raw) == 4:
        item["originalPrice"] = raw[3]
    return item


def compact_choice(raw, field):
    """套餐选项简写：[code, name]、[code, name, 差价]、[code, name, 差价, 1(默认项)]、[code, name, 差价, 1, 上限]。
    差价写 0/"+1"/"-0.5"；第 5 项是官方返回的 maxQuantity（这一轮该选项最多选几份），省略表示不限。"""
    if isinstance(raw, dict):
        return {**raw, "isDefault": default_flag(raw.get("isDefault"), field + ".isDefault")}
    need(isinstance(raw, list) and 2 <= len(raw) <= 5, field + " 简写须为 [code, name(, 差价(, 1(, 上限)))]")
    item = {"code": raw[0], "name": raw[1]}
    if len(raw) >= 3:
        item["diffPrice"] = raw[2]
    if len(raw) >= 4:
        item["isDefault"] = default_flag(raw[3], field + ".isDefault")
    if len(raw) == 5 and raw[4] not in (None, "", -1, "-1"):
        item["maxQuantity"] = raw[4]
    return item


def merge_fragments(data, folder):
    """合并片段；独立映射错误汇总，冲突商品不默丢。"""
    need(isinstance(data, dict), "request 必须是 JSON 对象")
    data = dict(data)
    errors = []
    paths = sorted(glob.glob(os.path.join(folder, "products_*.json")))
    try:
        for path in input_list(data.get("product_files"), "product_files"):
            try:
                need(isinstance(path, str) and os.path.isabs(path), "product_files 须为绝对路径")
                if path not in paths:
                    paths.append(path)
            except InputError as error:
                errors.append(str(error))
    except InputError as error:
        errors.append(str(error))
    if not paths:
        input_errors(errors)
        return data
    products, singles, refs = [], [], {}
    origins = {"products": [], "singles": []}
    demands = data.get("demands")
    if isinstance(demands, list):
        demands = [dict(d) if isinstance(d, dict) else d for d in demands]
        data["demands"] = demands
    by_id = {str(d.get("id") or "d%d" % (i + 1)): d for i, d in enumerate(demands or [])
             if isinstance(d, dict)} if isinstance(demands, list) else {}
    parts = [("request", data)]
    for path in paths:
        try:
            part = read_json(path)
            need(isinstance(part, dict), "必须是 JSON 对象")
            parts.append((path, part))
        except (InputError, OSError, json.JSONDecodeError) as error:
            errors.append(path + "：" + str(error))
    for name, part in parts:
        for key, target in (("products", products), ("singles", singles)):
            try:
                items = input_list(part.get(key), name + "." + key)
                target.extend(items)
                origins[key].extend("%s.%s[%d]" % (name, key, i) for i in range(len(items)))
            except InputError as error:
                errors.append(str(error))
        try:
            values = part.get("reference_prices", {})
            need(isinstance(values, dict), name + ".reference_prices 必须是对象")
            for code, value in values.items():
                refs.setdefault(code, value)
        except InputError as error:
            errors.append(str(error))
        if name == "request":
            continue
        for key in ("demand_codes", "demand_pieces"):
            try:
                mapping = part.get(key, {})
                need(isinstance(mapping, dict), name + "." + key + " 必须是对象")
            except InputError as error:
                errors.append(str(error))
                continue
            for demand_id, value in mapping.items():
                field = name + "." + key + "." + str(demand_id)
                try:
                    need(str(demand_id) in by_id, field + " 里的需求 id 不存在")
                    d = by_id[str(demand_id)]
                    if key == "demand_codes":
                        need(isinstance(value, list), field + " 必须是 code 数组")
                        codes = [code_of(c, field) for c in value]
                        existing = [code_of(c, field + ".codes") for c in input_list(d.get("codes"), field + ".codes")]
                        d["codes"] = existing + [c for c in dict.fromkeys(codes) if c not in existing]
                    else:
                        need(isinstance(value, dict), field + " 必须是块数对象")
                        if not value and d.get("pieces") is None:
                            continue
                        existing = d.get("pieces")
                        if existing is None:
                            existing = {}
                        need(isinstance(existing, dict), field + ".pieces 必须是块数对象")
                        need(all(type(v) is int and v >= 1 for v in value.values()), field + " 块数必须为正整数")
                        d["pieces"] = {**existing, **{code_of(k, field): v for k, v in value.items()}}
                except InputError as error:
                    errors.append(str(error))
    seen = {}
    for key, items in (("singles", singles), ("products", products)):
        unique = []
        for item, field in zip(items, origins[key]):
            try:
                raw = compact_single(item, field) if key == "singles" else item
                need(isinstance(raw, dict), field + " 必须是对象")
                code = code_of(raw.get("code"), field + ".code")
                coupon = raw.get("coupon")
                need(coupon is None or isinstance(coupon, dict), field + ".coupon 必须是对象")
                sid = code + "#券" + str(coupon.get("couponId")) if coupon is not None else code
                if sid in seen:
                    previous, previous_field = seen[sid]
                    need(canonical(raw) == canonical(previous), field + " 冲突重复 product：" + sid + "（与 " + previous_field + "）")
                    continue
                seen[sid] = (raw, field)
            except InputError as error:
                errors.append(str(error))
                continue
            unique.append(item)
        data[key] = unique
    input_errors(errors)
    data["reference_prices"] = refs
    return data

MAX_VARIANTS = 64
ABOUT_RATIO_NUM, ABOUT_RATIO_DEN = 4, 5  # “左右”的下限：说出数量的 80%


def piece_splits(total, sizes):
    """凑够至少 total 块的“最少份数组合”：每种块数各买几份，去掉任意一份就不够。"""
    sizes = sorted(set(sizes), reverse=True)
    result = []

    def walk(i, counts, got):
        if got >= total:
            if all(got - w < total for w, k in zip(sizes, counts) if k):
                result.append(dict((w, k) for w, k in zip(sizes, counts) if k))
            return
        if i == len(sizes):
            return
        w = sizes[i]
        for k in range(-(-(total - got) // w), -1, -1):
            walk(i + 1, counts + [k], got + k * w)
    walk(0, [], 0)
    return result


def demand_variants(demands, lazy=False):
    """整份块数分配的最小组合；回退路径按需迭代，不预先展开笛卡尔积。"""
    groups = []
    for d in demands:
        need_quantity = d.get("min_quantity", d["quantity"])
        if "pieces" not in d:
            options = [[{"id": d["id"], "label": d["label"], "quantity": need_quantity, "codes": d["codes"]}]]
        else:
            by_size = {}
            for code in d["codes"]:
                by_size.setdefault(d["pieces"][code], []).append(code)
            options = [[{"id": d["id"], "label": d["label"], "quantity": k, "codes": by_size[w]}
                        for w, k in sorted(split.items())]
                       for split in piece_splits(need_quantity, by_size)]
        groups.append(options)

    def walk(i, prefix):
        if i == len(groups):
            yield prefix
        else:
            for option in groups[i]:
                yield from walk(i + 1, prefix + option)

    variants = walk(0, [])
    return variants if lazy else list(variants)

class Request:
    def __init__(self, data):
        need(isinstance(data, dict), "request 必须是 JSON 对象")
        errors = []
        for parse in (self._context, self._settings, self._effort):
            try:
                parse(data)
            except InputError as error:
                errors.append(str(error))
        self.demands = []
        seen = set()
        try:
            demands = input_list(data.get("demands"), "demands")
            need(bool(demands), "demands 不能为空")
            for i, raw in enumerate(demands):
                try:
                    demand = self._demand(raw, "demands[%d]" % i, i)
                    need(demand["id"] not in seen, "重复的 demand id：" + demand["id"])
                    seen.add(demand["id"])
                    self.demands.append(demand)
                except InputError as error:
                    errors.append(str(error))
        except InputError as error:
            errors.append(str(error))
        self._variants = None
        self.total_units = sum(-(-d.get("min_quantity", d["quantity"]) //
                                  min(d.get("pieces", {c: 1 for c in d["codes"]}).values()))
                               for d in self.demands)
        if self.total_units > 30:
            errors.append("必须餐品合计超过 30 份，请拆开计算")

        self.names = {}
        self.sources = []
        self.card_requests = {}
        sids = set()
        queried = set()
        for key in ("singles", "products"):
            try:
                items = input_list(data.get(key), key)
            except InputError as error:
                errors.append(str(error))
                continue
            for i, raw in enumerate(items):
                field = "%s[%d]" % (key, i)
                try:
                    if key == "singles":
                        raw = compact_single(raw, field)
                    if isinstance(raw, dict):
                        queried.add(code_of(raw.get("code"), field + ".code"))
                    source = self._source(raw, field)
                    need(source["sid"] not in sids, field + " 重复的 product：" + source["sid"])
                    sids.add(source["sid"])
                    self.sources.append(source)
                except InputError as error:
                    errors.append(str(error))
        if not self.sources and not errors:
            errors.append("products 不能为空")
        for key in ("expected_product_codes", "expected_single_codes"):
            if key not in data:
                continue
            try:
                need(isinstance(data[key], list), key + " 必须是 code 数组")
                expected = {code_of(c, key) for c in data[key]}
                missing = sorted(expected - queried)
                need(not missing, key + " 缺少已查询商品 code：" + "、".join(missing))
            except InputError as error:
                errors.append(str(error))

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
        try:
            refs = data.get("reference_prices", {})
            need(isinstance(refs, dict), "reference_prices 必须是对象")
            for code, value in refs.items():
                try:
                    self.ref_prices[str(code)] = yuan_to_cents(value, "reference_prices." + str(code))
                except InputError as error:
                    errors.append(str(error))
        except InputError as error:
            errors.append(str(error))
        input_errors(errors)
        self.estimated_prices = self._infer_missing_prices()
        self.contexts = [NONE_CTX] + sorted(self.card_requests)

    def _context(self, data):
        context = data.get("context")
        need(isinstance(context, dict), "缺少 context")
        for key in ("storeCode", "orderType", "beType"):
            need(key in context, "context 缺少 " + key)
        need("items" not in context and "withOrder" not in context, "context 不放 items/withOrder")
        self.context = dict(context)
        self.dining = data.get("dining")
        need(self.dining is None or (isinstance(self.dining, str) and self.dining in DININGS),
             "dining 只能是 " + "/".join(DININGS))
        delivery = context.get("orderType") == 2
        if self.dining == "麦乐送":
            need(delivery and context.get("beType") == 2, "麦乐送需要 context.orderType=2、beType=2")
        elif self.dining is not None:
            need(not delivery and context.get("beType") == 1, "到店取餐需要 context.orderType=1、beType=1")
        if delivery:
            need(bool(context.get("beCode")), "外送场景 context 必须带配送门店的 beCode")

    def _settings(self, data):
        self.forbidden = {code_of(c, "forbidden_codes") for c in input_list(data.get("forbidden_codes"), "forbidden_codes")}
        self.strict = data.get("strict", False)
        need(type(self.strict) is bool, "strict 必须为 true/false")
        self.max_quotes = data.get("max_quotes", DEFAULT_MAX_QUOTES)
        need(type(self.max_quotes) is int and 1 <= self.max_quotes <= 30, "max_quotes 取 1~30")
        self.node_limit = data.get("node_limit", DEFAULT_NODE_LIMIT)
        need(type(self.node_limit) is int and self.node_limit >= 1, "node_limit 必须为正整数")
        self.card_value = yuan_to_cents(data.get("card_value", DEFAULT_CARD_VALUE / 100), "card_value")
        need(self.card_value >= 0, "card_value 不能为负")
        for key in ("store_name", "address", "store_note"):
            value = data.get(key)
            need(value is None or isinstance(value, str), key + " 必须为字符串")
            setattr(self, key, value)
        self.unused_coupons = []
        for i, raw in enumerate(input_list(data.get("unused_coupons"), "unused_coupons")):
            field = "unused_coupons[%d]" % i
            if isinstance(raw, str):
                raw = {"title": raw}
            need(isinstance(raw, dict), field + " 必须是对象或字符串")
            title = str(raw.get("title") or "").strip()
            need(bool(title), field + " 缺少 title")
            self.unused_coupons.append({"title": title, "reason": str(raw.get("reason") or "").strip()})

    def _effort(self, data):
        effort = data.get("effort", {})
        need(isinstance(effort, dict), "effort 必须是对象")
        for key, value in effort.items():
            need(key in EFFORT_FIELDS, "effort 不支持字段 " + str(key))
            need(type(value) is int and value >= 0, "effort." + key + " 必须为非负整数")
        self.effort = dict(effort)

    def _demand(self, raw, field, i):
        need(isinstance(raw, dict), field + " 必须是对象")
        demand_id = code_of(raw.get("id") or "d%d" % (i + 1), field + ".id")
        quantity = raw.get("quantity")
        need(type(quantity) is int and quantity >= 1, field + ".quantity 必须为正整数")
        codes = [code_of(c, field + ".codes") for c in input_list(raw.get("codes"), field + ".codes")]
        need(codes and len(set(codes)) == len(codes), field + ".codes 需要不重复的真实餐品 code")
        if hasattr(self, "forbidden"):
            need(not self.forbidden.intersection(codes), field + " 的 codes 与 forbidden_codes 冲突")
        demand = {"id": demand_id, "label": str(raw.get("label") or raw.get("name") or demand_id), "quantity": quantity, "codes": codes}
        pieces = raw.get("pieces")
        if pieces is not None:
            need(isinstance(pieces, dict) and set(pieces) == set(codes), field + ".pieces 必须给出每个 code 一份的块数")
            need(all(type(v) is int and v >= 1 for v in pieces.values()), field + ".pieces 块数必须为正整数")
            demand["pieces"] = {str(k): v for k, v in pieces.items()}
        about = raw.get("about", False)
        need(type(about) is bool, field + ".about 必须为 true/false")
        if about:
            demand["about"] = True
            demand["min_quantity"] = max(1, -(-quantity * ABOUT_RATIO_NUM // ABOUT_RATIO_DEN))
        return demand

    @property
    def variants(self):
        if self._variants is None:
            self._variants = demand_variants(self.demands)
        return self._variants

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
            codes = [code_of(c, field + ".coupon.couponCodes") for c in input_list(coupon.get("couponCodes"), field + ".coupon.couponCodes")]
            need(codes and len(set(codes)) == len(codes), field + ".coupon.couponCodes 需要不重复的券码")
            # paid 是买这张券已经花掉的钱（抖音/快手等外购券，官方这单只收券后价），两者相加才是真实成本
            paid = yuan_to_cents(coupon.get("paid", 0), field + ".coupon.paid（买这张券花的钱）")
            need(paid >= 0, field + ".coupon.paid 不能为负")
            coupon = {"couponId": str(coupon["couponId"]), "couponCodes": codes, "title": str(coupon.get("title") or name),
                      "paid": paid}
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
        rounds, errors = [], []
        for j, raw_round in enumerate(input_list(raw.get("rounds"), field + ".rounds")):
            try:
                parsed = self._round(raw_round, "%s.rounds[%d]" % (field, j))
                if parsed is not None:
                    rounds.append(parsed)
            except InputError as error:
                errors.append(str(error))
        input_errors(errors)
        food = code_of(raw.get("food_code", code), field + ".food_code") if not rounds else None
        if food:
            self.names.setdefault(food, name)
        return {"sid": sid, "code": code, "name": name, "food": food, "rounds": rounds, "coupon": coupon,
                "card_key": card_key, "current": current, "original": original, "limit": limit,
                "modification": parsed_modification(raw, field) if not rounds else None}

    def _round(self, raw, field):
        need(isinstance(raw, dict), field + " 必须是对象")
        round_id = raw.get("id", raw.get("round"))
        need(round_id is not None, field + " 缺少 id")
        units = raw.get("minQuantity", raw.get("min"))
        need(type(units) is int and units >= 0, field + ".minQuantity 必须为非负整数")
        if units == 0:
            return None  # 可选加购轮次不参与搜索，也不传
        choices, errors = [], []
        seen = set()
        for k, choice in enumerate(input_list(raw.get("choices"), field + ".choices")):
            cf = "%s.choices[%d]" % (field, k)
            try:
                choice = compact_choice(choice, cf)
                code = code_of(choice.get("code"), cf + ".code")
                need(code not in seen, field + " 内重复选项 " + code)
                seen.add(code)
                cap = choice.get("maxQuantity", -1)
                need(type(cap) is int and cap >= -1, cf + ".maxQuantity 必须为 -1 或非负整数")
                cap = units if cap == -1 else min(cap, units)
                name = str(choice.get("name") or code)
                choices.append({"code": code, "name": name, "diff": diff_to_cents(choice.get("diffPrice"), cf + ".diffPrice"),
                                "cap": cap, "default": default_flag(choice.get("isDefault"), cf + ".isDefault"), "index": k,
                                "modification": parsed_modification(choice, cf)})
                self.names[code] = name  # 详情里的选项名带规格，优先使用
            except InputError as error:
                errors.append(str(error))
        input_errors(errors)
        need(bool(choices), field + " 没有选项")
        return {"id": str(round_id), "units": units, "choices": choices}

    def price(self, source, ctx):
        if source["coupon"]:
            return source["current"] + source["coupon"]["paid"]  # 券后价 + 买券花的钱
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

    def run(self, source, sink, limit, max_cost=None):
        """完整增广到 limit；允许负费用，不能用中途费用作总费用下界。"""
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
    codes = [c for d in demands for c in d["codes"]]
    if len(codes) == len(set(codes)):
        return all(sum(supply.get(c, 0) * d.get("pieces", {}).get(c, 1)
                       for c in d["codes"]) >= d.get("min_quantity", d["quantity"])
                   for d in demands)
    if any("pieces" in d for d in demands):
        # 重叠类别必须分配整份餐品，不能把一份五块装拆流给两个需求。
        return any(covers(variant, supply) for variant in demand_variants(demands, lazy=True))
    total = sum(d.get("min_quantity", d["quantity"]) for d in demands)
    if sum(supply.get(c, 0) for c in set(codes)) < total:
        return False
    remaining = {c: min(total, supply.get(c, 0)) for c in set(codes) if supply.get(c, 0) > 0}
    owners = {c: [] for c in remaining}
    targets = [d.get("min_quantity", d["quantity"]) for d in demands]

    def assign(i, seen_codes, seen_demands):
        if i in seen_demands:
            return False
        seen_demands.add(i)
        for code in demands[i]["codes"]:
            if code not in owners or code in seen_codes:
                continue
            seen_codes.add(code)
            if len(owners[code]) < remaining[code]:
                owners[code].append(i)
                return True
            for j, previous in enumerate(owners[code]):
                if assign(previous, seen_codes, seen_demands):
                    owners[code][j] = i
                    return True
        return False

    return all(assign(i, set(), set()) for i, target in enumerate(targets) for _ in range(target))


# ---------------------------------------------------------------- 本地精确搜索

class Solver:
    def __init__(self, request):
        self.r = request
        self._config_cache = {}

    def _demands(self, bans, variant):
        result = []
        for d in variant:
            codes = [c for c in d["codes"] if (d["id"], c) not in bans]
            if not codes:
                return None
            result.append({**d, "codes": codes})
        return result

    def _combos(self, ctx, demands, notes, coupon=None):
        demand_codes = {c for d in demands for c in d["codes"]}
        combos = []
        for source in self.r.sources:
            if not source["rounds"] or self._skip(source, coupon):
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
            minimum = self.r.price(source, ctx)
            if usable:
                for item in info:
                    remaining = item["round"]["units"]
                    for ch in sorted(item["allowed"], key=lambda ch: ch["diff"]):
                        n = min(remaining, ch["cap"])
                        minimum += n * ch["diff"]
                        remaining -= n
            activates_card = ctx != NONE_CTX and source["card_key"] == ctx and not self.r.strict
            if not usable or (not serves and minimum >= 0 and not activates_card):
                if usable is False and notes is not None:
                    notes.add(source["name"] + "：必选内容含禁止或无法满足严格要求，未使用")
                continue
            coverable = sum(d["quantity"] for d in demands if serves.intersection(d["codes"]))
            units = sum(d["quantity"] for d in demands)
            need(minimum >= 0 or source["limit"] is not None or self.r.strict,
                 source["name"] + "：无限购负价商品使最低价无界")
            if minimum < 0 and not self.r.strict:
                bound = source["limit"]
            else:
                bound = min(max(coverable, int(activates_card)), units, units if source["limit"] is None else source["limit"])
            if bound <= 0:
                continue
            fixed = minimum
            combos.append({"source": source, "info": info, "bound": bound, "fixed": fixed})
        combos.sort(key=lambda c: (c["fixed"], c["source"]["sid"]))
        return combos

    def _singles(self, ctx, demands, coupon=None):
        demand_codes = {c for d in demands for c in d["codes"]}
        result = {}
        for source in self.r.sources:
            if (not source["rounds"] and source["food"] not in self.r.forbidden
                    and (source["food"] in demand_codes or (not self.r.strict and
                         (self.r.price(source, ctx) < 0 or (ctx != NONE_CTX and source["card_key"] == ctx))))
                    and not self._skip(source, coupon)):
                if source["limit"] == 0:
                    continue
                result.setdefault(source["food"], []).append(source)
        return result

    def _skip(self, source, coupon):
        """按“用哪张券”分别搜索：coupon=None 表示不用券，ALL_COUPONS 表示不限制（多券同单）。"""
        if coupon is None:
            return bool(source["coupon"])
        if coupon == ALL_COUPONS:
            return False
        return bool(source["coupon"]) and source["sid"] != coupon

    def coupon_options(self):
        sids = [s["sid"] for s in self.r.sources if s["coupon"]]
        # 官方计价接口不支持多券同单，但小程序可以一起下单：多券组合照样搜，
        # 核价拿到报错时 pick 会按各券券后价合计，不再作废这个候选
        return [None] + sids + ([ALL_COUPONS] if len(sids) > 1 else [])

    def solve(self, ctx, bans=frozenset(), keep=DEFAULT_KEEP, notes=None):
        """最低价独立精确DP；候选标签另用预算，巨大/重叠状态回退数量搜索。"""
        stats = {"nodes": 0, "complete": True, "combos": 0, "engine": "weighted_dp",
                 "candidate_nodes": 0, "candidates_complete": True,
                 "quantity_nodes": 0, "candidate_quantity_nodes": 0}
        best, keys, per_coupon = [], set(), {}
        demands = self._demands(bans, self.r.demands)
        if demands is None:
            return [], stats, {}
        tasks = [c for c in self.coupon_options() if self._coupon_fits(demands, c)]
        all_codes = [c for d in demands for c in d["codes"]]
        disjoint = len(all_codes) == len(set(all_codes))
        # 重叠需求也走加权覆盖 DP：改用“同一份餐品不双计”的子集记帐，见 _overlap_dims。
        # 只有能启用子集记帐的重叠才走新路径；块数需求重叠仍回退到变体数量搜索。
        overlap = None if disjoint else self._overlap_dims(demands)
        candidate_tasks = []
        candidate_seeds = {}
        for coupon in tasks:
            local = []
            if self._unreachable(demands, self._combos(ctx, demands, notes, coupon),
                                 self._singles(ctx, demands, coupon)):
                continue
            was_complete = stats["complete"]
            fast = None
            if disjoint:
                fast = self._dp(ctx, demands, coupon, 1, notes, stats)
            elif overlap is not None:
                fast = self._dp(ctx, demands, coupon, 1, notes, stats, overlap=overlap)
            if fast is not None:
                for item in fast:
                    self._insert(best=local, keys=set(), plan=item, keep=keep)
                if keep > 1 and fast:
                    candidate_tasks.append(coupon)
                    candidate_seeds[coupon] = [entry[3] for entry in local]
            else:
                stats["engine"] = "quantity_search" if stats["nodes"] == 0 else "adaptive"
                fallback_tasks = []
                variants_complete = True
                for i, variant in enumerate(demand_variants(demands, lazy=True)):
                    if i >= self.r.node_limit:
                        variants_complete = False
                        stats["candidates_complete"] = False
                        break
                    if self._coupon_fits(variant, coupon):
                        combos = self._combos(ctx, variant, notes, coupon)
                        singles = self._singles(ctx, variant, coupon)
                        if not self._unreachable(variant, combos, singles):
                            fallback_tasks.append(variant)
                stats["complete"] = True
                remaining = self.r.node_limit
                for i, filtered in enumerate(fallback_tasks):
                    budget = max(1, remaining // (len(fallback_tasks) - i))
                    before = stats["nodes"]
                    self._search(ctx, filtered, 1, notes, stats, local,
                                 {e[2] for e in local}, coupon, budget)
                    used = stats["nodes"] - before
                    stats["quantity_nodes"] += used
                    remaining -= used
                stats["complete"] &= was_complete and variants_complete
                if keep > 1:
                    candidate_tasks.append((coupon, fallback_tasks))
            if coupon is not None and local:
                per_coupon[coupon] = local[0][3]
            for entry in local:
                if entry[2] not in keys:
                    keys.add(entry[2])
                    bisect.insort(best, entry)
                    if len(best) > keep:
                        keys.discard(best.pop()[2])
        # 全部最低价任务先跑完，再共享一份备选预算；备选失败不能抹掉最低价。
        remaining = self.r.node_limit
        for task in candidate_tasks:
            if remaining <= 0:
                stats["candidates_complete"] = False
                break
            before = stats["candidate_nodes"]
            if isinstance(task, tuple):
                coupon, variants = task
                candidate_stats = {"nodes": 0, "complete": True, "combos": 0}
                local = []
                local_keys = set()
                if coupon in per_coupon:
                    self._insert(local, local_keys, per_coupon[coupon], keep)
                for i, variant in enumerate(variants):
                    budget = max(1, remaining // (len(variants) - i))
                    used = candidate_stats["nodes"]
                    self._search(ctx, variant, keep, notes, candidate_stats, local, local_keys, coupon, budget)
                    remaining -= candidate_stats["nodes"] - used
                for entry in local:
                    self._insert(best, keys, entry[3], keep)
                if coupon is not None and local:
                    per_coupon[coupon] = local[0][3]
                stats["candidate_nodes"] += candidate_stats["nodes"]
                stats["candidate_quantity_nodes"] += candidate_stats["nodes"]
                stats["candidates_complete"] &= candidate_stats["complete"]
                continue
            extra = {"overlap": overlap} if overlap is not None else {}
            alternatives = self._dp(ctx, demands, task, keep, notes, stats, candidates=True, budget=remaining,
                                    seeds=candidate_seeds.get(task, ()), **extra)
            for item in alternatives or []:
                self._insert(best, keys, item, keep)
                if task is not None and (task not in per_coupon or item["cost"] < per_coupon[task]["cost"]):
                    per_coupon[task] = self._materialize(item)
            remaining -= stats["candidate_nodes"] - before
        # 数量回退每个购买数量只求一个费用流配置，不能声称全部备选已穷尽。
        if any(isinstance(task, tuple) for task in candidate_tasks):
            stats["candidates_complete"] = False
        if not stats["complete"]:
            stats["candidates_complete"] = False
        stats["candidate_scope"] = ("加权DP每状态至多keep条购买路径；不是全部口味、升级或少附带方案的完备枚举"
                                    if stats["engine"] == "weighted_dp" else
                                    "数量回退每个购买数量仅取一个最小费用流配置；其他配置可能未返回")
        stats["guarantee"] = ("菜单估价最低金额；不保证全部口味、升级或少附带候选"
                              if stats["complete"] else "仅已找到的可行方案；最低金额尚未证明")
        return [entry[3] for entry in best], stats, per_coupon

    def _insert(self, best, keys, plan, keep):
        if plan is None or (plan["ctx"] != NONE_CTX and not plan["uses_card"]):
            return
        if len(best) >= keep and (plan["cost"], plan["extra_count"]) > best[-1][:2]:
            return
        self._materialize(plan)
        if plan["key"] in keys:
            return
        keys.add(plan["key"])
        bisect.insort(best, (plan["cost"], plan["extra_count"], plan["key"], plan))
        if len(best) > keep:
            keys.discard(best.pop()[2])

    def _overlap_dims(self, demands):
        """重叠需求的加权覆盖维度：同一份餐品不能同时顶掉两个需求。

        不重叠时按“每个需求各记一份”累加即可；一旦某个 code 同属多个需求，按需求逐份累加
        会把同一份餐品在两个需求里各算一次（买 2 份 A 既算 2 份需求0 又算 2 份需求1）。
        所以改成按子集记帐（Hall 条件）：每个子集 S 记“属于 S 中任一需求的餐品总份数”，
        所有子集都不小于该子集需求合计时，一定存在一种把每一份餐品分给一个需求的整份分配
        （二分 b-匹配的存在性定理），也就不会出现同一份双计；分配结果由 _allocate 真正做出来。
        只需单点维度与“在重叠关系图上连通”的子集：不连通子集的条件由各连通分量相加得出。
        块数需求要求整份不可拆（一份五块装不能拆给两个需求），与按份数的子集记帐语义不同，
        因此只要共享 code 落在块数需求里就不启用本路径，继续走变体回退（见 solve）。
        返回 None 表示不启用（不重叠、块数重叠、或维度会炸）。
        """
        owners = {}
        for i, d in enumerate(demands):
            for code in d["codes"]:
                owners[code] = owners.get(code, 0) | 1 << i
        shared = [code for code, mask in owners.items() if mask & mask - 1]
        if not shared:
            return None
        piece_demands = {i for i, d in enumerate(demands) if "pieces" in d}
        piece_mask = 0
        for i in piece_demands:
            piece_mask |= 1 << i
        for code in shared:
            if owners[code] & piece_mask:
                return None  # 共享 code 属于块数需求：整份不可拆，交给变体回退
        plain = [i for i in range(len(demands)) if i not in piece_demands]
        if len(plain) > 10:
            return None  # 需求太多，子集枚举本身不可控，回退
        plain_mask = 0
        for i in plain:
            plain_mask |= 1 << i
        targets = [d.get("min_quantity", d["quantity"]) for d in demands]
        masks, caps, goals, piece_flags = [], [], [], []
        for i in range(len(demands)):
            masks.append(1 << i)
            caps.append(targets[i])
            goals.append(targets[i])
            piece_flags.append(i in piece_demands)
        neighbours = {i: set() for i in plain}
        for code, mask in owners.items():
            if not mask & mask - 1:
                continue
            for a in plain:
                if not mask >> a & 1:
                    continue
                neighbours[a].update(b for b in plain if b != a and mask >> b & 1)
        for bits in range(1, 1 << len(plain)):
            group = [plain[b] for b in range(len(plain)) if bits >> b & 1]
            if len(group) < 2 or not self._linked(group, neighbours):
                continue
            mask = 0
            for i in group:
                mask |= 1 << i
            masks.append(mask)
            caps.append(sum(targets[i] for i in group))
            goals.append(caps[-1])
            piece_flags.append(False)
        exact = ()
        if self.r.strict and len(plain) > 1:
            # 严格模式每一份购买都必须不可少：非块数需求多买的那一份没有需求可顶，
            # 用“总份数”维度挡掉（上限放宽 1 份以便识别超额，判定时要求恰好等于需求合计）。
            total = sum(targets[i] for i in plain)
            if plain_mask in masks:
                index = masks.index(plain_mask)
            else:
                masks.append(plain_mask)
                caps.append(None)
                goals.append(total)
                piece_flags.append(False)
                index = len(masks) - 1
            caps[index] = total + 1
            goals[index] = total
            exact = (index,)
        if len(masks) > 12:
            return None  # 维度过多，状态空间不可控，回退
        size = 1
        for goal in goals:
            size *= goal + 1
            if size > 400000:
                return None
        return {"masks": tuple(masks), "caps": tuple(caps), "targets": tuple(goals),
                "pieces": tuple(piece_flags), "exact": exact, "owners": owners,
                "piece_demands": piece_demands, "plain": tuple(plain),
                "sig": tuple(zip(masks, goals, piece_flags))}

    def _linked(self, group, neighbours):
        seen = {group[0]}
        stack = [group[0]]
        while stack:
            for other in neighbours[stack.pop()]:
                if other in group and other not in seen:
                    seen.add(other)
                    stack.append(other)
        return len(seen) == len(group)

    def _match_plain(self, demands, plain, supply):
        """非块数的重叠需求做整份分配：同一份餐品只顶一个需求（逐份增广的二分匹配）。
        Hall 条件成立时匹配必定存在；返回 None 表示这份供给其实凑不齐。"""
        result = {demands[i]["id"]: {} for i in plain}
        if not plain:
            return result
        total = sum(demands[i].get("min_quantity", demands[i]["quantity"]) for i in plain)
        remaining = {}
        for i in plain:
            for code in demands[i]["codes"]:
                if supply.get(code, 0) > 0:
                    remaining[code] = min(total, supply[code])
        owners = {code: [] for code in remaining}

        def place(i, seen_codes, seen_demands):
            if i in seen_demands:
                return False
            seen_demands.add(i)
            for code in demands[i]["codes"]:
                if code not in owners or code in seen_codes:
                    continue
                seen_codes.add(code)
                if len(owners[code]) < remaining[code]:
                    owners[code].append(i)
                    return True
                for j, previous in enumerate(owners[code]):
                    if place(previous, seen_codes, seen_demands):
                        owners[code][j] = i
                        return True
            return False

        for i in plain:
            for _ in range(demands[i].get("min_quantity", demands[i]["quantity"])):
                if not place(i, set(), set()):
                    return None
        for code, assigned in owners.items():
            for i in assigned:
                result[demands[i]["id"]][code] = result[demands[i]["id"]].get(code, 0) + 1
        return result

    def _allocate(self, demands, supply, overlap):
        """重叠需求的分配：块数需求独占自己的 code，非块数需求走 _match_plain 的整份匹配。
        返回 None 表示这份供给凑不齐（Hall 条件保证不会发生，兜底丢弃该方案）。"""
        matched = self._match_plain(demands, overlap["plain"], supply)
        if matched is None:
            return None
        allocation = {}
        for d in demands:
            if d["id"] in matched:
                allocation[d["id"]] = matched[d["id"]]
                continue
            remaining = d.get("min_quantity", d["quantity"])
            taken = {}
            for code in d["codes"]:
                w = d.get("pieces", {}).get(code, 1)
                n = min(supply[code], max(0, -(-remaining // w)))
                if n:
                    taken[code] = n
                    remaining -= n * w
            if "pieces" in d:
                got = sum(n * d["pieces"][c] for c, n in taken.items())
                for code in d["codes"]:
                    w = d["pieces"][code]
                    removable = min(taken.get(code, 0), max(0, (got - d.get("min_quantity", d["quantity"])) // w))
                    if removable:
                        taken[code] -= removable
                        got -= removable * w
                taken = {c: n for c, n in taken.items() if n}
            allocation[d["id"]] = taken
        return allocation

    def _dp(self, ctx, demands, coupon, keep, notes, stats, candidates=False, sources=None, budget=None, seeds=(), config_fallback=False, overlap=None):
        targets = tuple(d.get("min_quantity", d["quantity"]) for d in demands)
        weights = {c: (i, d.get("pieces", {}).get(c, 1))
                   for i, d in enumerate(demands) for c in d["codes"]}
        piece_dims = {i for i, d in enumerate(demands) if "pieces" in d}
        strict = self.r.strict
        # 重叠需求换成子集维度（见 _overlap_dims）；overlap 为 None 时退化为逐维记账。
        caps = targets
        piece_flags = tuple(i in piece_dims for i in range(len(demands)))
        exact = ()
        owners = None
        dim_masks = ()
        dim_sig = ()
        if overlap is not None:
            targets = overlap["targets"]
            caps = overlap["caps"]
            piece_flags = overlap["pieces"]
            exact = overlap["exact"]
            owners = overlap["owners"]
            dim_masks = overlap["masks"]
            dim_sig = (overlap["sig"],)
        zero = (0,) * len(targets)
        # 最低价每个状态一个标签；备选独立保留 keep 条购买路径，不是 beam。
        budget = self.r.node_limit if budget is None else budget
        visited = 0
        merge_cache = {}
        config_budget = budget // 2 if candidates else budget
        field = "candidate_nodes" if candidates else "nodes"
        phase_stats = stats.setdefault("dp", {}).setdefault("candidate" if candidates else "minimum", {
            "config_nodes": 0, "transition_nodes": 0, "config_fallbacks": 0,
            "config_fallback_reasons": {}, "config_cache_hits": 0,
            "reduced_config_cache_hits": 0, "config_cache_misses": 0,
            "uncached_config_skips": 0,
        })

        def config_exhausted():
            phase_stats["config_nodes"] += visited
            phase_stats["config_fallbacks"] += 1
            reason = "candidate_half_budget" if candidates else "node_limit"
            reasons = phase_stats["config_fallback_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            stats["candidates_complete" if candidates else "complete"] = False
            if not candidates:
                return None
            # 不把半轮选项当可购买配置；退回已完整缓存的配置，并保留最低价见证。
            return self._dp(ctx, demands, coupon, keep, notes, stats, candidates=True,
                            sources=sources, budget=budget - visited, seeds=seeds, config_fallback=True)

        def tick():
            nonlocal visited
            if visited >= budget:
                return False
            visited += 1
            stats[field] = stats.get(field, 0) + 1
            return True

        def merge(a, am, b, bm):
            key = (a, am, b, bm)
            if key in merge_cache:
                return merge_cache[key]
            v, m = [], []
            for j, target in enumerate(targets):
                got = a[j] + b[j]
                least = min(x for x in (am[j], bm[j]) if x) if am[j] or bm[j] else 0
                cap = caps[j]
                if strict:
                    if piece_flags[j]:
                        if got and got - least >= target:
                            return None  # 块数严格模式只允许每份都不可删的超额组合
                        v.append(got)
                        m.append(least)
                        continue
                    # 重叠的子集维度不能按单个需求的上限截断：A 同属两个需求时，
                    # 买 A 和 B 各顶一个需求是合法的，超额由“总份数”维度在严格模式下统一挡掉。
                    if owners is None and got > target:
                        return None
                v.append(got if cap is None else min(cap, got))
                m.append(0)
            result = tuple(v), tuple(m)
            if len(merge_cache) < budget:
                merge_cache[key] = result
            return result

        def contribution(code, n):
            v, m = list(zero), list(zero)
            if code in weights and n:
                i, w = weights[code]
                if owners is None:
                    v[i], m[i] = n * w, w
                else:
                    mask = owners[code]
                    for j, dim in enumerate(dim_masks):
                        if mask & dim:  # 同一份餐品在任一子集维度里只记一次
                            v[j] = n * w if piece_flags[j] else n
                            m[j] = w if piece_flags[j] else 0
            return tuple(v), tuple(m)

        def retain(table, key, entry, limit):
            labels = table.setdefault(key, [])
            if limit == 1:
                if not labels or entry[:3] < labels[0][:3]:
                    labels[:] = [entry]
                return
            if len(labels) >= limit and entry[:3] >= labels[-1][:3]:
                return
            if any(e[2] == entry[2] for e in labels):
                return
            # 只比较前三项，保持 bisect_left 的左插语义，不比较回溯路径。
            entry_key = entry[:3]
            lo, hi = 0, len(labels)
            while lo < hi:
                mid = (lo + hi) // 2
                if labels[mid][:3] < entry_key:
                    lo = mid + 1
                else:
                    hi = mid
            labels.insert(lo, entry)
            if len(labels) > limit:
                labels.pop()

        groups = []
        weight_signature = tuple(sorted(weights.items()))
        selected_sources = self.r.sources if sources is None else sources
        stats["combos"] = max(stats["combos"], sum(bool(s["rounds"]) for s in selected_sources))
        for source in selected_sources:
            if self._skip(source, coupon) or source["limit"] == 0:
                continue
            configs = {(zero, zero): [(0, 0, (), ())]}
            cache_key = (source["sid"], targets, weight_signature, strict, keep) + dim_sig
            cached = cache_key in self._config_cache
            if not cached:
                phase_stats["config_cache_misses"] += 1
            if cached:
                phase_stats["config_cache_hits"] += 1
                usable, configs = self._config_cache[cache_key]
            elif config_fallback:
                reduced_key = (source["sid"], targets, weight_signature, strict, 1) + dim_sig
                if reduced_key in self._config_cache:
                    phase_stats["reduced_config_cache_hits"] += 1
                    usable, configs = self._config_cache[reduced_key]
                elif source["rounds"]:
                    phase_stats["uncached_config_skips"] += 1
                    continue
                else:
                    usable = source["food"] not in self.r.forbidden and (not strict or source["food"] in weights)
                    cv, cm = contribution(source["food"], 1)
                    merged = merge(zero, zero, cv, cm)
                    configs = {merged: [(0, 0, (), None)]} if merged is not None else {}
            elif source["rounds"]:
                usable = True
                for rnd in source["rounds"]:
                    allowed = [ch for ch in rnd["choices"] if ch["code"] not in self.r.forbidden
                               and (not strict or ch["code"] in weights) and ch["cap"]]
                    if sum(ch["cap"] for ch in allowed) < rnd["units"]:
                        usable = False
                        if notes is not None:
                            notes.add(source["name"] + "：必选内容含禁止或无法满足严格要求，未使用")
                        break
                    fills = {(0, zero, zero): [(0, 0, (), ())]}
                    for ch in allowed:
                        nxt = {}
                        for (used, vec, mins), labels in fills.items():
                            for n in range(min(ch["cap"], rnd["units"] - used) + 1):
                                cv, cm = contribution(ch["code"], n)
                                merged = merge(vec, mins, cv, cm)
                                if merged is None:
                                    continue
                                for cost, tie, signature, chosen in labels:
                                    if visited >= config_budget or not tick():
                                        return config_exhausted()
                                    pick = ((ch["code"], n),) if n else ()
                                    retain(nxt, (used + n, *merged),
                                           (cost + n * ch["diff"], tie + n * (not ch["default"]),
                                            signature + pick, chosen + pick), keep)
                        fills = nxt
                    nxt = {}
                    lookup = {ch["code"]: ch for ch in allowed}
                    for (used, rv, rm), labels in fills.items():
                        if used != rnd["units"]:
                            continue
                        for (vec, mins), existing in configs.items():
                            merged = merge(vec, mins, rv, rm)
                            if merged is None:
                                continue
                            for cost, tie, signature, rounds in existing:
                                for rc, rt, rs, chosen in labels:
                                    if visited >= config_budget or not tick():
                                        return config_exhausted()
                                    retain(nxt, merged, (cost + rc, tie + rt, signature + ((rnd["id"], rs),),
                                           rounds + ((rnd["id"], list(chosen), lookup),)), keep)
                    configs = nxt
            else:
                usable = source["food"] not in self.r.forbidden and (not strict or source["food"] in weights)
                cv, cm = contribution(source["food"], 1)
                merged = merge(zero, zero, cv, cm)
                configs = {merged: [(0, 0, (), None)]} if merged is not None else {}
            if not config_fallback and len(self._config_cache) < 512:
                self._config_cache[cache_key] = usable, configs
            if not usable or not configs:
                continue
            options = []
            for (vec, mins), labels in configs.items():
                for diff, tie, signature, rounds in labels:
                    cost = self.r.price(source, ctx) + diff
                    if not any(vec) and cost >= 0 and not (ctx != NONE_CTX and source["card_key"] == ctx):
                        continue
                    options.append((vec, mins, cost, tie, rounds))
            if not options:
                continue
            negative = any(o[2] < 0 for o in options)
            need(not negative or source["limit"] is not None or strict,
                 source["name"] + "：无限购负价商品使最低价无界")
            bound = self.r.total_units if source["limit"] is None else source["limit"]
            if not negative or strict:
                bound = min(bound, self.r.total_units)
            groups.append((source, options, bound))
        # 阶段计数按 visited 差值汇总，tick 不额外写入阶段统计。
        config_nodes = visited
        phase_stats["config_nodes"] += config_nodes
        if not groups:
            return list(seeds)[:keep] if candidates else []
        if sources is None:
            groups.sort(key=lambda g: (g[0]["card_key"] != ctx, min(o[2] for o in g[1]), g[0]["sid"]))
        # 默认项只打破同价同份数的平局，不能改变金额或少附带的排序。
        tie_scale = 1 + sum(bound * (sum(r["units"] for r in source["rounds"]) if source["rounds"] else 1)
                            for source, _, bound in groups)
        suffix = [zero] * (len(groups) + 1)
        negative_suffix = [0] * (len(groups) + 1)
        for gi in range(len(groups) - 1, -1, -1):
            _, options, bound = groups[gi]
            suffix[gi] = tuple(suffix[gi + 1][i] + bound * max(o[0][i] for o in options)
                               for i in range(len(targets)))
            negative_suffix[gi] = negative_suffix[gi + 1] + bound * min(0, min(o[2] for o in options))
        if any(v < t for v, t in zip(suffix[0], targets)):
            return list(seeds)[:keep] if candidates else []
        # key 包括已处理商品层（隐式）、覆盖向量、严格块数最小规格、是否使用本卡。
        dp = {(zero, zero, False): [(0, 0, (), None)]}
        interrupted = False
        incumbent = None
        for gi, (source, options, bound) in enumerate(groups):
            units = sum(r["units"] for r in source["rounds"]) if source["rounds"] else 1
            unit_tie = units * tie_scale
            accumulated = {key: list(labels) for key, labels in dp.items()}
            current = dp
            for quantity in range(1, bound + 1):
                nxt = {}
                for (vec, mins, card), labels in current.items():
                    for oi, (cv, cm, price, tie, rounds) in enumerate(options):
                        merged = merge(vec, mins, cv, cm)
                        if merged is None:
                            continue
                        used_card = card or (ctx != NONE_CTX and source["card_key"] == ctx)
                        if not candidates and merged == (vec, mins) and used_card == card and price >= 0:
                            continue  # 无新增覆盖、不开卡且非负价的购买，删掉不可能更差。
                        for cost, extras, signature, path in labels:
                            if not tick():
                                interrupted = True
                                break
                            item = (gi, oi)
                            position = bisect.bisect_left(signature, item)
                            next_signature = signature[:position] + (item,) + signature[position:]
                            entry = (cost + price, extras + unit_tie + tie,
                                     next_signature, (path, source, rounds))
                            retain(nxt, (*merged, used_card), entry, keep)
                        if interrupted:
                            break
                    if interrupted:
                        break
                if interrupted:
                    for key, labels in nxt.items():
                        for entry in labels:
                            retain(accumulated, key, entry, keep)
                    break
                if not nxt:
                    break
                for key, labels in nxt.items():
                    for entry in labels:
                        retain(accumulated, key, entry, keep)
                current = nxt
            dp = accumulated
            feasible = [entry[0] for (vec, _, card), labels in dp.items()
                        if all(v >= t for v, t in zip(vec, targets)) and all(vec[j] == targets[j] for j in exact)
                        and (ctx == NONE_CTX or card) for entry in labels]
            if feasible:
                incumbent = min(feasible) if incumbent is None else min(incumbent, min(feasible))
            if interrupted:
                stats["candidates_complete" if candidates else "complete"] = False
                break
            # 余下商品的各维最大供给是可达性上界；负价商品全部购买是安全费用下界。
            dp = {key: labels for key, labels in dp.items()
                  if all(v + room >= t for v, room, t in zip(key[0], suffix[gi + 1], targets))}
            if not candidates and incumbent is not None:
                dp = {key: labels for key, labels in dp.items()
                      if labels[0][0] + negative_suffix[gi + 1] <= incumbent}
        phase_stats["transition_nodes"] += visited - config_nodes
        finals = []
        for (vec, mins, card), labels in dp.items():
            if (all(v >= t for v, t in zip(vec, targets)) and all(vec[j] == targets[j] for j in exact)
                    and (ctx == NONE_CTX or card)):
                finals.extend(labels)
        finals.sort(key=lambda e: e[:3])
        if interrupted and not finals:
            return list(seeds)[:keep] if candidates else None
        plans = []
        for cost, _, _, path in finals[:keep]:
            lines = []
            while path is not None:
                path, source, rounds = path
                lines.append({"source": source, "quantity": 1, "rounds": rounds})
            grouped = {}
            for line in reversed(lines):
                key = (line["source"]["sid"], tuple((rid, tuple(chosen)) for rid, chosen, _ in line["rounds"])) if line["rounds"] else (line["source"]["sid"], ())
                if key in grouped:
                    grouped[key]["quantity"] += 1
                else:
                    grouped[key] = line
            lines = [grouped[k] for k in sorted(grouped)]
            supply = Counter()
            for line in lines:
                if line["rounds"] is None:
                    supply[line["source"]["food"]] += line["quantity"]
                else:
                    for _, chosen, _ in line["rounds"]:
                        for code, n in chosen:
                            supply[code] += n * line["quantity"]
            if owners is None:
                allocation = {}
                for d in demands:
                    remaining = d.get("min_quantity", d["quantity"])
                    taken = {}
                    for code in d["codes"]:
                        w = d.get("pieces", {}).get(code, 1)
                        n = min(supply[code], max(0, -(-remaining // w)))
                        if n:
                            taken[code] = n
                            remaining -= n * w
                    if "pieces" in d:
                        got = sum(n * d["pieces"][c] for c, n in taken.items())
                        for code in d["codes"]:
                            w = d["pieces"][code]
                            removable = min(taken.get(code, 0), max(0, (got - d.get("min_quantity", d["quantity"])) // w))
                            if removable:
                                taken[code] -= removable
                                got -= removable * w
                        taken = {c: n for c, n in taken.items() if n}
                    allocation[d["id"]] = taken
            else:
                # 重叠需求必须做整份匹配，逐需求贪心会把同一份餐品算进两个需求。
                allocation = self._allocate(demands, supply, overlap)
                if allocation is None:
                    continue
            plans.append(self._finish(ctx, demands, allocation, supply, lines, cost,
                                     any(ctx != NONE_CTX and line["source"]["card_key"] == ctx for line in lines)))
        if candidates and seeds:
            best, keys = [], set()
            for plan in list(seeds) + plans:
                self._insert(best, keys, plan, keep)
            return [entry[3] for entry in best]
        return plans

    def _search(self, ctx, demands, keep, notes, stats, best, keys, coupon=None, budget=None):
        budget = self.r.node_limit if budget is None else budget
        combos = self._combos(ctx, demands, notes, coupon)
        singles = self._singles(ctx, demands, coupon)
        units = sum(d["quantity"] for d in demands)
        stats["combos"] = max(stats["combos"], len(combos))
        if self._unreachable(demands, combos, singles):
            return  # 怎么点都凑不齐：一个方案也没有，不用穷举

        # 按“已花的钱”从少到多展开：越早摸到便宜的方案，门槛越低，后面大量更贵的组合直接不用算
        heap = [(0, (), 0)]  # (已花的钱, 已买的套餐, 下一个可选位置)
        used = 0
        monotone = (all(c["fixed"] >= 0 for c in combos)
                    and all(self.r.price(s, ctx) >= 0 for items in singles.values() for s in items))
        max_items = units + int(ctx != NONE_CTX and not self.r.strict) + sum(c["bound"] for c in combos if c["fixed"] < 0)
        while heap:
            fixed, multiset, start = heapq.heappop(heap)
            threshold = best[-1][0] if len(best) >= keep else None
            if monotone and threshold is not None and fixed > threshold:
                break
            counts = Counter(multiset)
            # 预算用尽不等于没搜完：堆里还有待展开项、或这一项还会展开出新分支才算没搜完；
            # 堆已空且这一项已到最大深度（展开它不会再产生新分支）时，正常展开完就结束。
            if used >= budget and (heap or len(multiset) < max_items):
                stats["complete"] = False
                return
            stats["nodes"] += 1
            used += 1
            plan = self.evaluate(ctx, demands, combos, singles, counts, threshold)
            self._insert(best, keys, plan, keep)
            threshold = best[-1][0] if len(best) >= keep else None
            if len(multiset) >= max_items:
                continue
            for position in range(start, len(combos)):
                combo = combos[position]
                if monotone and threshold is not None and fixed + combo["fixed"] > threshold:
                    break  # 非负完整轮次成本才是安全下界
                if counts[position] < combo["bound"]:
                    heapq.heappush(heap, (fixed + combo["fixed"], multiset + (position,), position))

    def _unreachable(self, demands, combos, singles):
        """这一堆需求怎么点都凑不齐吗：套餐全按上限买、单点全放开，也顶不掉必须餐品就算凑不齐。
        凑不齐时一个方案都出不来，也就永远没有“已知最优”能拿来剪枝，穷举它会白白吃掉几分钟，
        所以整轮搜索直接跳过（官方计价拿了这种单子也只会失败）。"""
        supply = Counter()
        for code, items in singles.items():
            supply[code] += sum(BIG if item["limit"] is None else item["limit"] for item in items)
        for combo in combos:
            for item in combo["info"]:
                for choice in item["allowed"]:
                    supply[choice["code"]] += combo["bound"] * min(item["round"]["units"], choice["cap"])
        codes = [c for d in demands for c in d["codes"]]
        if any("pieces" in d for d in demands) and len(codes) != len(set(codes)):
            # 这里只做必要条件预判，避免预算开始前枚举重叠块数的全部变体。
            # 整份分配的充分性由后续受预算限制的变体搜索证明。
            return any(not covers([d], supply) for d in demands)
        return not covers(demands, supply)

    def _coupon_foods(self, source):
        """这张券能顶掉哪些餐品：单品券看 food_code，套组券（有 rounds）看它每一轮的可选餐品。
        套组券的 food 是 None（它是“一份券换好几份餐品”），只看 food 会把它整张漏掉。"""
        if source["food"]:
            return {source["food"]}
        foods = set()
        for item in source["rounds"]:
            for choice in item["choices"]:
                foods.add(choice["code"])
        return foods

    def _coupon_fits(self, demands, coupon):
        """这张券能不能顶掉这一堆需求里的某个餐品。
        顶不掉时这一轮搜索和“不用券”完全一样（套餐与单点都没变），再算一遍纯属重复。"""
        if coupon is None:
            return True
        codes = {code for d in demands for code in d["codes"]}
        for source in self.r.sources:
            if source["coupon"] and (coupon == ALL_COUPONS or source["sid"] == coupon):
                if self._coupon_foods(source) & codes:
                    return True
                minimum = source["current"] + source["coupon"]["paid"]
                for rnd in source["rounds"]:
                    left = rnd["units"]
                    for ch in sorted(rnd["choices"], key=lambda ch: ch["diff"]):
                        n = min(left, ch["cap"])
                        minimum += n * ch["diff"]
                        left -= n
                if not self.r.strict and minimum < 0:
                    return True
        return False

    def baseline(self):
        """全单点：不用任何套餐与券。"""
        if not any("pieces" in d for d in self.r.demands):
            demands = [{**d, "quantity": d.get("min_quantity", d["quantity"])} for d in self.r.demands]
            singles = self._singles(NONE_CTX, demands)
            plan = self.evaluate(NONE_CTX, demands, [], singles, Counter())
            return self._materialize(plan) if plan is not None else None
        codes = [c for d in self.r.demands for c in d["codes"]]
        if len(codes) == len(set(codes)):
            stats = {"nodes": 0, "combos": 0}
            result = self._dp(NONE_CTX, self.r.demands, None, 1, None, stats,
                              sources=[s for s in self.r.sources if not s["rounds"] and not s["coupon"]])
            if result is not None:
                return self._materialize(result[0]) if result else None
        options = []
        for variant in demand_variants(self.r.demands, lazy=True):
            demands = self._demands(frozenset(), variant)
            singles = {food: [s for s in items if not s["coupon"]] for food, items in self._singles(NONE_CTX, demands).items()}
            plan = self.evaluate(NONE_CTX, demands, [], singles, Counter())
            if plan is not None:
                options.append(self._materialize(plan))
        return min(options, key=lambda p: (p["cost"], p["key"])) if options else None

    def evaluate(self, ctx, demands, combos, singles, counts, max_cost=None, required_single=None):
        """同时优化需求和全部必选槽位；虚拟单点的未使用流量直接进入余量槽。"""
        r = self.r
        if (ctx != NONE_CTX and required_single is None
                and not any(count and combos[i]["source"]["card_key"] == ctx for i, count in counts.items())):
            # 当前套餐未激活本卡：逐一强制购买一份本卡单品，不能先取无卡最优再丢弃。
            options = []
            for items in singles.values():
                for item in items:
                    if item["card_key"] == ctx:
                        candidate = self.evaluate(ctx, demands, combos, singles, counts, max_cost, item)
                        if candidate is not None:
                            options.append(candidate)
            return min(options, key=lambda p: (p["cost"], p["extra_count"])) if options else None
        n_units = sum(d["quantity"] for d in demands)
        slots = sum(count * sum(i["round"]["units"] for i in combos[index]["info"])
                    for index, count in counts.items())
        if r.strict and slots > n_units:
            return None
        negative_caps = sum(item["limit"] or 0 for items in singles.values() for item in items
                            if r.price(item, ctx) < 0 and not r.strict)
        dummy = n_units + negative_caps
        required = slots + dummy + int(required_single is not None)
        scale = required + 1  # 平局项永远不能改变一分钱的排序
        flow = MinCostFlow()
        source, sink, extra = flow.node(), flow.node(), flow.node()
        flow.edge(extra, sink, required - n_units, 0)
        code_nodes = {}

        def code_node(code):
            if code not in code_nodes:
                code_nodes[code] = flow.node()
                if not r.strict:
                    flow.edge(code_nodes[code], extra, required, 0)
            return code_nodes[code]

        demand_edges = []
        for d in demands:
            node = flow.node()
            flow.edge(node, sink, d["quantity"], 0)
            for code in d["codes"]:
                demand_edges.append((d["id"], code, flow.edge(code_node(code), node, d["quantity"], 0)))
        optional = flow.node()
        flow.edge(source, optional, dummy, 0)
        flow.edge(optional, extra, dummy, 0)
        single_edges = []
        if required_single is not None:
            flow.edge(source, code_node(required_single["food"]), 1, 0)
        for food, items in singles.items():
            for item in items:
                price = r.price(item, ctx)
                need(price >= 0 or item["limit"] is not None or r.strict,
                     item["name"] + "：无限购负价商品使最低价无界")
                cap = dummy if item["limit"] is None else item["limit"]
                if item is required_single:
                    cap = max(0, cap - 1)
                single_edges.append((item, flow.edge(optional, code_node(food), cap, price * scale)))
        round_edges = []
        for index, count in counts.items():
            combo = combos[index]
            for ri, item in enumerate(combo["info"]):
                node = flow.node()
                capacity = count * item["round"]["units"]
                flow.edge(source, node, capacity, 0)
                for choice in item["allowed"]:
                    cost = choice["diff"] * scale + (0 if choice["default"] else 1)
                    ref = flow.edge(node, code_node(choice["code"]), count * choice["cap"], cost)
                    round_edges.append((index, ri, choice, ref))
        total, _ = flow.run(source, sink, required)
        if total < required:
            return None

        allocation = {}
        for demand_id, code, ref in demand_edges:
            used = flow.used(ref)
            if used:
                allocation.setdefault(demand_id, {})[code] = used
        single_counts = Counter()
        if required_single is not None:
            single_counts[required_single["sid"]] = 1
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
        if max_cost is not None and cost > max_cost:
            return None
        return self._finish(ctx, demands, allocation, supply, lines, cost, uses_card)

    def _finish(self, ctx, demands, allocation, supply, lines, cost, uses_card):
        used = Counter()
        for codes in allocation.values():
            used.update(codes)
        extras = supply - used
        core = sorted((d, c, n) for d, codes in allocation.items() for c, n in codes.items())
        # 只在候选入榜/最终返回时生成官方请求与展示文本。
        return {"ctx": ctx, "cost": cost, "allocation": allocation,
                "supply": {c: n for c, n in supply.items() if n}, "extras": dict(extras), "extra_count": sum(extras.values()),
                "uses_card": uses_card, "core": core, "_demands": demands, "_lines": lines}

    def _materialize(self, plan):
        if "payload" not in plan:
            plan["payload"] = self.payload(plan["ctx"], plan["_lines"])
            plan["key"] = digest(plan["payload"])
            plan["display"] = self.display(plan["_lines"])
            plan["lines"] = self.brief(plan["_lines"])
        return plan

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
            if line["rounds"] is not None:  # 每份套餐里有什么
                contents = [{"code": code, "name": lookup[code]["name"], "quantity": quantity}
                            for _, chosen, lookup in line["rounds"] for code, quantity in chosen]
            else:
                contents = [{"code": source["food"], "name": self.r.name(source["food"]), "quantity": 1}]
            result.append({"kind": kind, "name": name, "quantity": line["quantity"], "contents": contents})
        return result

    def payload(self, ctx, lines):
        items = []
        for line in lines:
            source = line["source"]
            # 券商品的 code 可能只是内部名（真实商品编码写在 food_code 里），
            # 官方计价只认真实商品编码，传内部名会被整单拒绝（600012「促销规则不支持该商品」）。
            base = {"productCode": source["food"] or source["code"]}
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
    coupon_bests = {}
    for ctx in r.contexts:
        plans, stats, per_coupon = solver.solve(ctx, notes=notes)
        results[ctx] = plans
        search[ctx] = stats
        for sid, item in per_coupon.items():
            if sid not in coupon_bests or item["cost"] < coupon_bests[sid]["cost"]:
                coupon_bests[sid] = item
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
    for item in sorted(coupon_bests.values(), key=lambda p: (p["cost"], p["key"])):
        take("best", item)  # 每张券各自的最低价、以及多券同单的最低价都送去官方核价
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
                    alt = solver.solve(ctx, bans=frozenset({(demand_id, code)}), keep=1)[0]
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

    priority = {"best": 0, "baseline": 1, "upgrade": 2, "flavor": 3, "lean": 4, "next": 5}
    ordered = sorted(chosen, key=lambda entry: (min(priority[x] for x in entry[0]), entry[1]["cost"]))
    reserved = []

    def reserve(entry):
        if entry is not None and entry not in reserved and len(reserved) < r.max_quotes:
            reserved.append(entry)

    # 先保留本地最低候选；未知卡费不能挤掉不开卡最低，基准只在还有名额时保留。
    reserve(min(chosen, key=lambda entry: entry[1]["cost"]) if chosen else None)
    reserve(next((entry for entry in ordered if entry[1]["ctx"] == NONE_CTX and "best" in entry[0]), None))
    reserve(next((entry for entry in ordered if "baseline" in entry[0]), None))
    for entry in ordered:
        reserve(entry)
    quotes_truncated = len(chosen) > len(reserved)
    chosen = reserved
    paid_by_coupon = {s["coupon"]["couponId"]: s["coupon"]["paid"] for s in r.sources if s["coupon"]}
    quotes = []
    for i, (roles, item) in enumerate(chosen, 1):
        # 外购券（抖音/快手买的）在麦当劳这单付 ¥0，钱早就付过了：官方报价里没有，要单独加回去
        prepaid = sum(paid_by_coupon.get(it.get("couponId"), 0) * it.get("quantity", 1)
                      for it in item["payload"]["items"] if it.get("couponId"))
        quotes.append({"id": "q%d" % i, "roles": roles, "context": item["ctx"], "estimate_cents": item["cost"],
                       "estimate": money(item["cost"]), "prepaid_cents": prepaid, "prepaid": money(prepaid),
                       "display": item["display"], "lines": item["lines"], "payload": item["payload"],
                       "allocation": item["allocation"], "extras": item["extras"], "extra_count": item["extra_count"],
                       "supply": item["supply"], "core": item["core"], "minimal": solver.is_minimal(item)})
    local_complete = all(stats["complete"] for stats in search.values())
    candidates_complete = all(stats.get("candidates_complete", stats["complete"]) for stats in search.values())
    if not quotes and local_complete:
        notes.add("按现有菜单数据凑不齐必须餐品：检查 demands.codes 是否都在 products 里有单点或套餐来源")
    if not local_complete:
        notes.add("搜索达到 node_limit，最低价尚未证明；仅比较已找到的候选，可提高 node_limit")
    # 备选完备性只留在 search/verification 中，不向点餐用户专门提示。
    if quotes_truncated:
        notes.add("核价名额不足，已优先保留本地最低、不开卡最低和单点基准；其余候选未全部核价")
    effort = dict(r.effort)
    effort.setdefault("details_checked", sum(1 for s in r.sources if s["rounds"]))
    effort["combos_usable"] = max(stats["combos"] for stats in search.values())
    effort["combinations"] = sum(stats["nodes"] for stats in search.values())
    effort["card_contexts"] = len(r.contexts) - 1
    mode = DININGS[r.dining] if r.dining else MODES.get(r.context.get("beType"), "")
    if r.address and r.context.get("orderType") == 2:
        mode += "（送到" + r.address + "）"
    store = {"name": r.store_name, "mode": mode, "note": r.store_note}
    return {"schema": "mcd-deal-detective/plan@1", "store": store, "effort": effort, "demands": r.demands, "strict": r.strict,
            "unused_coupons": r.unused_coupons,
            "forbidden_codes": sorted(r.forbidden), "contexts": {ctx: r.card_requests.get(ctx) for ctx in r.contexts},
            "names": r.names, "ref_prices": r.ref_prices, "estimated_prices": r.estimated_prices,
            "card_value_cents": r.card_value, "quotes": quotes, "search": search, "notes": sorted(notes),
            "verification": {"local_minimum_complete": local_complete, "candidates_complete": candidates_complete,
                             "quote_candidates_truncated": quotes_truncated, "scope": "selected_candidates"},
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


def quote_fees(result, price):
    """官方计价里的配送费/打包费/餐具费（整数分，已含在 data.price 里）。"""
    fees = {}
    for field, label in FEE_FIELDS:
        value = result.get(field)
        if value is None:
            continue
        need(type(value) is int and value >= 0, field + " 必须是官方返回的整数分")
        if value:
            fees[label] = value
    need(sum(fees.values()) <= price, "配送/打包/餐具费合计超过整单实付，回显异常")
    return fees


def quote_coupon_ids(quote):
    """这一单用了哪几张券（按 couponId 去重排序）。"""
    return sorted({str(i["couponId"]) for i in quote["payload"]["items"] if i.get("couponId")})


def is_multi_coupon_error(reason):
    """官方计价接口“暂不支持多张券使用”（600022）。小程序能把两张券放进一单，所以这条报错不算数。"""
    return any(mark in reason for mark in MULTI_COUPON_MARKS)


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
    extra = [p for p in products if str(p.get("productCode")) not in ordered
             and "服务费" not in str(p.get("productName") or "")]  # 打包服务费等行已计入 packingPrice/data.price
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


def purchase_blocks(entry, plan_data):
    """把方案按“怎么买”分块：每个套餐一块（列出里面的餐品）、单点合成一块、用券一块、随单开卡一块。
    餐品标 demanded=True 表示满足用户点名需求（先算单点、再算券、最后算套餐，套餐多出来的就是附带）。"""
    remaining = Counter()
    for codes in entry["allocation"].values():
        remaining.update(codes)
    order = {"single": 0, "coupon": 1, "combo": 2}
    lines = sorted(entry.get("lines") or [], key=lambda l: order.get(l["kind"], 3))
    blocks, singles = [], []
    for line in lines:
        items = []
        for content in line.get("contents") or []:
            total = content["quantity"] * line["quantity"]
            wanted = min(total, remaining[content["code"]])
            remaining[content["code"]] -= wanted
            if wanted:
                items.append({"name": content["name"], "quantity": wanted, "demanded": True})
            if total - wanted:
                items.append({"name": content["name"], "quantity": total - wanted, "demanded": False})
        merged = {}
        for item in items:  # 同一份套餐里两轮选了同一样东西时合并显示（麦辣鸡腿汉堡×2）
            key = (item["name"], item["demanded"])
            merged[key] = merged.get(key, 0) + item["quantity"]
        items = [{"name": n, "quantity": q, "demanded": d} for (n, d), q in merged.items()]
        if line["kind"] == "single":
            singles.extend(items)
        else:
            blocks.append({"kind": line["kind"], "title": times(line["name"], line["quantity"], True), "items": items})
    blocks.sort(key=lambda b: order[b["kind"]], reverse=True)  # 套餐在前、券其次
    if singles:
        merged = {}
        for item in singles:
            key = (item["name"], item["demanded"])
            merged[key] = merged.get(key, 0) + item["quantity"]
        blocks.append({"kind": "single", "title": "", "items": [{"name": n, "quantity": q, "demanded": d} for (n, d), q in merged.items()]})
    if entry.get("card"):
        blocks.append({"kind": "card", "title": "%s %s" % (entry["card"]["name"], entry["card"]["fee"]), "items": []})
    return blocks


BLOCK_TAGS = {"combo": ("套餐", "background:var(--color-background-info);color:var(--color-text-info);"),
              "coupon": ("用券", "background:var(--color-background-warning);color:var(--color-text-warning);"),
              "single": ("单点", "background:var(--color-background-secondary);color:var(--color-text-secondary);"),
              "card": ("随单开卡", "background:var(--color-background-secondary);color:var(--color-text-secondary);")}


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
    if entry.get("estimated"):  # 多券餐品估价，附加费只有成功回显后才补入
        notes.append("多张券一起用，官方计价接口不给算，按各券券后价合计及其余餐品菜单价；以下单页为准")
        notes.extend(entry.get("pricing_notes") or [])
    prepaid = entry.get("prepaid_cents") or 0
    if prepaid:  # 券购入成本只计算一次；费用未知时不把已知部分称为整单付款
        if entry.get("fees_complete", True):
            notes.append("已含你买券花的%s；麦当劳这单付%s" % (money(prepaid), money(entry["price"] - prepaid)))
        else:
            notes.append("已含你买券花的%s；未核实的附加费另计" % money(prepaid))
    fees = entry.get("fees") or {}
    if fees:
        notes.append("含" + "、".join(label + money(v) for label, v in fees.items()))
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
    if not entry.get("fees_complete", True):
        return notes  # 缺费金额不是完整实付，不能据此宣称省了多少
    # 和“同样这些东西全按单点买”比：同口味、同数量（含多送的），才是这个方案真正省了多少
    same, estimated, unknown = single_equivalent(entry, plan_data["ref_prices"], plan_data.get("estimated_prices") or {})
    paid = entry["price"] - sum(fees.values())  # 配送/打包费单点也要付，比较时只比餐品部分（含卡费）
    if same > paid:
        if unknown:  # 还有餐品查不到单点价：只算已知部分，是下限
            names = "、".join(plan_data["names"].get(c, c) for c in unknown)
            notes.append("同样这些单点至少要%s（%s未计入），省%s以上" % (money(same), names, money(same - paid)))
        else:
            prefix = "约" if estimated else ""
            notes.append("同样这些单点%s要%s，省%s" % (prefix, money(same), money(same - paid)))
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
    else:
        parts.append("翻了本店全部菜单")
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
        complete = all(s.get("complete", False) for s in plan_data.get("search", {}).values())
        if any(s.get("engine") == "weighted_dp" for s in plan_data.get("search", {}).values()):
            parts.append("用加权覆盖比较单点、套餐换选和券，共计算 %d 个状态转移" % effort["combinations"])
        else:
            parts.append("本地%s比较了 %d 种套餐组合" % ("精确" if complete else "已", effort["combinations"]))
    else:
        parts.append("店里没有能装下这些餐品的套餐，只能单点")
    parts.append("挑出 %d 个候选向麦当劳官方核价（%d 个有效）" % (submitted, valid))
    return "；".join(parts)


def title_line(plan_data):
    store = plan_data.get("store") or {}
    title = "**" + " · ".join(x for x in ("🕵️ 麦侦探", store.get("name"), store.get("mode")) if x) + "**"
    return title + ("\n\n" + store["note"] if store.get("note") else "")


def render_table(cards):
    """不支持卡片组件的客户端用的 Markdown 对比表。"""
    if not cards:
        return ""
    lines = ["| | " + " | ".join("**方案%d · %s**" % (c["no"], c["label"]) for c in cards) + " |",
             "|:--|" + ":--|" * len(cards),
             "| 实付 | " + " | ".join("**%s**" % c["price"] for c in cards) + " |"]
    for i in range(max(len(c["blocks"]) for c in cards)):
        cells = []
        for c in cards:
            if i < len(c["blocks"]):
                block = c["blocks"][i]
                foods = "、".join(("**%s**" if it["demanded"] else "%s") % times(it["name"], it["quantity"], True) for it in block["items"])
                head = "【%s】%s" % (BLOCK_TAGS[block["kind"]][0], block["title"])
                cells.append(head + ("：" + foods if foods and block["title"] else foods))
            else:
                cells.append("")
        lines.append("| %s | %s |" % ("怎么买" if i == 0 else "", " | ".join(cells)))
    lines.append("| 备注 | " + " | ".join("；".join(c["notes"]) or "—" for c in cards) + " |")
    return "\n".join(lines)


def render_cards(cards, plan_data):
    """方案卡片 HTML 片段（WorkBuddy 内联组件），每张卡：特点标签、实付、按“套餐/用券/单点/随单开卡”分块的餐品（点名的加粗、附带灰色）、备注。"""
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
        for block in c["blocks"]:
            tag, tag_style = BLOCK_TAGS[block["kind"]]
            head = ('<div style="display:flex;align-items:center;gap:6px;">'
                    '<span style="%sfont-size:11px;padding:1px 6px;border-radius:var(--border-radius-md);white-space:nowrap;">%s</span>'
                    % (tag_style, tag))
            if block["title"]:
                head += '<span style="font-size:13px;font-weight:500;color:var(--color-text-primary);">%s</span>' % escape(block["title"])
            foods = []
            for item in block["items"]:
                text = escape(times(item["name"], item["quantity"], True))
                if item["demanded"]:
                    foods.append('<span style="font-weight:500;color:var(--color-text-primary);">%s</span>' % text)
                else:
                    foods.append('<span style="color:var(--color-text-tertiary);">%s</span>' % text)
            body = ""
            if foods and block["title"]:
                body = ('<div style="font-size:13px;line-height:1.6;margin:4px 0 0 4px;padding-left:8px;'
                        'border-left:2px solid var(--color-border-tertiary);">%s</div>' % "<br>".join(foods))
                head += '</div>'
            elif foods:
                head += '<span style="font-size:13px;line-height:1.6;">%s</span></div>' % "、".join(foods)
            else:
                head += '</div>'
            rows.append('<div style="margin-top:10px;">%s%s</div>' % (head, body))
        parts.append(
            '<div style="background:var(--color-background-primary);%sborder-radius:var(--border-radius-lg);padding:1rem 1.25rem;">' % box
            + '<div style="display:inline-block;%sfont-size:12px;padding:2px 8px;border-radius:var(--border-radius-md);margin-bottom:8px;">方案%d · %s</div>'
            % (badge, c["no"], escape(c["label"]))
            + '<div style="font-size:24px;font-weight:500;">%s</div>' % escape(c["price"])
            + '<div style="margin-top:4px;">%s</div>' % "".join(rows)
            + ('<div style="font-size:12px;line-height:1.5;color:var(--color-text-tertiary);margin-top:12px;padding-top:8px;'
               'border-top:0.5px solid var(--color-border-tertiary);">%s</div>' % "<br>".join(escape(n) for n in c["notes"]) if c["notes"] else "")
            + '</div>')
    parts.append('</div></div>')
    return "\n".join(parts)


LEGEND = "加粗是你点名要的，灰色是套餐附带多送的。"


def render_footer(cards, conclusion, unused=None):
    """方案卡片**之后**的内容：结论、用不上的券、价格说明。
    图例（LEGEND）要紧贴表格、在卡片之前单独输出，不放在这里。"""
    lines = []
    if conclusion:
        if lines:
            lines.append("")
        lines.append("💡 **结论**：" + conclusion)
    if unused:
        if lines:
            lines.append("")
        lines.append("🎫 **这单用不上的券**：" + "；".join(
            "「%s」%s" % (item["title"], item["reason"]) if item["reason"] else "「%s」" % item["title"] for item in unused))
    lines.append("")
    if cards:
        if any(card.get("estimated") for card in cards):
            lines.append("已核价方案用官方试算；多券方案为标注的估价，未知附加费另计，实际以下单页显示为准。")
        else:
            lines.append("价格是麦当劳官方刚刚的试算，实际以下单页显示为准。")
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
    valid, excluded, unquoted, pending_estimates = [], [], [], []
    for quote_id, quote in quotes.items():
        result = results.get(quote_id)
        if result is None:
            unquoted.append(quote_id)
            continue
        if result.get("error"):
            reason = str(result["error"])
            if len(quote_coupon_ids(quote)) > 1 and is_multi_coupon_error(reason):
                # 接口限制不作废多券；先等同一开卡情况的有效报价，补齐可知费用再比较。
                pending_estimates.append(quote)
                continue
            excluded.append({"id": quote_id, "reason": "官方计价失败：" + reason})
            continue
        try:
            price, card = check_quote(quote, result)
            fees = quote_fees(result, price)
        except InputError as error:
            excluded.append({"id": quote_id, "reason": str(error)})
            continue
        price += quote.get("prepaid_cents", 0)  # 外购券的钱早就付过了，官方这单收 ¥0，得加回来才算真实成本
        valid.append({**quote, "price": price, "card": card, "fees": fees})
    # 只有成功回显能提供附加费证据；不从另一开卡情况或另一条估价借卡费。
    verified = list(valid)
    for quote in pending_estimates:
        peers = [entry for entry in verified if entry["context"] == quote["context"]]
        delivery = quote["payload"].get("orderType") == 2
        known_fields = {}
        for field, label in FEE_FIELDS:
            values = [results[entry["id"]][field] for entry in peers if field in results[entry["id"]]]
            if values:
                known_fields[label] = max(values)
        complete_fee_vectors = [tuple(results[entry["id"]][field] for field, _ in FEE_FIELDS)
                                for entry in peers if all(field in results[entry["id"]] for field, _ in FEE_FIELDS)]
        fee_known = not delivery or bool(complete_fee_vectors) and len(set(complete_fee_vectors)) == 1
        card_known = quote["context"] == NONE_CTX or bool(peers)
        cards = [entry["card"] for entry in peers if entry["card"]]
        card = max(cards, key=lambda item: item["fee_cents"]) if cards else None
        if cards and len({entry["card"]["fee_cents"] if entry["card"] else 0 for entry in peers}) > 1:
            card_known = False
        fees = {label: value for label, value in known_fields.items() if value}
        price = quote["estimate_cents"] + sum(fees.values()) + (card["fee_cents"] if card else 0)
        pricing_notes = []
        if not fee_known:
            pricing_notes.append("配送/打包/餐具费未核实；所示金额仅含餐品和已知费用，不参与最低实付排序")
        if not card_known:
            pricing_notes.append("随单开卡的卡费未核实；所示金额未含未知卡费，不参与最低实付排序")
        if known_fields or card:
            pricing_notes.append("附加费参考同一开卡情况的成功报价，可能随餐品/优惠变化；以下单页为准")
        valid.append({**quote, "price": price, "card": card, "fees": fees, "estimated": True,
                      "fees_complete": bool(fee_known and card_known), "pricing_notes": pricing_notes})
    hints = []
    repeats = {}
    for item in excluded:
        repeats[item["reason"]] = repeats.get(item["reason"], 0) + 1
    for reason, count in sorted(repeats.items(), key=lambda kv: -kv[1]):
        if count >= 2:
            hints.append("%d 个候选被官方用同一原因拒绝（%s）。常见原因是套餐选项漏写了 maxQuantity"
                         "（如“最多可选1份”），补进 products_*.json 后重跑 plan 更准。" % (count, reason))
    refs = plan_data["ref_prices"]
    worth = lambda e: sum(refs.get(c, 0) * n for c, n in e["supply"].items())
    # 同价时：附带少的优先，再优先不开卡，再优先餐品原价更高的（同价拿到更值钱的口味）
    order = lambda e: (e["price"], e["extra_count"], e["context"] != NONE_CTX, -worth(e), e["id"])
    incomplete_estimates = sorted((entry for entry in valid if not entry.get("fees_complete", True)), key=order)
    eligible = sorted((entry for entry in valid if entry.get("fees_complete", True)), key=order)
    valid_count = len(valid)
    valid = eligible
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
    # 缺费估价继续展示，但不能抢走有效整单报价的“最低实付”位置。
    if incomplete_estimates:
        candidate = {**incomplete_estimates[0], "label": "估价备选（费用未全）"}
        if len(recommendations) >= MAX_RECOMMENDATIONS:
            recommendations[-1] = candidate
        else:
            recommendations.append(candidate)
    output = []
    for i, entry in enumerate(recommendations, 1):
        output.append({"no": i, "label": entry["label"], "quote_id": entry["id"], "price_cents": entry["price"],
                       "price": money(entry["price"]), "summary": order_summary(entry), "items": food_rows(entry, plan_data), "blocks": purchase_blocks(entry, plan_data),
                       "notes": card_notes(entry, plan_data, baseline, recommendations[0]), "context": entry["context"],
                       "card": entry["card"], "fees": entry["fees"], "estimated": bool(entry.get("estimated")),
                       "fees_complete": entry.get("fees_complete", True), "prepaid_cents": entry.get("prepaid_cents", 0),
                       "upgrade": entry.get("upgrade"), "display": entry["display"],
                       "allocation": entry["allocation"], "extras": entry["extras"], "payload": entry["payload"]})
    card_names = []
    for ctx in plan_data["contexts"]:
        if ctx != NONE_CTX:
            named = next((e["card"]["name"] for e in valid if e["context"] == ctx and e["card"]), None)
            card_names.append(named or "卡")
    effort = effort_text(plan_data, len(results), valid_count, card_names)
    conclusion = None
    if recommendations and baseline is not None:
        saving = baseline["price"] - recommendations[0]["price"]
        if recommendations[0]["id"] == baseline["id"] or saving <= 0:
            conclusion = "本次拿到完整价格的候选中，直接单点最省。"
        else:
            conclusion = "只点你要的、全按最便宜的单点买要 %s；最省的方案 %s，少花 **%s**。" % (
                money(baseline["price"]), money(recommendations[0]["price"]), money(saving))
    elif recommendations:
        conclusion = ("目前只有费用未全的估价备选，暂时不能确定最低实付。" if not recommendations[0].get("fees_complete", True)
                      else "全单点没拿到官方价，暂时算不出省了多少。")
    title = title_line(plan_data)
    table = render_table(output)
    cards_html = render_cards(output, plan_data)
    # 输出顺序：地址说明（title）→ 侦查过程 → 图例（legend）→ 方案表格 → 其余说明（footer）
    effort_line = "🔍 **侦查过程**：" + effort + "。"
    legend = LEGEND if output else None
    footer = render_footer(output, conclusion, plan_data.get("unused_coupons") or None)
    warnings = user_notes(plan_data.get("notes"))
    if excluded or unquoted:
        warnings.append("部分候选未获得有效报价；最低实付仅比较本次拿到完整价格的方案")
    if warnings:
        footer += "\n\n" + "；".join(warnings) + "。"
    message = "\n\n".join(x for x in (title, effort_line, legend, table, footer) if x)
    return {"schema": "mcd-deal-detective/picked@1", "recommendations": output,
            "baseline": {"quote_id": baseline["id"], "price_cents": baseline["price"], "price": money(baseline["price"])} if baseline else None,
            "valid_count": valid_count, "excluded": excluded, "unquoted": sorted(unquoted), "hints": hints, "effort": effort, "conclusion": conclusion,
            "verification": {"scope": "selected_candidates", "official_global_minimum_proven": False,
                             "local_minimum_complete": plan_data.get("verification", {}).get("local_minimum_complete", False),
                             "estimated_candidates": len(pending_estimates), "incomplete_fee_candidates": len(incomplete_estimates)},
            "title": title, "effort_line": effort_line, "legend": legend, "cards_html": cards_html, "table_markdown": table, "footer": footer,
            "message": message}


# ---------------------------------------------------------------- CLI

def collect_quotes(path, folder):
    """读取 quotes.json 以及同目录的 quotes_*.json（分批写入的计价结果），按 id 合并；plan_file 默认同目录 plan.json。"""
    paths = [path] if os.path.exists(path) else []
    paths += sorted(p for p in glob.glob(os.path.join(folder, "quotes_*.json")) if p not in paths)
    need(bool(paths), "没有找到 quotes.json 或 quotes_*.json")
    merged = {"plan_file": os.path.join(folder, "plan.json"), "results": []}
    by_id = {}
    for p in paths:
        part = read_json(p)
        if isinstance(part, list):
            part = {"results": part}
        need(isinstance(part, dict), os.path.basename(p) + " 必须是 JSON 对象或数组")
        if part.get("plan_file"):
            merged["plan_file"] = part["plan_file"]
        for result in part.get("results") or []:
            if isinstance(result, dict) and result.get("id"):
                by_id[result["id"]] = result
    merged["results"] = list(by_id.values())
    return merged


def reject_constant(value):
    raise InputError("JSON 不允许 NaN/Infinity")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, parse_constant=reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "pick"))
    parser.add_argument("target", nargs="?", help="运行目录（推荐）或输入文件")
    parser.add_argument("--input")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        source = args.input or args.target
        need(bool(source), "请给出运行目录，如 optimizer.py plan <RUN>")
        if os.path.isdir(source):
            folder = os.path.abspath(source)
            args.input = os.path.join(folder, "request.json" if args.command == "plan" else "quotes.json")
        else:
            folder = os.path.dirname(os.path.abspath(source))
            args.input = source
        args.output = args.output or os.path.join(folder, "plan.json" if args.command == "plan" else "picked.json")
        if args.command == "plan":
            result = plan(merge_fragments(read_json(args.input), folder))
        else:
            result = pick(collect_quotes(args.input, folder))
    except (InputError, OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    print(console_text(args.command, result, args.output))
    return 0


def user_notes(notes):
    """只隐藏备选未完备，最低搜索与费用风险仍然提示。"""
    return [note for note in notes or [] if not str(note).startswith("备选搜索未完备")]


def console_text(command, result, output):
    """直接打印下一步要用的内容，调用方不必再读输出文件。"""
    if command == "plan":
        lines = ["已写入 %s：%d 个候选。逐个把下面的 payload 原样提交 calculate-price：" % (output, len(result["quotes"]))]
        for note in user_notes(result["notes"]):
            lines.append("提示：" + note)
        for q in result["quotes"]:
            lines.append("%s %s" % (q["id"], json.dumps(q["payload"], ensure_ascii=False, separators=(",", ":"))))
        return "\n".join(lines)
    lines = ["已写入 %s：%d 个方案，%d 个报价作废，%d 个未计价。" % (
        output, len(result["recommendations"]), len(result["excluded"]), len(result["unquoted"]))]
    for item in result["excluded"]:
        lines.append("作废 %s：%s" % (item["id"], item["reason"]))
    for hint in result.get("hints") or []:
        lines.append("提示：" + hint)
    lines += ["===== TITLE =====", result["title"], "===== EFFORT =====", result["effort_line"]]
    if result.get("legend"):
        lines += ["===== LEGEND =====", result["legend"]]
    lines += ["===== CARDS_HTML =====", result["cards_html"],
              "===== FOOTER =====", result["footer"], "===== END ====="]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
