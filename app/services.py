"""名单与封存/揭晓/弃权/奖品发放确认的写流程（状态检查、名单读写必须在同一个
IMMEDIATE 事务内）。

每个 ``*_tx`` 函数只负责在调用方给定的事务（``db.transaction(conn)``）内完成
“重新读取轮次状态 → 读名单 → 校验 → 写数据”，自身不提交、不回滚：

- HTTP 路由在请求事务中提交，请求间由 IMMEDIATE 写锁串行；
- 确定性交错测试可以用两个真实 SQLite 连接精确安排两边的加锁/提交先后。

弃权与递补（仅 revealed 状态）：见 RULES.md 第 4 节。第 k 次弃权（k 从 0 起，
按操作顺序）的递补编号是按既有 sha256-argmax-v1 规则“接着抽”第 prize_count+k
轮的结果，即扩展抽签序列的第 prize_count+k 个编号；无候补时席位留空。
弃权记录与递补在同一事务、同一行内一次写入，不会只记弃权不记递补。
**已发放确认的席位不可再弃权**（见 RULES.md 第 5 节）。

奖品发放确认（仅 revealed 状态）：见 RULES.md 第 5 节。只能对当前有人占用的
席位、且以该席位当前有效编号确认；记录席位、当时编号与确认前的弃权次数。
同一席位同一编号重复提交只返回既有记录（幂等）；空席、旧编号、未揭晓一律拒绝。
确认与同席位弃权并发时由写锁按提交顺序串行生效：
弃权先完成 → 确认在锁内读到新持有者，旧编号确认失败，递补者仍可被确认；
确认先完成 → 弃权在锁内读到确认记录而失败。确认与检查同一事务，不留半次操作。
"""
import json
import secrets

from . import rules
from .db import now_iso


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


MAX_PRIZE = 10000


def get_round(conn, round_id: str):
    return conn.execute(
        "SELECT * FROM rounds WHERE id=?", (round_id,)
    ).fetchone()


def participant_rows(conn, round_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_id, position FROM participants WHERE round_id=? "
        "ORDER BY position, entry_id",
        (round_id,),
    ).fetchall()
    return [{"id": r["entry_id"], "position": r["position"]} for r in rows]


def create_round_tx(conn, title: str, prize_count, commitment: str):
    """创建轮次。调用方已做基础类型校验，这里保留事务内的最终校验。"""
    title = (title or "").strip()
    commitment = (commitment or "").strip().lower()
    if not title:
        raise ApiError("请填写轮次名称")
    if not isinstance(prize_count, int) or isinstance(prize_count, bool):
        raise ApiError("中奖名额必须是整数")
    if not 1 <= prize_count <= MAX_PRIZE:
        raise ApiError(f"中奖名额必须在 1 到 {MAX_PRIZE} 之间")
    if not rules.is_hex64(commitment):
        raise ApiError("承诺必须是 64 位十六进制 SHA-256 摘要")

    round_id = secrets.token_hex(6)
    conn.execute(
        "INSERT INTO rounds (id,title,prize_count,commit_hex,created_at) "
        "VALUES (?,?,?,?,?)",
        (round_id, title, prize_count, commitment, now_iso()),
    )
    return get_round(conn, round_id)


def _require_open_tx(conn, round_id: str):
    """事务内重新读取轮次并要求 open；封存/揭晓后的修改一律 409。"""
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] != "open":
        raise ApiError("名单已封存，此后拒绝任何修改", 409)
    return row


def replace_participants_tx(conn, round_id: str, raw) -> tuple[list[dict], int]:
    """整体替换名单：全部校验与删除/插入在同一事务内。"""
    _require_open_tx(conn, round_id)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ApiError("participants 必须是字符串数组")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        entry = rules.normalize_entry(item)
        if not entry:
            continue
        if entry in seen:
            raise ApiError(f"编号重复：{entry}")
        if "\n" in entry or "\r" in entry:
            raise ApiError(f"编号不能包含换行符：{entry}")
        if len(entry) > 200:
            raise ApiError(f"编号过长（>200 字符）：{entry}")
        seen.add(entry)
        cleaned.append(entry)

    conn.execute("DELETE FROM participants WHERE round_id=?", (round_id,))
    conn.executemany(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        [(round_id, e, i, now_iso()) for i, e in enumerate(cleaned)],
    )
    return participant_rows(conn, round_id), len(cleaned)


