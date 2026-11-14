import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.masking import (
    DomainError,
    build_sample_rows,
    mask_value,
    normalize_mask_type,
    normalize_strength,
    strength_meta,
    today,
)

# 共享范围：external 对外共享（强制掩码），internal 内部使用（按策略可明文但须审计）
SEED_PIPELINES = [
    ("订单明细日抽", "orders_daily", "待抽取", "mock://orders/day", "external"),
    ("用户画像宽表", "user_profile", "待抽取", "mock://users/profile", "external"),
    ("库存快照落地", "inventory_snap", "待抽取", "mock://stock/snap", "internal"),
    ("支付流水清洗", "pay_clean", "待抽取", "mock://pay/flow", "internal"),
]

SEED_RULES = [
    ("手机号掩码", "手机号", "phone", "*"),
    ("证件号掩码", "证件号", "id_card", "*"),
    ("邮箱掩码", "邮箱", "email", "*"),
]

PIPELINE_STATUS_OPEN_EXTRACT = "抽取中"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_pipeline_columns(conn: sqlite3.Connection) -> None:
    """旧库补列：已有值不动。"""
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(pipelines)").fetchall()
    }
    additions = {
        "share_scope": "TEXT NOT NULL DEFAULT 'internal'",
        "mask_status": "TEXT NOT NULL DEFAULT '未就绪'",
        "mask_strength": "TEXT NOT NULL DEFAULT ''",
        "masked_at": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE pipelines ADD COLUMN {name} {ddl}")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              loaded INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mask_rules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              field TEXT NOT NULL,
              mask_type TEXT NOT NULL DEFAULT 'generic',
              pattern TEXT NOT NULL DEFAULT '*',
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS mask_policies (
              day TEXT PRIMARY KEY,
              strength TEXT NOT NULL,
              strength_name TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS mask_claims (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              claimer TEXT NOT NULL,
              day TEXT NOT NULL,
              status TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS mask_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              day TEXT NOT NULL,
              action TEXT NOT NULL,
              field TEXT NOT NULL DEFAULT '',
              mask_type TEXT NOT NULL DEFAULT '',
              before_value TEXT NOT NULL DEFAULT '',
              after_value TEXT NOT NULL DEFAULT '',
              strength TEXT NOT NULL DEFAULT '',
              reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        _ensure_pipeline_columns(conn)
        # 只允许同一管道同一天有一笔成功的脱敏提交。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mask_claims_won "
            "ON mask_claims (pipeline_id, day) WHERE status = 'won'"
        )
        n = conn.execute("SELECT COUNT(*) AS c FROM pipelines").fetchone()["c"]
        if n == 0:
            conn.executemany(
                "INSERT INTO pipelines (title, code, status, source_uri, share_scope, mask_status) "
                "VALUES (?,?,?,?,?, '未就绪')",
                SEED_PIPELINES,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )
        else:
            # 旧库补上已知管道的共享范围；已有值不动。
            known = {code: scope for _, code, _, _, scope in SEED_PIPELINES}
            for code, scope in known.items():
                conn.execute(
                    "UPDATE pipelines SET share_scope = ? "
                    "WHERE code = ? AND (share_scope IS NULL OR share_scope = '')",
                    (scope, code),
                )
        rules = conn.execute("SELECT COUNT(*) AS c FROM mask_rules").fetchone()["c"]
        if rules == 0:
            conn.executemany(
                "INSERT INTO mask_rules (name, field, mask_type, pattern) VALUES (?,?,?,?)",
                SEED_RULES,
            )


def _pipeline_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["share_scope"] = item.get("share_scope") or "internal"
    item["share_scope_name"] = "对外共享" if item["share_scope"] == "external" else "内部使用"
    item["mask_status"] = item.get("mask_status") or "未就绪"
    item["strength_name"] = strength_meta(item.get("mask_strength") or "")["name"] if item.get("mask_strength") else ""
    return item


def list_pipelines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, created_at, "
        "share_scope, mask_status, mask_strength, masked_at "
        "FROM pipelines ORDER BY id"
    ).fetchall()
    return [_pipeline_row(r) for r in rows]


def get_pipeline(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, created_at, "
        "share_scope, mask_status, mask_strength, masked_at "
        "FROM pipelines WHERE id = ?",
        (pipeline_id,),
    ).fetchone()
    return _pipeline_row(row)


# ---------------------------------------------------------------- 规则册

def list_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, field, mask_type, pattern, created_at, updated_at "
        "FROM mask_rules ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def extraction_open(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM pipelines WHERE status = ?",
        (PIPELINE_STATUS_OPEN_EXTRACT,),
    ).fetchone()
    return row["c"] > 0


