#!/usr/bin/env python3
"""麦侦探求解器离线测试（标准库 unittest，不联网）。"""
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimizer as opt  # noqa: E402

CONTEXT = {"storeCode": "1960649", "orderType": 1, "beType": 1, "needTableware": False}
MOD_1440 = {"items": [{"maxValues": 2, "minValues": 0, "values": [
    {"code": "100136", "name": "麦香鸡酱", "selectedQuantity": 1, "selectedKey": "0-1", "unselectedKey": "0-0"},
    {"code": "120512", "name": "切块生菜", "selectedQuantity": 1, "selectedKey": "0-1", "unselectedKey": "0-0"}]}]}


def single(code, name, price, original=None, **extra):
    item = {"code": code, "name": name, "currentPrice": price, "originalPrice": original or price}
    item.update(extra)
    return item


def choice(code, name, diff="+ ¥0", default=0, **extra):
    item = {"code": code, "name": name, "diffPrice": diff, "maxQuantity": -1, "isDefault": default}
    item.update(extra)
    return item


def combo(code, name, price, rounds, original=None, **extra):
    item = single(code, name, price, original, **extra)
    item["rounds"] = [{"id": i + 1, "minQuantity": units, "maxQuantity": units, "choices": choices}
                      for i, (units, choices) in enumerate(rounds)]
    return item


def eight_piece():
    """西安长和卜蜂 1960649 真实菜单与详情（2026-10-11，每轮保留必须相关选项与最低差价选项）。"""
    return combo("9900011056", "全明星双人分享餐八件套", "49.9", [
        (1, [choice("1440", "麦辣鸡腿汉堡", default=1, supportModify=True, modification=MOD_1440),
             choice("521816", "酥酥多笋卷")]),
        (1, [choice("1406", "板烧鸡腿堡", default=1), choice("521952", "龙焰鸡腿堡", "+ ¥1"),
             choice("1440", "麦辣鸡腿汉堡", supportModify=True, modification=MOD_1440)]),
        (1, [choice("515106", "麦乐鸡4块", default=1), choice("1700", "麦辣鸡翅2块", "+ ¥3.5")]),
        (1, [choice("1700", "麦辣鸡翅2块", default=1), choice("505416", "麦麦脆汁鸡-琵琶腿")]),
        (2, [choice("4810", "中薯条", default=1)]),
        (2, [choice("3050", "可乐中杯", default=1)]),
    ], original="114.5", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "880"}, canWithOrder=True)


def store_request(**extra):
    request = {
        "context": dict(CONTEXT),
        "demands": [
            {"id": "burger", "label": "汉堡", "quantity": 2, "codes": ["1440", "1406"]},
            {"id": "nugget", "label": "麦乐鸡", "quantity": 1, "codes": ["1401", "515106"]},
            {"id": "wing", "label": "麦辣鸡翅", "quantity": 1, "codes": ["1700"]},
            {"id": "fries", "label": "薯条", "quantity": 1, "codes": ["4810"]},
        ],
        "products": [
            single("1440", "麦辣鸡腿汉堡", "22.5"), single("1406", "板烧鸡腿堡", "23.5"),
            single("1401", "麦乐鸡5块", "14"), single("1700", "麦辣鸡翅2块", "15"),
            single("4810", "中薯条", "13.5"), eight_piece(),
        ],
        "reference_prices": {"3050": "9.5", "515106": "11.5"},
    }
    request.update(extra)
    return request


def quote_result(quote, price, card=None):
    products = []
    for code, quantity in sorted(opt.aggregate(quote["payload"]["items"], "items").items()):
        products.append({"productCode": code, "productName": code, "quantity": quantity, "subtotal": 0})
    if card:
        products.append({"productCode": card[0], "productName": card[1], "quantity": 1, "subtotal": card[2]})
    return {"id": quote["id"], "price": price, "productList": products}


def run_pick(plan_data, results):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "plan.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(plan_data, handle, ensure_ascii=False)
        return opt.pick({"plan_file": path, "results": results})


def best_quote(plan, ctx="none"):
    return [q for q in plan["quotes"] if "best" in q["roles"] and q["context"] == ctx][0]


class MoneyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(opt.yuan_to_cents("49.9", "x"), 4990)
        self.assertEqual(opt.yuan_to_cents("¥ 9.90", "x"), 990)
        self.assertEqual(opt.yuan_to_cents(13, "x"), 1300)
        self.assertEqual(opt.diff_to_cents("+ ¥0", "x"), 0)
        self.assertEqual(opt.diff_to_cents("- ¥1", "x"), -100)
        self.assertEqual(opt.diff_to_cents("+ ¥6.5", "x"), 650)
        self.assertEqual(opt.money(5980), "¥59.80")
        for bad in ("abc", "1.234", None, True):
            with self.assertRaises(opt.InputError):
                opt.yuan_to_cents(bad, "x")
        with self.assertRaises(opt.InputError):
            opt.diff_to_cents("加一元", "x")

    def test_modification_serialization(self):
        result = opt.default_modification({"modification": {"items": [{"values": [
            {"code": "A", "selectedQuantity": 1, "selectedKey": "0-1", "unselectedKey": "0-0"},
            {"code": "B", "selectedQuantity": 0, "selectedKey": "0-1", "unselectedKey": "0-0"}]}]}}, "x")
        self.assertEqual(result, {"values": [{"code": "A", "key": "0-1", "quantity": 1}, {"code": "B", "key": "0-0", "quantity": 1}]})
        self.assertIsNone(opt.default_modification({}, "x"))


