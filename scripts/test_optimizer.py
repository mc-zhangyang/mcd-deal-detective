#!/usr/bin/env python3
"""麦侦探求解器离线测试（标准库 unittest，不联网）。"""
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from unittest.mock import patch

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
    """西安长和卜蜂 1960649 真实菜单与详情（每轮保留必须相关选项与最低差价选项）。"""
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


class InputValidationRegressionTest(unittest.TestCase):
    def request(self, **extra):
        data = {"context": dict(CONTEXT),
                "demands": [{"id": "a", "quantity": 1, "codes": ["A"]}],
                "products": [single("A", "汉堡", "10")]}
        data.update(extra)
        return data

    def write_fragment(self, folder, name, data):
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        return path

    def test_default_flags_numeric_string_bool_and_empty_are_equivalent(self):
        for value, expected in ((0, False), ("0", False), (False, False), (0.0, False),
                                (1, True), ("1", True), (True, True), (1.0, True),
                                (None, False), ("", False)):
            for compact in (False, True):
                with self.subTest(value=value, compact=compact):
                    raw = ["A", "汉堡", "-0.5", value] if compact else choice("A", "汉堡", "-0.5", value)
                    parsed = opt.compact_choice(raw, "option")
                    self.assertIs(parsed["isDefault"], expected)
                    self.assertEqual(parsed["diffPrice"], "-0.5")
                    product = combo("C", "套餐", "12", [(1, [raw])])
                    request = opt.Request(self.request(products=[product]))
                    result = request.sources[0]["rounds"][0]["choices"][0]
                    self.assertIs(result["default"], expected)
                    self.assertEqual(result["diff"], -50)
        for raw in (["A", "汉堡"], ["A", "汉堡", 0], {"code": "A"}):
            product = combo("C", "套餐", "12", [(1, [raw])])
            parsed = opt.Request(self.request(products=[product]))
            self.assertIs(parsed.sources[0]["rounds"][0]["choices"][0]["default"], False)

    def test_invalid_default_flags_are_rejected(self):
        for value in (2, -1, 0.5, "false", "true", "yes", " 0", "01", [], {}):
            for raw in (["A", "汉堡", 0, value], {"code": "A", "isDefault": value}):
                with self.subTest(value=value, raw=raw):
                    with self.assertRaisesRegex(opt.InputError, r"option\.isDefault"):
                        opt.compact_choice(raw, "option")
                    product = combo("C", "套餐", "12", [(1, [raw])])
                    with self.assertRaisesRegex(opt.InputError, r"products\[0\].rounds\[0\].choices\[0\].isDefault"):
                        opt.Request(self.request(products=[product]))

    def test_compact_diff_price_is_not_silently_changed(self):
        for value in (0, "0", None, "", "+1", "-0.5"):
            with self.subTest(value=value):
                self.assertEqual(opt.compact_choice(["A", "汉堡", value, "0"], "option")["diffPrice"], value)
        with self.assertRaisesRegex(opt.InputError, "diffPrice"):
            opt.Request(self.request(products=[combo("C", "套餐", "12", [(1, [["A", "汉堡", False, "0"]])])]))

    def test_independent_errors_are_reported_together_before_solving(self):
        data = self.request(context={"storeCode": "1"}, effort={"menu_items": -1},
                            demands=[{"id": "a", "quantity": 0, "codes": ["A"]},
                                     {"id": "b", "quantity": 1, "codes": []}],
                            products=[single("A", "汉堡", "坏金额"), single("B", "薯条", "1.234")])
        with patch.object(opt, "Solver", side_effect=AssertionError("坏输入进入求解")):
            with self.assertRaises(opt.InputError) as raised:
                opt.plan(data)
        message = str(raised.exception)
        for field in ("context", "effort.menu_items", "demands[0].quantity", "demands[1].codes",
                      "products[0].currentPrice", "products[1].currentPrice"):
            self.assertIn(field, message)

    def test_multiple_rounds_and_products_are_reported_together(self):
        product = combo("C", "套餐", "12", [(1, [choice("A", "汉堡")]), (1, [choice("B", "薯条")])])
        product["rounds"][0]["minQuantity"] = -1
        product["rounds"][1]["choices"][0]["isDefault"] = "bad"
        with self.assertRaises(opt.InputError) as raised:
            opt.Request(self.request(effort={"menu_items": "1"}, products=[product, single("X", "X", None)]))
        for field in ("effort.menu_items", "products[0].rounds[0].minQuantity",
                      "products[0].rounds[1].choices[0].isDefault", "products[1].currentPrice"):
            self.assertIn(field, str(raised.exception))

    def test_bad_container_types_raise_input_error(self):
        for extra in ({"context": []}, {"effort": []}, {"demands": {}}, {"singles": "A"},
                      {"products": {}}, {"forbidden_codes": "A"}, {"reference_prices": []},
                      {"demands": [{"quantity": 1, "codes": "A"}]},
                      {"products": [single("A", "A", "1", rounds={})]},
                      {"products": [combo("C", "C", "1", [(1, {})])]}):
            with self.subTest(extra=extra), self.assertRaises(opt.InputError):
                opt.Request(self.request(**extra))

    def test_bad_modifications_are_input_errors_and_aggregate(self):
        for modification in ({"items": [None]}, {"items": [{"values": [None]}]},
                             {"items": [{"values": [{"selectedQuantity": 1, "selectedKey": "0-1"}]}]}):
            with self.subTest(modification=modification):
                with self.assertRaises(opt.InputError) as raised:
                    opt.Request(self.request(products=[single("A", "A", "1", modification=modification),
                                                       single("B", "B", "bad")]))
                self.assertIn("products[0].modification", str(raised.exception))
                self.assertIn("products[1].currentPrice", str(raised.exception))

    def test_empty_non_piece_fragment_does_not_create_pieces(self):
        data = self.request()
        with tempfile.TemporaryDirectory() as folder:
            self.write_fragment(folder, "products_empty.json", {"demand_pieces": {"a": {}}})
            merged = opt.merge_fragments(data, folder)
            self.assertNotIn("pieces", merged["demands"][0])
            self.assertNotIn("pieces", opt.Request(merged).demands[0])
            self.assertTrue(opt.plan(merged)["quotes"])
        self.assertNotIn("pieces", data["demands"][0])

    def test_nonempty_piece_fragment_remains_strict(self):
        for pieces in ({"B": 4}, {"A": 0}, {"A": "4"}):
            with tempfile.TemporaryDirectory() as folder, self.subTest(pieces=pieces):
                self.write_fragment(folder, "products_pieces.json", {"demand_pieces": {"a": pieces}})
                with self.assertRaises(opt.InputError):
                    opt.Request(opt.merge_fragments(self.request(), folder))
        with tempfile.TemporaryDirectory() as folder:
            self.write_fragment(folder, "products_pieces.json", {"demand_pieces": {"a": {}}})
            data = self.request(demands=[{"id": "a", "quantity": 4, "codes": ["A"], "pieces": {"A": 4}}])
            self.assertEqual(opt.Request(opt.merge_fragments(data, folder)).demands[0]["pieces"], {"A": 4})

    def test_fragment_mapping_errors_include_all_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.write_fragment(folder, "products_1.json", {"demand_codes": [], "demand_pieces": {"unknown": {}}})
            second = self.write_fragment(folder, "products_2.json", {"demand_codes": {"missing": ["B"], "a": "B"},
                                                                     "demand_pieces": {"a": []}})
            with self.assertRaises(opt.InputError) as raised:
                opt.merge_fragments(self.request(), folder)
            message = str(raised.exception)
            for field in (first + ".demand_codes", first + ".demand_pieces.unknown",
                          second + ".demand_codes.missing", second + ".demand_codes.a", second + ".demand_pieces.a"):
                self.assertIn(field, message)

    def test_optional_checklists_report_all_missing_codes(self):
        with self.assertRaises(opt.InputError) as raised:
            opt.Request(self.request(expected_product_codes=["A", "C", "D"], expected_single_codes=["E", "F"]))
        self.assertIn("expected_product_codes 缺少已查询商品 code：C、D", str(raised.exception))
        self.assertIn("expected_single_codes 缺少已查询商品 code：E、F", str(raised.exception))
        self.assertTrue(opt.Request(self.request()).sources)
        self.assertTrue(opt.Request(self.request(expected_product_codes=[], expected_single_codes=["A"])).sources)
        for key in ("expected_product_codes", "expected_single_codes"):
            for value in (None, "A", {}, [True]):
                with self.subTest(key=key, value=value), self.assertRaises(opt.InputError):
                    opt.Request(self.request(**{key: value}))

    def test_checklists_use_merged_real_codes_not_coupon_food_or_choices(self):
        coupon = {"code": "COUPON", "food_code": "FOOD", "price": "0",
                  "coupon": {"couponId": "K", "couponCodes": ["ticket"], "food_code": "NESTED"}}
        product = combo("C", "套餐", "12", [(1, [choice("CHOICE", "选项")])])
        with tempfile.TemporaryDirectory() as folder:
            self.write_fragment(folder, "products_1.json", {"products": [product, coupon], "singles": [[123, "单品", "2"]]})
            data = self.request(expected_product_codes=["A", "C", "COUPON"], expected_single_codes=[123])
            merged = opt.merge_fragments(data, folder)
            self.assertEqual({s["code"] for s in opt.Request(merged).sources}, {"A", "C", "COUPON", "123"})
            with self.assertRaises(opt.InputError) as raised:
                opt.Request({**merged, "expected_product_codes": ["FOOD", "NESTED", "CHOICE"],
                             "expected_single_codes": ["FOOD"]})
            self.assertIn("CHOICE、FOOD、NESTED", str(raised.exception))
            self.assertIn("expected_single_codes 缺少已查询商品 code：FOOD", str(raised.exception))

    def test_identical_fragment_products_deduplicate_but_conflicts_fail(self):
        product = combo("C", "套餐", "12", [(1, [choice("A", "汉堡")])])
        with tempfile.TemporaryDirectory() as folder:
            first = self.write_fragment(folder, "products_1.json", {"products": [product]})
            second = self.write_fragment(folder, "products_2.json", {"products": [product]})
            self.assertEqual(len(opt.Request(opt.merge_fragments(self.request(), folder)).sources), 2)
            self.write_fragment(folder, "products_2.json", {"products": [{**product, "currentPrice": "13"}]})
            with self.assertRaises(opt.InputError) as raised:
                opt.merge_fragments(self.request(), folder)
            for field in (first, second, "冲突重复 product：C"):
                self.assertIn(field, str(raised.exception))

    def test_single_and_round_product_with_same_sid_are_not_merged(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write_fragment(folder, "products_1.json", {"products": [combo("A", "套餐", "10", [(1, [choice("A", "汉堡")])])]})
            with self.assertRaisesRegex(opt.InputError, "冲突重复 product：A"):
                opt.merge_fragments(self.request(singles=[["A", "汉堡", "10"]], products=[]), folder)

    def test_cli_errors_do_not_write_or_overwrite_plan(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimizer.py")
        for bad in (self.request(effort={"menu_items": -1}, products=[single("A", "A", "bad"), single("B", "B", None)]),
                    self.request(expected_product_codes=["C", "D"], expected_single_codes=["E", "F"])):
            with tempfile.TemporaryDirectory() as folder:
                self.write_fragment(folder, "request.json", bad)
                output = os.path.join(folder, "plan.json")
                for existing in (False, True):
                    if existing:
                        self.write_fragment(folder, "plan.json", {"unchanged": True})
                    done = subprocess.run([sys.executable, "-B", script, "plan", folder], capture_output=True, text=True)
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn("输入错误", json.loads(done.stderr)["error"])
                    self.assertEqual(done.stdout, "")
                    if existing:
                        with open(output, encoding="utf-8") as handle:
                            self.assertEqual(json.load(handle), {"unchanged": True})
                    else:
                        self.assertFalse(os.path.exists(output))
        with tempfile.TemporaryDirectory() as folder:
            self.write_fragment(folder, "request.json", self.request())
            fragment = self.write_fragment(folder, "products_bad.json", {"demand_codes": {"a": "B"}, "demand_pieces": {"missing": {}}})
            done = subprocess.run([sys.executable, "-B", script, "plan", folder], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, done.stderr)
            self.assertIn(fragment + ".demand_codes.a", json.loads(done.stderr)["error"])
            self.assertIn(fragment + ".demand_pieces.missing", json.loads(done.stderr)["error"])
            self.assertFalse(os.path.exists(os.path.join(folder, "plan.json")))


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


class DpHotpathRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ast
        import inspect
        import textwrap

        dp = ast.parse(textwrap.dedent(inspect.getsource(opt.Solver._dp))).body[0]
        retain = next(node for node in dp.body if isinstance(node, ast.FunctionDef) and node.name == "retain")
        scope = dict(vars(opt))
        exec(compile(ast.Module(body=[retain], type_ignores=[]), "<retain>", "exec"), scope)
        cls.retain = staticmethod(scope["retain"])
        retain_source = ast.unparse(retain)
        binary_source = retain_source[retain_source.index("    entry_key ="):retain_source.index("    labels.insert")]
        scope = dict(vars(opt))
        exec("def position(labels, entry):\n" + binary_source + "    return lo\n", scope)
        cls.position = staticmethod(scope["position"])
        transition = next(node for node in ast.walk(dp) if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "next_signature" for target in node.targets))
        cls.signature_expression = compile(ast.Expression(body=transition.value), "<signature>", "eval")

    def stats(self):
        return {"nodes": 0, "candidate_nodes": 0, "combos": 0,
                "complete": True, "candidates_complete": True}

    def assert_phase_totals(self, stats):
        for phase, field in (("minimum", "nodes"), ("candidate", "candidate_nodes")):
            counters = stats["dp"].get(phase, {"config_nodes": 0, "transition_nodes": 0})
            self.assertEqual(counters["config_nodes"] + counters["transition_nodes"], stats[field])

    def test_manual_binary_search_matches_bisect_left(self):
        import bisect

        rng = random.Random(20261017)
        for size in (0, 1, 2, 5, 20, 80):
            for _ in range(80):
                labels = sorted([(rng.randrange(5), rng.randrange(4), ((rng.randrange(3), rng.randrange(4)),), object())
                                 for _ in range(size)], key=lambda entry: entry[:3])
                entry = rng.choice(labels) if labels and rng.randrange(2) else (2, 1, ((1, 2),), object())
                self.assertEqual(self.position(labels, entry), bisect.bisect_left([e[:3] for e in labels], entry[:3]))

    def test_signature_insertion_matches_sorted_tuple(self):
        import bisect

        rng = random.Random(880)
        for size in (0, 1, 2, 4, 8, 16, 32):
            for _ in range(100):
                signature = tuple(sorted((rng.randrange(5), rng.randrange(8)) for _ in range(size)))
                item = (rng.randrange(5), rng.randrange(8))
                position = bisect.bisect_left(signature, item)
                actual = eval(self.signature_expression, {}, {"signature": signature, "item": item, "position": position})
                self.assertEqual(actual, tuple(sorted(signature + (item,))))

    def test_phase_totals_include_cache_hits_and_early_stops(self):
        data = {"context": dict(CONTEXT), "products": [single("A", "A", "20"),
                combo("C", "两份餐", "10", [(2, [choice("A", "A", default=1), choice("B", "B")])])],
                "demands": [{"id": "a", "quantity": 2, "codes": ["A"]}]}
        solver = opt.Solver(opt.Request(data))
        stats = self.stats()
        seeds = solver._dp("none", solver.r.demands, None, 1, None, stats)
        self.assertTrue(seeds)
        self.assertGreater(stats["dp"]["minimum"]["config_nodes"], 0)
        self.assertGreater(stats["dp"]["minimum"]["transition_nodes"], 0)
        solver._dp("none", solver.r.demands, None, 1, None, stats)
        self.assertGreater(stats["dp"]["minimum"]["config_cache_hits"], 0)
        solver._dp("none", solver.r.demands, None, 5, None, stats, candidates=True, seeds=seeds)
        self.assert_phase_totals(stats)
        for budget in (0, 1, 3, 15):
            with self.subTest(budget=budget):
                local = self.stats()
                fresh = opt.Solver(opt.Request(data))
                fresh._dp("none", fresh.r.demands, None, 1, None, local, budget=budget)
                self.assert_phase_totals(local)
                if local["dp"]["minimum"]["config_fallbacks"]:
                    self.assertFalse(local["complete"])
                    self.assertEqual(local["dp"]["minimum"]["config_fallback_reasons"], {"node_limit": 1})
                cached = self.stats()
                solver._dp("none", solver.r.demands, None, 1, None, cached, budget=budget)
                self.assert_phase_totals(cached)
        empty = self.stats()
        self.assertEqual(solver._dp("none", solver.r.demands, None, 1, None, empty, sources=[]), [])
        self.assert_phase_totals(empty)
        unreachable = self.stats()
        self.assertEqual(solver._dp("none", solver.r.demands, None, 1, None, unreachable,
                                   sources=[{**solver.r.sources[0], "limit": 1}]), [])
        self.assertEqual(unreachable["dp"]["minimum"]["transition_nodes"], 0)
        self.assert_phase_totals(unreachable)

    def test_solve_totals_include_quantity_fallback(self):
        for extra in ({"node_limit": 3}, {"node_limit": 1},
                      {"demands": [{"id": "a", "quantity": 1, "codes": ["1440"]},
                                   {"id": "b", "quantity": 1, "codes": ["1440"]}]}):
            with self.subTest(extra=extra):
                _, stats, _ = opt.Solver(opt.Request(store_request(**extra))).solve("none", keep=5)
                for phase, total, fallback in (("minimum", "nodes", "quantity_nodes"),
                                               ("candidate", "candidate_nodes", "candidate_quantity_nodes")):
                    dp = stats.get("dp", {}).get(phase, {})
                    self.assertEqual(dp.get("config_nodes", 0) + dp.get("transition_nodes", 0) + stats[fallback], stats[total])

    def test_candidate_config_fallback_preserves_minimum_seed(self):
        data = {"context": dict(CONTEXT), "products": [single("A", "A", "20"),
                combo("C", "双份餐", "10", [(2, [choice("A", "A", default=1), choice("B", "B")])])],
                "demands": [{"id": "a", "quantity": 2, "codes": ["A"]}]}
        for clear_cache in (False, True):
            for budget in (0, 1, 3):
                with self.subTest(clear_cache=clear_cache, budget=budget):
                    outcome = []
                    solver = opt.Solver(opt.Request(data))
                    stats = self.stats()
                    seeds = solver._dp("none", solver.r.demands, None, 1, None, stats)
                    for seed in seeds:
                        solver._materialize(seed)
                    if clear_cache:
                        solver._config_cache.clear()
                    plans = solver._dp("none", solver.r.demands, None, 5, None, stats,
                                       candidates=True, budget=budget, seeds=seeds)
                    self.assertTrue(plans)
                    self.assertIn(seeds[0]["key"], [p["key"] for p in plans])
                    self.assertEqual(plans[0]["cost"], seeds[0]["cost"])
                    self.assertTrue(stats["complete"])
                    self.assertFalse(stats["candidates_complete"])
                    outcome.append((plans, stats))
                    counters = outcome[-1][1]["dp"]["candidate"]
                    self.assertEqual(counters["config_nodes"], budget // 2)
                    self.assertEqual(counters["config_fallbacks"], 1)
                    self.assertEqual(counters["config_fallback_reasons"], {"candidate_half_budget": 1})
                    self.assertGreater(counters["config_cache_misses"], 0)
                    self.assertGreater(counters["uncached_config_skips"] if clear_cache else counters["reduced_config_cache_hits"], 0)
                    self.assertLessEqual(outcome[-1][1]["candidate_nodes"], budget)
                    self.assert_phase_totals(outcome[-1][1])


def overlap_oracle(data):
    """独立穷举 oracle：枚举“每种商品买几份 + 套餐每轮选什么”，按“同一份餐品只能顶一个需求”
    判定可行与成本。返回 (最低金额分 或 None, 达到最低金额的购物车集合)。

    不调用求解器的 DP/流/variants/covers：可行性由“把每个 code 的份数分给拥有它的需求”的
    独立枚举判定（非严格模式允许多买的份数不顶任何需求），与求解器内部实现互不依赖。
    只覆盖非块数需求（块数需求整份不可拆，另有变体回退覆盖）。
    """
    demands = data["demands"]
    assert not any("pieces" in d for d in demands), "oracle 只覆盖非块数需求"
    targets = [d.get("quantity") for d in demands]
    demand_codes = {code for d in demands for code in d["codes"]}
    strict = bool(data.get("strict"))
    owner = {code: [i for i, d in enumerate(demands) if code in d["codes"]] for code in demand_codes}

    def cents(value):
        text = str(value).replace("¥", "").replace("￥", "").replace(" ", "")
        return int(Decimal(text or 0) * 100)

    configs = []  # (整份成本, 供给, 来源下标)
    limits = []
    for index, raw in enumerate(data["products"]):
        coupon = raw.get("coupon")
        price = cents(raw["price"] if coupon else raw["currentPrice"]) + cents((coupon or {}).get("paid", 0))
        limit = raw.get("maxQuantity")
        if coupon:
            limit = len(coupon["couponCodes"]) if limit is None else min(limit, len(coupon["couponCodes"]))
        limits.append(limit)
        if not raw.get("rounds"):
            configs.append((price, Counter({raw.get("food_code", raw["code"]): 1}), index))
            continue
        per_round = []
        for rnd in raw["rounds"]:
            choices = rnd["choices"]
            fills = []
            for picked in itertools.combinations_with_replacement(choices, rnd["minQuantity"]):
                counts = Counter(ch["code"] for ch in picked)
                if any(ch.get("maxQuantity", -1) >= 0 and counts[ch["code"]] > ch["maxQuantity"] for ch in choices):
                    continue
                fills.append((sum(cents(ch.get("diffPrice", 0)) for ch in picked), counts))
            per_round.append(fills)
        for parts in itertools.product(*per_round):
            supply = Counter()
            for _, counts in parts:
                supply.update(counts)
            configs.append((price + sum(cost for cost, _ in parts), supply, index))
    assert all(cost > 0 for cost, _, _ in configs), "oracle 的上界论证要求每个完整配置成本为正"

    def splits(total, size):  # 把 total 份分到 size 个桶里的全部组合
        if size == 1:
            return [(total,)]
        result = []
        for first in range(total + 1):
            for rest in splits(total - first, size - 1):
                result.append((first,) + rest)
        return result

    cache = {}

    def feasible(supply):
        cart = tuple(sorted(supply.items()))
        if cart in cache:
            return cache[cart]
        codes = [code for code, count in cart if count and code in demand_codes]
        if strict:
            if sum(supply.values()) != sum(targets) or any(code not in demand_codes for code in supply if supply[code]):
                cache[cart] = False
                return False

        def walk(position, got):
            if position == len(codes):
                return all(g >= t for g, t in zip(got, targets)) and (not strict or all(g == t for g, t in zip(got, targets)))
            code = codes[position]
            buckets = list(owner[code]) if strict else list(owner[code]) + [-1]  # -1：这份不顶任何需求
            for parts in splits(supply[code], len(buckets)):
                nxt = list(got)
                for slot, amount in enumerate(parts):
                    if buckets[slot] >= 0:
                        nxt[buckets[slot]] += amount
                if walk(position + 1, nxt):
                    return True
            return False

        cache[cart] = walk(0, [0] * len(demands))
        return cache[cart]

    best = [None, set()]
    max_units = sum(targets)  # 每个配置成本为正：最优购物车不会买多余份数，购买份数不超过需求合计

    def walk(start, cost, supply, used, size):
        if size and feasible(supply):
            cart = tuple(sorted(supply.items()))
            if best[0] is None or cost < best[0]:
                best[0], best[1] = cost, {cart}
            elif cost == best[0]:
                best[1].add(cart)
            return
        if size >= max_units:
            return
        for i in range(start, len(configs)):
            added, added_supply, index = configs[i]
            if limits[index] is not None and used[index] >= limits[index]:
                continue
            used[index] += 1
            walk(i, cost + added, supply + added_supply, used, size + 1)
            used[index] -= 1

    walk(0, 0, Counter(), [0] * len(data["products"]), 0)
    return best[0], best[1]


class OverlapDpRegressionTest(unittest.TestCase):
    """重叠需求（一个 code 同属多个需求）走加权覆盖 DP 的回归。

    语义底线：同一份实体餐品只能顶掉一个需求。A 同属需求0/需求1 时，买 2 份 A 只能算
    “2 份需求0”“2 份需求1”或“各 1 份”，不能两边各算 2 份。这类输入走加权覆盖 DP，
    共享 code 落在块数需求时才回退 variant 数量搜索；这里用独立穷举 oracle 对照最低金额与购物车。
    """

    def request(self, products, demands, **extra):
        return {"context": dict(CONTEXT), "products": products, "demands": demands, **extra}

    def check_oracle(self, data, keep=1):
        expected, carts = overlap_oracle(data)
        solver = opt.Solver(opt.Request(data))
        plans, stats, _ = solver.solve("none", keep=keep)
        self.assertTrue(stats["complete"], stats)
        self.assertEqual(stats["engine"], "weighted_dp", "重叠需求也应走加权覆盖 DP")
        self.assertFalse(stats.get("quantity_nodes"), "重叠需求不应再回退到数量搜索")
        if expected is None:
            self.assertFalse(plans, data)
            return plans
        self.assertTrue(plans, data)
        self.assertEqual(plans[0]["cost"], expected, data)
        # 购物车必须落在 oracle 的某个最优购物车上
        self.assertIn(tuple(sorted(plans[0]["supply"].items())), carts, data)
        for plan in plans:
            self.assertGreaterEqual(plan["cost"], expected, data)
            used = Counter()
            for demand in data["demands"]:
                taken = plan["allocation"].get(demand["id"], {})
                self.assertTrue(set(taken).issubset(demand["codes"]), plan["allocation"])
                used.update(taken)
                self.assertGreaterEqual(sum(taken.values()), demand["quantity"])
            # 同一份餐品不能同时顶两个需求
            self.assertTrue(all(used[code] <= plan["supply"].get(code, 0) for code in used), plan["allocation"])
            if data.get("strict"):
                self.assertEqual(dict(used), plan["supply"], plan["allocation"])
        return plans

    def test_shared_single_code_is_not_double_counted(self):
        """2 份 A 只能顶一个需求：正确解是 2A+2B（¥26），不是只买 2 份 A（¥20）。"""
        products = [single("A", "A", "10"), single("B", "B", "3")]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A"]}]
        plans = self.check_oracle(self.request(products, demands))
        self.assertEqual(plans[0]["cost"], 2600)
        self.assertEqual(plans[0]["supply"], {"A": 2, "B": 2})
        self.assertEqual(plans[0]["allocation"], {"d0": {"B": 2}, "d1": {"A": 2}})

    def test_shared_code_inside_combo_rounds(self):
        """共享 code 出现在套餐选项里：一份套餐选 2 份 A 顶 d1，d0 另买 2 份 B，合计 ¥18。

        关键点是套餐提供的共享 A 也按“份数”记帐，且不能在同一份上给两个需求各记一次：
        ¥24（两份套餐共 4 份 A）和 ¥10（只买 2 份 A 双计）都不是答案。
        """
        products = [single("A", "A", "10"), single("B", "B", "3"),
                    combo("K", "双人餐", "12", [(2, [choice("A", "A", default=1), choice("B", "B")])])]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A"]}]
        plans = self.check_oracle(self.request(products, demands), keep=5)
        self.assertEqual(plans[0]["cost"], 1800)
        self.assertEqual(plans[0]["supply"], {"A": 2, "B": 2})

    def test_shared_code_as_single_and_inside_combo(self):
        """同一个共享 code 既有单点又出现在套餐里，且套餐带限量。"""
        products = [single("A", "A", "10"), single("B", "B", "6"),
                    combo("K", "A+B餐", "9", [(2, [choice("A", "A", default=1, maxQuantity=1),
                                                   choice("B", "B", maxQuantity=1)])])]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A"]}]
        self.assertEqual(self.check_oracle(self.request(products, demands))[0]["cost"], 1800)
        limited = products[:2] + [{**products[2], "maxQuantity": 1}]
        plans = self.check_oracle(self.request(limited, demands), keep=5)
        self.assertEqual(plans[0]["cost"], 2500)  # 套餐限 1 份：1 份套餐 + A + B
        self.assertEqual(plans[0]["supply"], {"A": 2, "B": 2})

    def test_shared_code_with_limit_on_single(self):
        """共享的 A 不是最便宜的：d0 用 2 份 B、d1 用 2 份 C，合计 ¥26 优于 2A+2B 的 ¥32。"""
        products = [{"code": "A", "name": "A", "currentPrice": "10", "originalPrice": "10", "maxQuantity": 2},
                    single("B", "B", "6"), single("C", "C", "7")]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A", "C"]}]
        plans = self.check_oracle(self.request(products, demands), keep=5)
        self.assertEqual(plans[0]["cost"], 2600)  # 2B + 2C
        self.assertEqual(plans[0]["supply"], {"B": 2, "C": 2})

    def test_strict_mode_shared_code(self):
        """严格模式：1 份 A 只能顶一个需求，最省是 B+C（¥7），不是 1 份 A（¥10）。"""
        products = [single("A", "A", "10"), single("B", "B", "3"), single("C", "C", "4")]
        demands = [{"id": "d0", "label": "d0", "quantity": 1, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 1, "codes": ["A", "C"]}]
        plans = self.check_oracle(self.request(products, demands, strict=True), keep=5)
        self.assertEqual(plans[0]["cost"], 700)
        self.assertEqual(plans[0]["supply"], {"B": 1, "C": 1})

    def test_three_demands_share_two_codes(self):
        """三个需求两两共享：同一份餐品不能在三个需求里各算一次。"""
        products = [single("A", "A", "10"), single("B", "B", "6"), single("C", "C", "4")]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d2", "label": "d2", "quantity": 1, "codes": ["A", "C"]}]
        plans = self.check_oracle(self.request(products, demands), keep=5)
        self.assertEqual(plans[0]["cost"], 2800)  # d0/d1 各 2 份 B、d2 1 份 C；5 份 A 要 ¥50
        self.assertEqual(plans[0]["supply"], {"B": 4, "C": 1})

    def random_case(self, rng, strict):
        foods = ["A", "B", "C"]
        products = [single(food, food, str(rng.randint(3, 12))) for food in foods]
        rounds = [(rng.randint(1, 2), [choice(food, food, "%+d" % rng.randint(0, 2), default=int(i == 0))
                                       for i, food in enumerate(rng.sample(foods, rng.randint(2, 3)))])]
        products.append(combo("K", "餐", str(rng.randint(6, 20)), rounds))
        if rng.random() < 0.5:
            products[rng.randrange(len(products))]["maxQuantity"] = rng.randint(1, 3)
        if rng.random() < 0.4:
            products[-1]["maxQuantity"] = rng.randint(1, 2)
        first = [foods[0]] + [food for food in foods[1:] if rng.random() < 0.6]
        second = [foods[0]] + [food for food in foods[1:] if food not in first and rng.random() < 0.6]
        demands = [{"id": "d0", "label": "d0", "quantity": rng.randint(1, 2), "codes": first},
                   {"id": "d1", "label": "d1", "quantity": rng.randint(1, 2), "codes": second}]
        if rng.random() < 0.5:
            demands.append({"id": "d2", "label": "d2", "quantity": 1, "codes": rng.sample(foods, rng.randint(1, 2))})
        return self.request(products, demands, strict=strict)

    def test_random_overlap_cases_match_oracle(self):
        for strict in (False, True):
            for seed in range(40):
                data = self.random_case(random.Random(1000 + seed), strict)
                with self.subTest(seed=seed, strict=strict):
                    self.check_oracle(data, keep=3)

    def test_forced_variant_fallback_is_never_cheaper(self):
        """把重叠需求强行打回 variant 数量搜索：金额不应比加权 DP 更低（可以相等）。"""
        products = [single("A", "A", "10"), single("B", "B", "6"), single("C", "C", "7"),
                    combo("K", "双人餐", "15", [(2, [choice("A", "A", default=1), choice("C", "C")])])]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A", "C"]}]
        data = self.request(products, demands)
        plans = self.check_oracle(data, keep=3)
        with patch.object(opt.Solver, "_overlap_dims", return_value=None):
            fallback_plans, fallback_stats, _ = opt.Solver(opt.Request(data)).solve("none", keep=3)
        self.assertIn(fallback_stats["engine"], ("quantity_search", "adaptive"))
        self.assertTrue(fallback_stats["complete"], fallback_stats)
        self.assertGreaterEqual(fallback_plans[0]["cost"], plans[0]["cost"])

    def test_disjoint_demands_do_not_use_overlap_dims(self):
        """不重叠的输入必须完全走原路径（_overlap_dims 不启用）。"""
        solver = opt.Solver(opt.Request(store_request()))
        self.assertIsNone(solver._overlap_dims(solver.r.demands))

    def test_pieces_overlap_still_falls_back(self):
        """块数需求重叠时整份不可拆，子集份数记帐不适用，继续走 variant 回退。"""
        products = [single("N4", "四块", "4"), single("N5", "五块", "5")]
        demands = [{"id": "n", "label": "n", "quantity": 8, "codes": ["N4", "N5"], "pieces": {"N4": 4, "N5": 5}},
                   {"id": "x", "label": "x", "quantity": 1, "codes": ["N4"]}]
        data = self.request(products, demands)
        solver = opt.Solver(opt.Request(data))
        self.assertIsNone(solver._overlap_dims(solver.r.demands))
        plans, stats, _ = solver.solve("none", keep=3)
        self.assertIn(stats["engine"], ("quantity_search", "adaptive"))
        self.assertTrue(stats["complete"])
        # 独立穷举：3 份四块装（n 拿 2 份=8 块，x 拿 1 份）¥12；两份五块装还要再补 x 的 ¥4
        self.assertEqual(plans[0]["cost"], 1200)
        self.assertEqual(plans[0]["supply"], {"N4": 3})

    def test_isolated_pieces_demand_with_overlap_uses_dp(self):
        """块数需求不共享 code 时可以和重叠需求并存，仍然走加权 DP。"""
        products = [single("A", "A", "10"), single("B", "B", "3"),
                    single("N4", "四块", "4"), single("N5", "五块", "5")]
        demands = [{"id": "d0", "label": "d0", "quantity": 2, "codes": ["A", "B"]},
                   {"id": "d1", "label": "d1", "quantity": 2, "codes": ["A"]},
                   {"id": "n", "label": "n", "quantity": 9, "codes": ["N4", "N5"], "pieces": {"N4": 4, "N5": 5}}]
        data = self.request(products, demands)
        solver = opt.Solver(opt.Request(data))
        self.assertIsNotNone(solver._overlap_dims(solver.r.demands))
        plans, stats, _ = solver.solve("none", keep=3)
        self.assertEqual(stats["engine"], "weighted_dp")
        self.assertTrue(stats["complete"])
        # d1 独占 A（2 份 ¥20）+ d0 用 2 份 B（¥6）= ¥26；n 要 9 块，一份四块装+一份五块装 = ¥9
        self.assertEqual(plans[0]["cost"], 3500)
        self.assertEqual(plans[0]["allocation"]["n"], {"N4": 1, "N5": 1})

    @staticmethod
    def pieces_oracle(products, demands):
        """块数需求的独立穷举：枚举每种商品买几份，再把每一份整份分给某个需求（或不用）。"""
        best = None
        for counts in itertools.product(*[range(6) for _ in products]):
            cost = sum(c * int(p["currentPrice"]) * 100 for c, p in zip(counts, products) if c)
            if best is not None and cost >= best:
                continue
            boxes = []
            for c, p in zip(counts, products):
                boxes += [p["code"]] * c
            for assigned in itertools.product(range(len(demands) + 1), repeat=len(boxes)):
                got = [0] * len(demands)
                ok = True
                for index, slot in enumerate(assigned):
                    if slot == len(demands):  # 这份不顶任何需求
                        continue
                    demand = demands[slot]
                    if boxes[index] not in demand["codes"]:
                        ok = False
                        break
                    got[slot] += demand.get("pieces", {}).get(boxes[index], 1)
                if ok and all(g >= d["quantity"] for g, d in zip(got, demands)):
                    best = cost
                    break
        return best

    def test_mixed_pieces_and_overlap_matches_split_oracle(self):
        """块数需求与重叠需求的 code 不交叉，最优解可拆成两块分别用独立 oracle 验证。"""
        for seed in range(8):
            rng = random.Random(3000 + seed)
            products = [single("A", "A", str(rng.randint(4, 12))), single("B", "B", str(rng.randint(3, 10))),
                        single("C", "C", str(rng.randint(3, 10))),
                        single("N4", "四块", str(rng.randint(3, 6))), single("N5", "五块", str(rng.randint(4, 8)))]
            plain = [{"id": "d0", "label": "d0", "quantity": rng.randint(1, 2), "codes": ["A", "B"]},
                     {"id": "d1", "label": "d1", "quantity": rng.randint(1, 2), "codes": ["A", "C"]}]
            pieces = [{"id": "n", "label": "n", "quantity": rng.randint(6, 12), "codes": ["N4", "N5"],
                       "pieces": {"N4": 4, "N5": 5}}]
            data = self.request(products, plain + pieces)
            expected = (overlap_oracle(self.request(products, plain))[0]
                        + self.pieces_oracle(products[3:], pieces))
            with self.subTest(seed=seed):
                solver = opt.Solver(opt.Request(data))
                self.assertIsNotNone(solver._overlap_dims(solver.r.demands))
                plans, stats, _ = solver.solve("none", keep=3)
                self.assertEqual(stats["engine"], "weighted_dp")
                self.assertTrue(stats["complete"])
                self.assertEqual(plans[0]["cost"], expected)


class BudgetRecoveryRegressionTest(unittest.TestCase):
    """数量回退的预算回收与完成标记：

    * 一份 node_limit 预算在 variant 之间共享并回收：某个 variant 提前展开完，剩下的预算要
      交给后面的 variant，而不是按最初均分值一直分下去；
    * 命中预算不等于没搜完：堆里已经没有待展开项时，把最后一项展开完就是搜完了。
    """
    def variants_case(self, node_limit):
        """重叠需求 + 块数分配：回退搜索要跑 6 个 variant，第一个 variant 的套餐上限很小。"""
        products = [single("N4", "四块", "4"), single("N5", "五块", "5")]
        for i in range(4):
            products.append(combo("K%d" % i, "餐%d" % i, "%d" % (10 + i),
                                  [(1, [choice("N4", "四块", default=1)])]))
        return {"context": dict(CONTEXT), "node_limit": node_limit, "products": products,
                "demands": [{"id": "n", "quantity": 21, "codes": ["N4", "N5"], "pieces": {"N4": 4, "N5": 5}},
                            {"id": "x", "quantity": 1, "codes": ["N5"]}]}

    def chain_case(self, node_limit):
        """组合数很少：套餐只含一份 A，展开两层就到头，最后堆里只剩一个叶子。"""
        return {"context": dict(CONTEXT), "node_limit": node_limit, "products": [
            single("A", "A", "20"), single("B", "B", "15"),
            combo("K", "A餐", "5", [(1, [choice("A", "A", default=1)])])],
            "demands": [{"id": "a", "quantity": 1, "codes": ["A", "B"]},
                        {"id": "b", "quantity": 1, "codes": ["A"]}]}

    def trace(self, data, keep=1):
        """跑一次 solve，并记录每次 _search 的预算、实际节点数与完成标记。"""
        calls = []
        original = opt.Solver._search

        def spy(self, ctx, demands, keep_, notes, stats, best, keys, coupon=None, budget=None):
            before = stats["nodes"]
            original(self, ctx, demands, keep_, notes, stats, best, keys, coupon, budget)
            calls.append({"budget": budget, "used": stats["nodes"] - before,
                          "complete": stats["complete"], "minimum": "candidate_nodes" in stats})

        with patch.object(opt.Solver, "_search", spy):
            plans, stats, _ = opt.Solver(opt.Request(data)).solve("none", keep=keep)
        return plans, stats, calls

    def test_fallback_budget_is_shared_and_recycled(self):
        limit = 61
        data = self.variants_case(limit)
        plans, stats, calls = self.trace(data)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(call["minimum"] for call in calls))
        used = [call["used"] for call in calls]
        share = limit // len(calls)
        # 第一个 variant 用不掉均分预算，余量回收后第二个 variant 拿到比均分更多的节点
        self.assertLess(used[0], share)
        self.assertEqual(calls[1]["budget"], (limit - used[0]) // (len(calls) - 1))
        self.assertGreater(used[1], share)
        self.assertLessEqual(sum(used), limit)
        self.assertLessEqual(stats["nodes"], limit)
        # 回收来的预算让结果不比更大预算的那次差
        rich = opt.Solver(opt.Request(self.variants_case(100000)))
        rich_plans, rich_stats, _ = rich.solve("none", keep=1)
        self.assertTrue(rich_stats["complete"])
        self.assertEqual([p["cost"] for p in plans], [p["cost"] for p in rich_plans])

    def test_budget_hit_with_empty_heap_stays_complete(self):
        data = self.chain_case(2)  # 预算刚好等于已展开数量：最后一项是叶子，展开完就到头
        solver = opt.Solver(opt.Request(data))
        plans, stats, calls = self.trace(data)
        self.assertTrue(stats["complete"])
        self.assertEqual(plans[0]["cost"], 1000)  # 两份 A 餐
        # 放开预算单独跑同一个 variant：收尾节点也展开完了，节点数与结果都完全一致
        best = []
        unlimited = {"nodes": 0, "complete": True, "combos": 0}
        solver._search("none", solver.r.demands, 1, None, unlimited, best, set(), None, 10 ** 9)
        self.assertTrue(unlimited["complete"])
        self.assertEqual(calls[0]["used"], unlimited["nodes"])
        self.assertEqual([p["key"] for p in plans], [entry[3]["key"] for entry in best])

    def test_completion_flag_agrees_in_candidate_phase(self):
        plans, stats, calls = self.trace(self.chain_case(2), keep=3)
        self.assertTrue(any(call["minimum"] for call in calls))
        self.assertTrue(any(not call["minimum"] for call in calls))  # 备选阶段同样走 _search
        self.assertTrue(all(call["complete"] for call in calls))  # 都记在各自的 complete 上
        self.assertTrue(stats["complete"])


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
        self.assertEqual(first["notes"], ["同样这些单点要¥118.50，省¥58.70"])
        self.assertEqual(first["blocks"], [
            {"kind": "combo", "title": "全明星双人分享餐八件套×1", "items": [
                {"name": "麦辣鸡腿汉堡", "quantity": 1, "demanded": True}, {"name": "板烧鸡腿堡", "quantity": 1, "demanded": True},
                {"name": "麦乐鸡4块", "quantity": 1, "demanded": True}, {"name": "麦辣鸡翅2块", "quantity": 1, "demanded": True},
                {"name": "中薯条", "quantity": 1, "demanded": True}, {"name": "中薯条", "quantity": 1, "demanded": False},
                {"name": "可乐中杯", "quantity": 2, "demanded": False}]},
            {"kind": "card", "title": "麦金卡体验卡（15天） ¥9.90", "items": []}])  # 22.5+23.5+11.5+15+13.5×2+9.5×2
        message = picked["message"]
        self.assertIn("| | **方案1 · 最低实付** | **方案2 · 不开卡** |", message)
        self.assertIn("| 实付 | **¥59.80** | **¥87.50** |", message)
        self.assertIn("| 怎么买 | 【套餐】全明星双人分享餐八件套×1：**麦辣鸡腿汉堡×1**、**板烧鸡腿堡×1**", message)
        self.assertIn("可乐中杯×2 | 【单点】**麦乐鸡5块×1**、**麦辣鸡腿汉堡×2**", message)
        self.assertIn("【套餐】全明星双人分享餐八件套×1：**麦辣鸡腿汉堡×2**、", message)  # 同一套餐两轮都选麦辣时合并
        self.assertIn("|  | 【随单开卡】麦金卡体验卡（15天） ¥9.90 |", message)
        self.assertIn("分别按「不开卡」和「随单开麦金卡体验卡（15天）」算", message)
        self.assertIn("全按最便宜的单点买要 ¥87.50；最省的方案 ¥59.80，少花 **¥27.70**", message)
        cards = picked["cards_html"]
        self.assertTrue(cards.startswith("<div>") and cards.endswith("</div></div>"))
        self.assertIn('<h2 class="sr-only">', cards)
        self.assertEqual(cards.count("border-radius:var(--border-radius-lg)"), len(labels))
        self.assertIn('<span style="font-weight:500;color:var(--color-text-primary);">麦辣鸡腿汉堡×1</span>', cards)
        self.assertIn('<span style="color:var(--color-text-tertiary);">可乐中杯×2</span>', cards)
        self.assertIn("方案1 · 最低实付", cards)
        self.assertNotIn("<script", cards)
        self.assertNotIn("<!--", cards)
        # 图例单独成段（紧贴表格、在卡片之前），不在 footer 里
        self.assertEqual(picked["legend"], "加粗是你点名要的，灰色是套餐附带多送的。")
        self.assertNotIn("加粗是你点名要的", picked["footer"])
        self.assertTrue(picked["footer"].startswith("💡 **结论**"))
        self.assertTrue(picked["title"].startswith("**🕵️ 麦侦探"))
        # 输出顺序：地址说明 → 侦查过程 → 图例 → 方案表格 → 其余说明
        self.assertTrue(picked["effort_line"].startswith("🔍 **侦查过程**"))
        self.assertEqual(picked["message"], "\n\n".join([picked["title"], picked["effort_line"],
                                                        picked["legend"], picked["table_markdown"], picked["footer"]]))
        self.assertNotIn("侦查过程", picked["footer"])

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
        self.assertIn({"kind": "card", "title": "麦金卡月卡（30天） ¥19.00", "items": []}, up["blocks"])
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
        plans = padded.solve("none")[0]
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

    def test_coupon_payload_uses_the_real_product_code(self):
        """券商品的 code 只是内部名时（真实编码在 food_code），
        官方计价的 productCode 必须是真实编码——传内部名会被整单拒绝（600012）。"""
        products = [single("A", "A", "20"),
                    {"code": "COUPON_1", "name": "A券", "price": "0", "food_code": "9900008604",
                     "coupon": {"couponId": "K1", "couponCodes": ["c1"], "title": "A券"}}]
        request = self.base_request(products, [{"id": "a", "label": "A", "quantity": 1, "codes": ["9900008604", "A"]}])
        best = best_quote(opt.plan(request))
        coupon_lines = [i for i in best["payload"]["items"] if i.get("couponId")]
        self.assertEqual([i["productCode"] for i in coupon_lines], ["9900008604"])

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
        self.assertIn("**🕵️ 麦侦探 · 测试门店 · 到店取餐**", message)
        self.assertIn("翻了本店 120 款菜单；拆开 1 个套餐/随心配/双人餐看全部可选项", message)
        self.assertIn("领了 2 张免费券，本单可用券 1 张都试过了", message)
        self.assertIn("用加权覆盖比较单点、套餐换选和券", message)
        self.assertIn("向麦当劳官方核价", message)
        self.assertIn("本次拿到完整价格的候选中，直接单点最省", message)
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

    def test_mcdelivery_fees_and_title(self):
        context = {"storeCode": "1950001", "orderType": 2, "beType": 2, "beCode": "1950001M01", "needTableware": False}
        products = [single("A", "汉堡", "20"), single("B", "薯条", "10"),
                    combo("AB", "套餐", "25", [(1, [choice("A", "汉堡", default=1)]), (1, [choice("B", "薯条", default=1)])])]
        demands = [{"id": "a", "label": "汉堡", "quantity": 1, "codes": ["A"]}, {"id": "b", "label": "薯条", "quantity": 1, "codes": ["B"]}]
        request = {"context": context, "products": products, "demands": demands, "dining": "麦乐送",
                   "store_name": "测试店", "address": "清竹园9号楼"}
        plan = opt.plan(request)
        self.assertTrue(all(q["payload"]["beCode"] == "1950001M01" and q["payload"]["orderType"] == 2 for q in plan["quotes"]))
        results = []
        for quote in plan["quotes"]:  # 官方 data.price 已含配送费 ¥9、打包费 ¥2
            result = quote_result(quote, quote["estimate_cents"] + 1100)
            result.update(deliveryPrice=900, packingPrice=200, tablewarePrice=0)
            results.append(result)
        picked = run_pick(plan, results)
        first = picked["recommendations"][0]
        self.assertEqual(first["price_cents"], 2500 + 1100)
        self.assertEqual(first["fees"], {"配送费": 900, "打包费": 200})
        self.assertIn("含配送费¥9.00、打包费¥2.00", first["notes"])
        self.assertIn("同样这些单点要¥30.00，省¥5.00", first["notes"])  # 只比餐品部分
        self.assertIn("全按最便宜的单点买要 ¥41.00；最省的方案 ¥36.00，少花 **¥5.00**", picked["message"])
        self.assertTrue(picked["title"].startswith("**🕵️ 麦侦探 · 测试店 · 麦乐送（送到清竹园9号楼）"))

    def test_dining_mode_validation_and_title(self):
        good = self.base_request([single("A", "A", "1")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}])
        self.assertEqual(opt.plan({**good, "dining": "到店取餐"})["store"]["mode"], "到店取餐")
        for bad in ({**good, "dining": "麦乐送"}, {**good, "dining": "堂食"}, {**good, "dining": "得来速"},
                    {**good, "dining": "到店取餐", "context": {**good["context"], "beType": 5, "beCode": "X"}},
                    {**good, "context": {"storeCode": "1", "orderType": 2, "beType": 2}}):
            with self.assertRaises(opt.InputError):
                opt.plan(bad)
        result = quote_result(opt.plan(good)["quotes"][0], 100)
        result["deliveryPrice"] = 500
        picked = run_pick(opt.plan(good), [result])
        self.assertIn("超过整单实付", picked["excluded"][0]["reason"])

    def test_footer_lists_how_to_order_without_order_prompt(self):
        plan = opt.plan(store_request(dining="到店取餐"))
        results = [quote_result(q, q["estimate_cents"] + (990 if q["context"] != "none" else 0),
                                ("521234", "麦金卡体验卡（15天）", 990) if q["context"] != "none" else None) for q in plan["quotes"]]
        footer = run_pick(plan, results)["footer"]
        self.assertNotIn("怎么点", footer)
        picked = run_pick(plan, results)
        cards = picked["cards_html"]
        self.assertIn(">套餐</span>", cards)
        self.assertIn(">随单开卡</span>", cards)
        self.assertIn("麦金卡体验卡（15天） ¥9.90", cards)
        self.assertIn("【套餐】全明星双人分享餐八件套×1：**麦辣鸡腿汉堡×1**", picked["table_markdown"])
        self.assertNotIn("回复方案编号", footer)
        self.assertIn("实际以下单页显示为准", footer)

    def test_minimal_choice_fields_are_enough(self):
        # 套餐选项只写 code/name，默认项标 isDefault=1，差价为 0 时省略 diffPrice、maxQuantity 省略
        products = [single("A", "汉堡", "20"), single("D", "咖啡", "9.5"),
                    {"code": "C", "name": "随心配", "currentPrice": "13.9",
                     "rounds": [{"id": 1, "minQuantity": 1, "choices": [{"code": "A", "name": "汉堡"}, {"code": "X", "name": "鸡排", "isDefault": 1}]},
                                {"id": 2, "minQuantity": 1, "choices": [{"code": "D", "name": "咖啡"}, {"code": "Y", "name": "可乐", "isDefault": 1}]}]}]
        demands = [{"id": "a", "label": "汉堡", "quantity": 1, "codes": ["A"]}, {"id": "d", "label": "咖啡", "quantity": 1, "codes": ["D"]}]
        best = best_quote(opt.plan(self.base_request(products, demands)))
        self.assertEqual(best["estimate_cents"], 1390)
        self.assertEqual(best["payload"]["items"][0]["roundList"], [
            {"round": "1", "comboItemList": [{"code": "A", "quantity": 1}]}, {"round": "2", "comboItemList": [{"code": "D", "quantity": 1}]}])

    def test_choice_shorthand_carries_max_quantity(self):
        """BFF四宫格小食盘那类轮次：一轮要选 3 份，但“麦乐鸡-5块”最多只能选 1 份。
        片段用 [code, 名称, 差价, 默认, 上限] 第 5 项抄下来，程序就不会让同一个选项吃满整轮（官方会整单报错）。"""
        rounds = [{"id": 1, "minQuantity": 3, "choices": [["516013", "麦辣鸡翅-2块", "0", 1, 1], ["516014", "麦乐鸡-5块", "0", 0, 1],
                                                          ["521157", "那么大鸡排", "-1", 0, 1]]},
                  {"id": 2, "minQuantity": 1, "choices": [["522012", "小薯条", "0", 1]]}]
        products = [single("1401", "麦乐鸡5块", "14"),
                    {"code": "9900015730", "name": "BFF四宫格小食盘", "currentPrice": "39.8", "originalPrice": "54.5", "rounds": rounds}]
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 15, "codes": ["1401", "516014"], "pieces": {"1401": 5, "516014": 5}}]
        plan = opt.plan(self.base_request(products, demands))
        for quote in plan["quotes"]:
            for item in quote["payload"]["items"]:
                for rnd in item.get("roundList") or []:
                    for pick_item in rnd["comboItemList"]:
                        self.assertLessEqual(pick_item["quantity"], 1)
        # 一盘只能凑出 5 块，15 块全靠盘子就得买 3 盘，而不是一盘里选 3 份（选项上限为 1，官方会整单报错）
        plate_only = [q for q in plan["quotes"] if set(q["allocation"]["n"]) == {"516014"}][0]
        self.assertEqual(plate_only["allocation"]["n"]["516014"], 3)
        self.assertEqual(plate_only["payload"]["items"][0]["quantity"], 3)

    def test_repeated_rejections_hint_at_missing_max_quantity(self):
        """同一原因被官方拒绝多个候选时，pick 要提示“多半漏了 maxQuantity”，别让这些作废默默过去。"""
        products = [single("1401", "麦乐鸡5块", "14"), single("1100", "巨无霸", "25.5"),
                    combo("9900000888", "巨无霸四件套", "32", [
                        (1, [choice("1100", "巨无霸", default=1)]),
                        (1, [choice("4810", "中薯条", default=1)]),
                        (1, [choice("515106", "麦乐鸡4块", default=1)]),
                        (1, [choice("3050", "可乐中杯", default=1)])])]
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 10, "codes": ["1401", "515106"],
                    "pieces": {"1401": 5, "515106": 4}}]
        plan = opt.plan(self.base_request(products, demands))
        self.assertGreaterEqual(len(plan["quotes"]), 3)
        failed = plan["quotes"][:2]
        results = [{"id": q["id"], "error": "麦乐鸡-5块最多可选1份"} for q in failed]
        results += [quote_result(q, q["estimate_cents"]) for q in plan["quotes"][2:]]
        picked = run_pick(plan, results)
        self.assertEqual(len(picked["excluded"]), 2)
        self.assertEqual(len(picked["hints"]), 1)
        self.assertIn("2 个候选被官方用同一原因拒绝", picked["hints"][0])
        self.assertIn("maxQuantity", picked["hints"][0])
        self.assertIn("提示：", opt.console_text("pick", picked, "/tmp/picked.json"))

    def test_each_coupon_gets_its_own_candidate(self):
        """官方计价常拒绝多券同单（600022），但小程序可能允许：每张券都要单独成候选，
        多券同单也额外出一个候选交给官方判定，不在本地提前否掉。"""
        products = [single("A", "A", "20"), single("B", "B", "12"),
                    {"code": "C1", "name": "A券", "price": "5", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "A券"}},
                    {"code": "C2", "name": "B券", "price": "3", "food_code": "B",
                     "coupon": {"couponId": "id2", "couponCodes": ["c2"], "title": "B券"}}]
        demands = [{"id": "a", "label": "A", "quantity": 2, "codes": ["A"]},
                   {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands))
        self.assertTrue(plan["quotes"])
        kinds = set()
        for quote in plan["quotes"]:
            kinds.add(tuple(sorted({i["couponId"] for i in quote["payload"]["items"] if i.get("couponId")})))
        self.assertIn(("id1",), kinds)  # 只用 A 券
        self.assertIn(("id2",), kinds)  # 只用 B 券
        self.assertIn((), kinds)  # 不用券
        self.assertIn(("id1", "id2"), kinds)  # 两张一起（由官方判）

    def test_multi_coupon_rejection_falls_back_to_the_sum_of_coupon_prices(self):
        """官方计价报“暂不支持多张券使用”（600022）只是接口限制，小程序能把两张券放进一单：
        这条报错要忽略，价格按各券券后价合计，并注明以下单页为准，不能把候选作废。"""
        products = [single("A", "A", "20"), single("B", "B", "12"),
                    {"code": "C1", "name": "A券", "price": "5", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "A券"}},
                    {"code": "C2", "name": "B券", "price": "3", "food_code": "B",
                     "coupon": {"couponId": "id2", "couponCodes": ["c2"], "title": "B券"}}]
        demands = [{"id": "a", "label": "A", "quantity": 2, "codes": ["A"]},
                   {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands))

        def coupons_of(quote):
            return [i for i in quote["payload"]["items"] if i.get("couponId")]

        multi = [q for q in plan["quotes"] if len({i["couponId"] for i in coupons_of(q)}) > 1]
        self.assertTrue(multi)
        results = [{"id": q["id"], "error": "暂不支持多张券使用"} for q in multi]
        results += [quote_result(q, q["estimate_cents"]) for q in plan["quotes"] if q not in multi]
        picked = run_pick(plan, results)
        self.assertEqual(picked["excluded"], [])
        self.assertEqual(picked["hints"], [])
        self.assertEqual(picked["valid_count"], len(plan["quotes"]))
        by_id = {q["id"]: q for q in plan["quotes"]}
        first = picked["recommendations"][0]
        self.assertIn(first["quote_id"], [q["id"] for q in multi])
        self.assertEqual(first["price_cents"], by_id[first["quote_id"]]["estimate_cents"])
        self.assertTrue(first["estimated"])
        self.assertTrue(any("各券券后价合计" in n for n in first["notes"]))
        self.assertIn("以下单页为准", "\n".join(first["notes"]))

    def test_single_coupon_error_still_excludes(self):
        """只有一张券却报错（不是 600022）按常规作废，不套用多券的兜底价。"""
        products = [single("A", "A", "20"),
                    {"code": "C1", "name": "A券", "price": "5", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "A券"}}]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.base_request(products, demands))
        coupon_quotes = [q for q in plan["quotes"] if any(i.get("couponId") for i in q["payload"]["items"])]
        self.assertTrue(coupon_quotes)
        results = [{"id": q["id"], "error": "门店已打烊"} for q in coupon_quotes]
        results += [quote_result(q, q["estimate_cents"]) for q in plan["quotes"] if q not in coupon_quotes]
        picked = run_pick(plan, results)
        self.assertEqual(len(picked["excluded"]), len(coupon_quotes))
        self.assertTrue(all("门店已打烊" in e["reason"] for e in picked["excluded"]))

    def coupon_products(self, paid):
        """券后价 ¥0 的券（抖音/快手等渠道买的外购券），paid 是用户买它花的钱。"""
        return [single("A", "A", "20"),
                {"code": "C1", "name": "A券", "price": "0", "food_code": "A",
                 "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "A券", "paid": paid}}]

    def coupon_plan_and_pick(self, paid):
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.base_request(self.coupon_products(paid), demands))
        # 官方计价：券本身在麦当劳收 ¥0，外购的钱不在官方报价里
        results = [quote_result(q, 0 if any(i.get("couponId") for i in q["payload"]["items"]) else q["estimate_cents"])
                   for q in plan["quotes"]]
        return plan, run_pick(plan, results)

    def test_coupon_bought_elsewhere_counts_as_real_cost(self):
        """券后价 ¥0 是外购券：买券花的钱要算进成本，并按这个成本决定它还值不值得用。"""
        plan, picked = self.coupon_plan_and_pick("9.9")
        quote = [q for q in plan["quotes"] if any(i.get("couponId") for i in q["payload"]["items"])][0]
        self.assertEqual(quote["estimate_cents"], 990)  # 券后价 ¥0 + 买券 ¥9.9
        self.assertEqual(quote["prepaid_cents"], 990)
        self.assertEqual(picked["recommendations"][0]["price_cents"], 990)  # 官方 ¥0 加回买券的钱
        self.assertTrue(any("已含你买券花的¥9.90" in n for n in picked["recommendations"][0]["notes"]))
        # 买贵了就不再是最省：¥30 的券不如 ¥20 单点
        _, expensive = self.coupon_plan_and_pick("30")
        self.assertEqual(expensive["recommendations"][0]["price_cents"], 2000)
        self.assertFalse(any("买券" in n for n in expensive["recommendations"][0]["notes"]))

    def test_multi_coupon_estimate_counts_paid_cost_once(self):
        """多券同单走本地估价时，买券的钱已经在估价里，不能重复加一遍。"""
        products = [single("A", "A", "20"), single("B", "B", "12"),
                    {"code": "C1", "name": "A券", "price": "0", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "A券", "paid": "5"}},
                    {"code": "C2", "name": "B券", "price": "0", "food_code": "B",
                     "coupon": {"couponId": "id2", "couponCodes": ["c2"], "title": "B券", "paid": "3"}}]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]},
                   {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        plan = opt.plan(self.base_request(products, demands))
        multi = [q for q in plan["quotes"] if len({i["couponId"] for i in q["payload"]["items"] if i.get("couponId")}) > 1]
        self.assertTrue(multi)
        results = [{"id": q["id"], "error": "暂不支持多张券使用"} for q in multi]
        results += [quote_result(q, q["estimate_cents"]) for q in plan["quotes"] if q not in multi]
        picked = run_pick(plan, results)
        self.assertEqual(picked["recommendations"][0]["price_cents"], 800)  # 5 + 3，只算一次
        self.assertTrue(any("已含你买券花的¥8.00" in n for n in picked["recommendations"][0]["notes"]))

    def test_unused_coupons_are_reported_in_the_footer(self):
        """用不上的券（比如不支持配送）不进方案，但要在最后告诉用户。"""
        plan = opt.plan(self.base_request([single("A", "A", "10")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}],
                                          unused_coupons=[{"title": "25块麦乐鸡", "reason": "不支持麦乐送，本单选不到"}, "免费脆薯饼"]))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        self.assertIn("🎫 **这单用不上的券**", picked["footer"])
        self.assertIn("「25块麦乐鸡」不支持麦乐送，本单选不到", picked["footer"])
        self.assertIn("「免费脆薯饼」", picked["footer"])

    def test_store_note_and_menu_count_optional(self):
        plan = opt.plan(self.base_request([single("A", "A", "10")], [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}],
                                          store_name="开元餐厅", dining="到店取餐", store_note="离钟楼 152m 最近的营业门店，想换店直接说"))
        picked = run_pick(plan, [quote_result(q, q["estimate_cents"]) for q in plan["quotes"]])
        self.assertEqual(picked["title"], "**🕵️ 麦侦探 · 开元餐厅 · 到店取餐**\n\n离钟楼 152m 最近的营业门店，想换店直接说")
        self.assertIn("翻了本店全部菜单", picked["effort_line"])

    def test_piece_splits_are_minimal(self):
        self.assertEqual(sorted(sorted(s.items()) for s in opt.piece_splits(10, [4, 5])),
                         sorted([[(5, 2)], [(4, 3)], [(4, 2), (5, 1)]]))
        self.assertEqual(opt.piece_splits(8, [4]), [{4: 2}])
        self.assertEqual(opt.piece_splits(5, [5, 4]), [{5: 1}, {4: 2}])

    def test_nuggets_counted_in_pieces_mix_four_and_five(self):
        """长和卜蜂 18:35 真实案例：10块麦乐鸡 = 两个巨无霸四件套里的 4块×2 + 单点 5块，比 2×5块 的方案便宜。"""
        products = [single("1401", "麦乐鸡5块", "14"), single("1100", "巨无霸", "25.5"), single("4810", "中薯条", "13.5"),
                    combo("9900000888", "巨无霸四件套", "32", [
                        (1, [choice("1100", "巨无霸", default=1)]),
                        (1, [choice("4810", "中薯条", default=1)]),
                        (1, [choice("515106", "麦乐鸡4块", default=1)]),
                        (1, [choice("3050", "可乐中杯", default=1)])],
                        original="58.5", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "880"})]
        demands = [{"id": "burger", "label": "牛肉汉堡", "quantity": 2, "codes": ["1100"]},
                   {"id": "nugget", "label": "麦乐鸡", "quantity": 10, "codes": ["1401", "515106"], "pieces": {"1401": 5, "515106": 4}}]
        plan = opt.plan(self.base_request(products, demands))
        best = best_quote(plan, "会员106/880")
        self.assertEqual(best["estimate_cents"], 3200 * 2 + 1400)
        self.assertEqual(best["allocation"]["nugget"], {"1401": 1, "515106": 2})
        baseline = [q for q in plan["quotes"] if "baseline" in q["roles"]][0]
        self.assertEqual(baseline["estimate_cents"], 2550 * 2 + 1400 * 2)
        # 不按块数（只认 5块装两份）时找不到这个点法
        old = opt.plan(self.base_request(products, [demands[0], {"id": "nugget", "label": "麦乐鸡", "quantity": 2, "codes": ["1401"]}]))
        self.assertGreater(best_quote(old, "会员106/880")["estimate_cents"], 7800)

    def test_about_allows_eighty_percent(self):
        """“10块左右”：8 块也算。2×安格斯四件套（各带 4块）就够，不用再单买。"""
        products = [single("1401", "麦乐鸡5块", "14"), single("511782", "芝士安格斯厚牛堡", "31.5"),
                    combo("9900006957", "安格斯厚牛堡四件套随心选", "37", [
                        (1, [choice("511782", "芝士安格斯厚牛堡", default=1)]),
                        (1, [choice("4810", "中薯条", default=1)]),
                        (1, [choice("515106", "麦乐鸡4块", default=1)]),
                        (1, [choice("3050", "可乐中杯", default=1)])],
                        original="66", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "880"})]
        nugget = {"id": "nugget", "label": "麦乐鸡", "quantity": 10, "codes": ["1401", "515106"], "pieces": {"1401": 5, "515106": 4}}
        burger = {"id": "burger", "label": "牛肉汉堡", "quantity": 2, "codes": ["511782"]}
        strict_count = best_quote(opt.plan(self.base_request(products, [burger, nugget])), "会员106/880")
        about = best_quote(opt.plan(self.base_request(products, [burger, {**nugget, "about": True}])), "会员106/880")
        self.assertEqual(strict_count["estimate_cents"], 3700 * 2 + 1400)
        self.assertEqual(about["estimate_cents"], 3700 * 2)
        self.assertEqual(about["allocation"]["nugget"], {"515106": 2})
        self.assertEqual(opt.Request(self.base_request(products, [{**burger, "quantity": 5, "about": True}])).variants[0][0]["quantity"], 4)

    def coupon_only_products(self):
        """20块麦乐鸡：5块装单点 + 25块装只能靠外购券，按块数会展开出“只有用券才凑得齐”的那一堆。"""
        return [single("1401", "麦乐鸡5块", "14"), single("1100", "巨无霸", "25.5"),
                combo("9900000888", "巨无霸四件套", "32", [
                    (1, [choice("1100", "巨无霸", default=1)]),
                    (1, [choice("4810", "中薯条", default=1)]),
                    (1, [choice("515106", "麦乐鸡4块", default=1)]),
                    (1, [choice("3050", "可乐中杯", default=1)])]),
                {"code": "Z", "name": "25块麦乐鸡", "price": "0", "food_code": "Z",
                 "coupon": {"couponId": "id1", "couponCodes": ["c1"], "title": "25块麦乐鸡", "paid": 30}}]

    def test_variant_without_any_source_is_not_enumerated(self):
        """按块数展开出的某堆需求可能根本没有来源（25块装只有券给，不用券时怎么点都凑不齐）。
        这种堆一个方案都出不来，也就永远没有“已知最优”可以剪枝，穷举它会白白吃掉几分钟。"""
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 20, "codes": ["1401", "515106", "Z"],
                    "pieces": {"1401": 5, "515106": 4, "Z": 25}},
                   {"id": "burger", "label": "牛肉汉堡", "quantity": 2, "codes": ["1100"]}]
        solver = opt.Solver(opt.Request(self.base_request(self.coupon_only_products(), demands)))
        hopeless = [v for v in solver.r.variants if any("Z" in d["codes"] for d in v)][0]
        stats = {"nodes": 0, "complete": True, "combos": 0}
        best = []
        solver._search("none", hopeless, 80, None, stats, best, set(), None)
        self.assertEqual(stats["nodes"], 0)  # 一个套餐组合都不用试
        self.assertEqual(best, [])
        # 同一堆需求带上那张券就有方案了：券顶 20 块，四件套顶两份巨无霸
        plan = opt.plan(self.base_request(self.coupon_only_products(), demands))
        coupon_quotes = [q for q in plan["quotes"] if any(i.get("couponId") == "id1" for i in q["payload"]["items"])]
        self.assertTrue(coupon_quotes)
        self.assertEqual(coupon_quotes[0]["allocation"]["n"], {"Z": 1})

    def test_coupon_search_is_skipped_when_the_coupon_cannot_apply(self):
        """无关券不生成专属候选；相关券的专属最低价必须真正使用该券。"""
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 20, "codes": ["1401", "515106", "Z"],
                    "pieces": {"1401": 5, "515106": 4, "Z": 25}},
                   {"id": "burger", "label": "牛肉汉堡", "quantity": 2, "codes": ["1100"]}]
        products = self.coupon_only_products() + [
            {"code": "UNRELATED", "name": "无关饮料券", "price": "1", "food_code": "DRINK",
             "coupon": {"couponId": "unused", "couponCodes": ["unused1"]}}]
        request = self.base_request(products, demands)
        plans, stats, per_coupon = opt.Solver(opt.Request(request)).solve("none")
        self.assertTrue(stats["complete"])
        self.assertTrue(plans)
        self.assertIn("Z#券id1", per_coupon)
        self.assertNotIn("UNRELATED#券unused", per_coupon)
        self.assertEqual(per_coupon["Z#券id1"]["cost"], 8100)
        self.assertTrue(any(i.get("couponId") == "id1" for i in per_coupon["Z#券id1"]["payload"]["items"]))
        self.assertTrue(all(i.get("couponId") != "unused"
                            for plan in plans for i in plan["payload"]["items"]))
        irrelevant = self.base_request(products, demands[:1])
        irrelevant["demands"] = [{"id": "n", "label": "麦乐鸡", "quantity": 20,
                                  "codes": ["1401", "515106"], "pieces": {"1401": 5, "515106": 4}}]
        _, irrelevant_stats, irrelevant_coupons = opt.Solver(opt.Request(irrelevant)).solve("none")
        self.assertTrue(irrelevant_stats["complete"])
        self.assertEqual(irrelevant_coupons, {})

    def test_bundle_coupon_counts_as_covering_the_demand(self):
        """套组券（有 rounds，food 为 None）也算“能顶掉这堆需求”——只看 food 会把它整张漏掉，
        最优解会明显变贵。"""
        products = [single("1401", "麦乐鸡5块", "14"), single("1100", "巨无霸", "25.5"),
                    {"code": "9900008604", "name": "25块麦乐鸡（券）", "price": "0",
                     "rounds": [{"id": 1, "minQuantity": 5, "choices": [["1401", "麦乐鸡5块"]]}],
                     "coupon": {"couponId": "K1", "couponCodes": ["c1"], "title": "25块麦乐鸡"}}]
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 20, "codes": ["1401"], "pieces": {"1401": 5}}]
        plan = opt.plan(self.base_request(products, demands))
        used = [q for q in plan["quotes"] if any(i.get("couponId") == "K1" for i in q["payload"]["items"])]
        self.assertTrue(used)
        self.assertEqual(used[0]["allocation"]["n"], {"1401": 4})

    def test_node_budget_is_split_between_searches(self):
        """低预算仍留下两种相关券与多券见证，不能让第一种搜索饿死后续券搜索。"""
        demands = [{"id": "n", "label": "麦乐鸡", "quantity": 20, "codes": ["1401", "515106", "Z"],
                    "pieces": {"1401": 5, "515106": 4, "Z": 25}},
                   {"id": "burger", "label": "牛肉汉堡", "quantity": 2, "codes": ["1100"]}]
        products = self.coupon_only_products() + [
            {"code": "BC", "name": "汉堡券", "price": "1", "food_code": "1100",
             "coupon": {"couponId": "id2", "couponCodes": ["b1", "b2"]}}]
        request = self.base_request(products, demands, node_limit=400)
        plans, stats, per_coupon = opt.Solver(opt.Request(request)).solve("none", keep=1)
        self.assertTrue(plans)
        self.assertGreater(stats["nodes"], 0)
        self.assertIs(type(stats["complete"]), bool)
        for sid, coupon_id, witness_cost in (("Z#券id1", "id1", 8100), ("BC#券id2", "id2", 5800)):
            self.assertIn(sid, per_coupon)
            self.assertLessEqual(per_coupon[sid]["cost"], witness_cost)
            self.assertTrue(any(i.get("couponId") == coupon_id for i in per_coupon[sid]["payload"]["items"]))
        self.assertIn(opt.ALL_COUPONS, per_coupon)
        self.assertEqual(per_coupon[opt.ALL_COUPONS]["cost"], 3200)
        self.assertEqual({i["couponId"] for i in per_coupon[opt.ALL_COUPONS]["payload"]["items"] if i.get("couponId")},
                         {"id1", "id2"})
        # 如预算确已结束，对外必须说明未完成；若已完成则必须与充分预算的最低价一致。
        limited = opt.plan(request)
        if stats["complete"]:
            full, full_stats, _ = opt.Solver(opt.Request({**request, "node_limit": 100000})).solve("none", keep=1)
            self.assertTrue(full_stats["complete"])
            self.assertEqual(plans[0]["cost"], full[0]["cost"])
        else:
            self.assertFalse(limited["search"]["none"]["complete"])
            self.assertIn("node_limit", "；".join(limited["notes"]))
            self.assertNotIn("按现有菜单数据凑不齐必须餐品", "；".join(limited["notes"]))

    def test_pieces_validation(self):
        base = self.base_request([single("A", "A", "1")], [])
        for bad in ({"id": "n", "label": "n", "quantity": 10, "codes": ["A"], "pieces": {"B": 5}},
                    {"id": "n", "label": "n", "quantity": 10, "codes": ["A"], "pieces": {"A": 0}}):
            with self.assertRaises(opt.InputError):
                opt.plan({**base, "demands": [bad]})

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
        plans = opt.Solver(opt.Request(data)).solve("none")[0]
        self.assertEqual(plans[0]["cost"] if plans else None, expected, "seed=%d strict=%s" % (seed, strict))

    def test_random_open_extras(self):
        for seed in range(120):
            self.check(seed, False)

    def test_random_strict(self):
        for seed in range(120, 200):
            self.check(seed, True)


