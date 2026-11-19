import type { InjectionKey } from 'vue'
import type { ThreadItem } from './threads-api'

export interface ChatSidebar {
  threads: ThreadItem[]
  selected: string | null
  agent: string
  loading: boolean
  hasMore: boolean
  newThread: () => void
  openThread: (thread: ThreadItem) => void
  loadMore: () => void
}
export const chatSidebarKey: InjectionKey<ChatSidebar> = Symbol('chat-sidebar')
export function emptyChatSidebar(): ChatSidebar {
  return { threads: [], selected: null, agent: '', loading: false, hasMore: false,
    newThread: () => {}, openThread: () => {}, loadMore: () => {} }
}