def add_participant_tx(conn, round_id: str, raw_id) -> list[dict]:
    """新增编号：状态检查、查重、插入在同一事务内。"""
    _require_open_tx(conn, round_id)
    entry = rules.normalize_entry(raw_id or "")
    if not entry:
        raise ApiError("请输入编号")
    if "\n" in entry or "\r" in entry or len(entry) > 200:
        raise ApiError("编号不合法")

    exists = conn.execute(
        "SELECT 1 FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry),
    ).fetchone()
    if exists:
        raise ApiError(f"编号已存在：{entry}", 409)
    pos = conn.execute(
        "SELECT COALESCE(MAX(position)+1,0) FROM participants WHERE round_id=?",
        (round_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        (round_id, entry, pos, now_iso()),
    )
    return participant_rows(conn, round_id)


def delete_participant_tx(conn, round_id: str, entry_id: str) -> list[dict]:
    """删除编号：状态检查与删除在同一事务内。"""
    _require_open_tx(conn, round_id)
    conn.execute(
        "DELETE FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry_id),
    )
    return participant_rows(conn, round_id)


def seal_round_tx(conn, round_id: str):
    """封存名单。

    在写锁事务内重新读取状态与名单：人数按“变更提交后的最新名单”复核，
    规范快照、摘要、人数在同一事务一次写入；重复封存直接返回既有快照，
    不重算、不覆盖。
    返回 (row, already_sealed)。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] == "revealed":
        raise ApiError("轮次已揭晓，无法封存", 409)
    if row["status"] == "sealed":
        return row, True

    # 关键：拿到写锁后再读名单，确保封存包含此前已提交的所有名单变更。
    entries = [p["id"] for p in participant_rows(conn, round_id)]
    if len(entries) < row["prize_count"]:
        raise ApiError(
            f"人数不足：当前 {len(entries)} 人，中奖名额 {row['prize_count']} 个，"
            "封存要求人数不少于名额"
        )

    snapshot = rules.canonical_snapshot(entries)
    digest = rules.sha256_hex(snapshot.encode("utf-8"))
    conn.execute(
        "UPDATE rounds SET status='sealed', snapshot=?, list_digest_hex=?, "
        "participant_count=?, sealed_at=? WHERE id=?",
        (snapshot, digest, len(entries), now_iso(), round_id),
    )
    return get_round(conn, round_id), False


def reveal_round_tx(conn, round_id: str, raw_seed: str):
    """揭晓：状态检查、承诺核对与结果写入在同一事务内。

    返回 dict，由路由组装响应；承诺不符时轮次保持 sealed，仅累加失败计数。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] == "revealed":
        return {"already_revealed": True, "row": row}

    if row["status"] != "sealed":
        raise ApiError("轮次尚未封存", 409)

    seed = (raw_seed or "").strip().lower()
    if not rules.seed_matches_commit(seed, row["commit_hex"]):
        conn.execute(
            "UPDATE rounds SET failed_attempts=failed_attempts+1 WHERE id=?",
            (round_id,),
        )
        return {"commit_mismatch": True, "row": get_round(conn, round_id)}

    entries = row["snapshot"].split("\n") if row["snapshot"] else []
    winners = rules.draw_winners(entries, seed, row["prize_count"])
    conn.execute(
        "UPDATE rounds SET status='revealed', seed_hex=?, winners_json=?, "
        "revealed_at=? WHERE id=?",
        (seed, json.dumps(winners, ensure_ascii=False), now_iso(), round_id),
    )
    return {"revealed": True, "winners": winners,
            "row": get_round(conn, round_id)}


# ---------------------------------------------------------------- 弃权与递补

def forfeit_rows(conn, round_id: str) -> list[dict]:
    """按操作顺序返回一轮次的全部弃权记录。"""
    rows = conn.execute(
        "SELECT seq, entry_id, replacement, created_at FROM forfeits "
        "WHERE round_id=? ORDER BY seq",
        (round_id,),
    ).fetchall()
    return [
        {
            "seq": r["seq"],
            "entry": r["entry_id"],
            "replacement": r["replacement"],
            "at": r["created_at"],
        }
        for r in rows
    ]


def confirmation_rows(conn, round_id: str) -> list[dict]:
    """返回一轮次的全部奖品发放确认记录（按席位序号排序，便于展示与导出）。"""
    rows = conn.execute(
        "SELECT slot, entry_id, forfeits_before, created_at FROM confirmations "
        "WHERE round_id=? ORDER BY slot",
        (round_id,),
    ).fetchall()
    return [
        {
            "slot": r["slot"],
            "entry": r["entry_id"],
            "forfeitsBefore": r["forfeits_before"],
            "at": r["created_at"],
        }
        for r in rows
    ]


def current_slots(winners: list[str], forfeits: list[dict]) -> list:
    """由原始中奖名单与弃权记录逐次原位替换，推出当前有效席位。

    返回与 ``winners`` 等长的列表，元素为当前有效编号或 None（空席）。
    纯函数：网页与核验器按同一规则独立重演（RULES.md 第 4 节）。
    """
    slots = list(winners)
    for f in forfeits:
        idx = slots.index(f["entry"])  # forfeit_tx 保证 entry 当时在席位中
        slots[idx] = f["replacement"]
    return slots


