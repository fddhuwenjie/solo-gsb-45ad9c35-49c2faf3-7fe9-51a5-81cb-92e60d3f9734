"""奖品发放确认：顺序流程、幂等、拒绝条件、核验包重演、并发与确定性交错。

固定演示种子（README 同一例子）下，名单 A001..A005、名额 3 的扩展抽签序列为
    ['A003', 'A005', 'A001', 'A002', 'A004']

确认记录（RULES.md 第 5 节）：席位 slot、当时编号 entry、确认前弃权次数
forfeitsBefore。关键不变量：

- 只有当前有人占用席位的当前有效编号可确认；未揭晓/空席/旧编号/越界均拒绝；
- 同一席位同一编号重复提交返回既有记录（幂等）；
- 确认后该席位不能再弃权，其他席位仍可照常弃权递补；
- 确认与同席位弃权并发时按提交顺序串行生效，不留半次操作；
- 核验器从种子、封存名单与弃权顺序重演确认当时的席位，识别伪造编号、
  错置的确认阶段、确认后的弃权；无确认字段的旧包照常核验通过。
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from app import services

from .helpers import create_round, demo_commitment, make_app, open_conn, replace
from .test_concurrent_http import _Gate
from .test_interleave import TxWorker
from app.db import transaction

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT_DIR, "verify", "verify.py")

DEMO_ENTRIES = ["A001", "A002", "A003", "A004", "A005"]


class ConfirmFlowTest(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_app()
        self.client = self.app.test_client()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    # ------------------------------------------------ 辅助

    def api(self, method, path, **kw):
        return self.client.open(path, method=method, **kw)

    def get_round(self, rid):
        resp = self.api("GET", f"/api/rounds/{rid}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["round"]

    def make_revealed_round(self, entries=DEMO_ENTRIES, prize_count=3):
        rid = create_round(
            self.client, title="确认演示", prize_count=prize_count,
            commitment=self.commitment,
        )
        replace(self.client, rid, entries)
        self.assertEqual(
            self.api("POST", f"/api/rounds/{rid}/seal").status_code, 200
        )
        self.assertEqual(
            self.api("POST", f"/api/rounds/{rid}/reveal",
                     json={"seed": self.seed}).status_code,
            200,
        )
        return rid

    def confirm(self, rid, slot, entry):
        return self.api("POST", f"/api/rounds/{rid}/confirm",
                        json={"slot": slot, "entry": entry})

    def forfeit(self, rid, entry):
        return self.api("POST", f"/api/rounds/{rid}/forfeit",
                        json={"entry": entry})

    def export_pkg(self, rid):
        r = self.api("GET", f"/api/rounds/{rid}/package.json")
        self.assertEqual(r.status_code, 200)
        return json.loads(r.get_data(as_text=True))

    def run_verifier(self, pkg):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_path = os.path.join(tmp, "package.json")
            with open(pkg_path, "w", encoding="utf-8") as f:
                json.dump(pkg, f, ensure_ascii=False)
            return subprocess.run(
                [sys.executable, VERIFIER, pkg_path],
                capture_output=True, text=True,
            )

    # ------------------------------------------------ 状态门槛

    def test_confirm_requires_revealed_and_valid_slot(self):
        rid = create_round(self.client, commitment=self.commitment)
        replace(self.client, rid, DEMO_ENTRIES)

        # open：不能确认
        r = self.confirm(rid, 0, "A001")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"],
                         "轮次尚未揭晓，不能登记奖品发放确认")

        # sealed：仍不能确认
        self.api("POST", f"/api/rounds/{rid}/seal")
        r = self.confirm(rid, 0, "A001")
        self.assertEqual(r.status_code, 409)

        # 揭晓后
        self.api("POST", f"/api/rounds/{rid}/reveal",
                 json={"seed": self.seed})

        # 席位越界
        r = self.confirm(rid, 3, "A001")
        self.assertEqual(r.status_code, 400)
        self.assertIn("越界", r.get_json()["error"])
        r = self.confirm(rid, -1, "A001")
        self.assertEqual(r.status_code, 400)

        # 席位类型错误（bool 也不接受）
        for bad_slot in ("0", 1.0, None, True):
            r = self.confirm(rid, bad_slot, "A001")
            self.assertEqual(r.status_code, 400, bad_slot)

        # 空编号
        r = self.confirm(rid, 0, "  ")
        self.assertEqual(r.status_code, 400)

        # 不存在的轮次
        r = self.api("POST", "/api/rounds/no-such-round/confirm",
                     json={"slot": 0, "entry": "A001"})
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------ 完整流程

    def test_confirm_records_slot_entry_and_forfeits_before(self):
        rid = self.make_revealed_round()
        # winners = [A003, A005, A001]

        # 1) 先弃权 A003 → A002（seq 0）
        r = self.forfeit(rid, "A003")
        self.assertEqual(r.status_code, 200)

        # 2) 旧编号 A003 确认席位 0：拒绝（已被递补替换）
        r = self.confirm(rid, 0, "A003")
        self.assertEqual(r.status_code, 409)
        self.assertIn("旧编号", r.get_json()["error"])
        self.assertEqual(self.get_round(rid)["confirmations"], [])

        # 3) 当前编号 A002 确认席位 0：记录 forfeitsBefore=1
        r = self.confirm(rid, 0, "A002")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["confirmation"],
                         {"slot": 0, "entry": "A002", "forfeitsBefore": 1})
        rnd = body["round"]
        self.assertEqual(rnd["slots"], ["A002", "A005", "A001"])
        self.assertEqual(len(rnd["confirmations"]), 1)
        self.assertEqual(rnd["confirmations"][0]["entry"], "A002")

        # 4) 同一席位同一编号重复提交：返回既有记录，不新增
        r = self.confirm(rid, 0, "A002")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["alreadyConfirmed"])
        self.assertEqual(len(body["round"]["confirmations"]), 1)

        # 5) 同一席位换编号确认：按旧编号/已确认拒绝
        # 先让席位 0 再变化是不可能的（已锁）；直接拿别的编号尝试
        r = self.confirm(rid, 0, "A005")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.get_round(rid)["confirmations"]), 1)

        # 6) 确认后该席位不能再弃权；其他席位照常弃权递补
        r = self.forfeit(rid, "A002")
        self.assertEqual(r.status_code, 409)
        self.assertIn("已登记奖品发放确认", r.get_json()["error"])

        r = self.forfeit(rid, "A005")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["forfeit"]["replacement"], "A004")
        final = self.get_round(rid)
        self.assertEqual(final["slots"], ["A002", "A004", "A001"])
        # 确认记录不被后续弃权改动
        self.assertEqual(final["confirmations"][0]["entry"], "A002")
        self.assertEqual(final["confirmations"][0]["forfeitsBefore"], 1)
        # 原始中奖结果与揭晓时间不变
        self.assertEqual(final["winners"], ["A003", "A005", "A001"])

    def test_confirm_before_any_forfeit_has_zero_count(self):
        rid = self.make_revealed_round()
        r = self.confirm(rid, 2, "A001")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.get_json()["confirmation"],
            {"slot": 2, "entry": "A001", "forfeitsBefore": 0},
        )

    def test_empty_seat_cannot_be_confirmed(self):
        """人数 == 名额：弃权后席位留空，空席确认被拒；递补者路径不受影响。"""
        rid = self.make_revealed_round(
            entries=["B001", "B002", "B003"], prize_count=3
        )
        winners = self.get_round(rid)["winners"]
        r = self.forfeit(rid, winners[0])
        self.assertIsNone(r.get_json()["forfeit"]["replacement"])

        # 空席：无论用什么编号都不能确认（报空席）
        r = self.confirm(rid, 0, winners[0])
        self.assertEqual(r.status_code, 409)
        self.assertIn("空席", r.get_json()["error"])

        # 其他有人席位仍可确认
        r = self.confirm(rid, 1, winners[1])
        self.assertEqual(r.status_code, 200)

    def test_other_seats_keep_advancing_after_confirm(self):
        """已确认席位不影响其他席位反复弃权递补；扩展序列序号不受影响。"""
        rid = self.make_revealed_round()
        # 确认席位 1（A005）
        self.assertEqual(self.confirm(rid, 1, "A005").status_code, 200)

        # 席位 0 弃权 → A002（扩展序列第 4 个）
        self.assertEqual(
            self.forfeit(rid, "A003").get_json()["forfeit"]["replacement"],
            "A002",
        )
        # 席位 2 弃权 → A004（扩展序列第 5 个）
        self.assertEqual(
            self.forfeit(rid, "A001").get_json()["forfeit"]["replacement"],
            "A004",
        )
        rnd = self.get_round(rid)
        self.assertEqual(rnd["slots"], ["A002", "A005", "A004"])
        # 席位 1 始终锁定
        self.assertEqual(self.forfeit(rid, "A005").status_code, 409)
        # 已确认席位上的编号也不会出现在后续递补里（A005 本就是原始中奖者）
        self.assertEqual(
            [f["replacement"] for f in rnd["forfeits"]], ["A002", "A004"]
        )

    # ------------------------------------------------ 核验包

    def test_package_with_confirmations_verifies(self):
        rid = self.make_revealed_round()
        # 揭晓即确认席位 0（fb=0）
        self.confirm(rid, 0, "A003")
        # 弃权席位 1：A005 → A002（seq0）
        self.forfeit(rid, "A005")
        # 弃权席位 2：A001 → A004（seq1）
        self.forfeit(rid, "A001")
        # 递补者 A004 确认席位 2（fb=2）
        self.confirm(rid, 2, "A004")

        pkg = self.export_pkg(rid)
        self.assertEqual(
            pkg["confirmations"],
            [
                {"slot": 0, "entry": "A003", "forfeitsBefore": 0,
                 "at": pkg["confirmations"][0]["at"]},
                {"slot": 2, "entry": "A004", "forfeitsBefore": 2,
                 "at": pkg["confirmations"][1]["at"]},
            ],
        )
        self.assertEqual(pkg["currentWinners"], ["A003", "A002", "A004"])

        proc = self.run_verifier(pkg)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("奖品发放确认", proc.stdout)
        self.assertIn("已确认发放", proc.stdout)
        self.assertIn("全部核验通过", proc.stdout)

    def test_verifier_detects_confirmation_tampering(self):
        rid = self.make_revealed_round()
        self.forfeit(rid, "A003")  # 席位0：A003 → A002（seq0）
        self.confirm(rid, 0, "A002")  # fb=1
        self.confirm(rid, 1, "A005")  # fb=1
        pkg = self.export_pkg(rid)

        def check(tampered, *keywords):
            proc = self.run_verifier(tampered)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            for kw in keywords:
                self.assertIn(kw, proc.stdout)

        # 1) 伪造确认编号（名单外）
        t = copy.deepcopy(pkg)
        t["confirmations"][0]["entry"] = "A009"
        check(t, "伪造编号")

        # 2) 错置确认阶段（fb=1 改成 0：当时持有者是 A003）
        t = copy.deepcopy(pkg)
        t["confirmations"][0]["forfeitsBefore"] = 0
        check(t, "错置的确认阶段")

        # 3) 确认之后的弃权（席位0 再追加一条 A002 的弃权）
        t = copy.deepcopy(pkg)
        t["forfeits"].append(
            {"seq": 1, "entry": "A002", "replacement": "A004", "at": "x"}
        )
        t["currentWinners"] = ["A004", "A005", "A001"]
        check(t, "确认", "之后")

        # 4) 同一席位两条确认
        t = copy.deepcopy(pkg)
        t["confirmations"].append(
            {"slot": 0, "entry": "A002", "forfeitsBefore": 1, "at": "x"}
        )
        check(t, "多条发放确认")

        # 5) forfeitsBefore 越界
        t = copy.deepcopy(pkg)
        t["confirmations"][0]["forfeitsBefore"] = 9
        check(t, "forfeitsBefore")

        # 6) 席位越界 / 类型错误
        t = copy.deepcopy(pkg)
        t["confirmations"][0]["slot"] = 7
        check(t, "发放确认席位非法")
        t = copy.deepcopy(pkg)
        t["confirmations"][0]["slot"] = "0"
        check(t, "发放确认席位非法")

        # 7) 伪造当前席位（确认锁定的席位被改成别的编号）
        t = copy.deepcopy(pkg)
        t["currentWinners"] = ["A004", "A005", "A001"]
        check(t, "确认后席位不应再变化")

    def test_verifier_accepts_packages_without_confirmation_field(self):
        """无 confirmations 字段的包（仅弃权版 / 完全旧版）照常核验通过。"""
        rid = self.make_revealed_round()
        self.forfeit(rid, "A003")
        self.confirm(rid, 0, "A002")
        pkg = self.export_pkg(rid)

        # 去掉 confirmations：弃权与席位仍应核验通过
        legacy = {k: v for k, v in pkg.items() if k != "confirmations"}
        proc = self.run_verifier(legacy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("无发放确认字段", proc.stdout)

        # 再去掉弃权字段（完全旧版）
        older = {
            k: v for k, v in pkg.items()
            if k not in ("confirmations", "forfeits", "currentWinners")
        }
        proc = self.run_verifier(older)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("旧版核验包", proc.stdout)

    def test_zip_package_contains_confirmations(self):
        import io
        import zipfile
        rid = self.make_revealed_round()
        self.confirm(rid, 0, "A003")
        r = self.api("GET", f"/api/rounds/{rid}/package.zip")
        self.assertEqual(r.status_code, 200)
        data = r.get_data()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "pkg.zip")
            with open(zip_path, "wb") as f:
                f.write(data)
            with zipfile.ZipFile(zip_path) as zf:
                pkg = json.loads(
                    zf.read(next(
                        n for n in zf.namelist() if n.endswith("package.json")
                    )).decode("utf-8")
                )
            self.assertEqual(pkg["confirmations"][0]["entry"], "A003")
            proc = subprocess.run(
                [sys.executable, VERIFIER, zip_path],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ConfirmConcurrentHttpTest(unittest.TestCase):
    """确认与同席位弃权并发：按提交顺序生效，两边都不留半次操作。"""

    def setUp(self):
        self.app, self.db_path = make_app()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def _revealed_round(self):
        client = self.app.test_client()
        rid = create_round(client, prize_count=3, commitment=self.commitment)
        replace(client, rid, DEMO_ENTRIES)
        self.assertEqual(client.post(f"/api/rounds/{rid}/seal").status_code, 200)
        self.assertEqual(
            client.post(f"/api/rounds/{rid}/reveal",
                        json={"seed": self.seed}).status_code,
            200,
        )
        return rid

    def test_concurrent_confirm_same_slot_same_entry_one_wins(self):
        """同席位同编号并发确认：恰好一条真正生效，另一条幂等返回。"""
        rid = self._revealed_round()
        gate = _Gate(2)
        results = []

        def do_confirm():
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            results.append(local.post(
                f"/api/rounds/{rid}/confirm",
                json={"slot": 0, "entry": "A003"},
            ))

        threads = [threading.Thread(target=do_confirm) for _ in range(2)]
        for t in threads:
            t.start()
        gate.wait_ready_and_open()
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())

        bodies = [r.get_json() for r in results]
        self.assertTrue(all(b["ok"] for b in bodies), bodies)
        self.assertEqual(
            sum(1 for b in bodies if b.get("alreadyConfirmed")), 1
        )
        final = self.app.test_client().get(
            f"/api/rounds/{rid}").get_json()["round"]
        self.assertEqual(len(final["confirmations"]), 1)
        self.assertEqual(final["confirmations"][0], {
            "slot": 0, "entry": "A003", "forfeitsBefore": 0,
            "at": final["confirmations"][0]["at"],
        })

    def test_concurrent_confirm_and_forfeit_same_slot(self):
        """同席位确认与弃权并发：必然一胜一负，不出现既确认又递补的半次状态。"""
        rid = self._revealed_round()
        gate = _Gate(2)
        outcomes = {}

        def do_confirm():
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            outcomes["confirm"] = local.post(
                f"/api/rounds/{rid}/confirm",
                json={"slot": 0, "entry": "A003"},
            )

        def do_forfeit():
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            outcomes["forfeit"] = local.post(
                f"/api/rounds/{rid}/forfeit",
                json={"entry": "A003"},
            )

        threads = [
            threading.Thread(target=do_confirm),
            threading.Thread(target=do_forfeit),
        ]
        for t in threads:
            t.start()
        gate.wait_ready_and_open()
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())

        final = self.app.test_client().get(
            f"/api/rounds/{rid}").get_json()["round"]

        confirm_ok = outcomes["confirm"].status_code == 200
        forfeit_ok = outcomes["forfeit"].status_code == 200
        self.assertNotEqual(
            confirm_ok, forfeit_ok,
            "同席位确认与弃权必须恰好一边成功，不能都成功或都失败："
            f"confirm={outcomes['confirm'].status_code} "
            f"forfeit={outcomes['forfeit'].status_code}",
        )
        # 无半次操作：
        n_confirm = len(final["confirmations"])
        n_forfeit = len(final["forfeits"])
        self.assertEqual(n_confirm + n_forfeit, 1)
        if confirm_ok:
            # 确认先完成：席位 0 仍是 A003，无弃权记录
            self.assertEqual(final["slots"], ["A003", "A005", "A001"])
            self.assertEqual(final["confirmations"][0]["entry"], "A003")
            self.assertEqual(outcomes["forfeit"].status_code, 409)
        else:
            # 弃权先完成：席位 0 已递补为 A002，旧编号确认失败，无确认记录
            self.assertEqual(final["slots"], ["A002", "A005", "A001"])
            self.assertEqual(final["forfeits"][0]["entry"], "A003")
            self.assertEqual(outcomes["confirm"].status_code, 409)
            # 递补者仍可确认
            r = self.app.test_client().post(
                f"/api/rounds/{rid}/confirm",
                json={"slot": 0, "entry": "A002"},
            )
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class ConfirmInterleaveTest(unittest.TestCase):
    """确定性交错：同席位确认/弃权在写锁两侧按提交顺序生效。"""

    def setUp(self):
        self.app, self.db_path = make_app()
        self.client = self.app.test_client()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def _revealed(self):
        rid = create_round(self.client, commitment=self.commitment)
        replace(self.client, rid, DEMO_ENTRIES)
        self.assertEqual(
            self.client.post(f"/api/rounds/{rid}/seal").status_code, 200
        )
        self.assertEqual(
            self.client.post(
                f"/api/rounds/{rid}/reveal", json={"seed": self.seed}
            ).status_code,
            200,
        )
        return rid

    def test_forfeit_commits_first_confirm_with_old_entry_fails(self):
        rid = self._revealed()

        # 主连接：弃权 A003（席位0），持锁不提交
        main = open_conn(self.db_path)
        main.execute("BEGIN IMMEDIATE")
        first = services.forfeit_tx(main, rid, "A003")
        self.assertEqual(first["forfeit"]["replacement"], "A002")

        # 工作线程：用旧编号 A003 确认席位0 —— 阻塞在写锁上
        worker = TxWorker(
            self.db_path,
            lambda c: services.confirm_tx(c, rid, 0, "A003"),
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        # 弃权先提交：旧编号确认失败（409），且没有半条确认
        from app.services import ApiError
        self.assertIsInstance(worker.exc, ApiError)
        self.assertEqual(worker.exc.status, 409)
        with transaction(open_conn(self.db_path)) as conn:
            self.assertEqual(services.confirmation_rows(conn, rid), [])
            forfeits = services.forfeit_rows(conn, rid)
        self.assertEqual([f["entry"] for f in forfeits], ["A003"])

        # 递补者 A002 可以确认，forfeitsBefore=1
        with transaction(open_conn(self.db_path)) as conn:
            result = services.confirm_tx(conn, rid, 0, "A002")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["confirmation"]["forfeitsBefore"], 1)

    def test_confirm_commits_first_following_forfeit_fails(self):
        rid = self._revealed()

        # 主连接：确认席位0=A003，持锁不提交
        main = open_conn(self.db_path)
        main.execute("BEGIN IMMEDIATE")
        services.confirm_tx(main, rid, 0, "A003")

        # 工作线程：弃权 A003 —— 阻塞
        worker = TxWorker(
            self.db_path, lambda c: services.forfeit_tx(c, rid, "A003")
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        # 确认先提交：弃权失败（409），没有半条弃权
        from app.services import ApiError
        self.assertIsInstance(worker.exc, ApiError)
        self.assertEqual(worker.exc.status, 409)
        with transaction(open_conn(self.db_path)) as conn:
            self.assertEqual(services.forfeit_rows(conn, rid), [])
            confirms = services.confirmation_rows(conn, rid)
        self.assertEqual(
            [(c["slot"], c["entry"], c["forfeitsBefore"]) for c in confirms],
            [(0, "A003", 0)],
        )

        # 其他席位仍可弃权递补
        with transaction(open_conn(self.db_path)) as conn:
            result = services.forfeit_tx(conn, rid, "A005")
        self.assertEqual(result["forfeit"]["replacement"], "A002")


if __name__ == "__main__":
    unittest.main()
