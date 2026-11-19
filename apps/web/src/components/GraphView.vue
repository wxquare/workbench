<script setup lang="ts">
import { computed, ref } from 'vue'
import { layoutGraph } from '../graph-layout'
import type { GraphEdge, GraphNode } from '../graphs-api'

const props = defineProps<{ nodes: GraphNode[]; edges: GraphEdge[] }>()
const selected = ref<string | null>(props.nodes.find(node => !node.id.startsWith('__'))?.id ?? props.nodes[0]?.id ?? null)
const layout = computed(() => layoutGraph(props.nodes, props.edges))
const selectedNode = computed(() => props.nodes.find(node => node.id === selected.value))
const byId = computed(() => new Map(props.nodes.map(node => [node.id, node])))
const colors: Record<string, string> = { prepare: '#eef2f8', model: '#dfe9ff', tool: '#e2f5ec', validate: '#fff0df', route: '#fff2cf', wait_human: '#f0e8ff', finish: '#f0f2f6' }
const kindNames: Record<string, string> = { prepare: '准备', model: '模型', tool: '工具', validate: '校验', route: '分支', wait_human: '人工等待', finish: '结束' }
const joined = (values: string[]) => values.length ? values.join('、') : '无'
</script>
<template>
  <div class="graph-workspace">
    <div class="graph-canvas">
      <div class="graph-canvas-toolbar"><span>{{ nodes.length }} 个节点 · {{ edges.length }} 条连线</span><span>点击节点查看说明</span></div>
      <div class="graph-scroll">
        <svg :viewBox="`0 0 ${layout.width} ${layout.height}`" role="group" aria-label="流程结构图" :style="{ minWidth: `${layout.width}px` }">
          <defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7184a2" /></marker></defs>
          <g v-for="edge in layout.edges" :key="`${edge.source}-${edge.target}-${edge.label}`">
            <path :d="edge.path" fill="none" stroke="#7184a2" stroke-width="1.8" :stroke-dasharray="edge.conditional ? '6 5' : undefined" marker-end="url(#graph-arrow)" />
            <text v-if="edge.label" :x="edge.labelX" :y="edge.labelY" text-anchor="middle" class="graph-edge-label">{{ edge.label }}</text>
          </g>
          <g v-for="node in layout.nodes" :key="node.id" :transform="`translate(${node.x},${node.y})`"
            class="graph-node" :class="{ selected: selected === node.id }" tabindex="0" role="button" :aria-label="byId.get(node.id)?.title" :aria-current="selected === node.id ? 'true' : undefined"
            @click="selected = node.id" @keydown.enter="selected = node.id" @keydown.space.prevent="selected = node.id">
            <rect x="-90" y="-30" width="180" height="60" rx="11" :fill="colors[byId.get(node.id)?.kind ?? ''] ?? '#f0f2f6'" />
            <text v-if="!node.id.startsWith('__')" y="-10" class="graph-node-kind" text-anchor="middle" dominant-baseline="middle">{{ kindNames[byId.get(node.id)?.kind ?? ''] ?? '节点' }}</text>
            <text :y="node.id.startsWith('__') ? 0 : 11" class="graph-node-title" text-anchor="middle" dominant-baseline="middle">{{ byId.get(node.id)?.title }}</text>
          </g>
        </svg>
      </div>
      <div class="graph-legend"><span>— 顺序</span><span>┄ 条件</span><span>↻ 重复</span><span>颜色区分节点类型</span></div>
    </div>
    <aside class="graph-inspector" aria-label="节点详情" aria-live="polite">
      <template v-if="selectedNode">
        <span class="graph-kind">{{ kindNames[selectedNode.kind] ?? '节点' }}</span>
        <h4>{{ selectedNode.title }}</h4><p>{{ selectedNode.summary }}</p>
        <dl><div><dt>读取</dt><dd>{{ joined(selectedNode.reads) }}</dd></div><div><dt>产出</dt><dd>{{ joined(selectedNode.writes) }}</dd></div><div><dt>消耗</dt><dd>{{ joined(selectedNode.cost) }}</dd></div></dl>
      </template>
      <p v-else>暂无节点信息。</p>
    </aside>
  </div>
</template>