def forfeit_tx(conn, round_id: str, raw_entry):
    """把当前有效中奖编号标记弃权，并按既有规则立即递补（同一事务内）。

    返回 dict，由路由组装响应：
    - already_forfeited：该编号此前已登记弃权，不重复递补，返回既有结果（幂等）；
    - 否则追加一条 forfeits 记录，递补为扩展抽签序列的下一个编号，
      名单已无人可补时 replacement=None（席位留空）。
    原始中奖名单（winners_json）与揭晓时间（revealed_at）始终不变。

    已做过奖品发放确认的席位不能再弃权：在写锁内重读确认记录后拒绝，
    因此与同席位确认并发时，只要确认先提交，弃权必然失败（不留半次操作）。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] != "revealed":
        raise ApiError("轮次尚未揭晓，不能标记弃权", 409)

    entry = rules.normalize_entry(raw_entry if isinstance(raw_entry, str) else "")
    if not entry:
        raise ApiError("请指定要弃权的编号")

    winners = json.loads(row["winners_json"])
    forfeits = forfeit_rows(conn, round_id)

    # 幂等：同一编号重复提交弃权，只返回既有结果
    if any(f["entry"] == entry for f in forfeits):
        return {"already_forfeited": True, "row": row}

    slots = current_slots(winners, forfeits)
    if entry not in slots:
        raise ApiError(f"编号 {entry} 不是当前有效中奖编号，不能弃权", 409)

    slot_index = slots.index(entry)
    confirmed = {c["slot"]: c for c in confirmation_rows(conn, round_id)}
    if slot_index in confirmed:
        raise ApiError(
            f"席位 {slot_index + 1}（编号 {confirmed[slot_index]['entry']}）"
            "已登记奖品发放确认，该席位不能再弃权",
            409,
        )

    # 递补：按既有规则“接着抽”——第 seq 次弃权对应扩展序列第 prize_count+seq 个
    # （候选池为封存名单减去此前全部已出现编号，因此递补绝不重复）。
    seq = len(forfeits)
    entries = row["snapshot"].split("\n") if row["snapshot"] else []
    draw_index = row["prize_count"] + seq
    replacement = None
    if draw_index < len(entries):
        replacement = rules.draw_winners(
            entries, row["seed_hex"], draw_index + 1
        )[draw_index]

    conn.execute(
        "INSERT INTO forfeits (round_id,seq,entry_id,replacement,created_at) "
        "VALUES (?,?,?,?,?)",
        (round_id, seq, entry, replacement, now_iso()),
    )
    return {
        "forfeited": True,
        "forfeit": {"seq": seq, "entry": entry, "replacement": replacement},
        "row": get_round(conn, round_id),
    }


# ------------------------------------------------------------ 奖品发放确认

def confirm_tx(conn, round_id: str, raw_slot, raw_entry):
    """对当前有人占用的席位登记奖品发放确认（同一事务内完成检查与写入）。

    记录三件事：席位 slot、确认时该席位的当前有效编号 entry、确认前已完成的
    弃权次数 forfeits_before（核验重演据此把确认放到对应“阶段”，RULES.md
    第 5 节）。

    返回 dict，由路由组装响应：
    - already_confirmed：同一席位同一编号重复提交，返回既有记录（幂等）；
    - 否则插入一条 confirmations 记录（每席位至多一条，主键约束兜底）。

    拒绝：未揭晓（409）、席位越界/格式错（400）、空席（409）、旧编号即
    编号不是该席位当前有效持有者（409）；同席位已确认别的编号也按旧编号拒绝。
    与同席位弃权并发时，写锁保证按提交顺序生效：弃权先提交则递补者成为新
    持有者，旧编号确认失败；确认先提交则弃权失败。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] != "revealed":
        raise ApiError("轮次尚未揭晓，不能登记奖品发放确认", 409)

    if isinstance(raw_slot, bool) or not isinstance(raw_slot, int):
        raise ApiError("席位序号必须是整数（0 起）")
    slot = raw_slot

    entry = rules.normalize_entry(raw_entry if isinstance(raw_entry, str) else "")
    if not entry:
        raise ApiError("请指定要确认发放的编号")

    winners = json.loads(row["winners_json"])
    if not 0 <= slot < len(winners):
        raise ApiError(
            f"席位序号越界：共 {len(winners)} 个席位（0 至 {len(winners) - 1}）"
        )

    forfeits = forfeit_rows(conn, round_id)
    existing = {
        c["slot"]: c for c in confirmation_rows(conn, round_id)
    }

    # 幂等：同一席位同一编号重复提交，原样返回既有记录
    if slot in existing:
        if existing[slot]["entry"] == entry:
            return {"already_confirmed": True, "row": row}
        raise ApiError(
            f"席位 {slot + 1} 已确认发放给编号 {existing[slot]['entry']}，"
            f"编号 {entry} 是旧编号，不能再确认",
            409,
        )

    slots = current_slots(winners, forfeits)
    current = slots[slot]
    if current is None:
        raise ApiError(f"席位 {slot + 1} 当前为空席，不能登记发放确认", 409)
    if current != entry:
        raise ApiError(
            f"编号 {entry} 不是席位 {slot + 1} 的当前有效编号"
            f"（当前为 {current}），旧编号不能确认发放",
            409,
        )

    forfeits_before = len(forfeits)
    conn.execute(
        "INSERT INTO confirmations "
        "(round_id,slot,entry_id,forfeits_before,created_at) "
        "VALUES (?,?,?,?,?)",
        (round_id, slot, entry, forfeits_before, now_iso()),
    )
    return {
        "confirmed": True,
        "confirmation": {
            "slot": slot,
            "entry": entry,
            "forfeitsBefore": forfeits_before,
        },
        "row": get_round(conn, round_id),
    }