def _guard_rule_change(conn: sqlite3.Connection) -> None:
    if extraction_open(conn):
        raise DomainError("抽取流程尚未收束，暂不能修改脱敏规则", 409)


def _payload_name(payload: dict) -> str:
    for key in ("name", "rule_name", "title", "label"):
        value = payload.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _payload_field(payload: dict) -> str:
    for key in ("field", "column", "target", "field_name"):
        value = payload.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def save_rule(conn: sqlite3.Connection, payload: dict) -> dict:
    name = _payload_name(payload)
    field = _payload_field(payload)
    if not name:
        raise DomainError("规则名不能为空，禁止保存", 400)
    if not field:
        raise DomainError("规则字段不能为空", 400)
    _guard_rule_change(conn)
    mask_type = normalize_mask_type(payload.get("mask_type") or payload.get("type"), field)
    pattern = str(payload.get("pattern") or "*")[:1] or "*"
    row = conn.execute(
        "INSERT INTO mask_rules (name, field, mask_type, pattern) VALUES (?,?,?,?) "
        "RETURNING id",
        (name, field, mask_type, pattern),
    ).fetchone()
    rule_id = row["id"]
    return get_rule(conn, rule_id)


def get_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, field, mask_type, pattern, created_at, updated_at "
        "FROM mask_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    return dict(row) if row else None


def update_rule(conn: sqlite3.Connection, rule_id: int, payload: dict) -> dict | None:
    current = get_rule(conn, rule_id)
    if not current:
        return None
    name_given = any(payload.get(k) is not None for k in ("name", "rule_name", "title", "label"))
    name = _payload_name(payload)
    field = _payload_field(payload)
    if (name_given and not name) or (not name_given and not current["name"]):
        raise DomainError("规则名不能为空，禁止保存", 400)
    if not name:
        name = current["name"]
    if not field:
        field = current["field"]
    if not field:
        raise DomainError("规则字段不能为空", 400)
    _guard_rule_change(conn)
    hint = field if not field.isascii() else ""
    mask_type = normalize_mask_type(
        payload.get("mask_type") or payload.get("type") or current["mask_type"], hint
    )
    pattern = str(payload.get("pattern", current["pattern"]) or "*")[:1] or "*"
    conn.execute(
        "UPDATE mask_rules SET name = ?, field = ?, mask_type = ?, pattern = ?, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (name, field, mask_type, pattern, rule_id),
    )
    return get_rule(conn, rule_id)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    current = get_rule(conn, rule_id)
    if not current:
        return False
    _guard_rule_change(conn)
    conn.execute("DELETE FROM mask_rules WHERE id = ?", (rule_id,))
    return True


# ---------------------------------------------------------------- 强度策略

def get_policy(conn: sqlite3.Connection, day: str | None = None) -> dict | None:
    day = day or today()
    row = conn.execute(
        "SELECT day, strength, strength_name, created_at, updated_at "
        "FROM mask_policies WHERE day = ?",
        (day,),
    ).fetchone()
    if not row:
        return None
    policy = dict(row)
    meta = strength_meta(policy["strength"])
    policy["desc"] = meta.get("desc", "")
    return policy


def set_policy(conn: sqlite3.Connection, raw_strength) -> dict:
    key = normalize_strength(raw_strength)
    if not key:
        raise DomainError("脱敏强度写歪了：无法识别提交的强度，可选：全掩码 / 按共享分流", 400)
    day = today()
    existing = get_policy(conn, day)
    if existing:
        if existing["strength"] == key:
            return existing
        raise DomainError(
            "脱敏强度写歪了：当天策略已锁定为「{}」，当天不允许更换".format(
                existing["strength_name"]
            ),
            409,
        )
    meta = strength_meta(key)
    conn.execute(
        "INSERT OR IGNORE INTO mask_policies (day, strength, strength_name) VALUES (?,?,?)",
        (day, key, meta["name"]),
    )
    created = get_policy(conn, day)
    if created and created["strength"] != key:
        raise DomainError(
            "脱敏强度写歪了：当天策略已锁定为「{}」，当天不允许更换".format(
                created["strength_name"]
            ),
            409,
        )
    return created


