"""Human-readable catalog of installed graphs and their actual compiled structure."""
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Literal

from workbench_agent.runtime.graphs.ids import GraphId


@dataclass(frozen=True)
class NodeInfo:
    title: str
    kind: Literal['prepare', 'model', 'tool', 'validate', 'route', 'wait_human', 'finish']
    summary: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    cost: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphCapabilities:
    modes: tuple[str, ...]
    entrypoints: tuple[Literal['manual', 'schedule', 'synthetic_alert'], ...]
    uses_tools: bool = False
    uses_history: bool = False
    needs_knowledge_tool: bool = False
    needs_workflow: bool = False
    may_interrupt: bool = False
    verified_answers: bool = False


@dataclass(frozen=True)
class GraphSpec:
    graph_id: GraphId
    title: str
    one_liner: str
    when_to_use: str
    when_not_to_use: str
    status: Literal['recommended', 'experimental']
    capabilities: GraphCapabilities
    nodes: dict[str, NodeInfo]
    edge_labels: dict[tuple[str, str], str]
    steps_text: tuple[str, ...]
    notes: tuple[str, ...] = ()


def node(title, kind, summary, reads, writes, cost=()):
    return NodeInfo(title, kind, summary, tuple(reads), tuple(writes), tuple(cost))


single_nodes = {'answer': node('生成回答', 'model', '根据输入调用模型并标记回答未经外部知识源验证。',
    ('当前问题', '模型配置'), ('回答和结果',), ('模型调用',))}
multi_nodes = {'answer': node('生成回答', 'model', '结合历史对话和技能文字指令调用模型。',
    ('当前问题', '技能文字指令', '最多 8 轮历史'), ('回答和结果',), ('模型调用',))}
qa_nodes = {
    'prepare_context': node('准备上下文', 'prepare', '记录本次问题。', ('问题',), ('输入消息',)),
    'route': node('选择模式', 'route', '按请求模式确定处理路径。', ('运行模式',), ('处理路径',)),
    'select_skills': node('准备技能', 'prepare', '记录技能选择阶段。', ('运行上下文',), ('阶段标记',)),
    'collect_baseline': node('准备检索', 'prepare', '记录检索准备阶段。', ('运行上下文',), ('阶段标记',)),
    'investigate': node('检索知识', 'tool', '在授权范围内通过知识专家检索。', ('问题', '知识工具', '查询范围'), ('证据引用', '待解决项'), ('模型调用', '工具调用')),
    'validate_evidence': node('校验证据', 'validate', '校验证据范围和可访问性。', ('查询范围', '证据'), ('校验结果',)),
    'compose_outcome': node('整理摘录', 'finish', '汇总可用证据和未解决项。', ('证据', '校验结果'), ('部分完成结果',), ('可能的模型调用',)),
    'finalize': node('结束', 'finish', '记录完成阶段。', ('运行状态',), ('阶段标记',)),
}
sop_nodes = {
    'prepare': node('准备工作流', 'prepare', '读取工作流和查询范围，校验工具授权。', ('工作流', '工具授权', '查询范围'), ('执行顺序', '准备状态')),
    'step': node('执行步骤', 'tool', '执行步骤并检查条件、输出和范围。', ('步骤定义', '已有步骤结果', '授权范围'), ('步骤结果', '证据'), ('工具调用',)),
    'compose': node('汇总结果', 'finish', '汇总执行状态与未完成项。', ('步骤结果',), ('巡检结果',)),
}
rca_nodes = {
    'prepare': node('准备告警', 'prepare', '检查合成告警输入和来源。', ('合成告警', '来源身份'), ('初始状态',)),
    'input': node('补充信息', 'wait_human', '缺少范围信息时等待人工输入。', ('告警范围',), ('补充信息',)),
    'scope': node('确定范围', 'validate', '校验并绑定查询范围。', ('告警和补充信息',), ('查询范围',)),
    'bind_baseline': node('检查基线授权', 'validate', '确定可用的基线工具与数据范围。', ('工具配置', '查询范围'), ('可用工具', '来源权限')),
    'collect_baseline': node('收集基线', 'tool', '读取授权的指标、日志和链路数据。', ('可用工具', '查询范围'), ('基线证据', '收集状态'), ('工具调用',)),
    'compose': node('汇总诊断', 'finish', '汇总可用基线和未解决项。', ('基线证据', '处理状态'), ('诊断结果',)),
}
proposal_node = node('生成提案', 'prepare', '按合成告警策略生成待授权操作提案。', ('范围和基线', '来源策略'), ('操作提案',))
knowledge_nodes = {
    'prepare': node('准备问题', 'prepare', '准备问题和运行上下文。', ('问题', '查询范围'), ('初始状态',)),
    'retrieve': node('检索知识', 'tool', '按范围检索授权知识来源。', ('问题', '知识工具', '查询范围'), ('候选来源',), ('工具调用',)),
    'draft_answer': node('起草回答', 'model', '依据检索来源起草回答。', ('问题', '候选来源'), ('带引用草稿',), ('模型调用',)),
    'validate_citations': node('校验引用', 'validate', '校验引用及来源有效期。', ('草稿', '来源', '有效期'), ('引用校验结果',)),
    'review_support': node('检查依据', 'model', '检查回答是否由来源支持。', ('回答草稿', '有效来源'), ('依据审查结果',), ('模型调用',)),
    'record_gap': node('记录知识缺口', 'validate', '记录来源或回答存在的缺口。', ('校验和审查结果',), ('知识缺口',)),
    'compose_outcome': node('整理结果', 'finish', '根据引用和审查状态整理回答。', ('回答', '校验结果', '知识缺口'), ('回答结果',)),
}

