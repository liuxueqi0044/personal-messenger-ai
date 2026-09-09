"""Conservative deterministic topic and prohibited-output classification."""

from __future__ import annotations

import re

from .models import SensitiveCategory

_TOPIC_TERMS: dict[SensitiveCategory, tuple[str, ...]] = {
    SensitiveCategory.MONEY: (
        "转账",
        "红包",
        "借钱",
        "借款",
        "还钱",
        "付款",
        "代付",
        "充值",
        "价格",
        "礼物",
        "买给",
        "银行卡",
        "支付宝",
        "微信支付",
        "收款",
        "花钱",
    ),
    SensitiveCategory.CREDENTIALS: (
        "密码",
        "验证码",
        "登录码",
        "账号凭据",
        "密保",
        "二维码",
        "扫码",
    ),
    SensitiveCategory.PRIVACY: (
        "住址",
        "地址",
        "实时位置",
        "定位",
        "身份证",
        "证件",
        "家庭成员",
        "手机号",
        "家庭隐私",
        "学校",
        "大学",
        "班级",
        "单位",
    ),
    SensitiveCategory.OFFLINE_ACTION: (
        "见面",
        "出来约",
        "线下",
        "约会",
        "酒店",
        "航班",
        "车票",
        "旅行",
        "旅游",
        "寄给",
        "寄送",
        "取件",
        "快递",
        "来找我",
        "去找你",
    ),
    SensitiveCategory.LEGAL_MEDICAL: (
        "律师",
        "法律",
        "起诉",
        "合同",
        "报警",
        "医生",
        "诊断",
        "处方",
        "用药",
        "剂量",
        "心理咨询",
        "财务顾问",
    ),
    SensitiveCategory.CONFLICT_CRISIS: (
        "分手",
        "拉黑",
        "别联系",
        "不要联系",
        "停止联系",
        "骚扰",
        "威胁",
        "勒索",
        "跟踪",
        "暴力",
        "自杀",
        "自伤",
        "想死",
        "杀了",
        "失踪",
        "绝望",
    ),
    SensitiveCategory.SEXUAL_CONTENT: (
        "裸照",
        "私密照",
        "性行为",
        "上床",
        "成人视频",
        "露骨",
        "开房",
    ),
    SensitiveCategory.MINOR: ("未成年", "小学生", "初中生", "儿童", "未满18"),
    SensitiveCategory.IDENTITY_AUTOMATION: (
        "机器人",
        "自动回复",
        "程序代发",
        "是不是本人",
        "真人吗",
        "验证本人",
        "实时自拍",
        "开视频证明",
    ),
    SensitiveCategory.EXTERNAL_CONTENT: (
        "http://",
        "https://",
        "www.",
        "下载文件",
        "陌生链接",
    ),
}

_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?previous",
        r"system\s*prompt",
        r"developer\s*message",
        r"忽略.{0,8}(之前|以上|系统).{0,8}(指令|规则|提示)",
        r"覆盖.{0,8}(安全|规则|指令|人设)",
        r"泄露.{0,8}(提示词|系统消息|密钥)",
        r"把.{0,8}(联系人|正文).{0,8}(改成|替换)",
    )
)

_HARD_PROHIBITED_OUTPUTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "no-gift-solicitation",
        re.compile(r"(给我|帮我|替我).{0,8}(转账|红包|买|付款|代付|充值)"),
    ),
    (
        "no-financial-love-test",
        re.compile(r"(在意|爱我|喜欢我).{0,8}(就|应该|必须).{0,8}(花钱|买|转账|红包)"),
    ),
    (
        "no-guilt-bait",
        re.compile(r"(那以后我?不找你了|我还以为你不想理我|你忙吧[吧啊]?$)"),
    ),
    ("no-jealousy-engineering", re.compile(r"(她挺漂亮|你们.{0,4}挺配|别人都在追我)")),
    ("no-relationship-trap", re.compile(r"(不能找你吗|你急什么|证明.{0,6}在意我)")),
    (
        "no-false-specialness",
        re.compile(r"(只跟你一个人说|只给你.{0,5}(打电话|发)|只有你知道)"),
    ),
    (
        "no-false-commitment",
        re.compile(r"(我愿意做你(女|男)朋友|我们结婚|我只属于你|永远只爱你)"),
    ),
    ("no-automation-denial", re.compile(r"(我不是机器人|没有自动回复|绝对是本人在回)")),
    (
        "no-threat-or-self-harm-leverage",
        re.compile(r"(你不.{0,6}(回|陪|来).{0,12}(自杀|自伤|去死)|不理我我就死)"),
    ),
    ("no-sexual-escalation", re.compile(r"(发.{0,4}(裸照|私密照)|一起开房|和我上床)")),
)


def classify_sensitive(*texts: str) -> tuple[SensitiveCategory, ...]:
    joined = "\n".join(texts).casefold()
    found = [
        category
        for category, terms in _TOPIC_TERMS.items()
        if any(term.casefold() in joined for term in terms)
    ]
    return tuple(found)


def has_prompt_injection(*texts: str) -> bool:
    joined = "\n".join(texts)
    return any(pattern.search(joined) for pattern in _INJECTION_PATTERNS)


def prohibited_output_rule_ids(text: str) -> tuple[str, ...]:
    return tuple(
        rule_id for rule_id, pattern in _HARD_PROHIBITED_OUTPUTS if pattern.search(text)
    )
