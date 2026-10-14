"""Finite policy and limited, conservative checks on a retrieval-only rewrite."""
import re
import unicodedata
from dataclasses import dataclass

POLICY_VERSION = "knowledge-loop-v2"


@dataclass(frozen=True)
class KnowledgePolicy:
    max_rounds: int = 2
    time_budget_seconds: float = 45.0

    def __post_init__(self):
        if type(self.max_rounds) is not int or self.max_rounds not in (1, 2):
            raise ValueError("Only one or two knowledge retrieval rounds are supported")


def normalized_query_key(value):
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def rewrite_change(original, revised):
    """These checks do not prove full semantic equivalence; original-question judging remains mandatory."""
    original, revised = [unicodedata.normalize("NFKC", text).casefold() for text in (original, revised)]
    key = normalized_query_key(revised)
    if not key or key == normalized_query_key(original):
        return "same_query"
    numbers = r"\d+(?:\.\d+)?\s*(?:%|千卡|卡路里|大卡|千克|公斤|毫克|kcal|kg|mg|克|斤|分钟|小时|个月|天|周|月|年|次|组|公里|米|毫升|ml|升|g)?"
    numeric_facts = lambda text: sorted(re.sub(r"\s+", "", item) for item in re.findall(numbers, text))
    if numeric_facts(original) != numeric_facts(revised):
        return "numeric_change"
    periods = (("每天", "每日", "一天"), ("每周", "每星期", "一周", "一星期"),
               ("每月", "每个月", "一个月"), ("每年", "一年"), ("每次",),
               ("空腹",), ("餐前", "饭前"), ("餐后", "饭后"), ("训练前",), ("训练后",))
    if any(any(word in original for word in group) != any(word in revised for word in group) for group in periods):
        return "temporal_change"
    # Preserve the presence of common negative/restricted formulations, without inventing ingredient semantics.
    negative = r"不(?!仅)|无(?!氧)|没有|避免|禁止|排除|素食|纯素"
    if bool(re.search(negative, original)) != bool(re.search(negative, revised)):
        return "negation_change"
    return None