RCA_NOTE = '仅合成告警入口；当前适配器仅供本地集成测试，不支持生产告警接入。'
EXPERIMENTAL_NOTE = '实验工作流需要 Worker 显式配置此工作流的能力。'
_specs = (
    GraphSpec(GraphId.CONVERSATION_SINGLE_TURN, '对话（单轮）', '仅根据当前问题生成回答。', '无需外部查询的单次问答。', '需要历史对话或外部知识验证的问题。', 'recommended',
        GraphCapabilities(('qa',), ('manual',)), single_nodes, {}, ('读取当前问题。', '调用模型并标记回答未经外部知识源验证。')),
    GraphSpec(GraphId.CONVERSATION_MULTI_TURN, '对话（多轮）', '结合最近对话和技能文字指令回答。', '追问、写作和多轮交流。', '需要查询知识库或外部系统的问题。', 'recommended',
        GraphCapabilities(('qa',), ('manual',), uses_history=True), multi_nodes, {}, ('读取问题、技能文字指令和最多 8 轮历史。', '调用模型并标记回答未经外部知识源验证。')),
    GraphSpec(GraphId.KNOWLEDGE_SCOPED_EXCERPT, '知识摘录', '在指定范围检索知识并返回摘录。', '需要来源摘录和范围限制的问答。', '需要已验证的完整结论。', 'experimental',
        GraphCapabilities(('qa',), ('manual',), uses_tools=True, needs_knowledge_tool=True), qa_nodes, {}, ('准备问题与查询范围。', '调用知识工具并校验证据。', '汇总摘录与未解决项。'), (EXPERIMENTAL_NOTE, '需要知识工具和查询范围；结果恒为部分完成。')),
    GraphSpec(GraphId.SOP_CONDITIONAL, 'SOP 巡检', '按照工作流执行有条件的巡检步骤。', '需要按工作流检查系统的场景。', '没有已发布工作流的自由问答。', 'experimental',
        GraphCapabilities(('sop','inspection'), ('manual','schedule'), uses_tools=True, needs_workflow=True), sop_nodes,
        {('prepare','step'):'存在待执行步骤', ('prepare','compose'):'没有可执行步骤', ('step','step'):'还有未执行的步骤', ('step','compose'):'所有步骤完成或已停止'},
        ('读取工作流和授权范围。', '按依赖顺序执行步骤并检查条件。', '汇总步骤结果。'), (EXPERIMENTAL_NOTE, '运行时须提供已发布的工作流。')),
    GraphSpec(GraphId.RCA_SYNTHETIC, '合成告警诊断', '收集合成告警范围内的基线信息。', '本地合成告警诊断测试。', '生产告警接入或普通手动运行。', 'experimental',
        GraphCapabilities(('rca',), ('synthetic_alert',), uses_tools=True, may_interrupt=True), rca_nodes, {}, ('读取合成告警，必要时等待补充范围。', '检查授权并收集基线。', '汇总诊断结果。'), (EXPERIMENTAL_NOTE, RCA_NOTE)),
    GraphSpec(GraphId.RCA_PROPOSAL, '合成告警诊断（提案）', '诊断合成告警并生成单项操作提案。', '本地测试合成告警与操作提案。', '生产告警接入或普通手动运行。', 'experimental',
        GraphCapabilities(('rca',), ('synthetic_alert',), uses_tools=True, may_interrupt=True), {**rca_nodes, 'propose': proposal_node}, {}, ('读取告警并确定范围。', '收集基线。', '生成操作提案并汇总结果。'), (EXPERIMENTAL_NOTE, RCA_NOTE)),
    GraphSpec(GraphId.RCA_BATCH_PROPOSAL, '合成告警诊断（批量提案）', '诊断合成告警并生成批量操作提案。', '本地测试合成告警与批量提案。', '生产告警接入或普通手动运行。', 'experimental',
        GraphCapabilities(('rca',), ('synthetic_alert',), uses_tools=True, may_interrupt=True), {**rca_nodes, 'propose': proposal_node}, {}, ('读取告警并确定范围。', '收集基线。', '生成批量操作提案并汇总结果。'), (EXPERIMENTAL_NOTE, RCA_NOTE)),
    GraphSpec(GraphId.KNOWLEDGE_CITATION_REVIEW, '知识问答（引用审查）', '检索知识并检查来源引用与支持关系。', '验证知识来源及引用的实验场景。', '真实来源和范围隔离尚未验收的环境。', 'experimental',
        GraphCapabilities(('qa',), ('manual',), uses_tools=True, needs_knowledge_tool=True, verified_answers=True), knowledge_nodes, {}, ('检索范围内的知识来源。', '起草回答并校验引用、有效期与支持关系。', '记录缺口并整理结果。'), (EXPERIMENTAL_NOTE, '真实知识源和范围隔离尚未验收，完成核对后才能在真实环境启用。', '需要知识工具、查询范围及来源有效期。')),
)
_by_id = {spec.graph_id: spec for spec in _specs}


