"""脱敏规则、强度策略与服务端掩码实现。

所有掩码都在服务端完成；前端只负责展示脱敏前后的对照样例。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class DomainError(Exception):
    """可直接转成 HTTP 响应的业务错误。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# 强度一旦选定即写入当天策略，当天不许更换。
STRENGTHS: dict[str, dict[str, str]] = {
    "full": {
        "key": "full",
        "name": "全掩码",
        "desc": "手机与证件一律掩码后才允许落库（所有管道）",
    },
    "shared": {
        "key": "shared",
        "name": "按共享分流",
        "desc": "仅外部共享管道强制掩码；内部管道可明文落库，但必须记审计",
    },
}

_STRENGTH_ALIASES = {
    "full": "full",
    "full_mask": "full",
    "full-mask": "full",
    "all": "full",
    "all_mask": "full",
    "all_masked": "full",
    "strict": "full",
    "mask_all": "full",
    "全掩码": "full",
    "全掩": "full",
    "严格": "full",
    "强制掩码": "full",
    "shared": "shared",
    "shared_only": "shared",
    "shared-only": "shared",
    "shared_split": "shared",
    "shared-split": "shared",
    "split": "shared",
    "分流": "shared",
    "按共享分流": "shared",
    "共享分流": "shared",
    "按共享": "shared",
    "仅外部": "shared",
}

MASK_TYPES = {
    "phone": "手机号",
    "id_card": "证件号",
    "email": "邮箱",
    "name": "姓名",
    "generic": "通用",
}

_FIELD_TO_TYPE = {
    "手机": "phone",
    "手机号": "phone",
    "电话": "phone",
    "证件号": "id_card",
    "身份证": "id_card",
    "身份证号": "id_card",
    "证件": "id_card",
    "邮箱": "email",
    "电子邮箱": "email",
    "邮箱地址": "email",
    "姓名": "name",
    "名字": "name",
}


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_strength(raw: Any) -> str | None:
    """把中文名、英文键或常见写法归一到 full / shared。"""
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return None
    return _STRENGTH_ALIASES.get(key) or _STRENGTH_ALIASES.get(key.lower())


def strength_meta(key: str) -> dict[str, str]:
    return STRENGTHS.get(key, {"key": key, "name": key, "desc": ""})


def normalize_mask_type(raw: Any, field: Any = "") -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "手机": "phone",
        "手机号": "phone",
        "phone": "phone",
        "tel": "phone",
        "mobile": "phone",
        "证件": "id_card",
        "证件号": "id_card",
        "身份证": "id_card",
        "身份证号": "id_card",
        "id": "id_card",
        "id_card": "id_card",
        "idcard": "id_card",
        "邮箱": "email",
        "email": "email",
        "mail": "email",
        "姓名": "name",
        "name": "name",
        "通用": "generic",
        "generic": "generic",
    }
    if value in aliases:
        return aliases[value]
    field_key = str(field or "").strip()
    if field_key in _FIELD_TO_TYPE:
        return _FIELD_TO_TYPE[field_key]
    if field_key.lower() in aliases:
        return aliases[field_key.lower()]
    return "generic"


# 演示用源记录：真实系统里来自抽取批次，这里按管道固定生成，保证前后对照稳定。
_NAMES = ["张伟", "李娜", "王强", "赵敏", "刘洋", "陈静", "周杰", "吴倩"]
_PHONES = [
    "13812345678",
    "13987654321",
    "13700138000",
    "15011223344",
    "18655667788",
    "13566778899",
    "18812340000",
    "15233445566",
]
_ID_CARDS = [
    "110101199001011234",
    "310101198802024567",
    "440301199505056789",
    "120101200003078888",
    "510101199309091111",
    "330101198707072222",
    "420101199811113333",
    "370101200104044444",
]
_EMAILS = [
    "zhangwei@example.com",
    "lina@example.org",
    "wangqiang@example.net",
    "zhaomin@example.cn",
    "liuyang@example.io",
    "chenjing@example.com",
    "zhoujie@example.org",
    "wuqian@example.net",
]


def mask_value(mask_type: str, value: str, fill: str = "*") -> str:
    """按字段类型掩码；手机与证件保留必要尾号，中间一律掩掉。"""
    text = str(value)
    if not text:
        return text
    fill = fill or "*"
    if mask_type == "phone":
        digits = text
        if len(digits) >= 7:
            return digits[:3] + fill * 4 + digits[-4:]
        return fill * len(digits)
    if mask_type == "id_card":
        if len(text) >= 10:
            return text[:6] + fill * (len(text) - 10) + text[-4:]
        return fill * len(text)
    if mask_type == "email":
        if "@" in text:
            local, domain = text.split("@", 1)
            head = local[:1] if local else ""
            return head + fill * 3 + "@" + domain
        return fill * len(text)
    if mask_type == "name":
        return (text[:1] + fill) if len(text) > 1 else fill
    if len(text) <= 2:
        return fill * len(text)
    return text[:1] + fill * (len(text) - 2) + text[-1:]


def build_sample_rows(pipeline_id: int, rules: list[dict]) -> list[dict]:
    """生成至少三条脱敏前后对照样例（服务端完成掩码）。"""
    active: list[dict] = []
    seen_fields: set[str] = set()
    for rule in rules:
        field = str(rule.get("field") or "").strip()
        if not field or field in seen_fields:
            continue
        seen_fields.add(field)
        active.append(rule)
    if not active:
        active = [
            {"field": "手机号", "mask_type": "phone", "pattern": ""},
            {"field": "证件号", "mask_type": "id_card", "pattern": ""},
            {"field": "邮箱", "mask_type": "email", "pattern": ""},
        ]

    rows: list[dict] = []
    base = max(int(pipeline_id or 1) - 1, 0) * 2
    for seq in range(5):
        idx = (base + seq) % len(_NAMES)
        source = {
            "姓名": _NAMES[idx],
            "手机号": _PHONES[idx],
            "证件号": _ID_CARDS[idx],
            "邮箱": _EMAILS[idx],
        }
        for rule in active:
            field = str(rule.get("field") or "").strip()
            mask_type = normalize_mask_type(rule.get("mask_type"), field)
            before = source.get(field)
            if before is None:
                # 规则册里自定义字段也给出可读的演示值
                before = f"{field}-{pipeline_id}-{seq + 1:03d}"
                mask_type = normalize_mask_type(rule.get("mask_type"), field)
                if mask_type == "generic":
                    before = f"{field}-{pipeline_id}-{seq + 1:03d}"
            after = mask_value(mask_type, str(before), str(rule.get("pattern") or "*")[:1] or "*")
            rows.append(
                {
                    "row_no": seq + 1,
                    "field": field,
                    "mask_type": mask_type,
                    "before": str(before),
                    "after": after,
                    # 兼容常见字段名，前端与外部校验都能读到前后值
                    "original": str(before),
                    "masked_value": after,
                    "changed": int(str(before) != after),
                    "masked": int(str(before) != after),
                }
            )
    return rows