class RealStoreTest(unittest.TestCase):
    def test_eight_piece_with_card_and_no_card_baseline(self):
        plan = opt.plan(store_request())
        self.assertEqual(plan["contexts"], {"none": None, "会员106/880": {"membershipCode": "106", "membershipSpecId": "880"}})
        baseline = [q for q in plan["quotes"] if "baseline" in q["roles"]][0]
        self.assertEqual(baseline["estimate_cents"], 8750)  # 2×麦辣22.5 + 麦乐鸡14 + 鸡翅15 + 薯条13.5
        card_best = best_quote(plan, "会员106/880")
        self.assertEqual(card_best["estimate_cents"], 4990)
        self.assertEqual(card_best["payload"]["withOrder"], {"membershipCode": "106", "membershipSpecId": "880"})
        items = card_best["payload"]["items"]
        self.assertEqual(len(items), 1)
        rounds = {r["round"]: r["comboItemList"] for r in items[0]["roundList"]}
        self.assertEqual(rounds["5"], [{"code": "4810", "quantity": 2}])
        self.assertEqual(rounds["1"][0]["modification"]["values"][0]["key"], "0-1")
        self.assertEqual(card_best["extras"], {"3050": 2, "4810": 1})
        self.assertLessEqual(len(plan["quotes"]), 10)

        results = []
        for quote in plan["quotes"]:
            if quote["id"] == card_best["id"]:
                results.append(quote_result(quote, 5980, ("521234", "麦金卡体验卡（15天）", 990)))
            elif quote["context"] == "none":
                results.append(quote_result(quote, quote["estimate_cents"]))
            else:
                results.append(quote_result(quote, quote["estimate_cents"] + 990, ("521234", "麦金卡体验卡（15天）", 990)))
        picked = run_pick(plan, results)
        first = picked["recommendations"][0]
        self.assertEqual((first["label"], first["price_cents"]), ("最低实付", 5980))
        self.assertEqual(first["card"]["name"], "麦金卡体验卡（15天）")
        labels = [r["label"] for r in picked["recommendations"]]
        self.assertIn("不开卡", labels)
        self.assertLessEqual(len(labels), 3)
        self.assertEqual(first["summary"], "全明星双人分享餐八件套＋随单开麦金卡体验卡（15天）")
        self.assertEqual([(i["name"], i["quantity"], i["demanded"]) for i in first["items"]], [
            ("麦辣鸡腿汉堡", 1, True), ("板烧鸡腿堡", 1, True), ("麦乐鸡4块", 1, True), ("麦辣鸡翅2块", 1, True),
            ("中薯条", 1, True), ("中薯条", 1, False), ("可乐中杯", 2, False)])  # 同价时保留套餐默认搭配（麦辣+板烧）
        self.assertEqual(first["notes"], ["含卡费¥9.90", "同样这些单点要¥118.50，省¥58.70"])  # 22.5+23.5+11.5+15+13.5×2+9.5×2
        message = picked["message"]
        self.assertIn("| | **方案1 · 最低实付** | **方案2 · 不开卡** |", message)
        self.assertIn("| 实付 | **¥59.80** | **¥87.50** |", message)
        self.assertIn("| 餐品 | **麦辣鸡腿汉堡×1** | **麦辣鸡腿汉堡×2** |", message)
        self.assertIn("|  | 可乐中杯×2 |", message)
        self.assertIn("| 点法 | 全明星双人分享餐八件套＋随单开麦金卡体验卡（15天） | 5 件单品直接点 |", message)
        self.assertIn("分别按「不开卡」和「随单开麦金卡体验卡（15天）」算", message)
        self.assertIn("全按最便宜的单点买要 ¥87.50；最省的方案 ¥59.80，少花 **¥27.70**", message)
        cards = picked["cards_html"]
        self.assertTrue(cards.startswith("<div>") and cards.endswith("</div></div>"))
        self.assertIn('<h2 class="sr-only">', cards)
        self.assertEqual(cards.count("border-radius:var(--border-radius-lg)"), len(labels))
        self.assertIn('<div style="font-weight:500;color:var(--color-text-primary);">麦辣鸡腿汉堡×1</div>', cards)
        self.assertIn('<div style="color:var(--color-text-secondary);">可乐中杯×2</div>', cards)
        self.assertIn("方案1 · 最低实付", cards)
        self.assertNotIn("<script", cards)
        self.assertNotIn("<!--", cards)
        self.assertTrue(picked["footer"].startswith("加粗是你点名要的"))
        self.assertTrue(picked["title"].startswith("**🕵️ 麦侦探"))
        self.assertEqual(picked["message"], "\n\n".join([picked["title"], picked["table_markdown"], picked["footer"]]))

    def test_existing_member_has_no_card_context(self):
        request = store_request()
        request["products"][-1]["discountType"] = "麦金卡优惠"  # 已是会员：现价直接可享
        plan = opt.plan(request)
        self.assertEqual(list(plan["contexts"]), ["none"])
        best = best_quote(plan)
        self.assertEqual(best["estimate_cents"], 4990)
        self.assertNotIn("withOrder", best["payload"])

    def test_no_card_context_uses_original_price(self):
        plan = opt.plan(store_request())
        for quote in plan["quotes"]:
            if quote["context"] == "none" and any(i["productCode"] == "9900011056" for i in quote["payload"]["items"]):
                self.assertGreaterEqual(quote["estimate_cents"], 11450)


