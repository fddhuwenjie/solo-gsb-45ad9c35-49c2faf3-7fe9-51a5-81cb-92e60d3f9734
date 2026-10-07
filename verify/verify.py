#!/usr/bin/env python3
"""现场抽签核验包独立校验工具（sha256-argmax-v1）。

用法：
    python3 verify.py package.json
    python3 verify.py lottery-<轮次编号>-package.zip
    python3 verify.py package.json --commitment <事先留存的承诺> \\
                                   --list-digest <事先留存的名单摘要>

本脚本只使用 Python 3 标准库。它不会相信核验包里的 winners 字段：
中奖编号一律由种子和名单按公开规则重新计算后逐项比对；
弃权/递补记录（forfeits/currentWinners，RULES.md 第 4 节）也从种子与名单
独立重演核对，能指出伪造或跳过的递补。旧版核验包（无弃权字段）按无弃权处理。
奖品发放确认（confirmations，RULES.md 第 5 节）按“确认前弃权次数”被放回
弃权序列的对应阶段重演：核验确认当时的席位持有者、识别伪造编号、错置的确认
阶段或确认之后的弃权；旧版核验包（无确认字段）按无确认处理。
发现任何不符，退出码为 1；全部通过退出码为 0。
"""
import argparse
import hashlib
import io
import json
import sys
import zipfile

RULE_ID = "sha256-argmax-v1"
COMMIT_PREFIX = "COMMIT:"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_snapshot(entries):
    return "\n".join(sorted(entries, key=lambda e: e.encode("utf-8")))


def draw_winners(entries, seed_hex, prize_count):
    pool = sorted(entries, key=lambda e: e.encode("utf-8"))
    winners = []
    for i in range(prize_count):
        best_id, best_score = None, -1
        for entry_id in pool:
            msg = f"{RULE_ID}|{seed_hex}|{entry_id}|{i}".encode("utf-8")
            score = int.from_bytes(hashlib.sha256(msg).digest(), "big")
            if score > best_score:  # 平局保留字节序靠前者
                best_score, best_id = score, entry_id
        winners.append(best_id)
        pool.remove(best_id)
    return winners


