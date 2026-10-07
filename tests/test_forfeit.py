"""弃权与递补：顺序流程、幂等、空席、核验包重演、并发串行与确定性交错。

固定演示种子（README 同一例子）下，名单 A001..A005、名额 3 的扩展抽签序列为
    ['A003', 'A005', 'A001', 'A002', 'A004']
即原始中奖 [A003, A005, A001]，第 1 次弃权递补 A002，第 2 次递补 A004，
再往后名单无人可补，席位留空。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

from app import rules
from app import services
from app.db import transaction

from .helpers import create_round, demo_commitment, make_app, open_conn, replace
from .test_concurrent_http import _Gate
from .test_interleave import TxWorker

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT_DIR, "verify", "verify.py")

DEMO_ENTRIES = ["A001", "A002", "A003", "A004", "A005"]


class ForfeitFlowTest(unittest.TestCase):
    """顺序流程：弃权 → 递补 → 幂等 → 空席 → 核验包独立重演。"""

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
            self.client, title="弃权演示", prize_count=prize_count,
            commitment=self.commitment,
        )
        replace(self.client, rid, entries)
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 200)
        r = self.api("POST", f"/api/rounds/{rid}/reveal",
                     json={"seed": self.seed})
        self.assertEqual(r.status_code, 200)
        return rid

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

    def test_forfeit_requires_revealed(self):
        rid = create_round(self.client, commitment=self.commitment)
        replace(self.client, rid, DEMO_ENTRIES)

        # open：不能弃权
        r = self.forfeit(rid, "A001")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "轮次尚未揭晓，不能标记弃权")

        # sealed：仍不能弃权
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 200)
        r = self.forfeit(rid, "A001")
        self.assertEqual(r.status_code, 409)

        # 不存在的轮次：404
        r = self.forfeit("no-such-round", "A001")
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------ 完整流程

    def test_forfeit_replacement_idempotent_and_empty_seat(self):
        rid = self.make_revealed_round()
        extended = rules.draw_winners(DEMO_ENTRIES, self.seed, 5)
        self.assertEqual(extended, ["A003", "A005", "A001", "A002", "A004"])

        revealed = self.get_round(rid)
        self.assertEqual(revealed["winners"], ["A003", "A005", "A001"])
        self.assertEqual(revealed["slots"], ["A003", "A005", "A001"])
        self.assertEqual(revealed["forfeits"], [])
        revealed_at = revealed["revealedAt"]

        # 非当前有效编号不可操作：名单内未中奖 / 名单外 / 空串
        for bad in ["A004", "A006", "", "  "]:
            r = self.forfeit(rid, bad)
            self.assertEqual(r.status_code, 409 if bad.strip() else 400, bad)
        self.assertEqual(self.get_round(rid)["forfeits"], [])

        # 1) 原中奖者 A003 弃权 → 递补扩展序列第 4 个 A002
        r = self.forfeit(rid, "A003")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["forfeit"],
                         {"seq": 0, "entry": "A003", "replacement": "A002"})
        rnd = body["round"]
        self.assertEqual(rnd["slots"], ["A002", "A005", "A001"])
        self.assertEqual(len(rnd["forfeits"]), 1)
        # 原始中奖顺序与揭晓时间不变
        self.assertEqual(rnd["winners"], ["A003", "A005", "A001"])
        self.assertEqual(rnd["revealedAt"], revealed_at)

        # 2) 重复提交同一弃权：返回既有结果，不重复递补
        r = self.forfeit(rid, "A003")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["alreadyForfeited"])
        self.assertEqual(len(body["round"]["forfeits"]), 1)
        self.assertEqual(body["round"]["slots"], ["A002", "A005", "A001"])

        # 3) 递补者 A002 也可弃权 → 递补下一个 A004
        r = self.forfeit(rid, "A002")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["forfeit"],
                         {"seq": 1, "entry": "A002", "replacement": "A004"})
        self.assertEqual(body["round"]["slots"], ["A004", "A005", "A001"])

        # 4) 名单共 5 人已全部出现：再弃权 A004 无候补，席位留空
        r = self.forfeit(rid, "A004")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNone(body["forfeit"]["replacement"])
        self.assertEqual(body["round"]["slots"], [None, "A005", "A001"])

        # 5) 空席不可操作；其余席位可继续弃权直至全部留空
        r = self.forfeit(rid, "A004")
        self.assertTrue(r.get_json()["alreadyForfeited"])
        r = self.forfeit(rid, "A005")
        self.assertIsNone(r.get_json()["forfeit"]["replacement"])
        r = self.forfeit(rid, "A001")
        self.assertIsNone(r.get_json()["forfeit"]["replacement"])
        final = self.get_round(rid)
        self.assertEqual(final["slots"], [None, None, None])
        self.assertEqual(len(final["forfeits"]), 5)
        # 原始结果与揭晓时间始终不变
        self.assertEqual(final["winners"], ["A003", "A005", "A001"])
        self.assertEqual(final["revealedAt"], revealed_at)

    def test_forfeit_with_exact_list_size_leaves_empty_seat(self):
        """人数 == 名额：第一次弃权即无候补，席位直接留空。"""
        rid = self.make_revealed_round(entries=["B001", "B002", "B003"],
                                       prize_count=3)
        winners = self.get_round(rid)["winners"]
        r = self.forfeit(rid, winners[0])
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNone(body["forfeit"]["replacement"])
        slots = body["round"]["slots"]
        self.assertIsNone(slots[0])
        self.assertEqual(slots[1:], winners[1:])

    # ------------------------------------------------ 核验包

    def test_package_with_forfeits_verifies_and_detects_tampering(self):
        rid = self.make_revealed_round()
        self.forfeit(rid, "A003")  # → A002
        self.forfeit(rid, "A005")  # → A004

        pkg = self.export_pkg(rid)
        self.assertEqual(
            pkg["forfeits"],
            [
                {"seq": 0, "entry": "A003", "replacement": "A002",
                 "at": pkg["forfeits"][0]["at"]},
                {"seq": 1, "entry": "A005", "replacement": "A004",
                 "at": pkg["forfeits"][1]["at"]},
            ],
        )
        self.assertEqual(pkg["currentWinners"], ["A002", "A004", "A001"])
        self.assertEqual(pkg["winners"], ["A003", "A005", "A001"])

        # 1) 原样核验通过
        proc = self.run_verifier(pkg)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("弃权/递补记录", proc.stdout)
        self.assertIn("全部核验通过", proc.stdout)

        # 2) 伪造递补：核验器必须指出
        forged = json.loads(json.dumps(pkg))
        forged["forfeits"][0]["replacement"] = "A004"
        forged["currentWinners"] = ["A004", "A004", "A001"]
        proc = self.run_verifier(forged)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("递补不符", proc.stdout)

        # 3) 跳过一条弃权记录：重演对不上当前席位
        skipped = json.loads(json.dumps(pkg))
        skipped["forfeits"] = skipped["forfeits"][:1]
        proc = self.run_verifier(skipped)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("当前有效席位", proc.stdout)

        # 4) 伪造弃权顺序（两条记录对调）
        swapped = json.loads(json.dumps(pkg))
        swapped["forfeits"] = [
            dict(swapped["forfeits"][1], seq=0),
            dict(swapped["forfeits"][0], seq=1),
        ]
        proc = self.run_verifier(swapped)
        self.assertEqual(proc.returncode, 1)

        # 5) 旧版包（无弃权字段）：继续核验通过
        legacy = {k: v for k, v in pkg.items()
                  if k not in ("forfeits", "currentWinners")}
        proc = self.run_verifier(legacy)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("旧版核验包", proc.stdout)
        self.assertIn("全部核验通过", proc.stdout)

    def test_zip_package_contains_forfeits_and_verifies(self):
        rid = self.make_revealed_round()
        self.forfeit(rid, "A003")

        r = self.api("GET", f"/api/rounds/{rid}/package.zip")
        self.assertEqual(r.status_code, 200)
        data = r.get_data()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "pkg.zip")
            with open(zip_path, "wb") as f:
                f.write(data)
            with zipfile.ZipFile(zip_path) as zf:
                pkg_name = next(
                    n for n in zf.namelist() if n.endswith("package.json")
                )
                pkg = json.loads(zf.read(pkg_name).decode("utf-8"))
            self.assertEqual(len(pkg["forfeits"]), 1)
            self.assertEqual(pkg["currentWinners"][0], "A002")
            pkg_path = os.path.join(tmp, "package.json")
            with open(pkg_path, "w", encoding="utf-8") as f:
                json.dump(pkg, f, ensure_ascii=False)
            proc = subprocess.run(
                [sys.executable, VERIFIER, zip_path],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0,
                             proc.stdout + proc.stderr)


class ForfeitConcurrentHttpTest(unittest.TestCase):
    """并发弃权：同一轮次内串行生效，不会同一人占两席或只记弃权不记递补。"""

    def setUp(self):
        self.app, self.db_path = make_app()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def _revealed_round(self, entries, prize_count):
        client = self.app.test_client()
        rid = create_round(client, prize_count=prize_count,
                           commitment=self.commitment)
        replace(client, rid, entries)
        self.assertEqual(client.post(f"/api/rounds/{rid}/seal").status_code, 200)
        resp = client.post(f"/api/rounds/{rid}/reveal",
                           json={"seed": self.seed})
        self.assertEqual(resp.status_code, 200)
        return rid

    def test_concurrent_forfeit_different_slots_get_distinct_replacements(self):
        rid = self._revealed_round(DEMO_ENTRIES, 3)
        gate = _Gate(2)
        results = {}

        def do_forfeit(tag, entry):
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            results[tag] = local.post(f"/api/rounds/{rid}/forfeit",
                                      json={"entry": entry})

        threads = [
            threading.Thread(target=do_forfeit, args=("t1", "A003")),
            threading.Thread(target=do_forfeit, args=("t2", "A005")),
        ]
        for t in threads:
            t.start()
        gate.wait_ready_and_open()
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())

        # 两次弃权都成功；递补按提交先后分别取扩展序列第 4、5 个
        replacements = []
        for tag in ("t1", "t2"):
            body = results[tag].get_json()
            self.assertEqual(results[tag].status_code, 200, body)
            self.assertNotIn("alreadyForfeited", body)
            replacements.append(body["forfeit"]["replacement"])
        self.assertEqual(sorted(replacements), ["A002", "A004"])

        final = self.app.test_client().get(
            f"/api/rounds/{rid}").get_json()["round"]
        self.assertEqual(len(final["forfeits"]), 2)
        self.assertEqual(
            [f["seq"] for f in final["forfeits"]], [0, 1]
        )
        # 不变量：当前席位无重复、无空席，且每个递补都紧跟对应弃权记录
        self.assertEqual(sorted(final["slots"]), ["A001", "A002", "A004"])
        for f in final["forfeits"]:
            self.assertIsNotNone(f["replacement"])
        self.assertEqual(final["winners"], ["A003", "A005", "A001"])

    def test_concurrent_forfeit_same_entry_is_idempotent(self):
        rid = self._revealed_round(DEMO_ENTRIES, 3)
        gate = _Gate(2)
        results = []

        def do_forfeit():
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            results.append(
                local.post(f"/api/rounds/{rid}/forfeit",
                           json={"entry": "A003"})
            )

        threads = [threading.Thread(target=do_forfeit) for _ in range(2)]
        for t in threads:
            t.start()
        gate.wait_ready_and_open()
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())

        bodies = [r.get_json() for r in results]
        self.assertTrue(all(b["ok"] for b in bodies))
        # 恰好一次真正生效，另一次返回既有结果
        self.assertEqual(
            sum(1 for b in bodies if b.get("alreadyForfeited")), 1
        )
        final = self.app.test_client().get(
            f"/api/rounds/{rid}").get_json()["round"]
        self.assertEqual(len(final["forfeits"]), 1)
        self.assertEqual(final["forfeits"][0]["replacement"], "A002")
        self.assertEqual(final["slots"], ["A002", "A005", "A001"])


class ForfeitInterleaveTest(unittest.TestCase):
    """确定性交错：后到弃权在写锁解除后必须读到先到的弃权记录。"""

    def setUp(self):
        self.app, self.db_path = make_app()
        self.client = self.app.test_client()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_second_forfeit_sees_first_and_takes_next_replacement(self):
        rid = create_round(self.client, commitment=self.commitment)
        replace(self.client, rid, DEMO_ENTRIES)
        self.assertEqual(
            self.client.post(f"/api/rounds/{rid}/seal").status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/rounds/{rid}/reveal",
                             json={"seed": self.seed}).status_code, 200)

        # 主连接：弃权 A003，持锁不提交
        main = open_conn(self.db_path)
        main.execute("BEGIN IMMEDIATE")
        first = services.forfeit_tx(main, rid, "A003")
        self.assertEqual(first["forfeit"]["replacement"], "A002")

        # 工作线程：弃权 A005 —— 必然阻塞在写锁上
        worker = TxWorker(
            self.db_path, lambda c: services.forfeit_tx(c, rid, "A005")
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        # 后到者读到已提交的弃权记录，递补为扩展序列的下一个 A004
        self.assertIsNone(worker.exc, worker.exc)
        self.assertEqual(worker.result["forfeit"]["seq"], 1)
        self.assertEqual(worker.result["forfeit"]["replacement"], "A004")

        with transaction(open_conn(self.db_path)) as conn:
            forfeits = services.forfeit_rows(conn, rid)
        self.assertEqual([f["entry"] for f in forfeits], ["A003", "A005"])
        self.assertEqual(
            [f["replacement"] for f in forfeits], ["A002", "A004"]
        )


if __name__ == "__main__":
    unittest.main()
