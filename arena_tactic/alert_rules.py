"""Structured alert-rule metadata for the tactical inspector."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AlertRule:
    code: str
    severity: str
    zh_label: str
    description: str
    recommendation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _rule(code: str, severity: str, zh_label: str, description: str, recommendation: str) -> AlertRule:
    return AlertRule(code, severity, zh_label, description, recommendation)


ALERT_RULES: dict[str, AlertRule] = {
    rule.code: rule
    for rule in (
        _rule("CORE_UNDER_ATTACK", "critical", "核心正在遭受攻击", "核心在当前生命周期内遭受直接攻击。", "立即复核敌军位置、防线覆盖和核心生存状态。"),
        _rule("HIDDEN_CORE_ATTACK", "critical", "视野外的核心遭袭", "核心在无可见敌人时受击，防守单位仍在等待。", "检查视野盲区、游侠射线和防线朝向。"),
        _rule("CORE_LOST_OR_RESPAWNED", "critical", "核心丢失或已重生", "近期出现核心摧毁或重生事件。", "复核战损原因，并确认重生后的经济与防御恢复。"),
        _rule("CORE_MIGRATION_LOOP", "critical", "核心迁移陷入循环", "核心迁移反复失败、取消或启动后未完成。", "检查目的地合法性、路径阻塞和迁移冷却。"),
        _rule("UNANSWERED_DAMAGE", "critical", "单位受击后无反击或规避", "战斗单位连续受损且未反击或确认脱离射程。", "复核目标选择、射程判断和撤退条件。"),
        _rule("UNIT_OSCILLATION", "warning", "单位往返振荡", "对象在少量格子间持续周期性往复。", "检查导航目标、避障冷却和路径去重。"),
        _rule("CARGO_DELIVERY_STAGNATION", "critical", "载货工人回矿停滞", "载货工人连续无法完成资源入库。", "检查核心位置、回矿路径和入库动作条件。"),
        _rule("DEPOSIT_FAILURES", "warning", "资源入库连续失败", "窗口内资源入库失败达到告警条件。", "按失败原因复核距离、容量和核心状态。"),
        _rule("PRODUCTION_FREEZE", "warning", "兵营生产冻结", "和平或经济模式下资源和人口空间充足但持续未生产。", "检查生产预算、编制目标和生成条件。"),
        _rule("INEFFECTIVE_STATIONARY", "warning", "对象长期无效静止", "对象长期原地等待或移动失败。", "检查阻塞目标、失败目的地冷却和等待原因。"),
        _rule("EXPLORATION_STALL", "warning", "迷雾探索停滞", "工人移动未形成有效位移，或资源探索长期无进展。", "复核扇区目标、前沿更新和永久障碍记忆。"),
        _rule("DEFENSE_DISENGAGED", "critical", "防守单位脱离交战", "可见威胁出现时战斗单位远离敌人并等待。", "检查防守分配、接敌距离和等待条件。"),
        _rule("BEACON_CARRIER_ISOLATED", "critical", "信标携带者被孤立", "信标携带者距战斗友军过远或没有护卫。", "重新分配护卫并缩短编队间距。"),
        _rule("DECISION_LATENCY_SPIKE", "critical", "决策延迟激增", "决策耗时超过巡检阈值。", "检查性能热点、阻塞 I/O 和提交时间余量。"),
    )
}


def serialized_rules() -> list[dict[str, str]]:
    """Return rules in stable registration order for discovery and JSON output."""
    return [rule.to_dict() for rule in ALERT_RULES.values()]