def _submit_strength(payload: dict):
    for key in ("strength", "level", "mask_strength", "mode", "policy", "intensity"):
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _check_strength(conn: sqlite3.Connection, submitted) -> dict | None:
    """校验提交强度与当天策略是否一致；写歪了就拦。"""
    policy = get_policy(conn)
    if not policy:
        if normalize_strength(submitted):
            # 提交里带了合法强度而当天还没选定：按该强度写入当天策略。
            return set_policy(conn, submitted)
        return None
    if submitted in (None, ""):
        return policy
    key = normalize_strength(submitted)
    if not key:
        raise DomainError("脱敏强度写歪了：无法识别提交的强度，当天策略为「{}」".format(policy["strength_name"]), 400)
    if key != policy["strength"]:
        raise DomainError(
            "脱敏强度写歪了：提交为「{}」，当天策略为「{}」（{}）".format(
                strength_meta(key)["name"], policy["strength_name"], policy["desc"]
            ),
            400,
        )
    return policy


# ---------------------------------------------------------------- 对照抽样 / 脱敏

def build_sample(conn: sqlite3.Connection, pipeline_id: int) -> list[dict]:
    rules = list_rules(conn)
    return build_sample_rows(pipeline_id, rules)


def _insert_audits(conn: sqlite3.Connection, pipeline_id: int, strength: str, rows: list[dict]) -> int:
    day = today()
    for row in rows:
        if not row.get("masked"):
            continue
        conn.execute(
            "INSERT INTO mask_audits (pipeline_id, day, action, field, mask_type, "
            "before_value, after_value, strength) VALUES (?,?,?,?,?,?,?,?)",
            (
                pipeline_id,
                day,
                "mask",
                row["field"],
                row["mask_type"],
                row["before"],
                row["after"],
                strength,
            ),
        )
    return len([r for r in rows if r.get("masked")])


def _record_lost(conn: sqlite3.Connection, pipeline_id: int, claimer: str, day: str, winner: str) -> str:
    note = "脱敏争抢：本轮已由 {} 提交成功，只成一笔".format(winner)
    conn.execute(
        "INSERT INTO mask_claims (pipeline_id, claimer, day, status, note) "
        "VALUES (?,?,?, 'lost', ?)",
        (pipeline_id, claimer, day, note),
    )
    # 争抢失败也要留痕，先落盘再抛错，避免随事务回滚。
    conn.commit()
    return note


def _claim_winner(conn: sqlite3.Connection, pipeline_id: int, claimer: str) -> tuple[bool, str]:
    """同一管道同一天只有一笔成功脱敏提交，其余争抢全部记败。"""
    day = today()
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    won = conn.execute(
        "SELECT claimer FROM mask_claims WHERE pipeline_id = ? AND day = ? AND status = 'won'",
        (pipeline_id, day),
    ).fetchone()
    if won:
        return False, _record_lost(conn, pipeline_id, claimer, day, won["claimer"])
    conn.execute(
        "INSERT INTO mask_claims (pipeline_id, claimer, day, status, note) "
        "VALUES (?,?,?, 'won', '')",
        (pipeline_id, claimer, day),
    )
    return True, ""


def submit_mask(conn: sqlite3.Connection, pipeline_id: int, claimer: str, payload: dict) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] == PIPELINE_STATUS_OPEN_EXTRACT:
        raise DomainError("抽取流程尚未收束，不能提交脱敏", 409)
    if item["status"] != "已抽取待加载":
        raise DomainError("当前状态不能提交脱敏，请先完成抽取", 409)
    policy = _check_strength(conn, _submit_strength(payload))
    if not policy:
        raise DomainError("尚未选定当天脱敏强度，不能提交脱敏", 409)
    if item["mask_status"] == "已掩码":
        winner = conn.execute(
            "SELECT claimer FROM mask_claims WHERE pipeline_id = ? AND day = ? AND status = 'won'",
            (pipeline_id, today()),
        ).fetchone()
        if winner:
            note = _record_lost(conn, pipeline_id, claimer, today(), winner["claimer"])
            raise DomainError(note, 409)
        raise DomainError("该管道本轮已掩码完成，拒绝重复提交", 409)
    won, note = _claim_winner(conn, pipeline_id, claimer)
    if not won:
        raise DomainError(note, 409)
    rows = build_sample(conn, pipeline_id)
    audit_count = _insert_audits(conn, pipeline_id, policy["strength"], rows)
    conn.execute(
        "UPDATE pipelines SET mask_status = '已掩码', mask_strength = ?, "
        "masked_at = datetime('now','localtime') WHERE id = ?",
        (policy["strength"], pipeline_id),
    )
    return {
        "pipeline": get_pipeline(conn, pipeline_id),
        "sample": rows,
        "audits": audit_count,
        "policy": policy,
        "claimer": claimer,
    }