def list_graphs():
    return _specs


def get_graph(graph_id):
    return _by_id.get(graph_id)


@lru_cache(maxsize=len(_specs))
def graph_structure(graph_id):
    spec = get_graph(graph_id)
    if spec is None:
        raise KeyError(graph_id)
    from workbench_agent.runtime.graphs.direct_qa import build_direct_qa
    from workbench_agent.runtime.graphs.conversation_qa import build_conversation_qa
    from workbench_agent.runtime.graphs.qa_v16 import build_dod
    from workbench_agent.runtime.graphs.sop_graph import build_sop
    from workbench_agent.runtime.graphs.rca_synthetic import build_rca as build_synthetic
    from workbench_agent.runtime.graphs.rca_proposal import build_rca as build_proposal
    from workbench_agent.runtime.graphs.rca_batch import build_rca as build_batch
    from workbench_agent.runtime.graphs.knowledge_qa import build_knowledge_qa
    builders = {GraphId.CONVERSATION_SINGLE_TURN: build_direct_qa, GraphId.CONVERSATION_MULTI_TURN: build_conversation_qa,
        GraphId.KNOWLEDGE_SCOPED_EXCERPT: build_dod, GraphId.SOP_CONDITIONAL: lambda saver: build_sop(saver, version=GraphId.SOP_CONDITIONAL),
        GraphId.RCA_SYNTHETIC: build_synthetic, GraphId.RCA_PROPOSAL: build_proposal,
        GraphId.RCA_BATCH_PROPOSAL: build_batch, GraphId.KNOWLEDGE_CITATION_REVIEW: build_knowledge_qa}
    structure = builders[spec.graph_id](None).get_graph()
    nodes = []
    for node_id in structure.nodes:
        info = spec.nodes.get(node_id)
        if info is None:
            info = NodeInfo('开始' if node_id == '__start__' else '结束', 'finish', '流程的起止点。', ('流程状态',), ('流程状态',))
        nodes.append({'id': node_id, **asdict(info)})
    edges = [{'source': edge.source, 'target': edge.target, 'conditional': edge.conditional,
        'label': spec.edge_labels.get((edge.source, edge.target), '')} for edge in structure.edges]
    return {'nodes': nodes, 'edges': edges}
