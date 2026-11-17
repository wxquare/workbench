export type RunStatus = 'running' | 'waiting' | 'completed' | 'partial'
export type DeliveryStatus = 'unknown' | 'delivered'
export interface DemoRun {
  id: string; title: string; category: string; source: string; status: RunStatus
  delivery?: DeliveryStatus; time: string; duration: string; summary: string
}
export const statusPresentation: Record<RunStatus | DeliveryStatus, {label: string; tone: string}> = {
  running: { label: '分析中', tone: 'blue' }, waiting: { label: '等待输入', tone: 'amber' },
  completed: { label: '分析完成', tone: 'green' }, partial: { label: '部分完成', tone: 'slate' },
  unknown: { label: '结果待核对', tone: 'amber' }, delivered: { label: '已投递', tone: 'green' },
}
export const demoRuns: DemoRun[] = [
  { id: 'run-2026-1108', title: '支付超时告警排查', category: '告警诊断', source: '告警 webhook', status: 'running', time: '今天 10:24', duration: '进行中', summary: '正在关联监控指标、日志与链路，定位支付超时的可能原因。' },
  { id: 'run-2026-1107', title: '订单回调延迟分析', category: '告警诊断', source: 'SeaTalk', status: 'waiting', time: '今天 09:48', duration: '等待输入', summary: '需要补充业务区域后，才能继续检索对应范围的日志。' },
  { id: 'run-2026-1106', title: '查询退款失败处理流程', category: '知识问答', source: '工作台', status: 'completed', delivery: 'unknown', time: '今天 09:15', duration: '2 分 18 秒', summary: '已整理退款失败的排查流程，外部动作结果等待核对。' },
  { id: 'run-2026-1105', title: '每日服务健康巡检', category: '定时巡检', source: '定时任务', status: 'completed', delivery: 'delivered', time: '今天 08:00', duration: '4 分 32 秒', summary: '巡检已结束，检查结果已形成示例记录。' },
  { id: 'run-2026-1104', title: '异常订单重试机制分析', category: '代码分析', source: '工作台', status: 'partial', time: '昨天 17:36', duration: '3 分 08 秒', summary: '已完成代码检索，部分证据仍需人工确认。' },
]
export const findRun = (id: string) => demoRuns.find(run => run.id === id)