def mask_detail(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    sample = build_sample(conn, pipeline_id)
    audits = conn.execute(
        "SELECT id, action, field, mask_type, before_value, after_value, strength, reason, created_at "
        "FROM mask_audits WHERE pipeline_id = ? ORDER BY id",
        (pipeline_id,),
    ).fetchall()
    claims = conn.execute(
        "SELECT id, claimer, day, status, note, created_at "
        "FROM mask_claims WHERE pipeline_id = ? ORDER BY id",
        (pipeline_id,),
    ).fetchall()
    return {
        "pipeline": item,
        "policy": get_policy(conn),
        "sample": sample,
        "audits": [dict(r) for r in audits],
        "claims": [dict(r) for r in claims],
    }


# ---------------------------------------------------------------- 总览

def overview(conn: sqlite3.Connection) -> dict:
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM pipelines "
        "WHERE status = '已抽取待加载' AND mask_status = '待脱敏'"
    ).fetchone()["c"]
    masked = conn.execute(
        "SELECT COUNT(*) AS c FROM pipelines WHERE mask_status = '已掩码'"
    ).fetchone()["c"]
    audits = conn.execute("SELECT COUNT(*) AS c FROM mask_audits").fetchone()["c"]
    policy = get_policy(conn)
    return {
        "pending": pending,
        "masked": masked,
        "audits": audits,
        "strength": policy["strength"] if policy else "",
        "strength_name": policy["strength_name"] if policy else "未选定",
        "strength_desc": policy["desc"] if policy else "当天尚未选定脱敏强度",
        "policy": policy,
        "day": today(),
        "extraction_open": extraction_open(conn),
        "rules": len(list_rules(conn)),
    }


# ---------------------------------------------------------------- 抽取 / 落库

def run_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    # 分步抽取未收束时，再点一次抽取即为收束。
    if item["status"] not in ("待抽取", "抽取失败", PIPELINE_STATUS_OPEN_EXTRACT):
        return item
    extracted = 12 + (pipeline_id % 5)
    conn.execute(
        "UPDATE pipelines SET status = ?, extracted = ?, loaded = 0, "
        "mask_status = '待脱敏' WHERE id = ?",
        ("已抽取待加载", extracted, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)


def start_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] in ("待抽取", "抽取失败"):
        conn.execute(
            "UPDATE pipelines SET status = ? WHERE id = ?",
            (PIPELINE_STATUS_OPEN_EXTRACT, pipeline_id),
        )
    return get_pipeline(conn, pipeline_id)


def finish_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] != PIPELINE_STATUS_OPEN_EXTRACT:
        return item
    return run_extract(conn, pipeline_id)


def run_load(conn: sqlite3.Connection, pipeline_id: int, payload: dict | None = None) -> dict | None:
    payload = payload or {}
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] != "已抽取待加载":
        return item

    policy = _check_strength(conn, _submit_strength(payload))
    if policy is None:
        if _submit_strength(payload):
            raise DomainError("尚未选定当天脱敏强度，禁止落库", 409)
        # 尚未启用脱敏策略的旧流程保持基线可用；一旦当天选定强度，闸门生效。
        conn.execute(
            "UPDATE pipelines SET status = ?, loaded = extracted, "
            "mask_status = CASE WHEN mask_status = '已掩码' THEN mask_status ELSE '明文落库' END "
            "WHERE id = ?",
            ("已落库", pipeline_id),
        )
        return get_pipeline(conn, pipeline_id)

    external = item["share_scope"] == "external"
    must_mask = policy["strength"] == "full" or external
    if must_mask:
        if item["mask_status"] != "已掩码":
            raise DomainError(
                "未脱敏完成，禁止落库：{}（当天强度「{}」）".format(
                    "对外共享管道" if external else "全掩码策略覆盖所有管道",
                    policy["strength_name"],
                ),
                409,
            )
        if item.get("mask_strength") and item["mask_strength"] != policy["strength"]:
            raise DomainError(
                "脱敏强度写歪了：管道记录的强度为「{}」，当天策略为「{}」".format(
                    strength_meta(item["mask_strength"])["name"], policy["strength_name"]
                ),
                400,
            )
    else:
        # 按共享分流下的内部管道：可明文落库，但必须留审计。
        if item["mask_status"] != "已掩码":
            conn.execute(
                "INSERT INTO mask_audits (pipeline_id, day, action, field, mask_type, "
                "before_value, after_value, strength, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    pipeline_id,
                    today(),
                    "plaintext_load",
                    "",
                    "",
                    "",
                    "",
                    policy["strength"],
                    "按共享分流：内部管道明文落库，记审计",
                ),
            )
            conn.execute(
                "UPDATE pipelines SET mask_status = '明文落库' WHERE id = ?",
                (pipeline_id,),
            )

    conn.execute(
        "UPDATE pipelines SET status = ?, loaded = extracted WHERE id = ?",
        ("已落库", pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)
