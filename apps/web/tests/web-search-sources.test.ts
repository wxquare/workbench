import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import WebSearchSources from '../src/components/WebSearchSources.vue'

it('renders search snippets as text and states that full pages were not read', () => {
  const wrapper = mount(WebSearchSources, {
    props: {
      sources: [{
        title: '<script>alert(1)</script>',
        url: 'https://docs.example.com/guide',
        snippet: 'Use <b>WAL</b> cautiously.',
        retrieved_at: '2026-11-24T00:00:00Z',
        published_at: null,
      }],
    },
  })
  expect(wrapper.text()).toContain('<script>alert(1)</script>')
  expect(wrapper.text()).toContain('未读取网页全文')
  expect(wrapper.find('script').exists()).toBe(false)
  expect(wrapper.find('b').exists()).toBe(false)
  expect(wrapper.find('a').attributes('rel')).toBe('noopener noreferrer')
})

it('shows the truncated state', () => {
  const wrapper = mount(WebSearchSources, {
    props: {
      sources: [{
        title: 'Docs',
        url: 'https://docs.example.com/guide',
        snippet: 'Snippet',
        retrieved_at: '2026-11-24T00:00:00Z',
        published_at: null,
      }],
      truncated: true,
    },
  })
  expect(wrapper.text()).toContain('搜索结果已被截断')
})