def whole_portion_feasible(data, supply):
    """独立分配整份餐品，不调用 variants、covers 或求解器的流/DP。"""
    demands = data["demands"]
    targets = tuple(max(1, (d["quantity"] * 4 + 4) // 5) if d.get("about") else d["quantity"]
                    for d in demands)
    portions = tuple(code for code, count in sorted(supply.items()) for _ in range(count))
    if data.get("strict") and len(portions) > sum(targets):
        return False

    @lru_cache(None)
    def assign(index, covered, smallest):
        if index == len(portions):
            return all(got >= target for got, target in zip(covered, targets))
        code = portions[index]
        for i, d in enumerate(demands):
            if covered[i] < targets[i] and code in d["codes"]:
                weight = d.get("pieces", {}).get(code, 1)
                updated, mins = list(covered), list(smallest)
                updated[i] += weight
                mins[i] = min(mins[i], weight) if mins[i] else weight
                # 严格块数允许最后一整份跨过下限，但不能留下可删去的多余整份。
                if data.get("strict") and updated[i] - mins[i] >= targets[i]:
                    continue
                if not data.get("strict"):
                    updated[i] = min(targets[i], updated[i])
                    mins[i] = 0
                if assign(index + 1, tuple(updated), tuple(mins)):
                    return True
        return not data.get("strict") and assign(index + 1, covered, smallest)

    return assign(0, (0,) * len(demands), (0,) * len(demands))


def whole_portion_brute(data):
    """从原始菜单枚举合法配置与购买多重集，源限量跨不同配置共享。"""
    def cents(value):
        return int(Decimal(str(value).replace("¥", "").replace(" ", "")) * 100)

    forbidden = set(data.get("forbidden_codes", []))
    options = []  # (整份成本, 供给, 来源下标)
    limits = []
    for source_index, raw in enumerate(data["products"]):
        coupon = raw.get("coupon")
        limit = raw.get("maxQuantity")
        if coupon:
            limit = min(len(coupon["couponCodes"]), limit) if limit is not None else len(coupon["couponCodes"])
        limits.append(limit)
        price = cents(raw["price"] if coupon else raw["currentPrice"])
        price += cents(coupon.get("paid", 0)) if coupon else 0
        if not raw.get("rounds"):
            food = raw.get("food_code", raw["code"])
            if food not in forbidden:
                options.append((price, Counter({food: 1}), source_index))
            continue
        per_round = []
        for rnd in raw["rounds"]:
            units = rnd["minQuantity"]
            choices = [ch for ch in rnd["choices"] if ch["code"] not in forbidden]
            fills = []
            for picked in itertools.combinations_with_replacement(choices, units):
                counts = Counter(ch["code"] for ch in picked)
                if any(ch.get("maxQuantity", -1) >= 0 and counts[ch["code"]] > ch["maxQuantity"] for ch in choices):
                    continue
                fills.append((sum(cents(ch.get("diffPrice", 0)) for ch in picked), counts))
            per_round.append(fills)
        for config in itertools.product(*per_round):
            supply = Counter()
            for _, portions in config:
                supply.update(portions)
            options.append((price + sum(part[0] for part in config), supply, source_index))
    if any(cost <= 0 for cost, _, _ in options):
        raise AssertionError("该暴力 oracle 的购买上界要求每个完整配置成本为正")
    options.sort(key=lambda item: item[0])
    max_size = sum(d["quantity"] for d in data["demands"])
    best = None
    used = Counter()

    @lru_cache(None)
    def feasible(supply):
        return whole_portion_feasible(data, dict(supply))

    def search(start, cost, supply, size):
        nonlocal best
        if feasible(tuple(sorted(supply.items()))):
            best = cost if best is None else min(best, cost)
            return
        if size >= max_size:
            return
        if data.get("strict") and sum(supply.values()) > max_size:
            return
        for i in range(start, len(options)):
            added_cost, added_supply, source_index = options[i]
            total = cost + added_cost
            if best is not None and total >= best:
                break
            if limits[source_index] is not None and used[source_index] >= limits[source_index]:
                continue
            used[source_index] += 1
            search(i, total, supply + added_supply, size + 1)
            used[source_index] -= 1

    search(0, 0, Counter(), 0)
    return best


class OptimizerRegressionTest(unittest.TestCase):
    def request(self, products, demands, **extra):
        return {"context": dict(CONTEXT), "products": products, "demands": demands, **extra}

    def assert_valid_plan(self, data, plan):
        sources = {(p["code"], (p.get("coupon") or {}).get("couponId")): p for p in data["products"]}
        supply = Counter()
        used = Counter()
        coupon_codes = set()
        cost = 0
        for line in plan["payload"]["items"]:
            coupon_id = line.get("couponId")
            candidates = [p for (code, cid), p in sources.items()
                          if cid == coupon_id and (code == line["productCode"] or p.get("food_code") == line["productCode"])]
            self.assertEqual(len(candidates), 1, line)
            raw = candidates[0]
            key = (raw["code"], coupon_id)
            quantity = line["quantity"]
            used[key] += quantity
            coupon = raw.get("coupon")
            cost += quantity * int(Decimal(str(raw["price"] if coupon else raw["currentPrice"])) * 100)
            if coupon:
                self.assertEqual(quantity, 1)
                self.assertIn(line["couponCode"], coupon["couponCodes"])
                self.assertNotIn((coupon_id, line["couponCode"]), coupon_codes)
                coupon_codes.add((coupon_id, line["couponCode"]))
                cost += int(Decimal(str(coupon.get("paid", 0))) * 100)
            rounds = {str(r["id"]): r for r in raw.get("rounds", [])}
            self.assertEqual({r["round"] for r in line.get("roundList", [])}, set(rounds))
            if not rounds:
                supply[raw.get("food_code", raw["code"])] += quantity
            for rnd in line.get("roundList", []):
                original = rounds[rnd["round"]]
                choices = {ch["code"]: ch for ch in original["choices"]}
                counts = Counter()
                for selected in rnd["comboItemList"]:
                    counts[selected["code"]] += selected["quantity"]
                self.assertEqual(sum(counts.values()), original["minQuantity"])
                for code, count in counts.items():
                    self.assertIn(code, choices)
                    cap = choices[code].get("maxQuantity", -1)
                    if cap >= 0:
                        self.assertLessEqual(count, cap)
                    supply[code] += quantity * count
                    diff = str(choices[code].get("diffPrice", 0)).replace("¥", "").replace(" ", "")
                    cost += quantity * count * int(Decimal(diff) * 100)
        for key, count in used.items():
            raw = sources[key]
            limit = raw.get("maxQuantity")
            if raw.get("coupon"):
                limit = min(limit, len(raw["coupon"]["couponCodes"])) if limit is not None else len(raw["coupon"]["couponCodes"])
            if limit is not None:
                self.assertLessEqual(count, limit)
        self.assertEqual(cost, plan["cost"])
        self.assertEqual(dict(supply), plan["supply"])
        self.assertFalse(set(supply).intersection(data.get("forbidden_codes", [])))
        allocated = Counter()
        for demand in data["demands"]:
            allocation = plan["allocation"].get(demand["id"], {})
            self.assertTrue(set(allocation).issubset(demand["codes"]))
            allocated.update(allocation)
            covered = sum(count * demand.get("pieces", {}).get(code, 1) for code, count in allocation.items())
            target = (demand["quantity"] * 4 + 4) // 5 if demand.get("about") else demand["quantity"]
            self.assertGreaterEqual(covered, target)
            if data.get("strict"):
                self.assertTrue(all(covered - demand.get("pieces", {}).get(code, 1) < target
                                    for code, count in allocation.items() if count))
        self.assertTrue(all(count <= supply[code] for code, count in allocated.items()))
        if data.get("strict"):
            self.assertEqual(allocated, supply)

    def check_oracle(self, data):
        expected = whole_portion_brute(data)
        plans, stats, _ = opt.Solver(opt.Request(data)).solve("none")
        self.assertTrue(stats["complete"], data)
        self.assertEqual(plans[0]["cost"] if plans else None, expected, data)
        if plans:
            self.assert_valid_plan(data, plans[0])
        return plans

    def check_exact_engines(self, data):
        """同一原始菜单同时验证加权DP和强制 evaluate 回退，而不是只靠数据猜引擎。"""
        expected = whole_portion_brute(data)
        normal = opt.Solver(opt.Request(data))
        plans, stats, _ = normal.solve("none", keep=1)
        self.assertEqual(stats["engine"], "weighted_dp")
        self.assertTrue(stats["complete"])
        self.assertEqual(plans[0]["cost"] if plans else None, expected)
        fallback = opt.Solver(opt.Request(data))
        with patch.object(fallback, "_dp", return_value=None), patch.object(fallback, "evaluate", wraps=fallback.evaluate) as evaluations:
            fallback_plans, fallback_stats, _ = fallback.solve("none", keep=1)
        self.assertGreater(evaluations.call_count, 0)
        self.assertIn(fallback_stats["engine"], ("quantity_search", "adaptive"))
        self.assertTrue(fallback_stats["complete"])
        self.assertEqual(fallback_plans[0]["cost"] if fallback_plans else None, expected)
        for result in (plans, fallback_plans):
            if result:
                self.assert_valid_plan(data, result[0])
        return plans, fallback_plans

    def test_mandatory_capacity_forced_dp_and_evaluate_fallback(self):
        products = [single("A", "A", "20"), single("B", "B", "0.5"),
                    combo("C", "必选两份", "10", [(2, [choice("A", "A", default=1, maxQuantity=1),
                                                          choice("B", "B", "+1", maxQuantity=1)])])]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        data = self.request(products, demands)
        for results in self.check_exact_engines(data):
            self.assertEqual(results[0]["cost"], 1100)
            self.assertEqual(results[0]["payload"]["items"], [{"productCode": "C", "quantity": 1,
                "roundList": [{"round": "1", "comboItemList": [{"code": "A", "quantity": 1}, {"code": "B", "quantity": 1}]}]}])
        solver = opt.Solver(opt.Request(data))
        combos = solver._combos("none", solver.r.demands, None)
        singles = solver._singles("none", solver.r.demands)
        evaluated = solver.evaluate("none", solver.r.demands, combos, singles, Counter({0: 1}), max_cost=1100)
        self.assertIsNotNone(evaluated)
        self.assertEqual(evaluated["cost"], 1100)
        self.assertEqual(evaluated["supply"], {"A": 1, "B": 1})
        self.assertIsNone(solver.evaluate("none", solver.r.demands, combos, singles, Counter({0: 1}), max_cost=1099))

    def test_mandatory_capacity_does_not_buy_redundant_cheap_single(self):
        products = [single("A", "A", "20"), single("B", "B", "0.5"),
                    combo("C", "两份套餐", "10", [(2, [choice("A", "A", maxQuantity=1), choice("B", "B", "+1", maxQuantity=1)])])]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        data = self.request(products, demands)
        best = best_quote(opt.plan(data))
        self.assertEqual(best["estimate_cents"], 1100)
        self.assertEqual(best["supply"], {"A": 1, "B": 1})
        self.assertEqual([(i["productCode"], i["quantity"]) for i in best["payload"]["items"]], [("C", 1)])
        self.assertEqual(self.check_oracle(data)[0]["cost"], 1100)

    def test_overlap_fallback_fills_all_mandatory_slots(self):
        products = [single("A", "A", "20"), single("B", "B", "0.5"),
                    combo("C", "两份套餐", "10", [(2, [choice("A", "A", maxQuantity=1), choice("B", "B", "+1", maxQuantity=1)])])]
        demands = [{"id": "a", "label": "A或X", "quantity": 1, "codes": ["A", "X"]},
                   {"id": "b", "label": "B或X", "quantity": 1, "codes": ["B", "X"]}]
        plans = self.check_oracle(self.request(products, demands))
        self.assertEqual(plans[0]["cost"], 1100)
        self.assertEqual(plans[0]["supply"], {"A": 1, "B": 1})

    def test_filler_capacity_combines_options_and_respects_forbidden(self):
        products = [single("A", "A", "20"), single("B", "B", "0.5"),
                    combo("C", "三份套餐", "10", [(3, [choice("A", "A", maxQuantity=1),
                        choice("B", "B", "+1", maxQuantity=1), choice("D", "D", "+2", maxQuantity=1),
                        choice("X", "X", "-2", maxQuantity=3)])])]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        data = self.request(products, demands, forbidden_codes=["X"])
        plans = self.check_oracle(data)
        self.assertEqual(plans[0]["cost"], 1300)
        self.assertEqual(plans[0]["extras"], {"D": 1})
        strict = self.check_oracle({**data, "strict": True})
        self.assertEqual(strict[0]["cost"], 2050)

    def test_capacity_is_per_instance_even_with_split_configurations(self):
        products = [combo("C", "三份套餐", "10", [(3, [choice("A", "A", maxQuantity=1),
                    choice("B", "B", "+1", maxQuantity=1), choice("D", "D", "+2", maxQuantity=1)])])]
        demands = [{"id": code, "label": code, "quantity": 2, "codes": [code]} for code in ("A", "B", "D")]
        plans = self.check_oracle(self.request(products, demands, strict=True))
        self.assertEqual(plans[0]["cost"], 2600)
        self.assertEqual(sum(i["quantity"] for i in plans[0]["payload"]["items"]), 2)

    def test_best_keeps_the_only_quote_ahead_of_dearer_baseline(self):
        products = [single("A", "A", "20"), combo("C", "套餐", "10", [(1, [choice("A", "A")])])]
        demands = [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}]
        plan = opt.plan(self.request(products, demands, max_quotes=1))
        self.assertEqual(len(plan["quotes"]), 1)
        self.assertIn("best", plan["quotes"][0]["roles"])
        self.assertEqual(best_quote(plan)["estimate_cents"], 1000)
        self.assertEqual(plan["quotes"][0]["payload"]["items"][0]["productCode"], "C")

    def test_negative_differences_include_required_filler(self):
        products = [single("A", "A", "20"), combo("C", "套餐", "10", [(2, [
            choice("A", "A", maxQuantity=1), choice("B", "B", "-1", maxQuantity=1),
            choice("D", "D", "-3", maxQuantity=1)])])]
        data = self.request(products, [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}])
        best = self.check_oracle(data)[0]
        self.assertEqual(best["cost"], 700)
        self.assertEqual(best["extras"], {"D": 1})
        strict = self.check_oracle({**data, "demands": [data["demands"][0],
            {"id": "d", "label": "D", "quantity": 1, "codes": ["D"]}], "strict": True})
        self.assertEqual(strict[0]["cost"], 700)

    def test_negative_and_default_fillers_respect_finite_capacity_in_both_engines(self):
        products = [single("A", "A", "20"), combo("C", "四份餐", "12", [(4, [
            choice("A", "A", maxQuantity=1), choice("D", "默认赠品", "-2", default=1, maxQuantity=1),
            choice("E", "第二赠品", "-2", maxQuantity=2), choice("X", "禁用赠品", "-9", maxQuantity=4)])])]
        data = self.request(products, [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}], forbidden_codes=["X"])
        for plans in self.check_exact_engines(data):
            self.assertEqual(plans[0]["cost"], 600)
            self.assertEqual(plans[0]["extras"], {"D": 1, "E": 2})
        # 同价补齐时默认项优先，但默认上限不能阻止继续用非默认项填满。
        tie = self.request([single("A", "A", "20"), combo("C", "三份餐", "10", [(3, [
            choice("A", "A", maxQuantity=1), choice("D", "默认赠品", default=1, maxQuantity=1),
            choice("E", "第二赠品", maxQuantity=2)])])], data["demands"])
        for plans in self.check_exact_engines(tie):
            self.assertEqual(plans[0]["cost"], 1000)
            self.assertEqual(plans[0]["extras"], {"D": 1, "E": 1})

    def test_default_truncation_keeps_no_card_best_and_baseline(self):
        products = [single("A", "A", "20"), combo("NC", "不开卡套餐", "12", [(1, [choice("A", "A")])])]
        for k in range(14):
            products.append(combo("MC%d" % k, "会员餐%d" % k, str(1 + k / 10), [(1, [choice("A", "A")])],
                original="25", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": str(800 + k)}))
        data = self.request(products, [{"id": "a", "label": "A", "quantity": 1, "codes": ["A"]}])
        full = opt.plan({**data, "max_quotes": 30})
        self.assertGreater(len(full["quotes"]), 10)
        limited = opt.plan(data)
        self.assertLessEqual(len(limited["quotes"]), 10)
        self.assertTrue(any(q["context"] == "none" and "best" in q["roles"] for q in limited["quotes"]),
                        "默认截断挤掉了不开卡最低价候选")
        self.assertEqual(best_quote(limited, "none")["estimate_cents"], 1200)
        baseline = [q for q in limited["quotes"] if "baseline" in q["roles"]]
        self.assertEqual(len(baseline), 1)
        self.assertEqual(baseline[0]["estimate_cents"], 2000)
        self.assertEqual(min(q["estimate_cents"] for q in limited["quotes"]), min(q["estimate_cents"] for q in full["quotes"]))

    def test_mixed_piece_sizes_and_product_limits_match_oracle_in_both_engines(self):
        products = [single("N4", "四块装", "3"), single("N5", "五块装", "4"), single("H", "汉堡", "20"),
                    combo("C", "汉堡四块餐", "10", [(1, [choice("H", "汉堡")]), (1, [choice("N4", "四块装")])], maxQuantity=1)]
        demands = [{"id": "n", "label": "鸡块", "quantity": 17, "codes": ["N4", "N5"], "pieces": {"N4": 4, "N5": 5}},
                   {"id": "h", "label": "汉堡", "quantity": 1, "codes": ["H"]}]
        for strict in (False, True):
            with self.subTest(strict=strict):
                for plans in self.check_exact_engines(self.request(products, demands, strict=strict)):
                    self.assertEqual(plans[0]["cost"], 2000)
                    self.assertEqual(plans[0]["allocation"]["n"], {"N4": 3, "N5": 1})

    def test_coupon_inventory_is_shared_across_configurations(self):
        products = [single("A", "A", "10"), single("B", "B", "10"),
                    {**combo("C", "限购任选券", "1", [(1, [choice("A", "A"), choice("B", "B")])],
                        coupon={"couponId": "id1", "couponCodes": ["c1", "c2"], "paid": "0.5"}, maxQuantity=1), "price": "1"}]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        for limit, inventory, expected in ((1, ["c1", "c2"], 1150), (2, ["c1"], 1150), (2, ["c1", "c2"], 300)):
            with self.subTest(limit=limit, inventory=inventory):
                data = self.request(json.loads(json.dumps(products)), demands, strict=True)
                data["products"][-1]["maxQuantity"] = limit
                data["products"][-1]["coupon"]["couponCodes"] = inventory
                for plans in self.check_exact_engines(data):
                    self.assertEqual(plans[0]["cost"], expected)

    def test_weighted_overlap_random_menus_against_oracle(self):
        for strict in (False, True):
            for seed in range(12):
                with self.subTest(seed=seed, strict=strict):
                    self.check_oracle(self.random_finite_case(200 + seed, strict, weighted=True, overlap=True))

    def test_negative_difference_does_not_prune_cheapest_choice(self):
        products = [single("A", "A", "2"), single("B", "B", "20"),
                    combo("C", "减价换选", "10", [(1, [choice("A", "A"), choice("B", "B", "-9")])])]
        data = self.request(products, [{"id": "ab", "label": "A或B", "quantity": 2, "codes": ["A", "B"]}])
        self.assertEqual(self.check_oracle(data)[0]["cost"], 200)

    def test_overlapping_weighted_demand_cannot_split_one_portion(self):
        products = [single("X", "一整份X", "3", maxQuantity=1), single("A", "A", "7"), single("B", "B", "8")]
        demands = [{"id": "a", "label": "A类", "quantity": 3, "codes": ["X", "A"], "pieces": {"X": 5, "A": 3}},
                   {"id": "b", "label": "B类", "quantity": 2, "codes": ["X", "B"], "pieces": {"X": 5, "B": 2}}]
        best = self.check_oracle(self.request(products, demands))[0]
        self.assertEqual(best["cost"], 1000)
        self.assertEqual(sum(a.get("X", 0) for a in best["allocation"].values()), 1)

    def random_finite_case(self, seed, strict, weighted=True, overlap=False):
        rng = random.Random(seed)
        groups = [["A0", "A1"], ["B0", "B1"]]
        foods = sum(groups, [])
        weights = {code: rng.randint(1, 3) if weighted else 1 for code in foods}
        demands = []
        for i, codes in enumerate(groups):
            accepted = codes + (["A0"] if overlap and i == 1 else [])
            demand = {"id": "d%d" % i, "label": "需求%d" % i, "quantity": rng.randint(1, 4 if weighted else 2), "codes": accepted}
            if weighted:
                demand["pieces"] = {code: weights[code] for code in accepted}
            demands.append(demand)
        products = [single(code, code, str(rng.randint(5, 12)), maxQuantity=rng.choice([None, 0, 1, 2])) for code in foods]
        for k in range(rng.randint(1, 2)):
            rounds = []
            for _ in range(rng.randint(1, 2)):
                codes = rng.sample(foods + ["D", "X"], rng.randint(2, 3))
                units = rng.randint(1, 3)
                rounds.append((units, [choice(code, code, "%+d" % rng.randint(-1, 2),
                    default=int(i == 0), maxQuantity=rng.randint(0, min(2, units))) for i, code in enumerate(codes)]))
            products.append(combo("K%d" % k, "套餐%d" % k, str(rng.randint(9, 18)), rounds, maxQuantity=rng.randint(1, 2)))
        for k in range(2):
            count = rng.randint(1, 2)
            raw = {"code": "C%d" % k, "name": "限量券%d" % k, "price": str(rng.randint(1, 4)),
                   "coupon": {"couponId": "id%d" % k, "couponCodes": ["c%d_%d" % (k, i) for i in range(count)],
                              "paid": str(rng.randint(0, 2))}, "maxQuantity": rng.randint(0, count)}
            if k == 0:
                raw["food_code"] = rng.choice(foods)
            else:
                raw["rounds"] = combo("unused", "unused", "1", [(1, [choice(rng.choice(groups[0]), "A")]),
                    (1, [choice(rng.choice(groups[1]), "B")])])["rounds"]
            products.append(raw)
        return self.request(products, demands, strict=strict, forbidden_codes=["X"] if seed % 2 else ["D", "X"])

    def test_random_weighted_finite_options_against_independent_oracle(self):
        for strict in (False, True):
            for seed in range(40):
                with self.subTest(seed=seed, strict=strict):
                    self.check_oracle(self.random_finite_case(seed, strict))

    def test_random_strict_overlap_finite_options_against_oracle(self):
        for seed in range(24):
            with self.subTest(seed=seed):
                self.check_oracle(self.random_finite_case(100 + seed, True, weighted=False, overlap=True))

    def test_small_samples_agree_with_independent_brute(self):
        reference = BruteForceTest()
        for strict in (False, True):
            for seed in range(20):
                data = reference.random_case(random.Random(300 + seed), strict)
                if seed % 2 == 0:
                    for i, demand in enumerate(data["demands"]):
                        demand["codes"] = ["F%d" % i]
                with self.subTest(seed=seed, strict=strict):
                    self.assertEqual(whole_portion_brute(data), reference.brute(data))
                    self.check_oracle(data)

    def test_weighted_small_samples_match_variant_search_when_available(self):
        for quantity in range(1, 11):
            products = [single("N4", "四块装", "4"), single("N5", "五块装", "5"), single("H", "汉堡", "9"),
                        combo("K", "汉堡四块餐", "10", [(1, [choice("H", "汉堡")]), (1, [choice("N4", "四块装")])])]
            demands = [{"id": "n", "label": "块数", "quantity": quantity, "codes": ["N4", "N5"], "pieces": {"N4": 4, "N5": 5}},
                       {"id": "h", "label": "汉堡", "quantity": 1, "codes": ["H"]}]
            data = self.request(products, demands)
            with self.subTest(quantity=quantity):
                expected = whole_portion_brute(data)
                solver = opt.Solver(opt.Request(data))
                plans, stats, _ = solver.solve("none")
                self.assertTrue(stats["complete"])
                self.assertEqual(plans[0]["cost"], expected)
                if hasattr(solver, "_search") and hasattr(solver.r, "variants"):
                    variant_best = []
                    variant_stats = {"nodes": 0, "complete": True, "combos": 0}
                    keys = set()
                    for variant in solver.r.variants:
                        solver._search("none", variant, 1, None, variant_stats, variant_best, keys, None)
                    self.assertTrue(variant_stats["complete"])
                    self.assertTrue(variant_best)
                    self.assertEqual(variant_best[0][0], expected)

    def test_two_weighted_demands_do_not_repeat_variant_search(self):
        products = [single(code, code, "10") for code in ("A4", "A5", "B4", "B5")]
        products += [single("H", "汉堡", "20")]
        for k in range(8):
            products.append(combo("K%d" % k, "套餐%d" % k, str(20 + k), [
                (1, [choice("A4", "A四块", default=1), choice("A5", "A五块", "+1")]),
                (1, [choice("B4", "B四块", default=1), choice("B5", "B五块", "+1")]),
                (1, [choice("H", "汉堡", default=1)])]))
        demands = [{"id": group, "label": group, "quantity": 20, "codes": [group + "4", group + "5"],
                    "pieces": {group + "4": 4, group + "5": 5}} for group in ("A", "B")]
        demands.append({"id": "burger", "label": "汉堡", "quantity": 3, "codes": ["H"]})
        data = self.request(products, demands)
        solver = opt.Solver(opt.Request(data))
        plans, stats, _ = solver.solve("none", keep=1)
        self.assertEqual(stats["engine"], "weighted_dp", "同一无券 context 应直接搜索加权状态，不重复展开份数组合")
        self.assertTrue(stats["complete"])
        # 三套 K0 两轮都加1元换五块装，两类各15块，再各单点一份五块装，共86元。
        self.assertEqual(plans[0]["cost"], 8600)
        self.assert_valid_plan(data, plans[0])

    def test_exhausted_search_does_not_claim_global_optimality(self):
        products = [single("A", "A", "20"), single("B", "B", "20"),
                    combo("C", "低价双份", "10", [(2, [choice("A", "A"), choice("B", "B")])])]
        # 两个需求共享真实代码，数量回退不能只拿第一条昂贵单点见证便声称证明了最优。
        demands = [{"id": "a", "label": "A或B", "quantity": 1, "codes": ["A", "B"]},
                   {"id": "b", "label": "B", "quantity": 1, "codes": ["B"]}]
        data = self.request(products, demands, node_limit=1)
        plans, stats, _ = opt.Solver(opt.Request(data)).solve("none", keep=1)
        self.assertFalse(stats["complete"])
        self.assertTrue(plans)
        limited = opt.plan(data)
        self.assertFalse(limited["search"]["none"]["complete"])
        self.assertIn("node_limit", "；".join(limited["notes"]))
        picked = run_pick(limited, [quote_result(q, q["estimate_cents"]) for q in limited["quotes"]])
        text = "\n".join((picked["message"], picked.get("effort_line", ""), picked.get("footer", "")))
        for overclaim in ("全局最优", "已穷举全部组合", "保证最优", "保证最低",
                          "这家店此刻直接单点就是最省的", "套餐、优惠券和开卡都没能更便宜"):
            self.assertNotIn(overclaim, text)
        self.assertIn("node_limit", text)
        full = opt.plan({**data, "node_limit": 100000})
        self.assertTrue(full["search"]["none"]["complete"])
        self.assertEqual(best_quote(full)["estimate_cents"], whole_portion_brute(data))
        self.assertEqual(best_quote(full)["estimate_cents"], 1000)

    def test_node_limit_reports_incomplete_instead_of_infeasible(self):
        products = [combo("C", "双份", "10", [(2, [choice("A", "A", maxQuantity=1), choice("B", "B", maxQuantity=1)])])]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        data = self.request(products, demands)
        limited = opt.plan({**data, "node_limit": 1})
        self.assertTrue(all(type(stats["complete"]) is bool for stats in limited["search"].values()))
        if all(stats["complete"] for stats in limited["search"].values()):
            self.assertEqual(best_quote(limited)["estimate_cents"], 1000)
        else:
            notes = "；".join(limited["notes"])
            self.assertIn("node_limit", notes)
            self.assertNotIn("按现有菜单数据凑不齐必须餐品", notes)
        self.assertEqual(best_quote(opt.plan(data))["estimate_cents"], 1000)


class MultiCouponFeeRegressionTest(unittest.TestCase):
    def build(self, delivery=False, card=False):
        products = [single("A", "A", "20"), single("B", "B", "12"),
                    {"code": "C1", "name": "A券", "price": "5", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"]}},
                    {"code": "C2", "name": "B券", "price": "3", "food_code": "B",
                     "coupon": {"couponId": "id2", "couponCodes": ["c2"]}}]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        if card:
            products += [single("H", "汉堡", "20"), combo("M", "会员汉堡", "5", [(1, [choice("H", "汉堡")])],
                original="20", discountType="随单购麦金卡优惠", withOrder={"membershipCode": "106", "specId": "880"})]
            demands.append({"id": "h", "label": "汉堡", "quantity": 1, "codes": ["H"]})
        context = {**CONTEXT, "orderType": 2, "beType": 2, "beCode": "1960649M01"} if delivery else dict(CONTEXT)
        plan = opt.plan({"context": context, "products": products, "demands": demands, "max_quotes": 30})
        ctx = "会员106/880" if card else "none"
        multi = [q for q in plan["quotes"] if q["context"] == ctx and
                 len({i["couponId"] for i in q["payload"]["items"] if i.get("couponId")}) == 2]
        self.assertTrue(multi)
        return plan, min(multi, key=lambda q: q["estimate_cents"])

    def rejected_multi(self, quote):
        return {"id": quote["id"], "error": "暂不支持多张券使用"}

    def test_delivery_multi_coupon_without_fees_is_not_verified_lowest(self):
        plan, multi = self.build(delivery=True)
        picked = run_pick(plan, [self.rejected_multi(multi)])
        self.assertEqual(picked["excluded"], [])
        recommendation = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(recommendation["estimated"])
        self.assertEqual(recommendation["price_cents"], 800)
        self.assertNotEqual(recommendation["label"], "最低实付")
        self.assertIn("配送", "；".join(recommendation["notes"]))
        self.assertNotIn("价格是麦当劳官方刚刚的试算", picked["footer"])

    def test_card_multi_coupon_without_fee_is_not_verified_lowest(self):
        plan, multi = self.build(card=True)
        picked = run_pick(plan, [self.rejected_multi(multi)])
        self.assertEqual(picked["excluded"], [])
        recommendation = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(recommendation["estimated"])
        self.assertEqual(recommendation["price_cents"], 1300)
        self.assertNotEqual(recommendation["label"], "最低实付")
        self.assertIn("卡费", "；".join(recommendation["notes"]))

    def test_multi_coupon_reuses_verified_fees_from_same_context_once(self):
        plan, multi = self.build(delivery=True, card=True)
        verified = next(q for q in plan["quotes"] if q["context"] == multi["context"] and q["id"] != multi["id"] and
                        len({i["couponId"] for i in q["payload"]["items"] if i.get("couponId")}) < 2)
        result = quote_result(verified, verified["estimate_cents"] + 990 + 1100, ("CARD", "体验卡", 990))
        result.update(deliveryPrice=900, packingPrice=200, tablewarePrice=0)
        picked = run_pick(plan, [self.rejected_multi(multi), result])
        self.assertEqual(picked["excluded"], [])
        recommendation = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(recommendation["estimated"])
        self.assertEqual(recommendation["price_cents"], 1300 + 990 + 1100)
        self.assertEqual(recommendation["fees"], {"配送费": 900, "打包费": 200})
        self.assertEqual(recommendation["card"]["fee_cents"], 990)

    def test_store_no_card_multi_coupon_can_rank_as_estimated_food_cost(self):
        plan, multi = self.build()
        baseline = next(q for q in plan["quotes"] if "baseline" in q["roles"])
        picked = run_pick(plan, [self.rejected_multi(multi), quote_result(baseline, baseline["estimate_cents"])])
        self.assertEqual(picked["excluded"], [])
        first = picked["recommendations"][0]
        self.assertEqual(first["quote_id"], multi["id"])
        self.assertEqual(first["price_cents"], 800)
        self.assertEqual(first["label"], "最低实付")
        self.assertTrue(first["estimated"])
        self.assertIn("以下单页为准", "；".join(first["notes"]))

    def test_incomplete_delivery_multi_coupon_cannot_outrank_verified_payment(self):
        plan, multi = self.build(delivery=True)
        baseline = next(q for q in plan["quotes"] if "baseline" in q["roles"])
        # 有有效总价不代表知道多券配送附加费，未返回费用字段时仍不能偷用餐品估价排序。
        result = quote_result(baseline, baseline["estimate_cents"] + 1100)
        picked = run_pick(plan, [self.rejected_multi(multi), result])
        self.assertEqual(picked["recommendations"][0]["quote_id"], baseline["id"])
        self.assertEqual(picked["recommendations"][0]["label"], "最低实付")
        estimated = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(estimated["estimated"])
        self.assertNotEqual(estimated["label"], "最低实付")
        self.assertIn("配送", "；".join(estimated["notes"]))

    def test_known_card_fee_is_kept_even_when_delivery_fees_are_missing(self):
        plan, multi = self.build(delivery=True, card=True)
        verified = next(q for q in plan["quotes"] if q["context"] == multi["context"] and
                        len({i["couponId"] for i in q["payload"]["items"] if i.get("couponId")}) < 2)
        result = quote_result(verified, verified["estimate_cents"] + 990, ("CARD", "体验卡", 990))
        picked = run_pick(plan, [self.rejected_multi(multi), result])
        estimated = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(estimated["estimated"])
        self.assertEqual(estimated["price_cents"], 1300 + 990)
        self.assertEqual(estimated["card"]["fee_cents"], 990)
        self.assertNotEqual(estimated["label"], "最低实付")
        self.assertIn("配送", "；".join(estimated["notes"]))

    def test_known_zero_delivery_fees_are_not_treated_as_missing(self):
        plan, multi = self.build(delivery=True)
        baseline = next(q for q in plan["quotes"] if "baseline" in q["roles"])
        result = quote_result(baseline, baseline["estimate_cents"])
        result.update(deliveryPrice=0, packingPrice=0, tablewarePrice=0)
        picked = run_pick(plan, [self.rejected_multi(multi), result])
        self.assertEqual(picked["recommendations"][0]["quote_id"], multi["id"])
        self.assertEqual(picked["recommendations"][0]["price_cents"], 800)
        self.assertEqual(picked["recommendations"][0]["label"], "最低实付")
        self.assertTrue(picked["recommendations"][0]["estimated"])

    def test_delivery_multi_coupon_adds_known_fees_and_prepaid_exactly_once(self):
        products = [single("A", "A", "20"), single("B", "B", "12"),
                    {"code": "C1", "name": "A券", "price": "0", "food_code": "A",
                     "coupon": {"couponId": "id1", "couponCodes": ["c1"], "paid": "5"}},
                    {"code": "C2", "name": "B券", "price": "0", "food_code": "B",
                     "coupon": {"couponId": "id2", "couponCodes": ["c2"], "paid": "3"}}]
        demands = [{"id": code, "label": code, "quantity": 1, "codes": [code]} for code in ("A", "B")]
        plan = opt.plan({"context": {**CONTEXT, "orderType": 2, "beType": 2, "beCode": "1960649M01"},
                         "products": products, "demands": demands})
        multi = next(q for q in plan["quotes"] if len({i["couponId"] for i in q["payload"]["items"] if i.get("couponId")}) == 2)
        baseline = next(q for q in plan["quotes"] if "baseline" in q["roles"])
        result = quote_result(baseline, baseline["estimate_cents"] + 1400)
        result.update(deliveryPrice=900, packingPrice=200, tablewarePrice=300)
        picked = run_pick(plan, [self.rejected_multi(multi), result])
        first = picked["recommendations"][0]
        self.assertEqual(first["quote_id"], multi["id"])
        self.assertEqual(first["price_cents"], 800 + 1400)
        self.assertTrue(first["estimated"])
        self.assertEqual(first["fees"], {"配送费": 900, "打包费": 200, "餐具费": 300})
        self.assertIn("已含你买券花的¥8.00", "；".join(first["notes"]))

    def test_card_fees_are_not_borrowed_from_a_different_context(self):
        plan, multi = self.build(card=True)
        none = next(q for q in plan["quotes"] if q["context"] == "none" and "baseline" in q["roles"])
        picked = run_pick(plan, [self.rejected_multi(multi), quote_result(none, none["estimate_cents"])])
        recommendation = next(r for r in picked["recommendations"] if r["quote_id"] == multi["id"])
        self.assertTrue(recommendation["estimated"])
        self.assertNotEqual(recommendation["label"], "最低实付")
        self.assertIn("卡费", "；".join(recommendation["notes"]))


class CandidateNoticeRegressionTest(unittest.TestCase):
    def test_only_candidate_incomplete_is_silent_with_internal_state_kept(self):
        data = {"context": dict(CONTEXT), "node_limit": 20,
                "demands": [{"id": "a", "quantity": 2, "codes": ["A"]}],
                "products": [single("A", "A", "20"),
                             combo("C", "双份餐", "10", [(2, [choice("A", "A", default=1), choice("B", "B")])])]}
        result = opt.plan(data)
        self.assertTrue(result["verification"]["local_minimum_complete"])
        self.assertFalse(result["verification"]["candidates_complete"])
        self.assertFalse(result["search"]["none"]["candidates_complete"])
        self.assertNotIn("备选搜索未完备", opt.console_text("plan", result, "plan.json"))
        picked = run_pick(result, [quote_result(q, q["estimate_cents"]) for q in result["quotes"]])
        for key in ("footer", "message", "cards_html"):
            self.assertNotIn("备选搜索未完备", picked[key])

    def test_candidate_notice_hidden_but_other_risks_kept(self):
        result = opt.plan({"context": dict(CONTEXT),
                           "demands": [{"id": "a", "quantity": 1, "codes": ["A"]}],
                           "products": [single("A", "A", "10")]})
        result["notes"] = ["备选搜索未完备（预算 node_limit 或配置覆盖限制）；已求得的最低候选保留",
                           "搜索达到 node_limit，最低价尚未证明", "附加费未核实"]
        text = opt.console_text("plan", result, "plan.json")
        self.assertNotIn("备选搜索未完备", text)
        self.assertIn("最低价尚未证明", text)
        self.assertIn("附加费未核实", text)
        picked = run_pick(result, [quote_result(q, q["estimate_cents"]) for q in result["quotes"]])
        self.assertNotIn("备选搜索未完备", picked["message"])
        self.assertIn("最低价尚未证明", picked["footer"])
        self.assertIn("附加费未核实", picked["footer"])

    def test_minimum_incomplete_still_warns(self):
        result = opt.plan({"context": dict(CONTEXT), "node_limit": 1,
                           "demands": [{"id": "a", "quantity": 2, "codes": ["A"]}],
                           "products": [single("A", "A", "20"),
                                        combo("C", "双份餐", "10", [(2, [choice("A", "A", default=1), choice("B", "B")])])]})
        self.assertFalse(result["verification"]["local_minimum_complete"])
        self.assertIn("最低价尚未证明", "；".join(result["notes"]))
        self.assertNotIn("备选搜索未完备", "；".join(result["notes"]))


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

    def test_packing_service_fee_line_is_ignored(self):
        quote = [q for q in self.plan["quotes"] if q["context"] == "none"][0]
        result = quote_result(quote, quote["estimate_cents"] + 300)
        result["productList"].append({"productCode": "920174", "productName": "打包服务费", "quantity": 46, "subtotal": 0})
        result["packingPrice"] = 300
        picked = run_pick(self.plan, [result])
        self.assertEqual(picked["excluded"], [])
        self.assertIsNone(picked["recommendations"][0]["card"])
        self.assertIn("含打包费¥3.00", picked["recommendations"][0]["notes"])

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
            printed = [line for line in done.stdout.splitlines() if line.startswith("q")]
            self.assertTrue(printed)
            first_id, first_payload = printed[0].split(" ", 1)
            self.assertEqual(json.loads(first_payload)["storeCode"], "1960649")
            with open(plan_path, encoding="utf-8") as handle:
                plan = json.load(handle)
            with open(quotes_path, "w", encoding="utf-8") as handle:
                json.dump({"plan_file": plan_path, "results": [quote_result(q, q["estimate_cents"]) for q in plan["quotes"] if q["context"] == "none"]}, handle)
            done = subprocess.run([sys.executable, "-B", script, "pick", "--input", quotes_path, "--output", picked_path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            out = done.stdout
            self.assertIn("===== CARDS_HTML =====\n<div>", out)
            # 图例要在卡片之前单独打印（宿主按这个顺序排：表格 → 图例），footer 里不含图例
            self.assertIn("===== LEGEND =====\n加粗是你点名要的", out)
            self.assertLess(out.index("===== LEGEND ====="), out.index("===== CARDS_HTML ====="))
            self.assertNotIn("===== FOOTER =====\n加粗是你点名要的", out)
            self.assertTrue(out.rstrip().endswith("===== END ====="))
            with open(picked_path, encoding="utf-8") as handle:
                self.assertTrue(json.load(handle)["recommendations"])
            done = subprocess.run([sys.executable, "-B", script, "plan", "--input", quotes_path, "--output", picked_path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2)
            self.assertIn("error", done.stderr)

    def test_run_folder_fragments_compact_and_batched_quotes(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimizer.py")
        with tempfile.TemporaryDirectory() as run:
            base = {"context": dict(CONTEXT),
                    "demands": [{"name": "汉堡", "quantity": 2, "codes": ["1440", "1406"]},
                                {"label": "薯条", "quantity": 1, "codes": ["4810"]}],
                    "singles": [["1440", "麦辣鸡腿汉堡", "22.5"], ["1406", "板烧鸡腿堡", "23.5"], ["4810", "中薯条", "13.5"]]}
            part1 = {"products": [{"code": "C1", "name": "双堡套餐", "currentPrice": "40", "rounds": [
                {"id": 1, "minQuantity": 2, "choices": [["1440", "麦辣鸡腿汉堡", 0, 1], ["1406", "板烧鸡腿堡", "+1"]]},
                {"id": 2, "minQuantity": 1, "choices": [["4810", "中薯条", 0, 1]]}]}],
                "reference_prices": {"3050": "9.5"}}
            base["demands"].append({"id": "nug", "label": "麦乐鸡", "quantity": 8, "codes": ["1401"], "pieces": {"1401": 5}})
            base["singles"].append(["1401", "麦乐鸡5块", "14"])
            part1["products"][0]["rounds"].append({"id": 3, "minQuantity": 1, "choices": [["515106", "麦乐鸡4块", 0, 1]]})
            part1["demand_codes"] = {"nug": ["515106"]}
            part1["demand_pieces"] = {"nug": {"515106": 4}}
            part2 = {"products": [part1["products"][0],  # 两个子代理查到同一个套餐，只保留一份
                                  {"code": "C2", "name": "薯条可乐", "currentPrice": "15", "rounds": [
                                      {"id": 1, "minQuantity": 1, "choices": [["4810", "中薯条", 0, 1]]},
                                      {"id": 2, "minQuantity": 1, "choices": [["3050", "可乐中杯", 0, 1]]}]}]}
            for name, value in (("request.json", base), ("products_1.json", part1), ("products_2.json", part2)):
                with open(os.path.join(run, name), "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False)
            done = subprocess.run([sys.executable, "-B", script, "plan", run], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            with open(os.path.join(run, "plan.json"), encoding="utf-8") as handle:
                plan = json.load(handle)
            self.assertEqual([d["id"] for d in plan["demands"]], ["d1", "d2", "nug"])
            self.assertEqual(plan["demands"][0]["label"], "汉堡")
            self.assertEqual(plan["demands"][2]["codes"], ["1401", "515106"])
            best = best_quote(plan)
            self.assertEqual(best["estimate_cents"], 4000 + 1400)  # 套餐里的 4 块 + 单点 5 块 ≥ 8 块
            self.assertEqual(sum(1 for c in plan["names"] if c in ("C1", "C2")), 2)
            quotes = plan["quotes"]
            half = len(quotes) // 2 or 1
            for i, chunk in enumerate((quotes[:half], quotes[half:]), 1):  # 分两批写，每批一个文件
                with open(os.path.join(run, "quotes_%d.json" % i), "w", encoding="utf-8") as handle:
                    json.dump([quote_result(q, q["estimate_cents"]) for q in chunk], handle)
            done = subprocess.run([sys.executable, "-B", script, "pick", run], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertIn("0 个未计价", done.stdout)
            self.assertTrue(os.path.exists(os.path.join(run, "picked.json")))

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
        plan = opt.plan(request)
        self.assertTrue(all(s["complete"] for s in plan["search"].values()))
        self.assertTrue(all(s["engine"] == "weighted_dp" for s in plan["search"].values()))
        # 三份B0单点15元、两份S0单点9元，共63元；两份最低套餐加汉堡65元、分享餐加汉堡74元。
        self.assertEqual(best_quote(plan)["estimate_cents"], 6300)
        self.assertLessEqual(len(plan["quotes"]), 10)


if __name__ == "__main__":
    unittest.main()
