<script setup lang="ts">
import { computed, ref } from 'vue'
import { layoutGraph } from '../graph-layout'
import type { GraphEdge, GraphNode } from '../graphs-api'

const props = defineProps<{ nodes: GraphNode[]; edges: GraphEdge[] }>()
const selected = ref<string | null>(null)
const layout = computed(() => layoutGraph(props.nodes, props.edges))
const selectedNode = computed(() => props.nodes.find(node => node.id === selected.value))
const byId = computed(() => new Map(props.nodes.map(node => [node.id, node])))
const colors: Record<string, string> = { prepare: '#e5e7eb', model: '#dbeafe', tool: '#dcfce7', validate: '#ffedd5', route: '#fef3c7', wait_human: '#f3e8ff', finish: '#e2e8f0' }
</script>
<template>
  <div class="graph-view">
    <p class="graph-legend">图例：实线为顺序，虚线为条件，回环为重复执行；颜色区分节点类型。</p>
    <div class="graph-scroll">
      <svg :viewBox="`0 0 ${layout.width} ${layout.height}`" role="img" aria-label="流程结构图" :style="{ minWidth: `${layout.width}px` }">
        <defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b" /></marker></defs>
        <g v-for="edge in layout.edges" :key="`${edge.source}-${edge.target}-${edge.label}`">
          <path :d="edge.path" fill="none" stroke="#64748b" :stroke-dasharray="edge.conditional ? '5 4' : undefined" marker-end="url(#graph-arrow)" />
          <text v-if="edge.label" :x="edge.labelX" :y="edge.labelY" class="graph-edge-label">{{ edge.label }}</text>
        </g>
        <g v-for="node in layout.nodes" :key="node.id" :transform="`translate(${node.x},${node.y})`"
          tabindex="0" role="button" :aria-label="byId.get(node.id)?.title" @click="selected = node.id" @keydown.enter="selected = node.id" @keydown.space.prevent="selected = node.id">
          <rect x="-90" y="-30" width="180" height="60" rx="10" :fill="colors[byId.get(node.id)?.kind ?? ''] ?? '#e2e8f0'" stroke="#94a3b8" />
          <text text-anchor="middle" dominant-baseline="middle">{{ byId.get(node.id)?.title }}</text>
        </g>
      </svg>
    </div>
    <section v-if="selectedNode" aria-live="polite"><h4>{{ selectedNode.title }}</h4><p>{{ selectedNode.summary }}</p></section>
    <div class="graph-nodes"><h4>节点说明</h4><dl v-for="node in nodes.filter(n => !n.id.startsWith('__'))" :key="node.id">
      <dt>{{ node.title }}：{{ node.summary }}</dt>
      <dd>读取：{{ node.reads.join('、') }}；产出：{{ node.writes.join('、') }}；消耗：{{ node.cost.join('、') || '无' }}</dd>
    </dl></div>
  </div>
</template>
