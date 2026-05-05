import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import * as api from '../api/client';
import type { Article, ArticleFilters, ChatMessage, ExtractAction, PaginatedArticles } from '../types';

const ARTICLES_LIMIT = 50;

export function useArticles(filters: ArticleFilters, freezeList = false) {
  return useInfiniteQuery({
    queryKey: ['articles', filters],
    queryFn: ({ pageParam = 0 }) => api.getArticles(filters, pageParam as number, ARTICLES_LIMIT),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((s, p) => s + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
    refetchInterval: freezeList ? false : 60_000,
    staleTime: freezeList ? Infinity : undefined,
    refetchOnMount: freezeList ? false : true,
  });
}

export function useUpdateArticle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: { is_read?: boolean; is_saved?: boolean } }) =>
      api.updateArticle(id, data),
    onSuccess: (result, { id }) => {
      // Update article in-place so it stays visible until the user navigates away
      qc.setQueriesData<InfiniteData<PaginatedArticles>>(
        { queryKey: ['articles'] },
        (old) => {
          if (!old) return old;
          return {
            ...old,
            pages: old.pages.map(page => ({
              ...page,
              items: page.items.map(a => a.id === id ? { ...a, ...result } : a),
            })),
          };
        },
      );
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['recommended-count'] });
      qc.invalidateQueries({ queryKey: ['unrecommended-count'] });
      qc.invalidateQueries({ queryKey: ['saved-count'] });
      qc.invalidateQueries({ queryKey: ['article', id] });
    },
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedId?: number) => api.markAllRead(feedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['recommended-count'] });
      qc.invalidateQueries({ queryKey: ['unrecommended-count'] });
    },
  });
}


export function useSummarizeArticle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.summarizeArticle(id),
    onSuccess: (data) => {
      qc.setQueryData(['article', data.id], data);
    },
  });
}

export function useExtractContent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.extractArticleContent(id),
    onSuccess: (data) => {
      qc.setQueryData(['article', data.id], data);
    },
  });
}

export function useRelatedArticles(id: number | null, limit = 3) {
  return useQuery({
    queryKey: ['related', id, limit],
    queryFn: () => api.getRelatedArticles(id as number, limit),
    enabled: id != null,
    staleTime: 60_000,
  });
}

export function useSuggestTags() {
  return useMutation({
    mutationFn: (id: number) => api.suggestTags(id),
  });
}

export function useChatWithArticle() {
  return useMutation({
    mutationFn: ({ id, message, history }: { id: number; message: string; history: ChatMessage[] }) =>
      api.chatWithArticle(id, message, history),
  });
}

export function useAiStatus() {
  return useQuery({
    queryKey: ['ai-status'],
    queryFn: api.getAiStatus,
    staleTime: 10_000,
    refetchInterval: (query) => (query.state.data?.running ? 10_000 : 30_000),
  });
}

export function useRecommendedCount() {
  return useQuery({
    queryKey: ['recommended-count'],
    queryFn: () => api.getArticles({ recommended: true }, 0, 1).then(r => r.total),
    staleTime: 60_000,
  });
}

export function useUnrecommendedCount() {
  return useQuery({
    queryKey: ['unrecommended-count'],
    queryFn: () => api.getArticles({ unrecommended: true }, 0, 1).then(r => r.total),
    staleTime: 60_000,
  });
}

export function useSavedCount() {
  return useQuery({
    queryKey: ['saved-count'],
    queryFn: () => api.getArticles({ is_saved: true }, 0, 1).then(r => r.total),
    staleTime: 60_000,
  });
}

export function useSearchArticles(q: string, filters: { feed_id?: number; is_saved?: boolean } = {}, offset = 0) {
  return useQuery({
    queryKey: ['search', q, filters, offset],
    queryFn: () => api.searchArticles(q, filters, offset),
    enabled: q.length > 0,
  });
}

export function useExtractFailed() {
  return useQuery({
    queryKey: ['extract-failed'],
    queryFn: api.listExtractFailed,
    staleTime: 30_000,
  });
}

export function useExtractAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: { id: number; action: ExtractAction }) =>
      api.extractAction(id, action),
    // Optimistically drop the row so the center-pane list updates instantly
    // after retry / skip / delete — otherwise the top item stays visible until
    // the invalidation refetch lands.
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: ['extract-failed'] });
      const previous = qc.getQueryData<Article[]>(['extract-failed']);
      qc.setQueryData<Article[]>(['extract-failed'], (old) =>
        old ? old.filter(a => a.id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous) qc.setQueryData(['extract-failed'], ctx.previous);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['extract-failed'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['ai-status'] });
    },
  });
}
