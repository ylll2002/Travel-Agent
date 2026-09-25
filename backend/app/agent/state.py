"""LangGraph 状态定义。

State 是 LangGraph 的核心概念，它存储整个对话过程中的所有信息。
每个 Agent 节点都会读取 State，处理后更新 State，然后传递给下一个节点。
"""

from operator import add
from typing import Annotated, Any, Required, Sequence, TypedDict


class EvidenceRef(TypedDict, total=False):
    """可追溯的外部数据引用。"""

    evidence_id: str
    source: str
    fetched_at: str
    data_type: str
    status: str
    warning: str


class PreferenceRecord(TypedDict, total=False):
    """带有强度、置信度和作用范围的用户偏好记录。"""

    key: str
    value: Any
    strength: str
    confidence: float
    scope: str
    is_hard_constraint: bool
    source: str
    active: bool


class SpecialistReport(TypedDict, total=False):
    """所有专业 Agent 共用的输出外层契约。"""

    status: str
    summary: str
    recommendations: list[dict]
    evidence_refs: list[EvidenceRef]
    uncertainties: list[str]
    cost_items: list[dict]
    question_candidates: list[str]


class ValidationResult(TypedDict, total=False):
    """单项行程或修订校验结果。"""

    status: str
    message: str
    warnings: list[str]


class SpecialistResult(TypedDict, total=False):
    """专业 Agent 的执行结果。"""

    agent: str
    report: SpecialistReport
    status: str
    error: str
    retryable: bool


class TripBrief(TypedDict, total=False):
    """从模糊主题逐步收敛出的旅行需求摘要。"""

    topic: str
    origin: str
    destination: str
    destination_scope: str
    travel_dates: dict
    duration_days: int
    budget: float
    preferences: list[PreferenceRecord]
    hard_constraints: list[PreferenceRecord]
    missing_fields: list[str]
    assumptions: list[str]
    granularity_level: str


class ItineraryCard(TypedDict, total=False):
    """可局部修改的行程卡片及其依赖。"""

    card_id: str
    day: int
    type: str
    title: str
    time: str
    duration: str
    location: str
    description: str
    reason: str
    cost: float
    evidence_refs: list[EvidenceRef]
    dependency_ids: list[str]
    status: str


class TravelAgentState(TypedDict, total=False):
    """旅行规划 Agent 的状态。
    
    这个类定义了对话过程中需要跟踪的所有信息。
    TypedDict 让 Python 知道每个字段的类型，方便类型检查和 IDE 提示。
    """
    
    # 用户输入和对话历史
    messages: Annotated[Sequence[dict], add]  # 使用 add 操作符：新消息会追加到列表末尾
    
    # 当前旅行主题和用户意图
    topic: str  # 用户的原始输入，例如 "我夏天想找个地方避暑"
    user_intent: str  # 理解后的意图，例如 "寻找避暑目的地"
    
    # 规划颗粒度和缺失信息
    granularity_level: str  # 当前颗粒度：topic/region/city/time/details/ready
    missing_info: list[str]  # 缺失的关键信息列表
    
    # 目的地相关
    candidate_destinations: list[dict]  # 候选目的地列表
    selected_destination: str  # 用户选择的目的地
    
    # 时间相关
    travel_dates: dict  # 出行时间：{start_date, end_date, duration_days}
    season: str  # 季节偏好
    
    # 用户偏好和约束
    user_preferences: dict  # 用户偏好：饮食、交通、活动类型等
    budget: float  # 预算
    hard_constraints: list[PreferenceRecord]  # 硬约束：过敏、签证、安全等
    
    # 行程规划
    draft_itinerary: list[dict]  # Draft v1 卡片列表
    current_itinerary: list[dict]  # 当前确认的行程
    
    # Agent 协作
    current_stage: str  # 当前阶段：understanding/destination/time/dining/transport/generation
    next_action: str  # 下一步动作：ask_question/generate_candidates/create_draft/modify_card
    questions_to_ask: list[str]  # 待询问的问题
    
    # 修改相关
    modification_request: str  # 用户的修改请求
    affected_cards: list[int]  # 受影响的卡片索引
    
    # 最终回复
    agent_response: str  # 返回给用户的回复

    # Multi-round planning state
    session_id: str
    planning_round: int
    clarification_round: int
    questions_asked: int
    workflow_status: str
    planning_goal: str
    trip_brief: TripBrief
    profile_memory: list[PreferenceRecord]
    session_memory: list[PreferenceRecord]
    candidate_memory: list[PreferenceRecord]
    confirmed_facts: dict
    assumptions: list[str]
    agent_reports: dict[str, SpecialistReport]
    specialist_results: list[SpecialistResult]
    agent_tasks: list[dict]
    workflow_events: list[dict]
    evidence_refs: list[EvidenceRef]
    validation_results: dict[str, ValidationResult]
    pending_question: str
    modification_scope: str
    time_candidates: list[dict]
    route_requests: list[dict]
    draft_cards: list[ItineraryCard]
    card_dependencies: dict[str, list[str]]
    revision_comparison: list[dict]
    user_action: str