class SearchTest(unittest.TestCase):
    def base_request(self, products, demands, **extra):
        request = {"context": dict(CONTEXT), "products": products, "demands": demands}
        request.update(extra)
        return request

    def test_combo_choice_serves_demand_and_keeps_extras(self):
        products = [single("A", "A", "10"), combo("C", "套餐", "12", [(1, [choice("A", "A", default=1), choice("B", "B")]), (1, [choice("D", "饮料", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        best = best_quote(opt.plan(self.base_request(products, demands)))
        self.assertEqual(best["estimate_cents"], 2200)  # 单点A + 套餐选B
        line = [i for i in best["payload"]["items"] if i["productCode"] == "C"][0]
        self.assertEqual(line["roundList"][0]["comboItemList"], [{"code": "B", "quantity": 1}])
        self.assertEqual(line["roundList"][1]["round"], "2")
        self.assertEqual(best["extras"], {"D": 1})

    def test_two_instances_with_different_configs_split_lines(self):
        products = [combo("C", "套餐", "10", [(1, [choice("A", "A", default=1), choice("B", "B")])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        best = best_quote(opt.plan(self.base_request(products, demands)))
        self.assertEqual(best["estimate_cents"], 2000)
        self.assertEqual(sorted(i["roundList"][0]["comboItemList"][0]["code"] for i in best["payload"]["items"]), ["A", "B"])
        self.assertTrue(all(i["quantity"] == 1 for i in best["payload"]["items"]))

    def test_identical_instances_merge_quantity(self):
        products = [combo("C", "套餐", "10", [(2, [choice("A", "A", default=1)]), (1, [choice("D", "饮料", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 4, "codes": ["A"]}]
        best = best_quote(opt.plan(self.base_request(products, demands)))
        self.assertEqual(best["payload"]["items"], [{"productCode": "C", "quantity": 2, "roundList": [
            {"round": "1", "comboItemList": [{"code": "A", "quantity": 2}]}, {"round": "2", "comboItemList": [{"code": "D", "quantity": 1}]}]}])

    def test_forbidden_filler_avoided_and_mandatory_forbidden_excluded(self):
        products = [single("A", "A", "20"),
                    combo("C", "带饮料套餐", "15", [(1, [choice("A", "A", default=1)]), (1, [choice("D", "可乐", default=1)])]),
                    combo("E", "可换套餐", "16", [(1, [choice("A", "A", default=1)]), (1, [choice("D", "可乐", default=1), choice("F", "派", "+ ¥1")])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.base_request(products, demands, forbidden_codes=["D"]))
        best = best_quote(plan)
        self.assertEqual(best["estimate_cents"], 1700)
        self.assertEqual(best["extras"], {"F": 1})
        self.assertTrue(any("带饮料套餐" in note for note in plan["notes"]))
        for quote in plan["quotes"]:
            self.assertNotIn("D", quote["supply"])

    def test_strict_mode_forbids_extras(self):
        products = [single("A", "A", "20"), combo("C", "套餐", "15", [(1, [choice("A", "A", default=1)]), (1, [choice("D", "可乐", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.base_request(products, demands, strict=True))
        self.assertTrue(all(q["extra_count"] == 0 for q in plan["quotes"]))
        self.assertEqual(min(q["estimate_cents"] for q in plan["quotes"]), 2000)

    def test_upgrade_threshold_is_one_fifth_of_added_original_price(self):
        def build(c_price):
            products = [single("A", "A", "15"), single("B", "B", "15"), single("C", "C", c_price),
                        combo("ABC", "三件套", "31", [(1, [choice("A", "A", default=1)]), (1, [choice("B", "B", default=1)]), (1, [choice("C", "C", default=1)])])]
            demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
            return opt.plan(self.base_request(products, demands))
        plan = build("5")  # 多 1 元，C 单点原价 5 元：1 <= 5/5
        self.assertTrue(any("upgrade" in q["roles"] for q in plan["quotes"]))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        self.assertEqual([r["label"] for r in picked["recommendations"]][:2], ["最低实付", "多花¥1.00升级"])
        self.assertEqual(picked["recommendations"][1]["upgrade"]["added"], {"C": 1})
        self.assertIn("比方案1多花¥1.00，多得C×1，原价约¥5.00", picked["recommendations"][1]["notes"])
        plan = build("4.5")  # 1 > 4.5/5
        self.assertFalse(any("upgrade" in q["roles"] for q in plan["quotes"]))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        self.assertFalse(any(r["upgrade"] for r in picked["recommendations"]))

    def test_upgrade_needs_known_reference_price(self):
        products = [single("A", "A", "15"), single("B", "B", "15"),
                    combo("ABC", "三件套", "30", [(1, [choice("A", "A", default=1)]), (1, [choice("B", "B", default=1)]), (1, [choice("C", "C", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands))
        picked = run_pick(plan, [quote_result(q, 3000) for q in plan["quotes"]])
        self.assertFalse(any(r["upgrade"] for r in picked["recommendations"]))
        plan = opt.plan(self.base_request(products, demands, reference_prices={"C": "6"}))
        picked = run_pick(plan, [quote_result(q, 3000) for q in plan["quotes"]])
        self.assertTrue(any(r["upgrade"] for r in picked["recommendations"]))

    def test_card_combo_can_be_upgrade_over_no_card_lowest(self):
        """钟鼓楼 1960602 真实案例：2辣+2不辣汉堡、1份炸鸡、至少2杯饮料。
        不开卡最低 ¥84.30；八件套+随心配×2 随单开月卡 ¥96.70（含卡费¥19），多得鸡翅/麦乐鸡、薯条×2、可乐×2 和月卡，应列为升级。"""
        products = [
            single("1440", "麦辣鸡腿汉堡", "22.5"), single("1450", "麦香鸡", "17"), single("521316", "高达吉士双牛堡", "22.5"),
            single("521143", "4块麦乐鸡", "11.5"), single("1700", "麦辣鸡翅2块", "15"), single("3757", "纯悦", "7.5"),
            combo("9900015188", "精选超值随心配", "13.9", [
                (1, [choice("521316", "高达吉士双牛堡", default=1), choice("1450", "麦香鸡")]),
                (1, [choice("515281", "阳光柠檬红茶中杯", default=1), choice("3050", "可乐中杯"), choice("4106", "香芋派")])]),
            combo("9900011056", "全明星双人分享餐八件套", "49.9", [
                (1, [choice("1440", "麦辣鸡腿汉堡", default=1)]),
                (1, [choice("1406", "板烧鸡腿堡", default=1), choice("1440", "麦辣鸡腿汉堡")]),
                (1, [choice("515106", "麦乐鸡4块", default=1)]),
                (1, [choice("1700", "麦辣鸡翅2块", default=1)]),
                (2, [choice("4810", "中薯条", default=1)]),
                (2, [choice("3050", "可乐中杯", default=1)])],
                original="114.5", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "870"}),
        ]
        drinks = ["3757", "3050", "515281"]
        demands = [{"id": "spicy", "label": "辣汉堡", "quantity": 2, "codes": ["1440"]},
                   {"id": "mild", "label": "不辣汉堡", "quantity": 2, "codes": ["1450", "521316", "1406"]},
                   {"id": "chicken", "label": "炸鸡", "quantity": 1, "codes": ["521143", "1700", "515106"]},
                   {"id": "drink", "label": "饮料", "quantity": 2, "codes": drinks}]
        refs = {"3050": "9.5", "515281": "9.5", "4810": "13.5", "515106": "11.5", "4106": "8.5"}
        plan = opt.plan(self.base_request(products, demands, reference_prices=refs))
        card = best_quote(plan, "会员106/870")
        self.assertIn("9900011056", [i["productCode"] for i in card["payload"]["items"]])
        results = []
        for quote in plan["quotes"]:
            if quote["context"] == "none":
                results.append(quote_result(quote, quote["estimate_cents"]))
            else:
                results.append(quote_result(quote, quote["estimate_cents"] + 1900, ("513817", "麦金卡月卡（30天）", 1900)))
        picked = run_pick(plan, results)
        labels = [r["label"] for r in picked["recommendations"]]
        self.assertEqual(picked["recommendations"][0]["price_cents"], 8430)
        upgrade = [r for r in picked["recommendations"] if r["upgrade"]]
        self.assertEqual(len(upgrade), 1, labels)
        self.assertEqual(upgrade[0]["price_cents"], card["estimate_cents"] + 1900)
        self.assertEqual(upgrade[0]["context"], "会员106/870")
        self.assertIn("麦金卡月卡（30天）", upgrade[0]["upgrade"]["added_text"])
        # 月卡按 ¥5 计：鸡翅换4块麦乐鸡+3.5、麦乐鸡4块11.5、薯条×2 27、可乐×2 19，再+5
        self.assertEqual(upgrade[0]["upgrade"]["added_value_cents"], 6600)
        self.assertIn("原价约¥66.00（卡按¥5.00计）", "；".join(upgrade[0]["notes"]))
        self.assertTrue(any(n.startswith("同样这些单点要") for n in upgrade[0]["notes"]))
        # 卡值调到 0 时，餐品价值 ¥61 不够 5×多花金额，不再算升级
        plan0 = opt.plan(self.base_request(products, demands, reference_prices=refs, card_value="0"))
        results0 = []
        for quote in plan0["quotes"]:
            if quote["context"] == "none":
                results0.append(quote_result(quote, quote["estimate_cents"]))
            else:
                results0.append(quote_result(quote, quote["estimate_cents"] + 1900, ("513817", "麦金卡月卡（30天）", 1900)))
        self.assertFalse(any(r["upgrade"] for r in run_pick(plan0, results0)["recommendations"]))

    def zhonggulou(self, **extra):
        products = [
            single("1440", "麦辣鸡腿汉堡", "22.5"), single("1450", "麦香鸡", "17"), single("521316", "高达吉士双牛堡", "22.5"),
            single("521143", "4块麦乐鸡", "11.5"), single("1700", "麦辣鸡翅2块", "15"), single("3757", "纯悦", "7.5"),
            combo("9900015188", "精选超值随心配", "13.9", [
                (1, [choice("521316", "高达吉士双牛堡", default=1), choice("1450", "麦香鸡")]),
                (1, [choice("515281", "阳光柠檬红茶中杯", default=1), choice("3050", "可乐中杯")])]),
            combo("9900011056", "全明星双人分享餐八件套", "49.9", [
                (1, [choice("1440", "麦辣鸡腿汉堡", default=1)]),
                (1, [choice("1406", "板烧鸡腿堡", default=1), choice("1440", "麦辣鸡腿汉堡")]),
                (1, [choice("515106", "麦乐鸡4块", default=1)]),
                (1, [choice("1700", "麦辣鸡翅2块", default=1)]),
                (2, [choice("4810", "中薯条", default=1)]),
                (2, [choice("3050", "可乐中杯", default=1)])],
                original="114.5", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "870"}),
        ]
        demands = [{"id": "spicy", "label": "辣汉堡", "quantity": 2, "codes": ["1440"]},
                   {"id": "mild", "label": "不辣汉堡", "quantity": 2, "codes": ["1450", "521316", "1406"]},
                   {"id": "chicken", "label": "炸鸡", "quantity": 1, "codes": ["521143", "1700", "515106"]},
                   {"id": "drink", "label": "饮料", "quantity": 2, "codes": ["3757", "3050", "515281"]}]
        refs = {"3050": "9.5", "515281": "9.5", "4810": "13.5", "515106": "11.5"}
        return opt.plan(self.base_request(products, demands, reference_prices=refs, **extra))

    def official(self, plan, card_fee):
        """模拟官方计价：开卡方案 data.price = 菜单估价 + 卡费，回显多一行卡。"""
        results = []
        for quote in plan["quotes"]:
            if quote["context"] == "none":
                results.append(quote_result(quote, quote["estimate_cents"]))
            else:
                results.append(quote_result(quote, quote["estimate_cents"] + card_fee, ("513817", "麦金卡月卡（30天）", card_fee)))
        return results

    def test_card_settles_at_real_fee_but_counts_five_yuan(self):
        plan = self.zhonggulou()
        results = self.official(plan, 1900)
        picked = run_pick(plan, results)
        official = {r["id"]: r["price"] for r in results}
        up = [r for r in picked["recommendations"] if r["upgrade"]][0]
        lowest = picked["recommendations"][0]
        # 实付 = 官方 data.price（已含 ¥19 卡费，不重复加、不按 ¥5 结算）
        self.assertEqual(up["price_cents"], official[up["quote_id"]])
        self.assertEqual(up["card"]["fee_cents"], 1900)
        self.assertIn("含卡费¥19.00", up["notes"])
        self.assertEqual(up["upgrade"]["delta_cents"], up["price_cents"] - lowest["price_cents"])
        self.assertEqual(up["payload"]["withOrder"], {"membershipCode": "106", "membershipSpecId": "870"})
        # ¥5 只用于判断值不值：餐品多得 ¥61 + 卡 ¥5
        self.assertEqual(up["upgrade"]["added_value_cents"], 6100 + 500)
        # 卡费低于 ¥5 时按卡费计（不超过卡费）
        self.assertEqual(opt.card_worth({"card": {"fee_cents": 300}}, 500), 300)
        self.assertEqual(opt.card_worth({"card": {"fee_cents": 1900}}, 500), 500)
        self.assertEqual(opt.card_worth({"card": None}, 500), 0)
        # 卡费涨到 ¥25：多花 ¥18.40，5×18.40=92 > 66，不再算升级——说明差价用的是真实卡费
        dear = run_pick(plan, self.official(plan, 2500))
        self.assertFalse(any(r["upgrade"] for r in dear["recommendations"]))
        # 自定义卡值
        plan8 = self.zhonggulou(card_value="8")
        up8 = [r for r in run_pick(plan8, self.official(plan8, 1900))["recommendations"] if r["upgrade"]][0]
        self.assertEqual(up8["upgrade"]["added_value_cents"], 6100 + 800)

    def test_card_fee_larger_than_total_is_rejected(self):
        plan = self.zhonggulou()
        quote = [q for q in plan["quotes"] if q["context"] != "none"][0]
        picked = run_pick(plan, [quote_result(quote, 1000, ("513817", "麦金卡月卡（30天）", 1900))])
        self.assertEqual(picked["recommendations"], [])
        self.assertIn("卡费", picked["excluded"][0]["reason"])

    def test_missing_single_price_inferred_from_same_round(self):
        # 八件套第3轮：麦乐鸡4块 +¥0、麦辣鸡翅2块 +¥3.5（单点 ¥15）→ 麦乐鸡4块 ≈ ¥11.50；
        # 两处都能推时取较低值
        products = [single("1440", "麦辣鸡腿汉堡", "22.5"), single("1700", "麦辣鸡翅2块", "15"), single("4437", "玉米杯", "13"),
                    combo("P", "八件套", "50", [(1, [choice("1440", "麦辣鸡腿汉堡", default=1)]),
                                              (1, [choice("515106", "麦乐鸡4块", default=1), choice("1700", "麦辣鸡翅2块", "+ ¥3.5")])]),
                    combo("Q", "随心配", "14.9", [(1, [choice("1440", "麦辣鸡腿汉堡", default=1)]),
                                                (1, [choice("3050", "可乐中杯", default=1), choice("4437", "玉米杯"), choice("515106", "麦乐鸡4块")])])]
        demands = [{"id": "b", "label": "汉堡", "quantity": 1, "codes": ["1440"]}]
        request = opt.Request(self.base_request(products, demands))
        self.assertEqual(request.ref_prices["515106"], 1150)
        self.assertEqual(request.ref_prices["3050"], 1300)  # 只从真实单点价（玉米杯 13）推一步，不用麦乐鸡4块的推算值
        # 防连环下压：同一餐品在不同套餐里差价不同时，推算值不再被用来推别的
        chain = [single("Y", "Y", "10"),
                 combo("K1", "K1", "20", [(1, [choice("Y", "Y", default=1), choice("X", "X", "+ ¥3")]), (1, [choice("W", "W", default=1)])]),
                 combo("K2", "K2", "20", [(1, [choice("X", "X", default=1), choice("Z", "Z", "+ ¥3")]), (1, [choice("W", "W", default=1)])])]
        chained = opt.Request(self.base_request(chain, [{"id": "y", "label": "Y", "quantity": 1, "codes": ["Y"]}]))
        self.assertEqual(chained.ref_prices["X"], 1300)
        self.assertNotIn("Z", chained.ref_prices)
        self.assertEqual(set(request.estimated_prices), {"515106", "3050"})
        # 明确给出的单点价优先，不被推算覆盖
        explicit = opt.Request(self.base_request(products, demands, reference_prices={"3050": "9.5"}))
        self.assertEqual(explicit.ref_prices["3050"], 950)
        self.assertNotIn("3050", explicit.estimated_prices)
        plan = opt.plan(self.base_request(products, demands))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        notes = "；".join(n for r in picked["recommendations"] for n in r["notes"])
        self.assertIn("同样这些单点约要", notes)

    def test_unknown_single_price_gives_lower_bound(self):
        products = [single("A", "A", "15"), single("B", "B", "15"),
                    combo("ABC", "三件套", "20", [(1, [choice("A", "A", default=1)]), (1, [choice("B", "B", default=1)]),
                                                (1, [choice("C", "神秘小食", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        first = picked["recommendations"][0]
        self.assertEqual(first["price_cents"], 2000)
        self.assertIn("同样这些单点至少要¥30.00（神秘小食未计入），省¥10.00以上", first["notes"])

    def test_padded_plans_are_not_upgrades(self):
        products = [single("A", "A", "10"), combo("AD", "A+饮料", "10.5", [(1, [choice("A", "A", default=1)]), (1, [choice("D", "饮料", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.base_request(products, demands, reference_prices={"D": "8"}))
        ups = [q for q in plan["quotes"] if "upgrade" in q["roles"]]
        self.assertTrue(ups)
        self.assertTrue(all(q["minimal"] for q in ups))
        self.assertTrue(all(len(q["payload"]["items"]) == 1 for q in ups))
        padded = opt.Solver(opt.Request(self.base_request(products, demands)))
        plans, _ = padded.solve("none")
        self.assertFalse(any(padded.is_minimal(p) for p in plans if sum(i["quantity"] for i in p["payload"]["items"]) > 1))

    def test_category_demand_offers_flavor_alternative(self):
        products = [single("X", "麦辣", "20"), single("Y", "板烧", "21"), single("Z", "麦香鸡", "25")]
        demands = [{"id": "burger", "label": "汉堡", "quantity": 2, "codes": ["X", "Y", "Z"]}]
        plan = opt.plan(self.base_request(products, demands))
        self.assertTrue(any("flavor" in q["roles"] for q in plan["quotes"]))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        labels = [r["label"] for r in picked["recommendations"]]
        self.assertEqual(labels[0], "最低实付")
        self.assertIn("换口味", labels)
        self.assertEqual(picked["recommendations"][0]["allocation"], {"burger": {"X": 2}})

    def test_coupon_lines_use_distinct_codes(self):
        products = [single("A", "A", "20"),
                    {"code": "CA", "name": "A券", "price": "9.9", "food_code": "A", "coupon": {"couponId": "K1", "couponCodes": ["c1", "c2"], "title": "9.9元A"}}]
        demands = [{"id": "a", "label": "A", "quantity": 3, "codes": ["A"]}]
        plan = opt.plan(self.base_request(products, demands))
        best = best_quote(plan)
        self.assertEqual(best["estimate_cents"], 990 * 2 + 2000)
        coupon_lines = [i for i in best["payload"]["items"] if i.get("couponId")]
        self.assertEqual(sorted(i["couponCode"] for i in coupon_lines), ["c1", "c2"])
        self.assertTrue(all(i["quantity"] == 1 for i in coupon_lines))
        baseline = [q for q in plan["quotes"] if "baseline" in q["roles"]][0]
        self.assertEqual(baseline["estimate_cents"], 6000)

    def test_single_round_product_gives_reference_price(self):
        products = [single("A", "A", "15"), combo("COLA", "可乐", "9.5", [(1, [choice("3050", "可乐中杯", default=1), choice("3100", "可乐大杯", "+ ¥2")])])]
        request = opt.Request(self.base_request(products, [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]))
        self.assertEqual(request.ref_prices["3050"], 950)
        self.assertEqual(request.ref_prices["3100"], 1150)

    def test_singles_cheapest_still_shows_the_work(self):
        products = [single("A", "A", "10"), single("B", "B", "8"),
                    combo("AB", "AB套餐", "25", [(1, [choice("A", "A", default=1)]), (1, [choice("B", "B", default=1)]), (1, [choice("D", "可乐", default=1)])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands, store_name="测试门店",
                                          effort={"menu_items": 120, "coupons_claimed": 2, "store_coupons": 1}))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        message = picked["message"]
        self.assertIn("**🕵️ 麦侦探 · 测试门店 · 到店自取**", message)
        self.assertIn("翻了本店 120 款菜单；拆开 1 个套餐/随心配/双人餐看全部可选项", message)
        self.assertIn("领了 2 张免费券，本单可用券 1 张都试过了", message)
        self.assertIn("本地精确比较了", message)
        self.assertIn("向麦当劳官方核价", message)
        self.assertIn("直接单点就是最省的", message)
        self.assertEqual(picked["recommendations"][0]["notes"], ["就是全单点"])
        self.assertEqual(picked["recommendations"][0]["summary"], "2 件单品直接点")

    def test_no_usable_combo_is_explained(self):
        plan = opt.plan(self.base_request([single("A", "A", "10")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        self.assertIn("店里没有能装下这些餐品的套餐，只能单点", picked["message"])

    def test_effort_field_validation(self):
        good = self.base_request([single("A", "A", "1")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}])
        for effort in ({"menu_items": -1}, {"unknown": 1}, {"menu_items": "131"}):
            with self.assertRaises(opt.InputError):
                opt.plan({**good, "effort": effort})

    def test_infeasible_reports_note(self):
        plan = opt.plan(self.base_request([single("A", "A", "1")], [{"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]))
        self.assertEqual(plan["quotes"], [])
        self.assertTrue(plan["notes"])

    def test_input_validation(self):
        good = self.base_request([single("A", "A", "1")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}])
        bad_cases = [
            {**good, "demands": []},
            {**good, "demands": [{"id": "a", "label": "A", "quantity": 0, "codes": ["A"]}]},
            {**good, "forbidden_codes": ["A"]},
            {**good, "context": {"storeCode": "1"}},
            {**good, "products": [single("A", "A", "1", discountType="随单购麦金卡优惠")]},
        ]
        for case in bad_cases:
            with self.assertRaises(opt.InputError):
                opt.plan(case)


class BruteForceTest(unittest.TestCase):
    """随机小菜单：本地精确搜索的最低估价 == 暴力穷举最低估价。"""

    def random_case(self, rng, strict):
        foods = ["F%d" % i for i in range(4)]
        products = []
        for food in foods:
            if rng.random() < 0.75:
                products.append(single(food, food, str(rng.randint(5, 20))))
        for k in range(rng.randint(1, 3)):
            rounds = []
            for _ in range(rng.randint(1, 3)):
                options = rng.sample(foods + ["X"], rng.randint(1, 3))
                rounds.append((rng.randint(1, 2), [choice(c, c, "%+d" % rng.randint(-1, 3), default=int(i == 0)) for i, c in enumerate(options)]))
            products.append(combo("K%d" % k, "K%d" % k, str(rng.randint(8, 30)), rounds))
        demands = []
        for i, food in enumerate(rng.sample(foods, rng.randint(1, 3))):
            codes = [food] + [f for f in foods if f != food and rng.random() < 0.3]
            demands.append({"id": "d%d" % i, "label": food, "quantity": rng.randint(1, 2), "codes": codes})
        return {"context": dict(CONTEXT), "products": products, "demands": demands, "strict": strict}

    def brute(self, data):
        request = opt.Request(data)
        options = []  # (cost, supply)
        for source in request.sources:
            if not source["rounds"]:
                options.append((source["current"], Counter({source["food"]: 1})))
                continue
            per_round = []
            for rnd in source["rounds"]:
                fills = []
                for picked in itertools.combinations_with_replacement(rnd["choices"], rnd["units"]):
                    counts = Counter(ch["code"] for ch in picked)
                    if all(counts[ch["code"]] <= ch["cap"] for ch in rnd["choices"]):
                        fills.append((sum(ch["diff"] for ch in picked), counts))
                per_round.append(fills)
            for picks in itertools.product(*per_round):
                supply = Counter()
                for _, counts in picks:
                    supply.update(counts)
                options.append((source["current"] + sum(p[0] for p in picks), supply))
        slots = [d for d in request.demands for _ in range(d["quantity"])]

        def feasible(supply):
            pool = Counter(supply)

            def assign(i):
                if i == len(slots):
                    return not data["strict"] or sum(pool.values()) == 0
                for code in slots[i]["codes"]:
                    if pool[code] > 0:
                        pool[code] -= 1
                        if assign(i + 1):
                            return True
                        pool[code] += 1
                return False
            return assign(0)

        options.sort(key=lambda o: o[0])
        self.assertTrue(all(o[0] > 0 for o in options))
        best = [None]

        def search(start, cost, supply, size):  # 所有购买项价格为正：可行后再加只会更贵
            if data["strict"] and sum(supply.values()) > request.total_units:
                return
            if size and feasible(supply):
                best[0] = cost if best[0] is None else min(best[0], cost)
                return
            if size == request.total_units:
                return
            for i in range(start, len(options)):
                total = cost + options[i][0]
                if best[0] is not None and total >= best[0]:
                    break
                search(i, total, supply + options[i][1], size + 1)
        search(0, 0, Counter(), 0)
        return best[0]

    def check(self, seed, strict):
        data = self.random_case(random.Random(seed), strict)
        expected = self.brute(data)
        plans, _ = opt.Solver(opt.Request(data)).solve("none")
        self.assertEqual(plans[0]["cost"] if plans else None, expected, "seed=%d strict=%s" % (seed, strict))

    def test_random_open_extras(self):
        for seed in range(120):
            self.check(seed, False)

    def test_random_strict(self):
        for seed in range(120, 200):
            self.check(seed, True)


class PickTest(unittest.TestCase):
    def setUp(self):
        self.plan = opt.plan(store_request())

    def test_echo_mismatch_and_errors_are_excluded(self):
        quotes = self.plan["quotes"]
        results = []
        for i, quote in enumerate(quotes):
            result = quote_result(quote, quote["estimate_cents"])
            if i == 0:
                result["productList"][0]["quantity"] += 1
            if i == 1:
                result = {"id": quote["id"], "error": "门店已售罄"}
            results.append(result)
        picked = run_pick(self.plan, results)
        reasons = {e["id"]: e["reason"] for e in picked["excluded"]}
        self.assertIn("回显数量不一致", reasons[quotes[0]["id"]])
        self.assertIn("售罄", reasons[quotes[1]["id"]])

    def test_unknown_extra_line_without_card_is_rejected(self):
        quote = [q for q in self.plan["quotes"] if q["context"] == "none"][0]
        picked = run_pick(self.plan, [quote_result(quote, quote["estimate_cents"], ("999", "神秘商品", 100))])
        self.assertEqual(picked["recommendations"], [])
        self.assertIn("未下单的商品", picked["excluded"][0]["reason"])

    def test_official_price_overrides_estimate(self):
        results = []
        for quote in self.plan["quotes"]:
            price = 3000 if "baseline" in quote["roles"] else quote["estimate_cents"] + 5000
            results.append(quote_result(quote, price, ("521234", "卡", 990) if quote["context"] != "none" else None))
        picked = run_pick(self.plan, results)
        top = picked["recommendations"][0]["quote_id"]
        self.assertIn("baseline", [q for q in self.plan["quotes"] if q["id"] == top][0]["roles"])

    def test_unquoted_listed_and_plan_path_required(self):
        picked = run_pick(self.plan, [])
        self.assertEqual(len(picked["unquoted"]), len(self.plan["quotes"]))
        self.assertIn("没有拿到有效的官方报价", picked["message"])
        with self.assertRaises(opt.InputError):
            opt.pick({"plan_file": "plan.json", "results": []})


class CliAndScaleTest(unittest.TestCase):
    def test_cli_round_trip(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimizer.py")
        with tempfile.TemporaryDirectory() as folder:
            request_path = os.path.join(folder, "request.json")
            plan_path = os.path.join(folder, "plan.json")
            quotes_path = os.path.join(folder, "quotes.json")
            picked_path = os.path.join(folder, "picked.json")
            with open(request_path, "w", encoding="utf-8") as handle:
                json.dump(store_request(), handle, ensure_ascii=False)
            done = subprocess.run([sys.executable, "-B", script, "plan", "--input", request_path, "--output", plan_path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            with open(plan_path, encoding="utf-8") as handle:
                plan = json.load(handle)
            with open(quotes_path, "w", encoding="utf-8") as handle:
                json.dump({"plan_file": plan_path, "results": [quote_result(q, q["estimate_cents"]) for q in plan["quotes"] if q["context"] == "none"]}, handle)
            done = subprocess.run([sys.executable, "-B", script, "pick", "--input", quotes_path, "--output", picked_path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            with open(picked_path, encoding="utf-8") as handle:
                self.assertTrue(json.load(handle)["recommendations"])
            done = subprocess.run([sys.executable, "-B", script, "plan", "--input", quotes_path, "--output", picked_path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2)
            self.assertIn("error", done.stderr)

    def test_realistic_store_size_is_fast_and_complete(self):
        burgers = ["B%d" % i for i in range(8)]
        snacks = ["S%d" % i for i in range(5)]
        drinks = ["D%d" % i for i in range(4)]
        products = [single(c, c, str(15 + i)) for i, c in enumerate(burgers)] + [single(c, c, str(9 + i)) for i, c in enumerate(snacks + drinks)]
        for k in range(12):
            rounds = [(1, [choice(b, b, "+ ¥%d" % (i % 3), default=int(i == 0)) for i, b in enumerate(burgers)]),
                      (1, [choice(s, s, "+ ¥%d" % (i % 2), default=int(i == 0)) for i, s in enumerate(snacks)]),
                      (1, [choice(d, d, default=int(i == 0)) for i, d in enumerate(drinks)])]
            products.append(combo("M%d" % k, "套餐%d" % k, str(25 + k), rounds))
        products.append(combo("SHARE", "双人餐", "59", [(2, [choice(b, b, default=int(i == 0)) for i, b in enumerate(burgers)]),
                                                     (2, [choice(s, s, default=int(i == 0)) for i, s in enumerate(snacks)]),
                                                     (2, [choice(d, d, default=int(i == 0)) for i, d in enumerate(drinks)])]))
        request = {"context": dict(CONTEXT), "products": products, "demands": [
            {"id": "burger", "label": "汉堡", "quantity": 3, "codes": burgers},
            {"id": "snack", "label": "小食", "quantity": 2, "codes": ["S0"]}]}
        start = time.time()
        plan = opt.plan(request)
        self.assertLess(time.time() - start, 30)
        self.assertTrue(all(s["complete"] for s in plan["search"].values()))
        self.assertLessEqual(len(plan["quotes"]), 10)


if __name__ == "__main__":
    unittest.main()