def load_package(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.endswith("package.json")]
            if not names:
                raise ValueError("zip 中找不到 package.json")
            return json.loads(zf.read(names[0]).decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def verify(pkg, expected_commitment=None, expected_digest=None):
    errors, checks = [], []

    def ok(msg):
        checks.append(msg)

    def fail(msg):
        errors.append(msg)

    # ---- 结构 ----
    if pkg.get("format") != "public-lottery-verification-v1":
        fail(f"format 字段不符：{pkg.get('format')!r}")
    rule_id = pkg.get("ruleId")
    if rule_id != RULE_ID:
        fail(f"规则标识不符：包内为 {rule_id!r}，本核验器只认 {RULE_ID}")

    rnd = pkg.get("round", {})
    title = rnd.get("title", "?")
    prize_count = rnd.get("prizeCount")
    commitment = (rnd.get("commitment") or "").lower()
    seed = (rnd.get("seed") or "").strip().lower() or None
    stored_digest = (rnd.get("listDigest") or "").lower() or None
    status = rnd.get("status")
    entries = pkg.get("participants")
    stored_winners = pkg.get("winners")

    print(f"轮次：{title}（{rnd.get('id','?')}）")
    print(f"状态：{status}    名额：{prize_count}    人数：{pkg.get('participantCount')}")
    print(f"规则：{rule_id}")
    print("-" * 68)

    if not isinstance(prize_count, int) or isinstance(prize_count, bool) or prize_count < 1:
        fail(f"中奖名额非法：{prize_count!r}")
        prize_count = None
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        fail("participants 必须是字符串数组")
        return errors, checks

    # ---- 名单本身：空编号 / 换行 / 重复 / 顺序 ----
    bad_ids = [e for e in entries if not e.strip() or e != e.strip()
               or "\n" in e or "\r" in e]
    if bad_ids:
        fail(f"名单含空编号或含换行/首尾空白的编号：{bad_ids[:5]}")
    dup = sorted({e for e in entries if entries.count(e) > 1})
    if dup:
        fail(f"名单存在重复编号：{dup}")

    want_order = [e for e in sorted(entries, key=lambda e: e.encode("utf-8"))]
    if entries != want_order:
        mismatch_pos = next(
            (i for i, (a, b) in enumerate(zip(entries, want_order)) if a != b),
            min(len(entries), len(want_order)),
        )
        fail(
            "名单顺序不是规范的 UTF-8 字节序："
            f"首个偏差在第 {mismatch_pos + 1} 项 "
            f"（包内 {entries[mismatch_pos] if mismatch_pos < len(entries) else '∅'}，"
            f"应为 {want_order[mismatch_pos] if mismatch_pos < len(want_order) else '∅'}）"
        )
    else:
        ok("名单为按 UTF-8 字节序排序的规范名单，且编号无重复")

    # ---- 名单摘要：重算 vs 包内 ----
    recomputed_digest = sha256_hex(canonical_snapshot(entries).encode("utf-8"))
    if stored_digest is None:
        fail("核验包缺少 listDigest（名单摘要）")
    elif recomputed_digest != stored_digest:
        fail(
            "名单摘要不符：包内名单重算为\n"
            f"        {recomputed_digest}\n"
            f"  包内记录为 {stored_digest}"
        )
    else:
        ok(f"名单摘要核对一致：{stored_digest}")

    # ---- 事先留存的摘要 ----
    if expected_digest is not None:
        expected_digest = expected_digest.strip().lower()
        if expected_digest != recomputed_digest:
            fail(
                "事先留存的名单摘要与包内名单重算结果不符：\n"
                f"        事先留存 {expected_digest}\n"
                f"        重新计算 {recomputed_digest}"
            )
        elif stored_digest and expected_digest != stored_digest:
            fail("事先留存的名单摘要与包内 listDigest 字段不符")
        else:
            ok("与事先留存的名单摘要一致")

    # ---- 承诺格式 ----
    if len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
        fail(f"承诺不是 64 位十六进制：{commitment!r}")

    # ---- 事先留存的承诺 ----
    if expected_commitment is not None:
        expected_commitment = expected_commitment.strip().lower()
        if expected_commitment != commitment:
            fail(
                f"事先留存的承诺与包内承诺不符：\n"
                f"        事先留存 {expected_commitment}\n"
                f"        包内记录 {commitment}"
            )
        else:
            ok("与事先留存的承诺一致")

    # ---- 种子—承诺 ----
    if seed is None:
        print("提示：该包处于封存（未揭晓）状态，暂无种子与结果可校验。")
        if status != "sealed":
            fail(f"包内无种子，但状态为 {status!r} 而非 'sealed'")
        return errors, checks

    if len(seed) != 64 or any(c not in "0123456789abcdef" for c in seed):
        fail(f"种子不是 64 位十六进制：{seed!r}")
        return errors, checks

    recomputed_commit = sha256_hex((COMMIT_PREFIX + seed).encode("utf-8"))
    if recomputed_commit != commitment:
        fail(
            "种子与承诺不匹配：\n"
            f"        由种子重算 {recomputed_commit}\n"
            f"        包内承诺     {commitment}"
        )
    else:
        ok("种子与承诺匹配：SHA256('COMMIT:'+seed) 与承诺一致")

    # ---- 状态与结果字段 ----
    if status != "revealed":
        fail(f"包内有种子/结果，但状态为 {status!r}，应为 'revealed'")

    # ---- 关键：按规则从种子+名单独立重抽，绝不只比对包内 winners ----
    if prize_count is not None and prize_count <= len(entries) and len(dup) == 0:
        recalc = draw_winners(entries, seed, prize_count)
        if stored_winners is None:
            fail("核验包缺少 winners 字段")
        elif not isinstance(stored_winners, list):
            fail("winners 字段不是数组")
        else:
            if len(stored_winners) != prize_count:
                fail(f"中奖数量 {len(stored_winners)} 不等于名额 {prize_count}")
            if len(set(stored_winners)) != len(stored_winners):
                fail("包内中奖编号存在重复")
            outside = [w for w in stored_winners if w not in entries]
            if outside:
                fail(f"包内中奖编号不在封存名单中：{outside}")
            if stored_winners != recalc:
                diffs = []
                for i, (a, b) in enumerate(zip(stored_winners, recalc)):
                    if a != b:
                        diffs.append(f"第 {i + 1} 位：包内 {a!r}，重算 {b!r}")
                if len(stored_winners) != len(recalc):
                    diffs.append(
                        f"数量不同：包内 {len(stored_winners)}，重算 {len(recalc)}"
                    )
                fail("按公开规则由种子与名单重算的结果与包内结果不一致：\n    "
                     + "\n    ".join(diffs[:20]))
            else:
                ok(f"按规则独立重算 {prize_count} 个中奖编号，与包内结果完全一致")
        print("-" * 68)
        print("独立重算的中奖编号（按产生顺序）：")
        for i, w in enumerate(recalc, 1):
            print(f"  {i:>3}. {w}")

        # ---- 弃权/递补与发放确认：从种子与名单独立重演 ----
        # 旧版包无弃权字段：按无弃权、无确认处理
        if "forfeits" not in pkg and "currentWinners" not in pkg:
            ok("旧版核验包：无弃权字段，按无弃权处理")
        else:
            _verify_post_reveal(
                pkg, entries, seed, prize_count, recalc, ok, fail
            )
    elif prize_count is not None and prize_count > len(entries):
        fail(f"人数 {len(entries)} 少于名额 {prize_count}，按规则本不该封存/揭晓")

    return errors, checks


def _verify_post_reveal(pkg, entries, seed, prize_count, recalc, ok, fail):
    """揭晓后的全部追加记录（弃权/递补 + 奖品发放确认）统一重演。

    两条记录流必须在同一条事件时间线上自洽：

    - 第 j 条弃权（j 从 0 起）发生在“阶段” j 与 j+1 之间，其递补必须是扩展
      抽签序列第 prize_count+j 个编号（无候补则为 null），弃权编号当时必须
      正在有效席位中，且该席位不得已做发放确认（确认后的弃权不可能发生）；
    - 发放确认带 forfeitsBefore 字段：它发生在阶段 forfeitsBefore 之后、
      下一次弃权之前；确认的编号必须正是该阶段该席位的持有者（伪造编号、
      错置的确认阶段都会对不上）；同一席位至多一条确认。

    最终当前席位必须与包内 currentWinners 完全一致。
    """
    forfeits = pkg.get("forfeits")
    current = pkg.get("currentWinners")
    if not isinstance(forfeits, list) or not all(
        isinstance(f, dict) for f in forfeits
    ):
        fail("forfeits 字段必须是对象数组")
        return
    if [f.get("seq") for f in forfeits] != list(range(len(forfeits))):
        fail("弃权记录 seq 不连续或缺失（应为 0,1,2,… 连续编号）")

    # 扩展序列：名额 + 弃权次数，受名单人数限制（超出部分无候补，席位留空）
    total = prize_count + len(forfeits)
    extended = draw_winners(entries, seed, min(total, len(entries)))

    # ---- 发放确认的结构校验与按阶段分组（缺失字段视为无确认，兼容旧包）----
    confirmations = pkg.get("confirmations", [])
    has_confirm_field = "confirmations" in pkg
    by_phase = {}
    confirm_valid = True
    if has_confirm_field:
        if not isinstance(confirmations, list) or not all(
            isinstance(c, dict) for c in confirmations
        ):
            fail("confirmations 字段必须是对象数组")
            confirm_valid = False
        else:
            seen_slots = set()
            for c in confirmations:
                slot = c.get("slot")
                fb = c.get("forfeitsBefore")
                c_entry = c.get("entry")
                if not isinstance(slot, int) or isinstance(slot, bool) \
                        or not 0 <= slot < prize_count:
                    fail(
                        f"发放确认席位非法：{slot!r}（应为 0 至 "
                        f"{prize_count - 1} 的整数）"
                    )
                    confirm_valid = False
                    continue
                if slot in seen_slots:
                    fail(
                        f"席位 {slot + 1} 出现多条发放确认"
                        "（每席位至多确认一次）"
                    )
                    confirm_valid = False
                    continue
                seen_slots.add(slot)
                if not isinstance(fb, int) or isinstance(fb, bool) \
                        or not 0 <= fb <= len(forfeits):
                    fail(
                        f"席位 {slot + 1} 的发放确认 forfeitsBefore 非法："
                        f"{fb!r}（应为 0 至弃权总次数 {len(forfeits)} 的整数，"
                        "确认阶段被错置）"
                    )
                    confirm_valid = False
                    continue
                if not isinstance(c_entry, str) or not c_entry:
                    fail(f"席位 {slot + 1} 的发放确认缺少 entry（确认编号）")
                    confirm_valid = False
                    continue
                by_phase.setdefault(fb, []).append(c)

    # ---- 逐阶段重演：先安置本阶段的确认，再重演本阶段的弃权 ----
    slots = list(recalc)
    locked = {}  # slot -> 已确认的编号；该席位之后的弃权必须失败（即记录非法）
    for j, f in enumerate(forfeits):
        # 阶段 j（第 j 条弃权之前）完成的确认
        for c in by_phase.get(j, []):
            slot, c_entry = c["slot"], c["entry"]
            holder = slots[slot]
            if holder is None:
                fail(
                    f"席位 {slot + 1} 的发放确认（{c_entry!r}）无法重演："
                    "该阶段此席位为空席（空席不能确认，记录系伪造）"
                )
                confirm_valid = False
                continue
            if c_entry != holder:
                fail(
                    f"席位 {slot + 1} 的发放确认编号为 {c_entry!r}，但确认阶段"
                    f"（第 {j} 次弃权之前）该席位的持有者是 {holder!r}"
                    "（伪造编号或错置的确认阶段）"
                )
                confirm_valid = False
                continue
            locked[slot] = c_entry

        entry = f.get("entry")
        expect = (
            extended[prize_count + j]
            if prize_count + j < len(entries)
            else None
        )
        if entry not in slots:
            fail(
                f"第 {j + 1} 条弃权记录无法重演：编号 {entry!r} 当时不在有效"
                "席位中（弃权顺序被篡改或记录系伪造）"
            )
            return
        fslot = slots.index(entry)
        if fslot in locked:
            fail(
                f"第 {j + 1} 条弃权（席位 {fslot + 1}，编号 {entry!r}）"
                f"发生在该席位奖品发放确认（{locked[fslot]!r}）之后："
                "已确认席位不能再弃权（确认后的弃权系伪造）"
            )
            return
        if f.get("replacement") != expect:
            fail(
                f"第 {j + 1} 条弃权（{entry!r}）的递补不符：包内为 "
                f"{f.get('replacement')!r}，按规则应为 {expect!r}"
                "（伪造或跳过的递补）"
            )
            return
        slots[fslot] = expect

    # 最后一个阶段（全部弃权之后）完成的确认
    for c in by_phase.get(len(forfeits), []):
        slot, c_entry = c["slot"], c["entry"]
        holder = slots[slot]
        if holder is None:
            fail(
                f"席位 {slot + 1} 的发放确认（{c_entry!r}）无法重演："
                "该席位当前为空席（空席不能确认，记录系伪造）"
            )
            confirm_valid = False
            continue
        if c_entry != holder:
            fail(
                f"席位 {slot + 1} 的发放确认编号为 {c_entry!r}，但全部弃权"
                f"结束后该席位的持有者是 {holder!r}"
                "（伪造编号或错置的确认阶段）"
            )
            confirm_valid = False
            continue
        locked[slot] = c_entry

    if forfeits:
        ok(f"{len(forfeits)} 条弃权/递补记录按规则从种子与名单重演，全部一致")
    else:
        ok("无弃权记录")

    if has_confirm_field:
        if confirmations and confirm_valid:
            ok(
                f"{len(confirmations)} 条奖品发放确认按弃权顺序重演确认当时的"
                "席位持有者，全部一致"
            )
        elif not confirmations:
            ok("无奖品发放确认记录")
    else:
        ok("旧版核验包：无发放确认字段，按无确认处理")

    if current is None:
        fail("核验包缺少 currentWinners（当前有效席位）字段")
    elif not isinstance(current, list) or len(current) != prize_count:
        fail("currentWinners 必须是与名额等长的数组")
    elif current != slots:
        diffs = []
        for i, (a, b) in enumerate(zip(current, slots)):
            if a != b:
                diffs.append(f"席位 {i + 1}：包内 {a!r}，重演 {b!r}")
        fail("当前有效席位与重演结果不一致：\n    " + "\n    ".join(diffs[:20]))
    else:
        ok("当前有效席位（currentWinners）与弃权/确认重演结果一致")

    # 已确认席位的当前持有者必须仍是确认时的编号（额外显式比对）
    for slot, c_entry in locked.items():
        if current is not None and 0 <= slot < len(current) \
                and current[slot] != c_entry:
            fail(
                f"席位 {slot + 1} 已确认发放给 {c_entry!r}，但当前席位为 "
                f"{current[slot]!r}（确认后席位不应再变化）"
            )

    print("-" * 68)
    if forfeits:
        print("弃权与递补记录（按操作顺序，已经规则重演核对）：")
        for f in forfeits:
            rep = f.get("replacement")
            print(
                f"  #{str(f.get('seq', '?')):>3} 弃权 {f.get('entry')} → "
                f"递补 {rep if rep is not None else '（无候补，席位留空）'}"
            )
    if has_confirm_field and confirmations:
        print("奖品发放确认（已经弃权顺序重演确认当时的席位持有者）：")
        valid_dicts = [c for c in confirmations if isinstance(c, dict)]
        for c in sorted(
            valid_dicts,
            key=lambda x: x.get("slot") if isinstance(x.get("slot"), int) else -1,
        ):
            slot_no = c.get("slot") + 1 if isinstance(c.get("slot"), int) else "?"
            print(
                f"  席位 {slot_no}  编号 {c.get('entry')}"
                f"（确认前弃权 {c.get('forfeitsBefore', '?')} 次）"
            )
    print("当前有效席位：")
    for i, s in enumerate(slots, 1):
        mark = "  ✅ 已确认发放" if (i - 1) in locked else ""
        print(f"  {i:>3}. {s if s is not None else '（空席）'}{mark}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="现场抽签核验包独立校验")
    parser.add_argument("package", help="package.json 或导出的 .zip 核验包")
    parser.add_argument("--commitment", help="事先留存的承诺（SHA-256 hex）")
    parser.add_argument("--list-digest", help="事先留存的名单摘要（SHA-256 hex）")
    args = parser.parse_args(argv)

    try:
        pkg = load_package(args.package)
    except Exception as e:  # noqa: BLE001
        print(f"无法读取核验包：{e}")
        return 2

    errors, checks = verify(pkg, args.commitment, args.list_digest)

    print("-" * 68)
    for c in checks:
        print(f"  [通过] {c}")
    if errors:
        print()
        for e in errors:
            for line in str(e).splitlines():
                print(f"  [不符] {line}")
        print()
        print(f"结论：发现 {len(errors)} 处不符，核验未通过。")
        return 1
    print()
    print("结论：全部核验通过。中奖结果由公开规则、种子与封存名单确定性地推出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