class InitialTravelAgentState(TypedDict):
    """状态工厂返回的完整状态，所有流程字段都已初始化。"""

    messages: Required[Annotated[Sequence[dict], add]]
    topic: Required[str]
    user_intent: Required[str]
    granularity_level: Required[str]
    missing_info: Required[list[str]]
    candidate_destinations: Required[list[dict]]
    selected_destination: Required[str]
    travel_dates: Required[dict]
    season: Required[str]
    user_preferences: Required[dict]
    budget: Required[float]
    hard_constraints: Required[list[PreferenceRecord]]
    draft_itinerary: Required[list[dict]]
    current_itinerary: Required[list[dict]]
    current_stage: Required[str]
    next_action: Required[str]
    questions_to_ask: Required[list[str]]
    modification_request: Required[str]
    affected_cards: Required[list[int]]
    agent_response: Required[str]
    session_id: Required[str]
    planning_round: Required[int]
    clarification_round: Required[int]
    questions_asked: Required[int]
    workflow_status: Required[str]
    planning_goal: Required[str]
    trip_brief: Required[TripBrief]
    profile_memory: Required[list[PreferenceRecord]]
    session_memory: Required[list[PreferenceRecord]]
    candidate_memory: Required[list[PreferenceRecord]]
    confirmed_facts: Required[dict]
    assumptions: Required[list[str]]
    agent_reports: Required[dict[str, SpecialistReport]]
    specialist_results: Required[list[SpecialistResult]]
    agent_tasks: Required[list[dict]]
    workflow_events: Required[list[dict]]
    evidence_refs: Required[list[EvidenceRef]]
    validation_results: Required[dict[str, ValidationResult]]
    pending_question: Required[str]
    modification_scope: Required[str]
    time_candidates: Required[list[dict]]
    route_requests: Required[list[dict]]
    draft_cards: Required[list[ItineraryCard]]
    card_dependencies: Required[dict[str, list[str]]]
    revision_comparison: Required[list[dict]]
    user_action: Required[str]


class TravelAgentUpdate(TypedDict, total=False):
    """LangGraph 节点允许写入的局部状态更新。"""

    messages: Sequence[dict]
    topic: str
    user_intent: str
    granularity_level: str
    missing_info: list[str]
    candidate_destinations: list[dict]
    selected_destination: str
    travel_dates: dict
    season: str
    user_preferences: dict
    budget: float
    hard_constraints: list[PreferenceRecord]
    draft_itinerary: list[dict]
    current_itinerary: list[dict]
    current_stage: str
    next_action: str
    questions_to_ask: list[str]
    modification_request: str
    affected_cards: list[int]
    agent_response: str
    session_id: str
    planning_round: int
    clarification_round: int
    questions_asked: int
    workflow_status: str
    planning_goal: str
    trip_brief: TripBrief
    profile_memory: list[PreferenceRecord]
    session_memory: list[PreferenceRecord]
    candidate_memory: list[PreferenceRecord]
    confirmed_facts: dict
    assumptions: list[str]
    agent_reports: dict[str, SpecialistReport]
    specialist_results: list[SpecialistResult]
    agent_tasks: list[dict]
    workflow_events: list[dict]
    evidence_refs: list[EvidenceRef]
    validation_results: dict[str, ValidationResult]
    pending_question: str
    modification_scope: str
    time_candidates: list[dict]
    route_requests: list[dict]
    draft_cards: list[ItineraryCard]
    card_dependencies: dict[str, list[str]]
    revision_comparison: list[dict]
    user_action: str


def preference_record(
    key: str,
    value: Any,
    *,
    strength: str = "weak",
    confidence: float = 0.5,
    scope: str = "session",
    is_hard_constraint: bool = False,
    source: str = "user",
) -> PreferenceRecord:
    """创建统一格式的偏好记录，避免不同 Agent 自行定义字段。"""
    return {
        "key": key,
        "value": value,
        "strength": strength,
        "confidence": confidence,
        "scope": scope,
        "is_hard_constraint": is_hard_constraint,
        "source": source,
        "active": True,
    }


def merge_memory_into_trip_brief(
    topic: str,
    parsed_preferences: dict[str, Any],
    parsed_constraints: list[dict[str, Any]],
    profile_memory: list[PreferenceRecord],
    session_memory: list[PreferenceRecord],
    candidate_memory: list[PreferenceRecord],
) -> tuple[TripBrief, list[PreferenceRecord], list[PreferenceRecord]]:
    """按优先级合并 Memory，并返回当前有效偏好和硬约束。

    当前明确表达只覆盖本次规划，不会自动写入长期 Profile；候选 Memory
    也只在没有更高优先级记录时参与当前规划。
    """
    merged: dict[str, PreferenceRecord] = {}
    for records in (profile_memory, candidate_memory, session_memory):
        for record in records:
            key = record.get("key")
            if record.get("active", True) and key:
                merged[key] = record

    for key, value in parsed_preferences.items():
        merged[key] = preference_record(key, value, source="current_session")

    constraints: list[PreferenceRecord] = []
    for record in merged.values():
        if record.get("is_hard_constraint"):
            constraints.append(record)
    for constraint in parsed_constraints:
        key = str(constraint.get("key", "")).strip()
        if key:
            record = preference_record(
                key,
                constraint.get("value"),
                strength="explicit",
                confidence=1.0,
                is_hard_constraint=True,
                source="current_session",
            )
            merged[key] = record
            constraints.append(record)

    preferences = [record for record in merged.values() if not record.get("is_hard_constraint")]
    brief: TripBrief = {
        "topic": topic,
        "preferences": preferences,
        "hard_constraints": constraints,
        "missing_fields": [],
        "assumptions": [],
        "granularity_level": "topic",
    }
    return brief, preferences, constraints
