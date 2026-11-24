<script setup lang="ts">
export interface WebSearchSource {
  title: string
  url: string
  snippet: string
  retrieved_at: string
  published_at: string | null
}

const props = defineProps<{
  sources: WebSearchSource[]
  truncated?: boolean
}>()

function host(url: string) {
  try {
    return new URL(url).hostname
  } catch {
    return ''
  }
}
</script>

<template>
  <section v-if="sources.length" class="result-section search-sources">
    <h3>网页搜索来源</h3>
    <p class="search-notice">
      搜索摘要，未读取网页全文；搜索摘要不等于事实核验。
    </p>
    <article v-for="(source, index) in sources" :key="`${source.url}:${index}`">
      <h4>{{ source.title }}</h4>
      <small>{{ host(source.url) }} · {{ new Date(source.retrieved_at).toLocaleString() }}</small>
      <p>{{ source.snippet }}</p>
      <a
        :href="source.url"
        target="_blank"
        rel="noopener noreferrer"
      >
        打开来源
      </a>
    </article>
    <p v-if="truncated" class="search-notice">搜索结果已被截断。</p>
  </section>
</template>

<style scoped>
.search-sources article {
  border-top: 1px solid #dbe2e8;
  padding: 10px 0;
}
.search-sources h4,
.search-sources p,
.search-sources small {
  overflow-wrap: anywhere;
}
.search-notice {
  color: #66737d;
}
</style>
